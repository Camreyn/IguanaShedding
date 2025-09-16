# IguanaShedding

---

```markdown
# AWX to AAP Migration via AWX Job Template

This repository contains an automated workflow to migrate AWX resources (Projects, Inventories, Credentials, Job Templates, Schedules, etc.) from AWX environments (test or prod) into a centralized Red Hat Ansible Automation Platform (AAP) instance.

It is designed to be run **from within AWX itself**, using a Job Template and dynamic survey input.

---

## Project Structure

```

migrate\_awx\_to\_aap/
├── inventory/
│   └── hosts                          # Inventory file with AWX and AAP hosts
├── files/
│   ├── migrate\_awx\_to\_aap.py          # Python script to convert AWX export for LDAP/AAP
│   ├── user\_mapping\_test.json         # Mapping of local users → LDAP users (test)
│   └── user\_mapping\_prod.json         # Mapping of local users → LDAP users (prod)
├── playbooks/
│   └── migrate\_awx\_to\_aap.yml         # Main playbook: export, transform, and import
├── exports/                           # Exported raw AWX files (runtime)
├── imports/                           # Transformed AAP-ready imports (runtime)
└── README.md                          # This file

````

---

## Requirements

- AWX nodes must have `awx-manage` available in `$PATH` (for export).
- AAP node must allow SSH access and support `awx-manage import`.
- SSH credentials must allow sudo execution of `awx-manage`.
- Python 3 installed on the control node for local transformation.
- LDAP usernames must be known in advance and defined in the appropriate mapping file.
- All hosts must be reachable via SSH from the AWX task container or execution node.

---

## Setup in AWX

### 1. Inventory Configuration

Define an inventory in AWX with the following groups and hosts:

| Group      | Host Example                  |
|------------|-------------------------------|
| `awx_test` | `awx-test.internal.local`      |
| `awx_prod` | `awx-prod.internal.local`      |
| `aap`      | `aap-main.internal.local`      |

Ensure the hostnames resolve and allow SSH access.

---

### 2. Credentials

Create one or more **Machine** credentials in AWX that:

- Provide SSH access to all AWX and AAP hosts
- Allow `sudo` for `awx-manage` commands

---

### 3. Project

Create a Git-backed **Project** in AWX pointing to this repository.

---

### 4. Job Template

Create a Job Template with the following:

- **Playbook**: `playbooks/migrate_awx_to_aap.yml`
- **Inventory**: The one created above
- **Credentials**: SSH credential(s)
- **Survey**: Enable a required variable named `environment`

#### Survey Variable Example

| Variable     | Type           | Choices        | Default |
|--------------|----------------|----------------|---------|
| `environment`| Multiple Choice| `test`, `prod` | `test`  |

---

## How It Works

1. **Exports** the AWX configuration using `awx-manage export` from the target AWX node.
2. **Fetches** the export JSON to the control node (or AWX runner).
3. **Transforms** the export using `migrate_awx_to_aap.py`, mapping users to their LDAP equivalents and removing local users.
4. **Transfers** the cleaned JSON to the AAP host.
5. **Imports** the transformed configuration into AAP using `awx-manage import`.

---

## Safe Testing (Dry Run)

To test without modifying AAP:

1. Edit `playbooks/migrate_awx_to_aap.yml`
2. Comment out the final `awx-manage import` task:

```yaml
# - name: Import transformed data into AAP
#   ansible.builtin.shell: |
#     awx-manage import < /tmp/{{ import_filename }}
#   delegate_to: "{{ aap_host }}"
````

You can then manually inspect `imports/aap_import_<env>.json` before applying.

---

## Notes

* Vault credentials, tokens, and sensitive data must be re-entered after import.
* Role and ownership mappings are preserved if user mappings are correct.
* Ensure that user mapping JSON files are kept updated with correct LDAP usernames.

---

## Example Usage in AWX

Run the Job Template and select `test` or `prod` from the `environment` dropdown. The playbook will automatically use the corresponding AWX server and user mapping.

---

## License

MIT

```
