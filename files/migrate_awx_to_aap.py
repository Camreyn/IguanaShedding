#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path


def map_username(username, user_map, seen_users):
    """Map an AWX username to a target LDAP username using a provided map."""
    seen_users.add(username)
    return user_map.get(username, username)


def transform_export(input_path, output_path, user_mapping_path, strip_users):
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    with open(user_mapping_path, "r", encoding="utf-8") as f:
        user_map = json.load(f)

    seen_users = set()

    # Remove local users if requested
    if strip_users and "users" in data:
        print("🧹 Stripping local users...")
        data["users"] = []

    # Transform roles
    for role in data.get("roles", []):
        if "user" in role:
            role["user"] = map_username(role["user"], user_map, seen_users)

    # Transform memberships
    for membership in data.get("memberships", []):
        if "user" in membership:
            membership["user"] = map_username(membership["user"], user_map, seen_users)

    # Transform teams
    for team in data.get("teams", []):
        if "members" in team:
            team["members"] = [map_username(u, user_map, seen_users) for u in team["members"]]

    # Transform organizations
    for org in data.get("organizations", []):
        if "created_by" in org:
            org["created_by"] = map_username(org["created_by"], user_map, seen_users)
        if "modified_by" in org:
            org["modified_by"] = map_username(org["modified_by"], user_map, seen_users)

    # Transform job templates
    for jt in data.get("job_templates", []):
        if "created_by" in jt:
            jt["created_by"] = map_username(jt["created_by"], user_map, seen_users)
        if "modified_by" in jt:
            jt["modified_by"] = map_username(jt["modified_by"], user_map, seen_users)

    # Optional: warn about unmapped users
    unmapped = {u for u in seen_users if u not in user_map}
    if unmapped:
        print(f"⚠️ Warning: {len(unmapped)} user(s) not in mapping and were left unchanged:")
        for u in sorted(unmapped):
            print(f"   - {u}")

    # Write the output
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Output written to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert AWX export JSON to be compatible with LDAP-based Automation Platform"
    )
    parser.add_argument("--input", required=True, help="Path to AWX export JSON")
    parser.add_argument("--output", required=True, help="Path to write the transformed JSON")
    parser.add_argument("--mapping", required=True, help="Path to user mapping JSON")
    parser.add_argument("--strip-local-users", action="store_true", help="Remove local users section")
    args = parser.parse_args()

    for path in (args.input, args.mapping):
        if not Path(path).is_file():
            print(f"❌ File not found: {path}")
            sys.exit(1)

    transform_export(args.input, args.output, args.mapping, args.strip_local_users)


if __name__ == "__main__":
    main()
