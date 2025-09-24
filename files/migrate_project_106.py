#!/usr/bin/env python3
# migrate_project_106.py
"""
Migrates a single project from AWX to AAP (Controller).
- Reads project from AWX /api/v2/projects/{id}/
- Creates it on AAP at /api/controller/v2/projects/ with organization in body
Includes defensive checks and clear error messages.

Usage:
  python3 migrate_project_106.py \
    --awx-host https://awx.example.com --awx-token XXXXX \
    --aap-host https://aap.example.com --aap-token YYYYY \
    --project-id 106 --organization-id 5
"""
import argparse
import sys
import textwrap
from typing import Dict, Any
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 30  # seconds


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Migrate a project from AWX to AAP (Controller)",
        epilog=textwrap.dedent("""
            Notes:
              • AAP uses /api/controller/v2 while older AWX/Tower used /api/v2.
              • Create projects by POSTing to /api/controller/v2/projects/ and include {"organization": <id>} in the JSON body.
        """),
    )
    p.add_argument('--awx-host', required=True, help='e.g., https://awx.example.com')
    p.add_argument('--awx-token', required=True)
    p.add_argument('--aap-host', required=True, help='e.g., https://aap.example.com')
    p.add_argument('--aap-token', required=True)
    p.add_argument('--project-id', required=True, type=int)
    p.add_argument('--organization-id', required=True, type=int)
    p.add_argument('--verify-tls', action='store_true', help='Enable TLS verification (default: disabled)')
    return p.parse_args()


def norm_host(host: str) -> str:
    """Normalize base host: strip trailing slashes."""
    return host.rstrip('/')


def headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def http_get_json(url: str, hdrs: Dict[str, str], verify: bool) -> Dict[str, Any]:
    r = requests.get(url, headers=hdrs, verify=verify, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text}")
    return r.json()


def http_post_json(url: str, hdrs: Dict[str, str], payload: Dict[str, Any], verify: bool) -> Dict[str, Any]:
    r = requests.post(url, headers=hdrs, json=payload, verify=verify, timeout=TIMEOUT)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"POST {url} -> {r.status_code}:\n{r.text}")
    return r.json()


def get_awx_project(awx_host: str, awx_token: str, project_id: int, verify: bool) -> Dict[str, Any]:
    url = f"{awx_host}/api/v2/projects/{project_id}/"
    return http_get_json(url, headers(awx_token), verify)


def clean_project_for_aap(src: Dict[str, Any], organization_id: int) -> Dict[str, Any]:
    """
    Prepare a minimal, valid payload for AAP Controller project creation.
    """
    # Drop read-only or AWX-only fields (defensive: pop if present)
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
        # Important: AAP expects organization in the body when POSTing to /projects/
        "organization": organization_id,
    }

    # If SCM URL is provided, ensure scm_type is set (Controller may reject empty type with URL)
    if payload["scm_url"] and not payload["scm_type"]:
        payload["scm_type"] = "git"

    return payload


def assert_aap_controller_reachable(aap_host: str, aap_token: str, verify: bool) -> None:
    """
    Sanity check that /api/controller/v2 is reachable and we’re authenticated.
    """
    # A simple capability endpoint to prove the base exists & token works.
    url = f"{aap_host}/api/controller/v2/ping/"
    r = requests.get(url, headers=headers(aap_token), verify=verify, timeout=TIMEOUT)
    if r.status_code == 401:
        raise RuntimeError("AAP token unauthorized (401). Check token scope/expiration.")
    if r.status_code == 403:
        raise RuntimeError("AAP token forbidden (403). Check RBAC/organization permissions.")
    if r.status_code != 200:
        raise RuntimeError(f"AAP controller not reachable at {url} -> {r.status_code}: {r.text}")


def assert_org_exists(aap_host: str, aap_token: str, org_id: int, verify: bool) -> None:
    url = f"{aap_host}/api/controller/v2/organizations/{org_id}/"
    r = requests.get(url, headers=headers(aap_token), verify=verify, timeout=TIMEOUT)
    if r.status_code == 404:
        raise RuntimeError(f"Organization id {org_id} not found on AAP.")
    if r.status_code not in (200,):
        raise RuntimeError(f"Failed to validate organization {org_id} -> {r.status_code}: {r.text}")


def create_project_on_aap(aap_host: str, aap_token: str, payload: Dict[str, Any], verify: bool) -> Dict[str, Any]:
    """
    Correct creation endpoint for AAP Controller:
      POST {aap_host}/api/controller/v2/projects/
      body includes: {"organization": <id>, ...}
    """
    url = f"{aap_host}/api/controller/v2/projects/"
    return http_post_json(url, headers(aap_token), payload, verify)


def main() -> int:
    args = parse_args()
    awx_host = norm_host(args.awx_host)
    aap_host = norm_host(args.aap_host)
    verify = args.verify_tls

    print(f"Fetching project {args.project_id} from AWX ...")
    awx_proj = get_awx_project(awx_host, args.awx_token, args.project_id, verify)
    name = awx_proj.get('name', f"project-{args.project_id}")
    print(f"Source project: {name}")

    print("Validating AAP Controller reachability and permissions ...")
    assert_aap_controller_reachable(aap_host, args.aap_token, verify)
    assert_org_exists(aap_host, args.aap_token, args.organization_id, verify)

    print(f"Creating project on AAP Controller at /api/controller/v2/projects/ in org {args.organization_id} ...")
    payload = clean_project_for_aap(awx_proj, args.organization_id)
    created = create_project_on_aap(aap_host, args.aap_token, payload, verify)
    print(f"Created project id {created.get('id')} on AAP (name={created.get('name')})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
