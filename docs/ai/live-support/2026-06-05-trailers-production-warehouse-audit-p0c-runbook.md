# P0-C Runbook: Production read-only warehouse audit

Date: 2026-06-05
Repository: Trailers
Status: documentation-only runbook
Related work:

- `27e3127 Add Trailers warehouse integrity audit`
- `docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0a-plan.md`
- `docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0b-report.md`
- `scripts/audit_warehouse_integrity.py`

## 1. Purpose

This runbook defines how to perform a safe production read-only warehouse integrity audit for Trailers.

Goal:

- verify production or a sanitized production dump for missing warehouse/location context;
- detect P0 records before warehouse guard implementation;
- avoid production data writes, migrations, deploys, and server configuration changes;
- produce a reviewed audit report that can drive the later P0-D implementation plan for warehouse guards.

This runbook does not authorize fixes. It only authorizes planning a safe read-only audit procedure.

## 2. Why local SQLite is insufficient

The P0-B local audit was completed in commit `27e3127`, but the local SQLite database is not representative.

Known local SQLite limitations:

- `customer_order` is missing locally;
- `customer_order_line` is missing locally;
- `production_request` is missing locally;
- `production_request_line` is missing locally;
- `supply_need` is missing locally;
- `reservation` is missing locally;
- `stock_movement` is missing locally;
- `produced_unit` is missing locally;
- `Trailer.warehouse_id` exists locally and all 784 local trailers have `warehouse_id`;
- `Warehouse.is_production` is defined in `models.py` but missing in local DB;
- `Trailer.lifecycle_status` is defined in `models.py` but missing in local DB.

Impact:

- local P0 counts for orders, production requests, supply needs, reservations, movements, produced units, and order lines are incomplete;
- local "no findings" cannot be treated as production-safe;
- production or a sanitized production dump must be audited before guards or repair work are planned.

## 3. Preconditions for safe production audit

Before running any audit command, all conditions below must be true.

| Requirement | Required state |
|---|---|
| User approval | Explicit approval for production read-only audit or sanitized dump audit. |
| Scope | Audit only; no data repair, no schema change, no deploy. |
| Target | Confirmed production DB path or sanitized production dump path. |
| Access mode | Read-only DB access only. |
| Backup/snapshot | Existing production backup or snapshot confirmed by operator before audit. |
| Code version | Branch/commit recorded in report. |
| Script support | Audit script confirmed safe for the target DB path and schema. |
| Output path | Report path chosen before running. |
| Secrets | No secrets copied into docs or report. |
| Personal data | Report redaction rules confirmed. |

If any precondition is not true, stop.

## 4. Required DB access mode: read-only

The audit must use read-only access.

Allowed access patterns:

- SQLite URI with `mode=ro` against a production DB file or sanitized dump;
- filesystem-level read-only copy of production DB;
- sanitized dump restored to a local read-only audit path;
- DB user/account with SELECT-only permissions if production uses a non-SQLite backend in the future.

Not acceptable:

- opening production DB with write permissions when a read-only mode is available;
- running the audit as part of the live Flask app request process;
- using Flask route handlers to gather audit data;
- using a DB shell session that can execute writes without explicit read-only controls;
- running anything that can create, update, delete, migrate, or vacuum production data.

## 5. Exact environment safety checks before running anything

Run these checks before any audit execution. These checks are informational and must not mutate files or the database.

### Git and code safety

Record branch and commit:

```bash
git -c core.quotepath=false branch --show-current
git -c core.quotepath=false rev-parse --short HEAD
git -c core.quotepath=false status --short
```

Required interpretation:

- branch must be the intended audit branch;
- working tree may contain unrelated local docs, but no application-code change should be part of the audit run;
- do not stage or commit unrelated files as part of the audit.

### Script safety

Inspect the audit script before production use:

```bash
git -c core.quotepath=false show --name-status --oneline HEAD -- scripts/audit_warehouse_integrity.py
rg -n "commit\\(|flush\\(|delete\\(|INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|migrate|upgrade|server_update|systemctl|nginx|pip install" scripts/audit_warehouse_integrity.py
```

Required interpretation:

- no `commit()`, `flush()`, `delete()`, write SQL, migrations, deploy, systemd/nginx, or dependency install commands may be present in the audit path;
- if any write-capable operation is present, stop and create a script update plan first.

### DB file safety for SQLite targets

For a SQLite DB file or sanitized dump, record file identity and permissions:

```bash
pwd
ls -lh /path/to/audit-target.db
```

Optional checksum for audit traceability:

```bash
sha256sum /path/to/audit-target.db
```

Required interpretation:

- path must point to the approved production DB copy or sanitized dump;
- do not run `chmod`, `chown`, copy, rsync, restore, or backup commands inside this audit step unless separately approved;
- if file identity is unclear, stop.

### Runtime safety

Before running Python:

```bash
python --version
```

Required interpretation:

- use the existing project runtime only;
- do not run `pip install`;
- do not create or modify virtual environments as part of this audit.

## 6. How to verify the DB target is production or a sanitized dump

The operator must explicitly label the audit target before running anything.

Required metadata to record in the report:

```text
DB target type: production-read-only / sanitized-production-dump
DB path or connection alias:
Host label, if applicable:
Snapshot/dump timestamp:
Sanitization owner, if applicable:
Sanitization confirmation: yes/no
Operator:
Audit branch:
Audit commit:
```

For production DB:

- verify the path/alias with the deployment owner;
- verify the database is opened read-only;
- verify the audit will not run through a writable Flask app session;
- do not expose full server paths containing secrets in public docs if they reveal sensitive infrastructure.

For sanitized dump:

- verify it was created from production after the relevant warehouse-setting removal;
- verify customer personal data was removed or masked;
- verify order numbers, internal IDs, warehouse IDs, VIN, and status fields are preserved enough for integrity checks.

If the target cannot be positively identified, stop.

## 7. How to run `scripts/audit_warehouse_integrity.py` safely if it supports the target DB

Current script state from P0-B:

- `scripts/audit_warehouse_integrity.py` opens SQLite using URI read-only mode when possible;
- it currently points to `ROOT / "instance" / "trailers.db"`;
- it writes reports to `.ai_local/reports/` and `docs/ai/live-support/`;
- it does not currently expose a documented CLI argument for an arbitrary production DB path.

Safe use is allowed only if one of these is true:

1. the approved audit target is a sanitized dump placed at the exact path the script expects, with read-only file permissions; or
2. the script has already been updated and reviewed under a separate plan to accept an explicit read-only target DB path; or
3. the operator runs it in an isolated copy of the repository where `instance/trailers.db` is a read-only sanitized production dump, not the live writable production database.

Safe run pattern for a supported local/sanitized SQLite target:

```bash
python scripts/audit_warehouse_integrity.py
```

Before running, confirm:

- `instance/trailers.db` is the approved read-only target or sanitized dump;
- script output path is acceptable;
- report will not include customer names, phone numbers, email addresses, tokens, secrets, payment details, contract contents, or full server paths;
- no production service is stopped or restarted;
- no migration command is run.

After running:

- review console output for P0/P1/P2 totals;
- review report file before sharing;
- redact any unexpected sensitive fields;
- do not immediately fix data based on the report.

## 8. Stop condition if the script does not support production DB safely

If `scripts/audit_warehouse_integrity.py` cannot safely target production or a sanitized production dump in read-only mode, stop.

Do not patch and run it ad hoc.

Create a separate script update plan first.

The script update plan must include:

- exact target mode, such as `--db-path` for SQLite read-only URI;
- read-only enforcement strategy;
- output path strategy;
- redaction rules;
- dry-run/no-write guarantee;
- verification steps;
- approval before script changes;
- no production execution until the updated script is reviewed.

## 9. Data that must be redacted

The production audit report must not include:

- customer full names;
- phone numbers;
- email addresses;
- personal identifiers;
- addresses;
- payment transaction references;
- payment links;
- contract text or files;
- SigeX/Kaspi/webhook tokens;
- `.env` values;
- server credentials;
- full secret-bearing server paths;
- raw comments that may contain personal or commercial sensitive data.

Allowed in the report:

- internal record IDs;
- order numbers;
- VIN or masked VIN, depending on audience;
- warehouse IDs/names if not sensitive;
- model/table names;
- status values;
- issue codes;
- counts by severity;
- created dates if needed for triage;
- branch/commit;
- sanitized DB target label.

If uncertain, redact.

## 10. Expected report outputs

Expected report files:

```text
.ai_local/reports/trailers-warehouse-integrity-p0b-report.md
docs/ai/live-support/2026-06-05-trailers-warehouse-audit-production-report.md
```

The production-safe report should include:

```text
# Production Warehouse Integrity Audit Report

Audit date:
DB target type:
DB target label:
Snapshot/dump timestamp:
Branch:
Commit:
Read-only confirmation:
Redaction confirmation:

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| P0 | 0 | Data integrity blockers |
| P1 | 0 | Workflow inconsistencies |
| P2 | 0 | Cleanup |

## Schema compatibility

## CustomerOrder without warehouse_id

## Trailer without warehouse_id

## ProductionRequest without target_warehouse_id

## Production warehouse configuration

## SupplyNeed without warehouse_id

## CustomerOrderLine source warehouse inconsistencies

## Totals by issue code

## Redactions applied

## Decision

## Recommended next step
```

The report must clearly state when any check was skipped because of schema mismatch.

## 11. Forbidden commands

The following commands/classes of commands are forbidden during this audit:

### Migrations

```bash
flask db migrate
flask db upgrade
flask db downgrade
alembic upgrade
alembic downgrade
```

### Write SQL

```sql
UPDATE ...
DELETE ...
INSERT ...
ALTER TABLE ...
DROP ...
CREATE ...
REINDEX ...
VACUUM ...
```

### Deploy and server update

```bash
scripts/server_update.sh
bash scripts/server_update.sh
./scripts/server_update.sh
git pull
git push
```

### systemd / Nginx / service changes

```bash
systemctl restart ...
systemctl stop ...
systemctl start ...
systemctl reload ...
service nginx ...
nginx -s reload
```

### Dependency installation

```bash
pip install ...
python -m pip install ...
npm install
```

### Destructive git commands

```bash
git reset --hard
git clean -fd
git clean -fdx
git checkout -- .
```

Also forbidden:

- editing application code;
- editing scripts during the runbook execution;
- editing templates;
- changing `.env` or secrets;
- changing Nginx/systemd;
- deleting files;
- pushing.

## 12. Decision table

| Outcome | Meaning | Decision | Next action |
|---|---|---|---|
| No P0 findings | No production data integrity blockers detected by supported checks. | Proceed to planning guards. | Create P0-D implementation plan for warehouse guards after report review. |
| P0 records found | Active records lack required warehouse/location context or have inconsistent source/target warehouses. | Stop implementation. | Review report, create targeted repair plan first; no automatic data fixes. |
| Schema mismatch found | Production/dump schema does not match expected current models or audit script assumptions. | Stop implementation. | Create schema compatibility/script update plan; do not run migrations from audit. |
| Audit cannot run safely | Read-only target, script support, redaction, or approval is not confirmed. | Stop. | Create a script update plan or request explicit safe audit approval. |

## 13. Next step after audit

After the production or sanitized-dump report is reviewed:

```text
P0-D implementation plan for warehouse guards
```

P0-D may be planned only after:

- production/sanitized audit report is reviewed;
- P0 records, if any, are triaged;
- repair requirements are separated from new-record guards;
- no data migration or production warehouse setting change is bundled into guard implementation;
- user approval is received.

P0-D guard planning may cover:

- preventing new `CustomerOrder` records without `CustomerOrder.warehouse_id`;
- preventing new stock `Trailer` records without `Trailer.warehouse_id`;
- preventing new `ProductionRequest` records without `ProductionRequest.target_warehouse_id`;
- preventing new active `SupplyNeed` records without `SupplyNeed.warehouse_id`;
- validating per-source `CustomerOrderLine` warehouse consistency.

P0-D must not include:

- production data repair;
- migrations;
- production warehouse setting restoration;
- deploy;
- role changes;
- unrelated UI refactors.

## Acceptance criteria

- Runbook exists at `docs/ai/live-support/2026-06-05-trailers-production-warehouse-audit-p0c-runbook.md`.
- Runbook is documentation-only.
- No application code was edited.
- No scripts were edited.
- No production commands were run.
- No migrations, deploy, dependency install, Nginx/systemd change, push, or file deletion occurred.
- Runbook explains why local SQLite is insufficient.
- Runbook requires read-only DB access.
- Runbook defines environment safety checks before running anything.
- Runbook explains how to verify production or sanitized dump target.
- Runbook states how to run `scripts/audit_warehouse_integrity.py` only if it safely supports the target DB.
- Runbook says to stop and create a script update plan if safe target support is missing.
- Runbook defines redaction rules.
- Runbook defines expected report outputs.
- Runbook lists forbidden commands.
- Runbook includes the required decision table.
- Runbook states the next step: P0-D implementation plan for warehouse guards only after report review.
