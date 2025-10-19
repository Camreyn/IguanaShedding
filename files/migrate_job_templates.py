#!/usr/bin/env python3
"""
Migrate AWX Job Templates → AAP (Controller), single or bulk.

Features:
- Force EE by AAP id:              --force-ee-id 5
- Force Machine cred by AAP id:    --force-machine-cred-id 31
- Surveys: auto-copied from /survey_spec/ and enabled after create
- Email notifications:             --with-notifications [--notif-secrets-file secrets.(yml|json)]
- Schedules:                       --with-schedules

Single JT:
  python3 migrate_job_templates.py ... --template-id 123 --force-ee-id 5 --force-machine-cred-id 31

Bulk:
  python3 migrate_job_templates.py ... --all --force-ee-id 5 --force-machine-cred-id 31 \
    --with-notifications --with-schedules
"""
import argparse
import os
import sys
import datetime
import re
import json
from typing import Any, Optional, Dict, Iterable, Optional, Tuple, Pattern, List
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 30

RRULE_DT_RE = re.compile(r"^DTSTART(?:;TZID=[^:]+)?:", re.IGNORECASE | re.MULTILINE)
RRULE_RULE_RE = re.compile(r"^RRULE:", re.IGNORECASE | re.MULTILINE)

SAFE_SCHEDULE_KEYS = {
    "name", "rrule", "enabled", "unified_job_template", "timezone",
    # Optional job overrides that Controller accepts on schedules:
    "extra_data", "inventory", "scm_branch", "limit",
    "job_tags", "skip_tags", "verbosity", "forks", "timeout",
    "execution_environment"
}

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

    # EE / credential overrides (by AAP ids)
    env_force_ee_id = os.getenv('FORCE_EE_ID')
    p.add_argument('--force-ee-id', type=int,
                   default=(int(env_force_ee_id) if env_force_ee_id and env_force_ee_id.isdigit() else None),
                   help='AAP Execution Environment API ID to set on migrated JTs (e.g., 5).')
    env_force_mc_id = os.getenv('FORCE_MACHINE_CRED_ID')
    p.add_argument('--force-machine-cred-id', type=int,
                   default=(int(env_force_mc_id) if env_force_mc_id and env_force_mc_id.isdigit() else None),
                   help='AAP Credential API ID to attach for any AWX “Machine” creds (e.g., 31).')

    # Optional features
    p.add_argument('--with-notifications', action='store_true', help='Migrate and attach EMAIL notification templates')
    p.add_argument('--notif-secrets-file', help='YAML/JSON for redacted email fields, keyed by notif name')
    p.add_argument('--with-schedules', action='store_true', help='Migrate schedules of each JT')

    return p.parse_args()

# ---------------- HTTP helpers ----------------
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
    if r.status_code in (200, 201, 202):
        try:
            return r.json()
        except ValueError:
            return {}
    if r.status_code == 204:  # AAP often returns 204 for association endpoints
        return {}
    raise RuntimeError(f"POST {url} -> {r.status_code}:\n{r.text}")

def PATCH(url: str, h: Dict[str, str], payload: Dict[str, Any], v: bool) -> Dict[str, Any]:
    r = requests.patch(url, headers=h, json=payload, verify=v, timeout=TIMEOUT)
    if r.status_code in (200, 201, 202):
        try:
            return r.json()
        except ValueError:
            return {}
    if r.status_code == 204:  # Some PATCH operations can also return 204
        return {}
    raise RuntimeError(f"PATCH {url} -> {r.status_code}:\n{r.text}")

def ping(aap: str, tok: str, v: bool) -> None:
    u = f"{aap}/api/controller/v2/ping/"
    r = requests.get(u, headers=H(tok), verify=v, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"AAP ping {r.status_code}: {r.text}")
    
def emit(event: str, **fields: Any) -> None:
    """Emit one JSON line (NDJSON) for machine-readable logs in AWX."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    rec.update(fields)
    print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))

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

def awx_jt_survey_spec(a: str, t: str, jtid: int, v: bool) -> Optional[Dict[str, Any]]:
    """
    Always attempt to read the survey spec from AWX regardless of enablement.
    Returns a dict with shape {"name": ..., "description": ..., "spec": [...]}
    or None if no survey exists.
    """
    u = f"{a}/api/v2/job_templates/{jtid}/survey_spec/"
    try:
        d = GET(u, H(t), v)
        if isinstance(d, dict) and d.get("spec"):
            return d
        return None
    except Exception:
        return None

def awx_jt_notifications(a: str, t: str, jtid: int, v: bool) -> Dict[str, List[Dict[str, Any]]]:
    hdr = H(t)
    def one(path: str) -> List[Dict[str, Any]]:
        d = GET(f"{a}/api/v2/job_templates/{jtid}/{path}/", hdr, v)
        return list(d.get('results', []))
    return {
        "started": one("notification_templates_started"),
        "success": one("notification_templates_success"),
        "error":   one("notification_templates_error"),
    }

def awx_jt_schedules(a: str, t: str, jtid: int, v: bool) -> List[Dict[str, Any]]:
    return list(GET(f"{a}/api/v2/job_templates/{jtid}/schedules/?page_size=200", H(t), v).get("results", []))

def iso_to_ics_dtstart(dt: str) -> str:
    """
    Convert ISO8601 ('2025-10-16T14:00:00Z' or with offset) to ICS DTSTART (UTC Z).
    """
    try:
        # Handle both Z and offset formats
        if dt.endswith("Z"):
            d = datetime.datetime.fromisoformat(dt.replace("Z", "+00:00"))
        else:
            d = datetime.datetime.fromisoformat(dt)
        d = d.astimezone(datetime.timezone.utc)
        return d.strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return ""

def normalize_rrule(raw_rrule: str, next_run: Optional[str], timezone_str: Optional[str]) -> Tuple[str, Optional[str]]:
    r = (raw_rrule or "").strip()
    if not r:
        return r, timezone_str

    # Ensure DTSTART on its own line, then RRULE on next line
    r = re.sub(r"(DTSTART[^\n]+)\s+(RRULE:)", r"\\1\n\\2", r, flags=re.IGNORECASE)

    has_dtstart = bool(RRULE_DT_RE.search(r))
    has_rrule   = bool(RRULE_RULE_RE.search(r))
    dt_line = _format_dtstart(next_run, timezone_str) if not has_dtstart else None

    # If RRULE is missing the keyword, leave as is (Controller will complain—we’ll log it)
    if dt_line:
        if has_rrule:
            # ensure DTSTART precedes RRULE
            if r.upper().startswith("RRULE:"):
                r = f"{dt_line}\n{r}"
            elif not r.upper().startswith("DTSTART"):
                r = f"{dt_line}\n{r}"
        else:
            r = f"{dt_line}\n{r}"

    return r, timezone_str

def _parse_iso(dt: str) -> Optional[datetime]:
    try:
        if dt.endswith("Z"):
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
        return datetime.fromisoformat(dt)
    except Exception:
        return None
    
def _format_dtstart(next_run_iso: Optional[str], tz: Optional[str]) -> Optional[str]:
    """
    Prefer TZ-aware DTSTART if we have a timezone; otherwise UTC 'Z'.
    """
    if not next_run_iso:
        return None
    d = _parse_iso(next_run_iso)
    if not d:
        return None
    if tz and ZoneInfo:
        try:
            d = d.astimezone(ZoneInfo(tz))
            return f"DTSTART;TZID={tz}:{d.strftime('%Y%m%dT%H%M%S')}"
        except Exception:
            pass
    # fallback UTC
    d = d.astimezone(timezone.utc)
    return f"DTSTART:{d.strftime('%Y%m%dT%H%M%SZ')}"

def schedule_payload_minimal(s: Dict[str, Any], jt_id: int, jt_inventory_id: Optional[int], aap_host: str) -> Dict[str, Any]:
    fixed_rrule, tz = normalize_rrule(
        s.get("rrule", ""),
        s.get("next_run") or s.get("next_run_original"),
        s.get("timezone")
    )
    p: Dict[str, Any] = {
        "name": s.get("name", ""),
        "enabled": bool(s.get("enabled", True)),
        "unified_job_template": jt_id,
        "rrule": fixed_rrule,
    }
    if tz:                           p["timezone"] = tz
    if jt_inventory_id is not None: p["inventory"] = jt_inventory_id  # force JT inventory to avoid missing AWX inventories
    if s.get("extra_data"):          p["extra_data"] = s["extra_data"]
    if s.get("scm_branch"):          p["scm_branch"] = s["scm_branch"]
    if s.get("limit"):               p["limit"] = s["limit"]           # preserve limit
    if s.get("job_tags"):            p["job_tags"] = s["job_tags"]
    if s.get("skip_tags"):           p["skip_tags"] = s["skip_tags"]
    if s.get("verbosity") is not None: p["verbosity"] = int(s.get("verbosity") or 0)
    if s.get("forks")     is not None: p["forks"]     = int(s.get("forks") or 0)
    if s.get("timeout")   is not None: p["timeout"]   = int(s.get("timeout") or 0)
    if s.get("execution_environment"): p["execution_environment"] = s["execution_environment"]
    return p

def schedule_payload_bareminimum(s: Dict[str, Any], jt_id: int, jt_inventory_id: Optional[int], aap_host: str) -> Dict[str, Any]:
    fixed_rrule, tz = normalize_rrule(
        s.get("rrule", ""),
        s.get("next_run") or s.get("next_run_original"),
        s.get("timezone")
    )
    p: Dict[str, Any] = {
        "name": s.get("name", ""),
        "enabled": bool(s.get("enabled", True)),
        "unified_job_template": jt_id,
        "rrule": fixed_rrule,
    }
    if tz:                           p["timezone"] = tz
    if jt_inventory_id is not None: p["inventory"] = jt_inventory_id
    if s.get("limit"):               p["limit"] = s["limit"]           # keep limit even in bare-min
    return p

def try_create_schedule(aap_host: str, aap_tok: str, verify: bool,
                        jt_id: int, jt_inventory_id: Optional[int], s: Dict[str, Any]) -> bool:
    """
    Attempts (in order):
      1) full payload (UJT id)           with JT inventory
      2) full payload (UJT URL)          with JT inventory
      3) bare-min payload (UJT id)       with JT inventory
      4) bare-min payload (UJT URL)      with JT inventory
    Emits NDJSON events with details (payload subset + server message).
    """
    def with_ujt_url(payload: Dict[str, Any]) -> Dict[str, Any]:
        u = f"{aap_host}/api/controller/v2/job_templates/{jt_id}/"
        q = dict(payload)
        q["unified_job_template"] = u
        return q

    variants = [
        ("full-id",  schedule_payload_minimal(s, jt_id, jt_inventory_id, aap_host)),
        ("full-url", with_ujt_url(schedule_payload_minimal(s, jt_id, jt_inventory_id, aap_host))),
        ("bare-id",  schedule_payload_bareminimum(s, jt_id, jt_inventory_id, aap_host)),
        ("bare-url", with_ujt_url(schedule_payload_bareminimum(s, jt_id, jt_inventory_id, aap_host))),
    ]

    for idx, (label, pl) in enumerate(variants, start=1):
        try:
            POST(f"{aap_host}/api/controller/v2/schedules/", H(aap_tok), pl, verify)
            emit("schedule.create.ok", attempt=idx, variant=label, name=pl.get("name",""), ujt=str(pl.get("unified_job_template")))
            return True
        except Exception as e:
            # log compact payload diagnostics
            diag = {k: pl.get(k) for k in ("name","unified_job_template","timezone","rrule","inventory","limit")}
            emit("schedule.create.fail", attempt=idx, variant=label, payload=diag, error=str(e))
    return False

# ---------------- AAP lookups & asserts ----------------
def q_one(aap: str, tok: str, endpoint: str, name: str, org: int, v: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    u = f"{aap}/api/controller/v2/{endpoint}/?name={quote(name)}&organization={org}"
    d = GET(u, H(tok), v)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and res:
        return res[0]
    return None

def assert_ee_exists_by_id(aap: str, tok: str, ee_id: int, org_id: int, v: bool) -> None:
    d = GET(f"{aap}/api/controller/v2/execution_environments/{ee_id}/", H(tok), v)
    ee_org = d.get('organization')
    if ee_org not in (None, org_id):
        raise RuntimeError(
            f"Execution Environment id {ee_id} belongs to organization {ee_org}, "
            f"which does not match target org {org_id}."
        )

def assert_credential_exists_by_id(aap: str, tok: str, cred_id: int, org_id: int, v: bool) -> None:
    d = GET(f"{aap}/api/controller/v2/credentials/{cred_id}/", H(tok), v)
    c_org = d.get('organization')
    if c_org not in (None, org_id):
        raise RuntimeError(
            f"Credential id {cred_id} belongs to organization {c_org}, "
            f"which does not match target org {org_id}."
        )

def find_aap_jt(aap: str, tok: str, name: str, org: int, v: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    u = f"{aap}/api/controller/v2/job_templates/?name={quote(name)}&organization={org}"
    d = GET(u, H(tok), v)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and res:
        return res[0]
    return None

# Credential resolution helpers
def aap_get_credential_type_id_by_name(aap: str, tok: str, name: str, verify: bool) -> Optional[int]:
    from requests.utils import quote
    d = GET(f"{aap}/api/controller/v2/credential_types/?name={quote(name)}", H(tok), verify)
    res = d.get('results') or d.get('data') or []
    return res[0]['id'] if isinstance(res, list) and res else None

def aap_find_credential(aap: str, tok: str, name: str,
                        awx_ctype_name: Optional[str],
                        org_id: int, verify: bool) -> Optional[Dict[str, Any]]:
    from requests.utils import quote
    d = GET(f"{aap}/api/controller/v2/credentials/?name={quote(name)}&organization={org_id}", H(tok), verify)
    res = d.get('results') or d.get('data') or []
    if isinstance(res, list) and len(res) == 1:
        return res[0]
    if isinstance(res, list) and len(res) > 1 and awx_ctype_name:
        for r in res:
            ctn = ((r.get('summary_fields') or {}).get('credential_type') or {}).get('name')
            if ctn and ctn == awx_ctype_name:
                return r
    if awx_ctype_name:
        ctid = aap_get_credential_type_id_by_name(aap, tok, awx_ctype_name, verify)
        if ctid:
            d = GET(f"{aap}/api/controller/v2/credentials/?name={quote(name)}&organization={org_id}&credential_type={ctid}",
                    H(tok), verify)
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
    return {
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
        "extra_vars": obj.get("extra_vars", "") or "",
    }
    if survey_spec:
        p["survey_spec"] = survey_spec
        p["survey_enabled"] = True
    return p

def schedule_payload_from_awx(s: Dict[str, Any], jt_id: int) -> Dict[str, Any]:
    return {
        "name": s.get("name", ""),
        "description": s.get("description", "") or "",
        "rrule": s.get("rrule", ""),
        "enabled": bool(s.get("enabled", True)),
        "unified_job_template": jt_id,
        "timezone": s.get("timezone") or None,
        "extra_data": s.get("extra_data") or {},
        "inventory": s.get("inventory"),
        "scm_branch": s.get("scm_branch") or "",
        "limit": s.get("limit") or "",
        "job_tags": s.get("job_tags") or "",
        "skip_tags": s.get("skip_tags") or "",
        "verbosity": s.get("verbosity", 0) or 0,
        "forks": s.get("forks", 0) or 0,
        "timeout": s.get("timeout", 0) or 0,
        "execution_environment": s.get("execution_environment"),
    }

# ---------------- notifications (EMAIL only) ----------------
REDACTIONS = {"********", "*****", "******", "<redacted>", "<REDACTED>"}

def load_notif_secrets(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    try:
        if path.endswith((".yml", ".yaml")):
            import yaml  # noqa: F401
            with open(path, "r", encoding="utf-8") as f:
                data = __import__("yaml").safe_load(f) or {}
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to read notif secrets file {path}: {e}")
    return data.get("notifications", data) if isinstance(data, dict) else {}

def merge_email_config(src: Dict[str, Any], secrets: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(src or {})
    for k, v in list(out.items()):
        if v in (None, "") or (isinstance(v, str) and v.strip() in REDACTIONS):
            if k in secrets:
                out[k] = secrets[k]
    for k, v in secrets.items():
        if k not in out or out[k] in (None, ""):
            out[k] = v
    return out

def aap_find_email_notif(aap: str, tok: str, name: str, org_id: int, v: bool) -> Optional[Dict[str, Any]]:
    return q_one(aap, tok, "notification_templates", name, org_id, v)

def create_email_notification_template(aap: str, tok: str, org_id: int, name: str, desc: str,
                                       config: Dict[str, Any], v: bool) -> Dict[str, Any]:
    payload = {
        "name": name, "description": desc or "", "organization": org_id,
        "notification_type": "email", "notification_configuration": config,
    }
    return POST(f"{aap}/api/controller/v2/notification_templates/", H(tok), payload, v)

def attach_notifs_email(aap: str, tok: str, jt_id: int, org_id: int, v: bool,
                        notifs: Dict[str, List[Dict[str, Any]]],
                        secrets_map: Dict[str, Dict[str, Any]],
                        dry_run: bool) -> None:
    kind_to_path = {
        "started": "notification_templates_started",
        "success": "notification_templates_success",
        "error":   "notification_templates_error",
    }
    for kind, path in kind_to_path.items():
        for n in notifs.get(kind, []):
            if (n.get("notification_type") or "").lower() != "email":
                continue
            name = n.get("name")
            if not name:
                continue
            existing = aap_find_email_notif(aap, tok, name, org_id, v)
            if not existing:
                conf = merge_email_config(n.get("notification_configuration") or {},
                                          secrets_map.get(name, {}))
                if dry_run:
                    print(f"  DRY-RUN: create email notification '{name}'")
                    notif_id = -1
                else:
                    created = create_email_notification_template(aap, tok, org_id, name, n.get("description", ""), conf, v)
                    notif_id = created.get("id")
                    print(f"  Created email notification '{name}' -> id {notif_id}")
            else:
                notif_id = existing.get("id")
            if dry_run:
                print(f"  DRY-RUN: attach notification '{name}' ({kind})")
            else:
                POST(f"{aap}/api/controller/v2/job_templates/{jt_id}/{path}/", H(tok), {"id": notif_id}, v)

# ---------------- create/attach ----------------
def create_jt(aap: str, tok: str, payload: Dict[str, Any], v: bool) -> Dict[str, Any]:
    return POST(f"{aap}/api/controller/v2/job_templates/", H(tok), payload, v)

def patch_enable_survey(aap: str, tok: str, jt_id: int, v: bool) -> None:
    PATCH(f"{aap}/api/controller/v2/job_templates/{jt_id}/", H(tok), {"survey_enabled": True}, v)

def attach_cred_to_jt(aap: str, tok: str, jt_id: int, cred_id: int, v: bool) -> None:
    POST(f"{aap}/api/controller/v2/job_templates/{jt_id}/credentials/", H(tok), {"id": cred_id}, v)

def resolve_cred_ids(aap: str, tok: str, org: int, v: bool,
                     awx_jt_creds_list: Iterable[Dict[str, Any]],
                     force_machine_cred_id: Optional[int]) -> List[int]:
    ids: List[int] = []
    for c in awx_jt_creds_list:
        nm = c.get('name')
        sf = c.get('summary_fields') or {}
        awx_type_name = (sf.get('credential_type') or {}).get('name')
        if force_machine_cred_id is not None and (awx_type_name or '').lower() == 'machine':
            assert_credential_exists_by_id(aap, tok, force_machine_cred_id, org, v)
            if force_machine_cred_id not in ids:
                ids.append(force_machine_cred_id)
            continue
        if not nm:
            continue
        found = aap_find_credential(aap, tok, nm, awx_type_name, org, v)
        if not found:
            raise RuntimeError(
                f"Credential '{nm}'"
                f"{' (type '+awx_type_name+')' if awx_type_name else ''} not found in AAP. "
                f"Migrate credentials first or supply --force-machine-cred-id for Machine creds."
            )
        cid = found['id']
        if cid not in ids:
            ids.append(cid)
    return ids

def post_survey_spec_to_aap(aap: str, tok: str, jt_id: int, spec: Dict[str, Any], v: bool) -> None:
    """
    POST survey_spec, then enable on the JT.
    Controller returns 200/204; both are success.
    """
    POST(f"{aap}/api/controller/v2/job_templates/{jt_id}/survey_spec/", H(tok), spec, v)
    PATCH(f"{aap}/api/controller/v2/job_templates/{jt_id}/", H(tok), {"survey_enabled": True}, v)

# ---------------- per-JT migration ----------------
def ensure_refs(aap: str, tok: str, org: int, v: bool,
                proj_name: Optional[str], inv_name: Optional[str],
                forced_ee_id: Optional[int]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
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

def migrate_one(args: argparse.Namespace, obj: Dict[str, Any],
                notif_secrets_map: Dict[str, Dict[str, Any]]) -> None:
    name = obj.get('name', f"jt-{obj.get('id', '?')}")
    sf = obj.get('summary_fields') or {}
    proj_name = (sf.get('project') or {}).get('name')
    inv_name  = (sf.get('inventory') or {}).get('name')

    # Resolve refs (EE forced via --force-ee-id)
    proj_id, inv_id, ee_id = ensure_refs(
        args.aap_host, args.aap_token, args.organization_id, args.verify_tls,
        proj_name, inv_name, args.force_ee_id
    )

    # Always fetch AWX survey spec (even if not enabled there)
    survey_spec = awx_jt_survey_spec(args.awx_host, args.awx_token, obj['id'], args.verify_tls)

    # Find or create JT on AAP
    existing = find_aap_jt(args.aap_host, args.aap_token, name, args.organization_id, args.verify_tls)
    jt_id: Optional[int] = None

    if existing:
        jt_id = existing.get('id')
        print(f"FOUND existing JT: {name} -> AAP id {jt_id}  [will update attachments/survey/schedules]")
        # If you prefer to skip updates on existing JTs, uncomment next line:
        # print(f"SKIP (exists): {name} -> AAP id {jt_id}"); return
    else:
        payload = jt_payload_from_awx(obj, args.organization_id, proj_id, inv_id, ee_id)
        if args.dry_run:
            print(f"DRY-RUN (create JT): {name}  [EE id {args.force_ee_id}]")
            # In dry-run we don’t have a real jt_id; don’t proceed with actions that need jt_id.
            return
        created = create_jt(args.aap_host, args.aap_token, payload, args.verify_tls)
        jt_id = created.get('id')
        print(f"CREATED JT: {name} -> AAP id {jt_id}  [EE id {args.force_ee_id}]")

    # Guard: from here down, jt_id must exist
    if jt_id is None:
        raise RuntimeError(f"Internal error: jt_id not set for template '{name}'")

    # Survey: POST spec then enable
    if survey_spec:
        if args.dry_run:
            print("  DRY-RUN: would POST survey_spec and enable survey")
        else:
            post_survey_spec_to_aap(args.aap_host, args.aap_token, jt_id, survey_spec, args.verify_tls)
            # verify
            try:
                check = GET(f"{args.aap_host}/api/controller/v2/job_templates/{jt_id}/survey_spec/",
                            H(args.aap_token), args.verify_tls)
                if isinstance(check, dict) and check.get("spec"):
                    print(f"  Survey copied & enabled ({len(check['spec'])} question(s))")
                else:
                    print("  WARN: survey POSTed but verification showed empty spec")
            except Exception as _e:
                print(f"  WARN: survey verification failed: {_e}")

    # Credentials
    creds = list(awx_jt_creds(args.awx_host, args.awx_token, obj['id'], args.verify_tls))
    if creds:
        if args.dry_run:
            print("  DRY-RUN: would attach credentials")
        else:
            ids = resolve_cred_ids(args.aap_host, args.aap_token, args.organization_id, args.verify_tls,
                                   creds, args.force_machine_cred_id)
            for cid in ids:
                attach_cred_to_jt(args.aap_host, args.aap_token, jt_id, cid, args.verify_tls)
            print(f"  Attached {len(ids)} credential(s)")

    # Notifications (email)
    if args.with_notifications:
        notifs = awx_jt_notifications(args.awx_host, args.awx_token, obj['id'], args.verify_tls)
        if any(notifs.values()):
            if args.dry_run:
                print("  DRY-RUN: would create/attach email notifications")
            else:
                attach_notifs_email(args.aap_host, args.aap_token, jt_id, args.organization_id, args.verify_tls,
                                    notifs, notif_secrets_map, args.dry_run)
                print("  Processed notifications (email)")

    # Schedules
    if args.with_schedules:
        schedules = awx_jt_schedules(args.awx_host, args.awx_token, obj['id'], args.verify_tls)
        created_cnt = 0
        for s in schedules:
            nm = s.get("name","")
            raw_rrule = (s.get("rrule") or "").strip()
            if not raw_rrule:
                print(f"  WARN: schedule '{nm}' has empty rrule; skipped")
                emit("schedule.skip.empty_rrule", name=nm)
                continue
            if args.dry_run:
                print(f"  DRY-RUN: would create schedule '{nm}' (force JT inventory, keep limit)")
                emit("schedule.dryrun", name=nm)
                created_cnt += 1
                continue
            ok = try_create_schedule(
                args.aap_host, args.aap_token, args.verify_tls,
                jt_id,   # from created/found earlier
                inv_id,  # force schedules to use JT inventory
                s
            )
            if ok:
                created_cnt += 1
            else:
                print(f"  WARN: giving up on schedule '{nm}' after 4 attempts")
                emit("schedule.create.giveup", name=nm)
    
        # verify
        try:
            aap_scheds = GET(f"{args.aap_host}/api/controller/v2/job_templates/{jt_id}/schedules/?page_size=200",
                             H(args.aap_token), args.verify_tls)
            total = len(aap_scheds.get("results", []))
            print(f"  Schedules created: {created_cnt}; AAP currently shows {total}")
            emit("schedule.verify", created=created_cnt, aap_count=total)
        except Exception as _e:
            print(f"  WARN: schedule verification fetch failed: {_e}")
            emit("schedule.verify.fail", error=str(_e))

# ---------------- main ----------------
def main() -> int:
    args = parse_args()
    args.awx_host = norm(args.awx_host); args.aap_host = norm(args.aap_host)
    ping(args.aap_host, args.aap_token, args.verify_tls)

    # Single vs bulk
    template_id = args.template_id
    if template_id is None:
        env_tid = os.getenv("TEMPLATE_ID")
        if env_tid and env_tid.isdigit():
            template_id = int(env_tid)

    inc: Optional[Pattern[str]] = re.compile(args.include) if args.include else None
    exc: Optional[Pattern[str]] = re.compile(args.exclude) if args.exclude else None

    # Load notif secrets (optional)
    notif_secrets_map: Dict[str, Dict[str, Any]] = {}
    if args.with_notifications and args.notif_secrets_file:
        notif_secrets_map = load_notif_secrets(args.notif_secrets_file)

    if template_id is not None:
        obj = awx_jt(args.awx_host, args.awx_token, template_id, args.verify_tls)
        migrate_one(args, obj, notif_secrets_map)
        return 0

    if not args.all:
        raise SystemExit("Specify --template-id <ID> (or set TEMPLATE_ID) for single JT, or use --all.")

    migrated = filtered = fail = 0
    for i, obj in enumerate(awx_jts(args.awx_host, args.awx_token, args.verify_tls), start=1):
        name = obj.get('name', f"jt-{obj.get('id', '?')}")
        if not filt(name, inc, exc):
            print(f"[{i}] SKIP (filtered): {name}"); filtered += 1; continue
        try:
            migrate_one(args, obj, notif_secrets_map); migrated += 1
        except Exception as e:
            print(f"[{i}] ERROR: {name}: {e}", file=sys.stderr); fail += 1

    print("\nSummary:")
    print(f"  Migrated attempts: {migrated}")
    print(f"  Filtered:          {filtered}")
    print(f"  Failures:          {fail}")
    return 0 if fail == 0 else 2

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)
