# P0-D VIN & Production Integrity Audit Report

- Audit date: 2026-06-05
- Run at: 2026-06-05 19:49 (server time UTC)
- Branch: crm-roles-production-logistics
- DB: instance/trailers.db (SQLite, read-only, production server)
- Script: scripts/audit_vin_production_integrity.py
- Read-only: YES — no INSERT/UPDATE/DELETE/COMMIT performed
- Server: ubuntu@3.67.82.83 /home/ubuntu/Trailers

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| P0 — BLOCKER | 1 | Data integrity: produced_unit status=vin_assigned with trailer_id=NULL |
| P1 — WARN    | 15 | Workflow inconsistency, suspicious VINs, historical pre-registry trailers |
| P2 — CLEANUP | 0 | — |

## A1. VIN duplicates — trailer table

**No exact duplicate VINs found.** DB UNIQUE constraint on `trailer.vin` is active and working.

Suspicious VIN values found (P1 — not duplicates, but invalid):

| severity | trailer_id | vin | status | lifecycle_status | reason |
|---|---|---|---|---|---|
| P1 | 784 | `00000000000000000000` | SOLD | customer_shipped | VIN length=20, all zeros — placeholder value |
| P1 | 959 | `MX000002` | SOLD | customer_shipped | VIN length=8 — short/test value |

Both trailers are SOLD + customer_shipped (historical). No active business risk.

## A2. VIN duplicates — vin_registry table

**No duplicate vin_full or serial7 found.** DB UNIQUE constraints present and working.

## A3. VIN cross-table mismatches

6 trailers have `trailer.vin` with no matching `vin_registry.vin_full`. All are SOLD + customer_shipped:

| severity | trailer_id | vin | status | lifecycle_status | created_at | reason |
|---|---|---|---|---|---|---|
| P1 | 229 | MX4000002R0001425 | SOLD | customer_shipped | 2025-12-08 | Pre-vin_registry flow |
| P1 | 232 | MX4000002P0001425 | SOLD | customer_shipped | 2025-12-08 | Pre-vin_registry flow |
| P1 | 250 | MX4000002R0001810 | SOLD | customer_shipped | 2025-12-08 | Pre-vin_registry flow |
| P1 | 784 | 00000000000000000000 | SOLD | customer_shipped | 2025-12-11 | Placeholder VIN |
| P1 | 828 | MX4000002S0001810 | SOLD | customer_shipped | 2026-02-17 | Pre-vin_registry flow |
| P1 | 959 | MX000002 | SOLD | customer_shipped | 2026-04-29 | Short/test VIN |

All 6 are historical SOLD records. No active workflow impact.

## B. VIN status inconsistencies

No issues found. All assigned/confirmed VIN registry rows have trailer_id properly set.

## C. Production release anomalies

### C1 — P0 BLOCKER: produced_unit id=27 — vin_assigned without trailer

| Field | Value |
|---|---|
| produced_unit.id | 27 |
| status | `vin_assigned` |
| trailer_id | **NULL** |
| order_id | NULL (stock replenishment, no order) |
| target_warehouse_id | 1 |
| item_id | 515 |
| production_request_line_id | 27 |
| created_at | 2026-05-15 11:00:04 |

**Linked production_request_line id=27:**
- production_request_id: 27
- supply_need_id: 29
- order_line_id: NULL (stock replenishment)
- item_id: 515
- status: `ready`

**Impact:** The produced_unit is stuck in `vin_assigned` with no trailer record. Any downstream logic checking `unit.trailer_id` will find NULL. The trailer either was not created, was created and then deleted, or the write was interrupted.

**Next step:** P0-F repair — check if a trailer exists with the VIN from the VIN registry row that was linked to this unit, then link it; or reset status to `produced_no_vin` and re-run VIN assignment.

### C2 — P1: Production requests completed with no produced_unit records

7 production requests show status=done/completed but have zero produced_unit rows:

| prod_req_id | request_number | status | target_warehouse_id |
|---|---|---|---|
| 4 | PR-000004 | done | (unknown) |
| 6 | PR-000006 | done | (unknown) |
| 31 | PR-000031 | done | (unknown) |
| 59 | PR-000059 | done | (unknown) |
| 61 | PR-000061 | done | (unknown) |
| 62 | PR-000062 | done | (unknown) |
| 64 | PR-000064 | done | (unknown) |

**Likely cause:** These requests were closed through an older production flow that predates the `produced_unit` tracking table. Historical P1 — not blocking current operations.

## D. Reservation/order anomalies

No issues found.

## E. Warehouse anomalies

No issues found. All trailers have warehouse_id set.

## F. Status mismatches

No issues found.

## G. DB constraint inspection

| Table | Column | Constraint | Status |
|---|---|---|---|
| `trailer` | `vin` | UNIQUE index | **PRESENT** |
| `vin_registry` | `vin_full` | UNIQUE index | **PRESENT** |
| `vin_registry` | `serial7` | UNIQUE index | **PRESENT** |

DB-level uniqueness constraints are in place and working. No DB-level duplicates possible through normal inserts.

## Totals by issue code

| issue_code | severity | count |
|---|---|---|
| `produced_unit_vin_assigned_no_trailer` | P0 | 1 |
| `production_request_completed_no_units` | P1 | 7 |
| `trailer_vin_not_in_registry` | P1 | 6 |
| `trailer_vin_suspicious_length` | P1 | 2 |

## Skipped checks

None — all expected tables present in production DB.

## Forbidden actions from this report

- No UPDATE/DELETE/INSERT on any table.
- No migration execution.
- No deploy or push until P0-F repair plan is reviewed and approved.
- No schema changes without separate migration ADR.

## Next steps

1. P0-F: data repair plan for `produced_unit id=27` — link correct trailer or reset status.
2. P0-G: app-level guard plan — add VIN uniqueness check to trailer edit route.
3. P1 review: confirm historical trailers (229, 232, 250, 784, 828, 959) are truly closed and no further action needed.
4. P1 review: confirm old production requests (PR-000004 etc.) are historical and no produced unit records are expected.
