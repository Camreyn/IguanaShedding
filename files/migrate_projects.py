#!/usr/bin/env python3
"""
Migrate AWX projects to AAP (Controller).

Modes:
  • Single-source (ATST only, existing behavior):
      --all [--include REGEX] [--exclude REGEX] [--limit N] [--dry-run]
      or --project-id <id>

  • PROD compare/migrate mode (new):
      --prod-mode --prod-awx-host ... --prod-awx-token ...
      [--include REGEX] [--exclude REGEX] [--prod-prefix STR] [--receipt-out PATH]
      Logic:
        1) Read all projects from PROD AWX and ATST AWX.
        2) For each PROD project, find any ATST project with same (scm_url, scm_branch).
        3) If match: skip migrate; record receipt: "MATCH: ATST=<name>, PROD=<name>, URL@branch".
        4) If no match: migrate PROD project to AAP; name is prefixed with PROD_ (configurable).
           Record receipt: "CREATED: AAP <id> name=<prefixed>; from PROD=<name> URL@branch".

Matching rules:
  • Normalize scm_url: strip trailing ".git", lower for scheme/host, preserve path; remove trailing slashes.
  • Normalize scm_branch: empty means "".

Exit codes: 0 success (or dry-run), 2 partial failures, 1 fatal.
"""

import argparse
import re
import sys
from typing import Dict, Any, Iterable, Optional, Pattern, Tuple, List
import requests
import urllib3
from urllib.parse import urlparse, urlunparse, quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TIMEOUT = 30  # seconds


# --------- args ---------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate AWX projects to AAP (Controller)")
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument('--project-id', type=int, help='Migrate a single project by ID (ATST source)')
    g.add_argument('--all', action='store_true', help='Migrate all projects from the primary AWX (ATST)')

    # Primary (ATST) and AAP
    p.add_argument('--awx-host', required=True, help='Primary AWX host (ATST)')
    p.add_argument('--awx-token', required=True, help='Primary AWX token (ATST)')
    p.add_argument('--aap-host', required=True)
    p.add_argument('--aap-token', required=True)
    p.add_argument('--organization-id', required=True, type=int)

    # Filters
    p.add_argument('--include', help='Regex; only project names matching are considered')
    p.add_argument('--exclude', help='Regex; project names matching are excluded')
    p.add_argument('--limit', type=int, help='Stop after N processed')
    p.add_argument('--dry-run', action='store_true', help='Preview only; no creates')
    p.add_argument('--verify-tls', action='store_true', help='Enable TLS verification (default off)')

    # PROD compare mode
    p.add_argument('--prod-mode', action='store_true', help='Enable PROD compare/migrate mode')
    p.add_argument('--prod-awx-host', help='PROD AWX host (required in --prod-mode)')
    p.add_argument('--prod-awx-token', help='PROD AWX token (required in --prod-mode)')
    p.add_argument('--prod-prefix', default='PROD_', help='Name prefix when creating PROD projects in AAP')
    p.add_argument('--receipt-out', default='migrate_projects_receipt.txt',
                   help='Path to write a migration receipt (text)')

    args = p.parse_args()

    if args.prod_mode:
        # In prod mode we drive from PROD set; --all/--project-id are ignored.
        if not args.prod_awx_host or not args.prod_awx_token:
            p.error("--prod-mode requires --prod-awx-host and --prod-awx-token")
    else:
        # legacy mode must specify one of single/bulk
        if not args.project_id and not args.all:
            p.error("Specify --project-id or --all (when not using --prod-mode)")

    return args


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


# --------- normalize & match ---------
def _strip_trailing_git(url: str) -> str:
    return url[:-4] if url.endswith(".git") else url


def _normalize_git_url(url: str) -> str:
    """
    Normalize SCM URL for comparison:
      • lower() scheme and netloc,
      • preserve path/case,
      • strip trailing ".git" and slashes,
      • remove default ports.
    """
    if not url:
        return ""
    u = urlparse(url.strip())
    scheme = (u.scheme or "").lower()
    netloc = (u.hostname or "").lower()
    if u.port and not ((scheme == "https" and u.port == 443) or (scheme == "http" and u.port == 80)):
        netloc = f"{netloc}:{u.port}"
    path = _strip_trailing_git(u.path or "").rstrip("/")
    norm = urlunparse((scheme, netloc, path, "", "", ""))
    return norm


def project_key(obj: Dict[str, Any]) -> Tuple[str, str]:
    url = _normalize_git_url(obj.get("scm_url") or "")
    branch = (obj.get("scm_branch") or "").strip()
    return (url, branch)


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
    url = f"{aap_host}/api/controller/v2/projects/?name={quote(name)}&organization={org_id}"
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


def load_projects_map(awx_host: str, awx_token: str, verify: bool) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Return mapping key=(norm_url, branch) -> project_obj."""
    mapping: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for obj in paged_awx_projects(awx_host, awx_token, verify):
        k = project_key(obj)
        # Keep first seen; if duplicates exist per key, prefer first by API order
        mapping.setdefault(k, obj)
    return mapping


def printable_key(key: Tuple[str, str]) -> str:
    url, branch = key
    return f"{url}@{branch or '(default)'}"


# --------- single-source legacy paths ---------
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


def run_bulk_atst(args) -> int:
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


# --------- PROD compare/migrate ---------
def run_prod_compare(args) -> int:
    """
    Compare PROD vs ATST by (scm_url, scm_branch).
    Migrate only PROD projects whose key is absent in ATST; apply prefix to name.
    """
    include_re: Optional[Pattern[str]] = re.compile(args.include) if args.include else None
    exclude_re: Optional[Pattern[str]] = re.compile(args.exclude) if args.exclude else None

    # Sanity
    aap_ping(args.aap_host, args.aap_token, args.verify_tls)
    assert_org_exists(args.aap_host, args.aap_token, args.organization_id, args.verify_tls)

    print("Loading ATST projects...")
    atst_map = load_projects_map(args.awx_host, args.awx_token, args.verify_tls)
    print(f"ATST projects indexed: {len(atst_map)}")

    print("Loading PROD projects...")
    prod_list: List[Dict[str, Any]] = list(paged_awx_projects(args.prod_awx_host, args.prod_awx_token, args.verify_tls))
    print(f"PROD projects discovered: {len(prod_list)}")

    # Receipt lines
    receipt: List[str] = []
    migrated = matches = filtered = failures = processed = 0

    for idx, p in enumerate(prod_list, start=1):
        name = p.get('name', f'project-{p.get("id","?")}')
        if not should_migrate(name, include_re, exclude_re):
            filtered += 1
            print(f"[{idx}] SKIP (filtered): {name}")
            continue

        processed += 1
        key = project_key(p)
        pretty = printable_key(key)
        atst_match = atst_map.get(key)

        if atst_match:
            matches += 1
            atst_name = atst_match.get('name', f'project-{atst_match.get("id","?")}')
            line = f"MATCH: ATST name='{atst_name}', PROD name='{name}', key={pretty}"
            print(f"[{idx}] {line}")
            receipt.append(line)
        else:
            # Create in AAP with prefixed name
            payload = clean_project_for_aap(p, args.organization_id)
            payload['name'] = f"{args.prod_prefix}{payload['name']}"
            try:
                if args.dry_run:
                    print(f"[{idx}] DRY-RUN (create): {payload['name']} from PROD '{name}' key={pretty}")
                    line = f"DRYRUN-CREATE: name='{payload['name']}', from PROD='{name}', key={pretty}"
                else:
                    created = create_aap_project(args.aap_host, args.aap_token, payload, args.verify_tls)
                    print(f"[{idx}] CREATED: {payload['name']} -> AAP id {created.get('id')} (from PROD '{name}')")
                    line = f"CREATED: AAP id={created.get('id')}, name='{payload['name']}', from PROD='{name}', key={pretty}"
                receipt.append(line)
                migrated += 1
            except Exception as e:
                failures += 1
                msg = f"ERROR creating from PROD '{name}' key={pretty}: {e}"
                print(f"[{idx}] {msg}", file=sys.stderr)
                receipt.append(msg)

        if args.limit and processed >= args.limit:
            print(f"Limit {args.limit} reached, stopping.")
            break

    # Write receipt
    try:
        with open(args.receipt_out, "w", encoding="utf-8") as f:
            f.write("== PROJECT MIGRATION RECEIPT ==\n")
            f.write(f"Mode: PROD compare\n")
            f.write(f"ATST: {args.awx_host}\nPROD: {args.prod_awx_host}\nAAP:  {args.aap_host}\n")
            f.write(f"OrgID: {args.organization_id}\nDryRun: {args.dry_run}\nPrefix: {args.prod_prefix}\n\n")
            for line in receipt:
                f.write(line + "\n")
            f.write("\nSummary:\n")
            f.write(f"  Matches (skipped): {matches}\n")
            f.write(f"  Migrated:          {migrated}\n")
            f.write(f"  Filtered:          {filtered}\n")
            f.write(f"  Failures:          {failures}\n")
        print(f"\nReceipt written to: {args.receipt_out}")
    except Exception as e:
        print(f"WARNING: failed to write receipt to {args.receipt_out}: {e}", file=sys.stderr)

    print("\nSummary:")
    print(f"  Matches (skipped): {matches}")
    print(f"  Migrated (or would migrate in dry-run): {migrated}")
    print(f"  Filtered by include/exclude:            {filtered}")
    print(f"  Failures:                               {failures}")
    return 0 if failures == 0 else 2


# --------- main ---------
def main() -> int:
    args = parse_args()
    args.awx_host = norm_host(args.awx_host)
    args.aap_host = norm_host(args.aap_host)
    if args.prod_mode:
        args.prod_awx_host = norm_host(args.prod_awx_host)

    if args.prod_mode:
        return run_prod_compare(args)

    if args.project_id:
        return run_single(args)
    return run_bulk_atst(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
