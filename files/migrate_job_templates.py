#!/usr/bin/env python3
"""
Migrate AWX Job Templates to AAP (Controller).

Key behavior in this version:
- Single or bulk migration:
    • Single: --template-id <ID>  (or env TEMPLATE_ID)
    • Bulk:   --all  (optional --include/--exclude)
- Force Execution Environment by AAP API **ID** via --force-ee-id (or env FORCE_EE_ID).
  This overrides whatever EE the JT used in AWX.
- Robust credential matching: resolves credential TYPE by NAME (IDs differ across installs).

Examples:
  # Single JT by id, forcing EE id 5
  python3 migrate_job_templates.py ... --template-id 123 --force-ee-id 5

  # Bulk with include filter
  python3 migrate_job_templates.py ... --all --include "^(Prod|Infra)-" --force-ee-id 5
"""

import argparse
import os
import sys
import re
from typing import Any, Dict, Iterable, Optional, Tuple, Pattern, List

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 30


# ---------------- CLI ----------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate AWX Job Templates to AAP")

    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument('--template-id', type=int, help='AWX Job Template ID (single-mode)')
    g.add_argument('--all', action='store_true', help='Migrate all Job Templates')

    p.add_argument('--awx-host', required=True)
    p.add_argument('--awx-token', required=True)
    p.add_argument('--aap-host', required=True)
    p.add_argument('--aap-token', required=True)
    p.add_argument('--organization-id', required=True, type=int)

    p.add_argument('--include', help='regex on template name (bulk)')
    p.add_argument('--exclude', help='regex on template name (bulk)')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--verify-tls', action='store_true')

    # NEW: force EE by AAP API id (takes precedence over any name-based logic)
    env_force_ee_id = os.getenv('FORCE_EE_ID')
    p.add_argument(
        '--force-ee-id',
        type=int,
        default=(int(env_force_ee_id) if env_force_ee_id and env_force_ee_id.isdigit() else None),
        help='AAP Execution Environment API ID to set on migrated JTs (e.g., 5).'
    )

    return p.parse_args()


# ---------------- HTTP ----------------
def norm(h: str) -> str:
    return h.rstrip('/')


def H(tok: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"}


def GET(url: str, h: Dict[str, str], v: bool) -> Dict[str, Any]:
    r = requests.get(url, headers=h, verify=v, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text}")
    return r.json()


def POST(url: str, h: Dict[str, str], payload: Dict[str, Any], v: bool) -> Dict[str, Any]:
    r = requests.post(url, headers=h, json=payload, verify=v, timeout=TIMEOUT)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"POST {url} -> {r.status_code}:\n{r.text}")
    return r.json()


def ping(aap: str, tok: str, v: bool) -> None:
    u = f"{aap}/api/controller/v2/ping/"
    r = requests.get(u, headers=H(tok), verify=v, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"AAP ping {r.status_code}: {r.text}")


# ---------------- AWX reads ----------------
def awx_jt(a: str, t: str, i: int, v: bool) -> Dict[str, Any]:
    return GET(f"{a}/api/v2/job_templates/{i}/", H(t), v)


def awx_jts(a: str, t: str, v: bool) -> Iterable[Dict[str, Any]]:
    u = f"{a}/api/v2/job_templates/?page_size=200"
    hdr = H(t)
    while u:
        d = GET(u, hdr, v)
        for o in d.get('results', []):
            yield o
        u = d.get('next')


def awx_jt_creds(a: str, t: str, jtid: int, v: bool) -> Iterable[Dict[str, Any]]:
    u = f"{a}/api/v2/job_templates/{jtid}/credentials/"
    d = GET(u, H(t), v)
    for o in d.get('results', d.get('data', [])):
        yield o


# ---------------- AAP lookups ----------------
def q_one(aap: str, tok: str, endpoint: str, name: str, org: int, v: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    u = f"{aap}/api/controller/v2/{endpoint}/?name={quote(name)}&organization={org}"
    d = GET(u, H(tok), v)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and res:
        return res[0]
    return None


def assert_ee_exists_by_id(aap: str, tok: str, ee_id: int, org_id: int, v: bool) -> None:
    """
    Verify the EE exists. Accept either global (organization is null/absent) or matches org_id.
    """
    d = GET(f"{aap}/api/controller/v2/execution_environments/{ee_id}/", H(tok), v)
    ee_org = d.get('organization')
    if ee_org not in (None, org_id):
        raise RuntimeError(
            f"Execution Environment id {ee_id} belongs to organization {ee_org}, "
            f"which does not match target org {org_id}."
        )


def ensure_refs(
    aap: str,
    tok: str,
    org: int,
    v: bool,
    proj_name: Optional[str],
    inv_name: Optional[str],
    forced_ee_id: Optional[int]
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    proj_id = inv_id = ee_id = None
    if proj_name:
        p = q_one(aap, tok, "projects", proj_name, org, v)
        if not p:
            raise RuntimeError(f"Project '{proj_name}' not found in AAP org {org}")
        proj_id = p['id']
    if inv_name:
        i = q_one(aap, tok, "inventories", inv_name, org, v)
        if not i:
            raise RuntimeError(f"Inventory '{inv_name}' not found in AAP org {org}")
        inv_id = i['id']

    if forced_ee_id is None:
        raise RuntimeError("You must supply --force-ee-id to set the Execution Environment by API id.")
    assert_ee_exists_by_id(aap, tok, forced_ee_id, org, v)
    ee_id = forced_ee_id

    return proj_id, inv_id, ee_id


def find_aap_jt(aap: str, tok: str, name: str, org: int, v: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    u = f"{aap}/api/controller/v2/job_templates/?name={quote(name)}&organization={org}"
    d = GET(u, H(tok), v)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and res:
        return res[0]
    return None


# ---- Credential type resolution (by NAME) ----
def aap_get_credential_type_id_by_name(aap: str, tok: str, name: str, verify: bool) -> Optional[int]:
    from requests.utils import quote
    u = f"{aap}/api/controller/v2/credential_types/?name={quote(name)}"
    d = GET(u, H(tok), verify)
    res = d.get('results') or d.get('data') or []
    return res[0]['id'] if isinstance(res, list) and res else None


def aap_find_credential(aap: str, tok: str, name: str,
                        awx_ctype_name: Optional[str],
                        org_id: int, verify: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    # Try name+org first
    u = f"{aap}/api/controller/v2/credentials/?name={quote(name)}&organization={org_id}"
    d = GET(u, H(tok), verify)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and len(res) == 1:
        return res[0]
    if isinstance(res, list) and len(res) > 1 and awx_ctype_name:
        for r in res:
            sf = r.get('summary_fields') or {}
            ctn = (sf.get('credential_type') or {}).get('name')
            if ctn and ctn == awx_ctype_name:
                return r
    if awx_ctype_name:
        aap_ctype_id = aap_get_credential_type_id_by_name(aap, tok, awx_ctype_name, verify)
        if aap_ctype_id:
            u = (f"{aap}/api/controller/v2/credentials/?name={quote(name)}"
                 f"&organization={org_id}&credential_type={aap_ctype_id}")
            d = GET(u, H(tok), verify)
            res = d.get('results') or d.get('data') or []
            if isinstance(res, list) and res:
                return res[0]
    return res[0] if isinstance(res, list) and res else None


# ---------------- transforms ----------------
def filt(name: str, inc: Optional[Pattern[str]], exc: Optional[Pattern[str]]) -> bool:
    if inc and not inc.search(name):
        return False
    if exc and exc.search(name):
        return False
    return True


def jt_payload_from_awx(obj: Dict[str, Any], org: int,
                        proj_id: Optional[int], inv_id: Optional[int], ee_id: Optional[int]) -> Dict[str, Any]:
    p = {
        "name": obj.get("name", "Migrated JT"),
        "description": obj.get("description", "") or "",
        "job_type": obj.get("job_type", "run"),
        "playbook": obj.get("playbook") or "",
        "project": proj_id,
        "inventory": inv_id,
        "execution_environment": ee_id,
        "forks": obj.get("forks", 0) or 0,
        "verbosity": obj.get("verbosity", 0) or 0,
        "become_enabled": bool(obj.get("become_enabled", False)),
        "limit": obj.get("limit") or "",
        "timeout": obj.get("timeout", 0) or 0,
        "organization": org,
        "allow_simultaneous": bool(obj.get("allow_simultaneous", False)),
        "use_fact_cache": bool(obj.get("use_fact_cache", False)),
        "ask_inventory_on_launch": bool(obj.get("ask_inventory_on_launch", False)),
        "ask_variables_on_launch": bool(obj.get("ask_variables_on_launch", False)),
        "ask_limit_on_launch": bool(obj.get("ask_limit_on_launch", False)),
        "ask_scm_branch_on_launch": bool(obj.get("ask_scm_branch_on_launch", False)),
        "ask_execution_environment_on_launch": bool(obj.get("ask_execution_environment_on_launch", False)),
        "ask_credential_on_launch": bool(obj.get("ask_credential_on_launch", False)),
        "survey_enabled": bool(obj.get("survey_enabled", False)),
        "extra_vars": obj.get("extra_vars", "") or ""
    }
    if obj.get("survey_enabled") and obj.get("survey"):
        p["survey_spec"] = obj.get("survey")
    return p


# ---------------- create/attach ----------------
def create_jt(aap: str, tok: str, payload: Dict[str, Any], v: bool) -> Dict[str, Any]:
    return POST(f"{aap}/api/controller/v2/job_templates/", H(tok), payload, v)


def attach_cred_to_jt(aap: str, tok: str, jt_id: int, cred_id: int, v: bool) -> None:
    POST(f"{aap}/api/controller/v2/job_templates/{jt_id}/credentials/", H(tok), {"id": cred_id}, v)


def resolve_cred_ids(aap: str, tok: str, org: int, v: bool,
                     awx_jt_creds_list: Iterable[Dict[str, Any]]) -> List[int]:
    ids: List[int] = []
    for c in awx_jt_creds_list:
        nm = c.get('name')
        awx_type_name = (c.get('summary_fields') or {}).get('credential_type', {}).get('name')
        if not nm:
            continue
        found = aap_find_credential(aap, tok, nm, awx_type_name, org, v)
        if not found:
            raise RuntimeError(
                f"Credential '{nm}'"
                f"{' (type '+awx_type_name+')' if awx_type_name else ''} not found in AAP. "
                f"Migrate credentials first."
            )
        ids.append(found['id'])
    return ids


# ---------------- per-JT migration ----------------
def migrate_one(args: argparse.Namespace, obj: Dict[str, Any]) -> None:
    name = obj.get('name', f"jt-{obj.get('id', '?')}")
    sf = obj.get('summary_fields') or {}
    proj_name = (sf.get('project') or {}).get('name')
    inv_name = (sf.get('inventory') or {}).get('name')

    proj_id, inv_id, ee_id = ensure_refs(
        args.aap_host, args.aap_token, args.organization_id, args.verify_tls,
        proj_name, inv_name, args.force_ee_id
    )

    existing = find_aap_jt(args.aap_host, args.aap_token, name, args.organization_id, args.verify_tls)
    if existing:
        print(f"SKIP (exists): {name} -> AAP id {existing.get('id')}")
        return

    payload = jt_payload_from_awx(obj, args.organization_id, proj_id, inv_id, ee_id)
    if args.dry_run:
        print(f"DRY-RUN (create JT): {name}  [EE id {args.force_ee_id}]")
        return

    created = create_jt(args.aap_host, args.aap_token, payload, args.verify_tls)
    jt_id = created.get('id')
    print(f"CREATED JT: {name} -> AAP id {jt_id}  [EE id {args.force_ee_id}]")

    creds = list(awx_jt_creds(args.awx_host, args.awx_token, obj['id'], args.verify_tls))
    if creds:
        ids = resolve_cred_ids(args.aap_host, args.aap_token, args.organization_id, args.verify_tls, creds)
        for cid in ids:
            attach_cred_to_jt(args.aap_host, args.aap_token, jt_id, cid, args.verify_tls)
        print(f"  Attached {len(ids)} credential(s)")


# ---------------- main ----------------
def main() -> int:
    args = parse_args()
    args.awx_host = norm(args.awx_host)
    args.aap_host = norm(args.aap_host)
    ping(args.aap_host, args.aap_token, args.verify_tls)

    # Single vs bulk
    template_id = args.template_id
    if template_id is None:
        env_tid = os.getenv("TEMPLATE_ID")
        if env_tid and env_tid.isdigit():
            template_id = int(env_tid)

    inc: Optional[Pattern[str]] = re.compile(args.include) if args.include else None
    exc: Optional[Pattern[str]] = re.compile(args.exclude) if args.exclude else None

    if template_id is not None:
        obj = awx_jt(args.awx_host, args.awx_token, template_id, args.verify_tls)
        migrate_one(args, obj)
        return 0

    if not args.all:
        raise SystemExit(
            "Specify --template-id <ID> (or set TEMPLATE_ID env var) for single JT, "
            "or use --all for bulk migration."
        )

    migrated = filtered = fail = 0
    for i, obj in enumerate(awx_jts(args.awx_host, args.awx_token, args.verify_tls), start=1):
        name = obj.get('name', f"jt-{obj.get('id', '?')}")
        if not filt(name, inc, exc):
            print(f"[{i}] SKIP (filtered): {name}")
            filtered += 1
            continue
        try:
            migrate_one(args, obj)
            migrated += 1
        except Exception as e:
            print(f"[{i}] ERROR: {name}: {e}", file=sys.stderr)
            fail += 1

    print("\nSummary:")
    print(f"  Migrated attempts: {migrated}")
    print(f"  Filtered:          {filtered}")
    print(f"  Failures:          {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
