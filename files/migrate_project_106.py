#!/usr/bin/env python3
import requests
import argparse
import sys
import urllib3

# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

parser = argparse.ArgumentParser()
parser.add_argument('--awx-host', required=True)
parser.add_argument('--awx-token', required=True)
parser.add_argument('--aap-host', required=True)
parser.add_argument('--aap-token', required=True)
parser.add_argument('--project-id', required=True)
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
    url = f"{args.awx_host}/api/controller/v2/projects/{args.project_id}/"
    response = requests.get(url, headers=HEADERS_AWX, verify=False)
    if response.status_code != 200:
        sys.exit(f"Failed to fetch project {args.project_id}: {response.status_code} {response.text}")
    return response.json()

def create_project_in_aap(project_data):
    # Remove fields AAP won't accept
    excluded = ["id", "related", "summary_fields", "created", "modified", "last_job", "last_job_run"]
    for key in excluded:
        project_data.pop(key, None)

    url = f"{args.aap_host}/api/controller/v2/projects/"
    response = requests.post(url, headers=HEADERS_AAP, json=project_data, verify=False)
    if response.status_code >= 300:
        sys.exit(f"Failed to create project in AAP: {response.status_code} {response.text}")
    print(f"Project created in AAP: {response.json().get('id')}")

if __name__ == "__main__":
    print(f"Fetching project {args.project_id} from AWX...")
    project = get_project_from_awx()
    print(f"Creating project in AAP with name: {project['name']}")
    create_project_in_aap(project)
