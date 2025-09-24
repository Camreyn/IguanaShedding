#!/usr/bin/env python3
import requests
import argparse
import sys
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

parser = argparse.ArgumentParser()
parser.add_argument('--awx-host', required=True)
parser.add_argument('--awx-token', required=True)
parser.add_argument('--aap-host', required=True)
parser.add_argument('--aap-token', required=True)
parser.add_argument('--project-id', required=True)
parser.add_argument('--organization-id', required=True, type=int)
args = parser.parse_args()

HEADERS_AWX = {
    "Authorization": f"Bearer {args.awx_token}",
    "Content-Type": "application/json"
}

HEADERS_AAP = {
    "Authorization": f"Bearer {args.aap_token}",
    "Content-Type": "application/json"
}

def get_project_from_awx():
    url = f"{args.awx_host}/api/v2/projects/{args.project_id}/"
    response = requests.get(url, headers=HEADERS_AWX, verify=False)
    if response.status_code != 200:
        sys.exit(f"Failed to fetch project {args.project_id}: {response.status_code} {response.text}")
    return response.json()

def clean_project_data(project_data):
    exclude_keys = [
        "id", "related", "summary_fields", "created", "modified",
        "last_job", "last_job_run", "default_environment"
    ]
    for key in exclude_keys:
        project_data.pop(key, None)

    # Required fields from AAP docs
    return {
        "name": project_data.get("name", "Unnamed Migrated Project"),
        "description": project_data.get("description", ""),
        "scm_type": project_data.get("scm_type") or "",
        "scm_url": project_data.get("scm_url") or "",
        "scm_branch": project_data.get("scm_branch") or "",
        "scm_clean": project_data.get("scm_clean", False),
        "scm_track_submodules": project_data.get("scm_track_submodules", False),
        "scm_delete_on_update": project_data.get("scm_delete_on_update", False),
        "scm_update_on_launch": project_data.get("scm_update_on_launch", False),
        "scm_update_cache_timeout": project_data.get("scm_update_cache_timeout", 0),
        "timeout": project_data.get("timeout", 0),
        "allow_override": project_data.get("allow_override", False),
        "organization": args.organization_id
    }

def create_project_in_aap(project_data):
    url = f"{args.aap_host}/api/controller/v2/organizations/{args.organization_id}/projects/"
    response = requests.post(url, headers=HEADERS_AAP, json=project_data, verify=False)
    if response.status_code >= 300:
        sys.exit(f"Failed to create project in AAP: {response.status_code} {response.text}")
    print(f"✅ Project created in AAP (ID: {response.json().get('id')})")

if __name__ == "__main__":
    print(f"📤 Fetching project {args.project_id} from AWX...")
    project = get_project_from_awx()
    print(f"📥 Creating project in AAP with name: {project['name']}")
    clean_data = clean_project_data(project)
    create_project_in_aap(clean_data)
