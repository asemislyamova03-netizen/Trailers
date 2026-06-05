# P0-FB Dry-Run Repair Report: produced_unit id=27

- Date: 2026-06-05
- Run at: 2026-06-05 20:02
- Branch: crm-roles-production-logistics
- DB: instance/trailers.db (SQLite, read-only)
- Mode: DRY-RUN — no changes applied
- Script: scripts/repair_produced_unit_27.py

## Target repair

| Field | Before | After |
|---|---|---|
| `produced_unit.id` | 27 | 27 (unchanged) |
| `produced_unit.trailer_id` | NULL | **1001** |
| `produced_unit.status` | vin_assigned | vin_assigned (unchanged) |

## Pre-checks R1–R14

| Check | Description | Expected | Actual | Status |
|---|---|---|---|---|
| R1 | produced_unit id=27 exists | `exists` | `exists` | **PASS** |
| R2 | produced_unit.status = 'vin_assigned' | `vin_assigned` | `vin_assigned` | **PASS** |
| R3 | produced_unit.trailer_id IS NULL | `NULL` | `None` | **PASS** |
| R4 | produced_unit.item_id = 515 | `515` | `515` | **PASS** |
| R5 | produced_unit.production_request_line_id = 27 | `27` | `27` | **PASS** |
| R6 | trailer id=1001 exists | `exists` | `exists` | **PASS** |
| R7 | trailer.vin = 'MX4000004T0002670' | `MX4000004T0002670` | `MX4000004T0002670` | **PASS** |
| R8 | trailer.item_id = 515 | `515` | `515` | **PASS** |
| R9 | trailer.warehouse_id NOT NULL | `not NULL` | `1` | **PASS** |
| R10 | vin_registry id=987 exists | `exists` | `exists` | **PASS** |
| R11 | vin_registry.supply_need_id = 29 | `29` | `29` | **PASS** |
| R12 | vin_registry.trailer_id = 1001 | `1001` | `1001` | **PASS** |
| R13 | supply_need id=29 exists and not cancelled | `exists, not cancelled` | `exists, status=READY, cancelled_at=None` | **PASS** |
| R14 | no other produced_unit links trailer_id=1001 (excluding id=27) | `0 rows` | `0 row(s)` | **PASS** |

## Proposed repair SQL

> **NOT EXECUTED** — dry-run only

```sql
-- P0-FC apply (not yet approved):
BEGIN;
UPDATE produced_unit
   SET trailer_id = 1001
 WHERE id = 27
   AND trailer_id IS NULL
   AND status = 'vin_assigned';
-- Expected: 1 row affected
COMMIT;
```

## Dry-run verdict

**READY_FOR_APPLY**

## Verification plan (after P0-FC apply)

1. Re-run `scripts/audit_vin_production_integrity.py` — expect P0 count = 0.
2. Confirm `produced_unit id=27` → `trailer_id=1001`.
3. Confirm no duplicate VIN in `trailer` table.
4. Confirm `trailers.service` still active.

## Forbidden actions

- No UPDATE/DELETE/INSERT without `--apply` flag (P0-FC).
- `--apply` not implemented in P0-FB.
- No migration, no deploy, no push, no service restart.

## Next step

**P0-FC:** After reviewing this report and confirming all checks PASS,
request explicit approval to implement `--apply` in the repair script
and execute the controlled repair on production.
