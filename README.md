# IguanaShedding

## AWX to AAP migration overview

This repository contains two migration patterns:

1. **Full AWX export/import flow** (`migrate_awx_to_aap.yml` + `files/migrate_awx_to_aap.py`) that exports an entire AWX instance, transforms user references, and imports into AAP.
2. **API-driven object migration** for Projects and Job Templates (`migrate_projects.yml`/`files/migrate_projects.py` and `migrate_job_templates.yml`/`files/migrate_job_templates.py`) with better duplicate handling and dry-run support.

## How the major playbooks work

### 1) Full export/import (`migrate_awx_to_aap.yml`)

- Selects source AWX host by `awx_env` (`test` or `prod`) via group name `awx_<env>`.
- Runs `awx-manage export` on AWX source host.
- Fetches JSON to `exports/`.
- Runs `files/migrate_awx_to_aap.py` to map usernames and optionally strip local users.
- Copies transformed JSON to AAP host.
- Import step is currently commented out (must be enabled deliberately).

Use this flow carefully for PROD because import is broad and can conflict with previously imported objects.

### 2) Projects migration (`migrate_projects.yml`)

A two-phase strategy intended for PROD-safe compare/migrate:

- **Phase A (pilotserver):** export an ATST/TEST project index artifact (`--export-awx-index`) from AWX.
- **Phase B (prod_server):** load PROD projects, compare each project by normalized key `(scm_url, scm_branch)` against ATST index, and only create missing ones in AAP with a configurable prefix (default `PROD_`).

This reduces duplicate creation when TEST and PROD share the same repo+branch.

### 3) Job template migration (`migrate_job_templates.yml`)

- Pulls AWX job templates (single `--template-id` or `--all`).
- Resolves refs in AAP (project by name; forced EE id; forced inventory id).
- Looks up AAP JT by **name + organization**:
  - If found: reuse existing JT id and continue updating/attaching survey, credentials, notifications, schedules.
  - If not found: create JT.

This behavior is idempotent for same-name JTs, but does not compare deep content drift.

## PROD migration runbook (recommended)

1. **Projects first in dry-run**
   - Run `migrate_projects.yml` with:
     - `survey_prod_awx_host` and `survey_prod_awx_token`
     - `survey_dry_run=true`
   - Review generated receipt under `artifacts/`.
2. **Projects real run**
   - Re-run with `survey_dry_run=false`.
   - Keep `survey_prod_name_prefix` set (default `PROD_`) so PROD-only creations are clearly labeled.
3. **Job templates dry-run**
   - Run `migrate_job_templates.yml` with `survey_awx_host`/token pointing to **PROD AWX**, with `survey_dry_run=true`.
   - Optionally filter with `survey_include_regex`/`survey_exclude_regex` for batches.
4. **Job templates real run**
   - Re-run with `survey_dry_run=false`.

## Duplicate behavior summary

- **Projects:** duplicate detection is based on normalized SCM URL + branch in PROD compare mode; matching entries are recorded as `MATCH` and skipped.
- **Job Templates:** duplicate detection is based on template name + org in AAP; existing templates are reused and updated.
- **Full export/import:** broad import without this duplicate logic; use only when you intend whole-instance style migration.

## Practical guidance for your current situation

Because you already migrated TEST months ago, use the API-driven playbooks for PROD now:

- Run `migrate_projects.yml` in PROD compare mode with dry-run first.
- Run `migrate_job_templates.yml` against PROD AWX with dry-run first.
- Apply include/exclude regex for phased cutover.
- Keep PROD naming prefix for newly created PROD-only projects.

This is the safest path to avoid creating duplicates while still bringing across PROD-only content.

## Move only PROD scheduled jobs

If you only want schedules from PROD AWX (and do **not** want to create/update JTs or projects), run `migrate_job_templates.yml` with:

- `survey_awx_host` / `survey_awx_token` set to PROD AWX
- `survey_with_schedules=true`
- `survey_schedules_only=true`
- optional `survey_include_regex` / `survey_exclude_regex` for phased batches

Behavior in `schedules_only` mode:

- Requires the target JT already exists in AAP (matched by `name + organization`).
- Skips JTs that do not already exist in AAP.
- Skips survey, credential, and notification changes.
- Migrates schedules only.
- Skips schedule creation when a schedule with the same name already exists on that AAP JT.
