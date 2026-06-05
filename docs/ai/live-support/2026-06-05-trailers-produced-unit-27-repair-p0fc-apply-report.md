# P0-FC Apply Report: produced_unit id=27 Repair

- Date: 2026-06-05
- Run at: 2026-06-05 20:11:26 (UTC)
- Branch: crm-roles-production-logistics
- DB: instance/trailers.db (SQLite, production server)
- Script: scripts/repair_produced_unit_27.py --apply --i-understand-production-data-change
- Plan: docs/ai/live-support/2026-06-05-trailers-produced-unit-27-repair-p0f-plan.md
- Dry-run: docs/ai/live-support/2026-06-05-trailers-produced-unit-27-repair-p0fb-dry-run-report.md

## Backup

| Field | Value |
|---|---|
| Backup file | `backups/trailers_before_produced_unit_27_repair_20260605_201126.db` |
| Backup size | 4,685,824 bytes |
| Backup status | **OK** |

## Pre-checks R1–R14 (before apply)

All 14 checks passed before data change was executed.

| Check | Description | Expected | Actual | Status |
|---|---|---|---|---|
| R1 | produced_unit id=27 exists | exists | exists | **PASS** |
| R2 | produced_unit.status = 'vin_assigned' | vin_assigned | vin_assigned | **PASS** |
| R3 | produced_unit.trailer_id IS NULL | NULL | None | **PASS** |
| R4 | produced_unit.item_id = 515 | 515 | 515 | **PASS** |
| R5 | produced_unit.production_request_line_id = 27 | 27 | 27 | **PASS** |
| R6 | trailer id=1001 exists | exists | exists | **PASS** |
| R7 | trailer.vin = 'MX4000004T0002670' | MX4000004T0002670 | MX4000004T0002670 | **PASS** |
| R8 | trailer.item_id = 515 | 515 | 515 | **PASS** |
| R9 | trailer.warehouse_id NOT NULL | not NULL | 1 | **PASS** |
| R10 | vin_registry id=987 exists | exists | exists | **PASS** |
| R11 | vin_registry.supply_need_id = 29 | 29 | 29 | **PASS** |
| R12 | vin_registry.trailer_id = 1001 | 1001 | 1001 | **PASS** |
| R13 | supply_need id=29 exists and not cancelled | exists, not cancelled | exists, status=READY | **PASS** |
| R14 | no other produced_unit links trailer_id=1001 | 0 rows | 0 row(s) | **PASS** |

## SQL executed

```sql
BEGIN IMMEDIATE;
-- PRAGMA foreign_keys = ON (set before transaction)
UPDATE produced_unit
   SET trailer_id = 1001
 WHERE id = 27
   AND trailer_id IS NULL
   AND status = 'vin_assigned';
-- rowcount verified = 1 before COMMIT
-- post-update: produced_unit.trailer_id=1001 verified ✓
COMMIT;
```

## Transaction result

| Field | Value |
|---|---|
| Rowcount | **1** (expected: 1) |
| Post-update in-transaction verify | produced_unit.trailer_id=1001 ✓ |
| Commit | **COMMITTED** |
| Post-commit read-only verify | produced_unit id=27, trailer_id=1001, status=vin_assigned ✓ |
| Result | **REPAIR APPLIED SUCCESSFULLY** |

## Post-repair audit (scripts/audit_vin_production_integrity.py)

Rerun immediately after repair:

| Metric | Before repair | After repair |
|---|---|---|
| P0 BLOCKERS | **1** | **0** |
| P1 WARNINGS | 15 | 15 (unchanged) |
| P2 CLEANUP | 0 | 0 |
| Duplicate VIN count | 0 | 0 |
| `produced_unit_vin_assigned_no_trailer` | 1 | **0** |

## P0 blocker resolved

**YES.** `produced_unit id=27` now has `trailer_id=1001`.
The single P0 data integrity blocker from the P0-D audit is **resolved**.

## Duplicate VIN after repair

**0** — no duplicate VINs introduced or existed.
DB UNIQUE constraints on `trailer.vin`, `vin_registry.vin_full`, `vin_registry.serial7` — all intact.

## Operational status

| Item | Status |
|---|---|
| Service restarted | **NO** — data-only repair, no restart needed |
| Migrations run | **NO** |
| Application code changed | **NO** |
| Templates changed | **NO** |
| Nginx/systemd changed | **NO** |
| Dependencies installed | **NO** |
| Pushed to remote | **NO** |

## Rollback path (if needed)

```bash
# On production server — only if unexpected issues arise:
sudo systemctl stop trailers.service
cp backups/trailers_before_produced_unit_27_repair_20260605_201126.db instance/trailers.db
sudo systemctl start trailers.service
```

Backup is retained in `backups/` directory. Do not delete for at least 30 days.

## Next safe steps

1. **P0-G guard plan:** Add VIN uniqueness check + format validation to the trailer edit route (`views.py ~line 2014`) to prevent future invalid VINs from being set without registry validation. This is a code change and requires separate approval.
2. **P1 review (optional):** 15 P1 warnings remain — all historical (pre-registry trailers, old production requests without produced_unit records). None are blocking current operations.
3. **Monitoring:** No further action required for produced_unit id=27.
