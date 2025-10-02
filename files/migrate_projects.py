#!/usr/bin/env python3
"""
Migrate AWX projects to AAP (Controller).

Modes:
  • Single: --project-id 106
  • Bulk:   --all  [--include REGEX] [--exclude REGEX] [--limit N] [--dry-run]

Endpoints:
  • AWX read:           /api/v2/projects/
  • AAP controller base /api/controller/v2
  • AAP create project: /api/controller/v2/projects/  (body includes {"organization": <id>})

Exit codes: 0 success (or dry-run), 2 partial failures, 1 fatal.
"""
import argparse
import re
import sys
from typing import Dict, Any, Iterable, Optional, Pattern
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TIMEOUT = 30  # seconds


# --------- args ---------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate AWX projects to AAP (Controller)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--project-id', type=int, help='Migrate a single project by ID')
    g.add_argument('--all', action='store_true', help='Migrate all projects')

    p.add_argument('--awx-host', required=True)
    p.add_argument('--awx-token', required=True)
    p.add_argument('--aap-host', required=True)
    p.add_argument('--aap-token', required=True)
    p.add_argument('--organization-id', required=True, type=int)
    p.add_argument('--include', help='Regex; only project names matching are migrated (bulk mode)')
    p.add_argument('--exclude', help='Regex; project names matching are excluded (bulk mode)')
    p.add_argument('--limit', type=int, help='Stop after N processed (bulk mode)')
    p.add_argument('--dry-run', action='store_true', help='Preview only; no creates')
    p.add_argument('--verify-tls', action='store_true', help='Enable TLS verification (default off)')
    return p.parse_args()


# --------- http utils ---------
def norm_host(host: str) -> str:
    return host.rstrip('/')


def headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}


def get_json(url: str, hdrs: Dict[str, str], verify: bool) -> Dict[str, Any]:
    r = requests.get(url, headers=hdrs, verify=verify, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text}")
    return r.json()


def post_json(url: str, hdrs: Dict[str, str], payload: Dict[str, Any], verify: bool) -> Dict[str, Any]:
    r = requests.post(url, headers=hdrs, json=payload, verify=verify, timeout=TIMEOUT)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"POST {url} -> {r.status_code}:\n{r.text}")
    return r.json()


# --------- AAP checks ---------
def aap_ping(aap_host: str, aap_token: str, verify: bool) -> None:
    url = f"{aap_host}/api/controller/v2/ping/"
    r = requests.get(url, headers=headers(aap_token), verify=verify, timeout=TIMEOUT)
    if r.status_code == 401:
        raise RuntimeError("AAP token unauthorized (401).")
    if r.status_code == 403:
        raise RuntimeError("AAP token forbidden (403).")
    if r.status_code != 200:
        raise RuntimeError(f"AAP controller not reachable at {url} -> {r.status_code}: {r.text}")


def assert_org_exists(aap_host: str, aap_token: str, org_id: int, verify: bool) -> None:
    url = f"{aap_host}/api/controller/v2/organizations/{org_id}/"
    r = requests.get(url, headers=headers(aap_token), verify=verify, timeout=TIMEOUT)
    if r.status_code == 404:
        raise RuntimeError(f"Organization id {org_id} not found on AAP.")
    if r.status_code != 200:
        raise RuntimeError(f"Failed to validate organization {org_id}: {r.status_code} {r.text}")


# --------- AWX reads ---------
def get_awx_project(awx_host: str, awx_token: str, project_id: int, verify: bool) -> Dict[str, Any]:
    url = f"{awx_host}/api/v2/projects/{project_id}/"
    return get_json(url, headers(awx_token), verify)


def paged_awx_projects(awx_host: str, awx_token: str, verify: bool) -> Iterable[Dict[str, Any]]:
    url = f"{awx_host}/api/v2/projects/?page_size=200"
    hdrs = headers(awx_token)
    while url:
        data = get_json(url, hdrs, verify)
        for obj in data.get('results', []):
            yield obj
        url = data.get('next')


# --------- transform & create ---------
def clean_project_for_aap(src: Dict[str, Any], org_id: int) -> Dict[str, Any]:
    excluded = {
        "id", "related", "summary_fields", "created", "modified",
        "last_job", "last_job_run", "current_update", "scm_last_revision",
        "default_environment", "signature_validation_credential",
        "last_update_failed", "status", "scm_revision", "organization",
    }
    cleaned = {k: v for k, v in src.items() if k not in excluded}
    payload = {
        "name": cleaned.get("name", "Unnamed Migrated Project"),
        "description": cleaned.get("description", "") or "",
        "scm_type": cleaned.get("scm_type") or "",
        "scm_url": cleaned.get("scm_url") or "",
        "scm_branch": cleaned.get("scm_branch") or "",
        "scm_clean": bool(cleaned.get("scm_clean", False)),
        "scm_track_submodules": bool(cleaned.get("scm_track_submodules", False)),
        "scm_delete_on_update": bool(cleaned.get("scm_delete_on_update", False)),
        "scm_update_on_launch": bool(cleaned.get("scm_update_on_launch", False)),
        "scm_update_cache_timeout": int(cleaned.get("scm_update_cache_timeout", 0) or 0),
        "timeout": int(cleaned.get("timeout", 0) or 0),
        "allow_override": bool(cleaned.get("allow_override", False)),
        "organization": org_id,
    }
    if payload["scm_url"] and not payload["scm_type"]:
        payload["scm_type"] = "git"
    return payload


def find_aap_project(aap_host: str, aap_token: str, name: str, org_id: int, verify: bool) -> Optional[Dict[str, Any]]:
    url = f"{aap_host}/api/controller/v2/projects/?name={requests.utils.quote(name)}&organization={org_id}"
    data = get_json(url, headers(aap_token), verify)
    results = data.get('results') or data.get('data') or []
    if not isinstance(results, list):
        results = []
    return results[0] if results else None


def create_aap_project(aap_host: str, aap_token: str, payload: Dict[str, Any], verify: bool) -> Dict[str, Any]:
    url = f"{aap_host}/api/controller/v2/projects/"
    return post_json(url, headers(aap_token), payload, verify)


# --------- helpers ---------
def should_migrate(name: str, include_re: Optional[Pattern[str]], exclude_re: Optional[Pattern[str]]) -> bool:
    if include_re and not include_re.search(name):
        return False
    if exclude_re and exclude_re.search(name):
        return False
    return True


# --------- main ---------
def run_single(args) -> int:
    awx_proj = get_awx_project(args.awx_host, args.awx_token, args.project_id, args.verify_tls)
    name = awx_proj.get('name', f"project-{args.project_id}")
    print(f"Source project: {name}")

    aap_ping(args.aap_host, args.aap_token, args.verify_tls)
    assert_org_exists(args.aap_host, args.aap_token, args.organization_id, args.verify_tls)

    existing = find_aap_project(args.aap_host, args.aap_token, name, args.organization_id, args.verify_tls)
    if existing:
        print(f"SKIP (exists): {name} -> AAP id {existing.get('id')}")
        return 0

    payload = clean_project_for_aap(awx_proj, args.organization_id)
    if args.dry_run:
        print(f"DRY-RUN (create): {name}")
        return 0

    created = create_aap_project(args.aap_host, args.aap_token, payload, args.verify_tls)
    print(f"CREATED: {name} -> AAP id {created.get('id')}")
    return 0


def run_bulk(args) -> int:
    include_re: Optional[Pattern[str]] = re.compile(args.include) if args.include else None
    exclude_re: Optional[Pattern[str]] = re.compile(args.exclude) if args.exclude else None

    aap_ping(args.aap_host, args.aap_token, args.verify_tls)
    assert_org_exists(args.aap_host, args.aap_token, args.organization_id, args.verify_tls)

    migrated = skipped_existing = skipped_filtered = failures = processed = 0

    for idx, awx_proj in enumerate(paged_awx_projects(args.awx_host, args.awx_token, args.verify_tls), start=1):
        name = awx_proj.get('name', f'project-{awx_proj.get("id","?")}')
        processed += 1

        if not should_migrate(name, include_re, exclude_re):
            print(f"[{idx}] SKIP (filtered): {name}")
            skipped_filtered += 1
            if args.limit and processed >= args.limit:
                break
            continue

        try:
            existing = find_aap_project(args.aap_host, args.aap_token, name, args.organization_id, args.verify_tls)
            if existing:
                print(f"[{idx}] SKIP (exists): {name} -> AAP id {existing.get('id')}")
                skipped_existing += 1
            else:
                payload = clean_project_for_aap(awx_proj, args.organization_id)
                if args.dry_run:
                    print(f"[{idx}] DRY-RUN (create): {name}")
                else:
                    created = create_aap_project(args.aap_host, args.aap_token, payload, args.verify_tls)
                    print(f"[{idx}] CREATED: {name} -> AAP id {created.get('id')}")
                migrated += 1
        except Exception as e:
            failures += 1
            print(f"[{idx}] ERROR: {name}: {e}", file=sys.stderr)

        if args.limit and processed >= args.limit:
            print(f"Limit {args.limit} reached, stopping.")
            break

    print("\nSummary:")
    print(f"  Migrated (or would migrate in dry-run): {migrated}")
    print(f"  Skipped existing:                      {skipped_existing}")
    print(f"  Skipped by include/exclude filters:    {skipped_filtered}")
    print(f"  Failures:                              {failures}")
    return 0 if failures == 0 else 2


def main() -> int:
    args = parse_args()
    args.awx_host = norm_host(args.awx_host)
    args.aap_host = norm_host(args.aap_host)

    if args.project_id:
        return run_single(args)
    return run_bulk(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
