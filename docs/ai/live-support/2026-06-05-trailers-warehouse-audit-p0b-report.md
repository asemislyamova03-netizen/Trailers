# P0-B Warehouse Integrity Audit Report

- Audit date: 2026-06-05
- Run at: 2026-06-05 20:24
- Database: trailers.db (local SQLite, read-only)
- Branch: crm-roles-production-logistics
- Script: scripts/audit_warehouse_integrity.py
- Read-only: YES — no INSERT/UPDATE/DELETE/COMMIT performed

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| P0 — BLOCKER | 0 | Data integrity: missing warehouse causes wrong stock/production/fulfillment |
| P1 — WARN | 12 | Workflow inconsistency: may confuse status/reporting but not immediate data loss |
| P2 — CLEANUP | 0 | Historical/cancelled records — low urgency |

## CustomerOrder without warehouse_id

No CustomerOrder records without warehouse_id found.

## Trailer without warehouse_id

No Trailer records without warehouse_id found.

## ProductionRequest without target_warehouse_id

No ProductionRequest records without target_warehouse_id found.

## Production warehouse configuration

| severity | finding | issue_code | warehouse_id | warehouse_name | is_active | reason | next_step |
|---|---|---|---|---|---|---|---|
| P1 | warehouse_is_production_column_missing | warehouse_is_production_column_missing |  | (3 warehouses in DB, is_production column absent) |  | Warehouse.is_production column does not exist in current DB schema; production warehouse context unavailable | P0-C: add is_production column via separate migration plan |
| OK | warehouse_listed | ok | 1 | Алматы | True | Warehouse exists; is_production unknown | P0-C: add is_production flag |
| OK | warehouse_listed | ok | 2 | Астана | True | Warehouse exists; is_production unknown | P0-C: add is_production flag |
| OK | warehouse_listed | ok | 3 | Кокшетау | True | Warehouse exists; is_production unknown | P0-C: add is_production flag |

## SupplyNeed without warehouse_id

No SupplyNeed records without warehouse_id found.

## CustomerOrderLine source warehouse inconsistencies

No CustomerOrderLine warehouse inconsistencies found.

## Schema gaps (tables/columns missing from local DB)

| severity | entity | issue | reason | note |
|---|---|---|---|---|
| P1 | customer_order | table 'customer_order' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | customer_order_line | table 'customer_order_line' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | production_request | table 'production_request' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | production_request_line | table 'production_request_line' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | supply_need | table 'supply_need' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | reservation | table 'reservation' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | stock_movement | table 'stock_movement' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | produced_unit | table 'produced_unit' missing from local DB | Table exists in models.py but has not been migrated to this DB instance | P0-A audit scope partially blocked for this entity |
| P1 | trailer.lifecycle_status | column 'lifecycle_status' missing from table 'trailer' | Column defined in models.py but not yet in DB schema (migration pending) | Partial audit only; field not available for query |
| P1 | warehouse.is_production | column 'is_production' missing from table 'warehouse' | Column defined in models.py but not yet in DB schema (migration pending) | Partial audit only; field not available for query |
| P1 | warehouse.code | column 'code' missing from table 'warehouse' | Column defined in models.py but not yet in DB schema (migration pending) | Partial audit only; field not available for query |

> **Impact:** Checks for missing entities were SKIPPED. Counts above reflect only tables present in the local DB.
> **Root cause:** models.py defines these tables but migrations have not run against this SQLite instance.

## Totals by issue code

No issues found.

## Non-mutating recommendations

- Do not auto-assign warehouses to existing records without a reviewed repair plan.
- Do not run data-fix SQL directly from this report.
- P0-B guard implementation: add warehouse validation in order create/edit routes only.
- P0-C: introduce `default_production_warehouse_id` setting or `Warehouse.is_production` restoration via ADR.

## Forbidden actions from this report

- No INSERT/UPDATE/DELETE on any table.
- No migration execution.
- No deploy or push.
- No dependency installation.
- No schema changes without separate migration plan.

## Proposed next steps

1. Review this report manually.
2. P0-B guard plan: add warehouse validation for new orders/production requests.
3. P0-C: create separate ADR for production warehouse setting/migration.
4. Repair plan: for P0 records found — manual review first, then targeted repair plan.
