# P0-F Repair Plan: Trailers produced_unit id=27

- Date: 2026-06-05
- Branch: crm-roles-production-logistics
- Linked audit: docs/ai/live-support/2026-06-05-trailers-vin-production-integrity-p0d-report.md
- Root cause: docs/ai/live-support/2026-06-05-trailers-vin-production-root-cause-p0e.md
- Status: PLAN ONLY — no data changes performed

---

## 1. Problem summary

`produced_unit id=27` has `status='vin_assigned'` but `trailer_id=NULL`.
This is the only P0 data integrity blocker found in the P0-D audit.

The produced_unit was created on 2026-05-15 as part of a stock replenishment production
request (PR-000027). The VIN assignment step completed partially: the VIN registry row was
updated and the produced_unit status was set to `vin_assigned`, but the Trailer record was
not created in the same transaction. As a result `produced_unit.trailer_id` was never set.

Subsequently, the same trailer was physically produced and entered the system via a
**separate customer order path** on 2026-05-19: Trailer id=1001 with the correct VIN
(`MX4000004T0002670`) was created, sold (ORD-000045), and physically shipped to the customer.
Documents were issued.

The `produced_unit` record is a dangling link — it represents a real production event, but
its trailer reference is missing. Linking it to trailer id=1001 closes the loop correctly.

---

## 2. Exact affected record

### produced_unit id=27

| Field | Current value | Expected after repair |
|---|---|---|
| id | 27 | 27 (unchanged) |
| status | `vin_assigned` | `vin_assigned` (unchanged) |
| trailer_id | **NULL** | **1001** |
| order_id | NULL | NULL (stock replenishment, no order) |
| order_line_id | NULL | NULL |
| item_id | 515 | 515 (unchanged) |
| target_warehouse_id | 1 | 1 (unchanged) |
| production_request_line_id | 27 | 27 (unchanged) |
| created_at | 2026-05-15 11:00:04 | unchanged |

### Related supply_need id=29

| Field | Value |
|---|---|
| id | 29 |
| need_type | STOCK_REPLENISHMENT |
| status | READY |
| item_id | 515 |
| warehouse_id | 1 (Алматы) |
| vin_modification_code | 000004 |
| cancelled_at | NULL (not cancelled) |

### Related vin_registry id=987

| Field | Value |
|---|---|
| id | 987 |
| vin_full | MX4000004T0002670 |
| serial7 | 0002670 |
| status | confirmed |
| supply_need_id | 29 (matches supply_need above) |
| customer_order_id | 45 |
| trailer_id | 1001 (correct trailer already linked) |
| assigned_at | 2026-05-15 11:03:00 |
| confirmed_at | 2026-05-29 02:48:42 |
| docs_issued_at | 2026-05-19 10:12:08 |

### Related trailer id=1001

| Field | Value |
|---|---|
| id | 1001 |
| vin | MX4000004T0002670 |
| status | SOLD |
| lifecycle_status | customer_shipped |
| warehouse_id | 1 (Алматы) |
| item_id | 515 (matches supply_need and produced_unit) |
| manufacture_date | 2026-05-14 |
| created_at | 2026-05-19 10:06:27 |

### Related customer_order id=45

| Field | Value |
|---|---|
| id | 45 |
| order_number | ORD-000045 |
| status | shipped |
| is_shipped | 1 |
| documents_issued | 1 |
| trailer_id | 1001 |
| warehouse_id | 1 |

---

## 3. Data relationships to verify before repair

The repair script must verify ALL of the following before applying any change:

| # | Check | Expected | Fail action |
|---|---|---|---|
| R1 | `produced_unit.id = 27` exists | yes | abort |
| R2 | `produced_unit.status = 'vin_assigned'` | yes | abort (already repaired or changed) |
| R3 | `produced_unit.trailer_id IS NULL` | yes | abort (already repaired) |
| R4 | `produced_unit.item_id = 515` | yes | abort (unexpected change) |
| R5 | `produced_unit.production_request_line_id = 27` | yes | abort |
| R6 | `trailer id=1001` exists | yes | use Decision B or D |
| R7 | `trailer.vin = 'MX4000004T0002670'` | yes | abort (unexpected VIN) |
| R8 | `trailer.item_id = 515` | yes | abort (item mismatch) |
| R9 | `trailer.warehouse_id = 1` | not NULL | abort if NULL |
| R10 | `vin_registry id=987` exists | yes | abort |
| R11 | `vin_registry.supply_need_id = 29` | yes | abort (wrong registry row) |
| R12 | `vin_registry.trailer_id = 1001` | yes | consistent with repair |
| R13 | `supply_need id=29` exists and not cancelled | yes | abort |
| R14 | `supply_need.item_id = 515` | yes | abort |
| No other produced_unit with trailer_id=1001 | 0 rows | abort if another unit already claims 1001 |

---

## 4. Decision tree

```
produced_unit id=27 — trailer_id IS NULL?
└── YES
    │
    ├── Does trailer id=1001 exist with VIN=MX4000004T0002670?
    │   └── YES
    │       ├── item_id matches? (515 == 515)
    │       │   └── YES
    │       │       ├── warehouse_id not NULL?
    │       │       │   └── YES → DECISION A: link produced_unit.trailer_id = 1001
    │       │       └── NO → DECISION D: manual review (warehouse missing)
    │       └── NO (item mismatch) → DECISION D: manual review
    │
    ├── Does vin_registry id=987 exist for supply_need=29?
    │   (Already checked above — yes, trailer_id=1001)
    │   └── Consistent with Decision A
    │
    └── Is repair already applied? (trailer_id != NULL)
        └── YES → abort (already repaired)
```

### DECISION A (current case — execute):

Set `produced_unit.trailer_id = 1001`.

Justification:
- Trailer 1001 is the physical unit produced for supply_need 29 (VIN matches, item matches, warehouse matches).
- `vin_registry id=987` already links `supply_need_id=29` → `trailer_id=1001`.
- Trailer is SOLD + customer_shipped — the business transaction is closed.
- This repair only closes the audit trail by linking the produced_unit record to the physical trailer.
- No financial, order, or document impact.

### DECISION B (not applicable — trailer exists):

If trailer 1001 did not exist but VIN registry row 987 existed:
- Verify all fields (item_id, warehouse_id, manufacture_date) from supply_need and vin_registry.
- Create Trailer with `vin=MX4000004T0002670`, `item_id=515`, `warehouse_id=1`.
- Then apply Decision A.
- This path is **not needed** — trailer already exists.

### DECISION C (not applicable — VIN is correct):

Reset `produced_unit.status = 'produced_no_vin'` and clear VIN assignment.
Use only if: vin_registry row is wrong, VIN was assigned in error, no trailer should exist.
**Not applicable here.**

### DECISION D (manual business decision):

Block automated repair and require explicit human approval if:
- Another produced_unit already claims trailer_id=1001.
- Item or warehouse mismatch is detected.
- Any pre-check (R1–R14) fails unexpectedly.

---

## 5. Required backup before repair

Before executing the repair script with `--apply`, the following backup must exist:

```bash
# On the production server:
cp instance/trailers.db backups/trailers_before_repair_unit27_$(date +%Y%m%d_%H%M%S).db
```

The repair script will verify that a backup newer than 1 hour exists before applying any change.
If no recent backup is found, the script will abort with an error.

---

## 6. Required dry-run before repair

The repair script must be run in dry-run mode first:

```bash
python scripts/repair_produced_unit_27.py
# or explicitly:
python scripts/repair_produced_unit_27.py --dry-run
```

Dry-run output must show:
- All pre-checks with PASS/FAIL status.
- Proposed change: `produced_unit.trailer_id: None → 1001`.
- No SQL executed beyond SELECT.
- No file or DB modification.

Only after dry-run output is reviewed and all checks show PASS should `--apply` be used.

---

## 7. Proposed repair script path

```
scripts/repair_produced_unit_27.py
```

---

## 8. Repair script safety requirements

The script must implement ALL of the following:

| Requirement | Detail |
|---|---|
| Dry-run by default | `--apply` flag required to execute changes |
| Targeted update only | Modify ONLY `produced_unit WHERE id=27` |
| Transaction | Wrap UPDATE in `BEGIN / COMMIT`; rollback on any error |
| Backup check | Verify backup file exists and is recent (< 4 hours) before applying |
| Pre-checks R1–R14 | All must PASS before any change |
| Before/after snapshot | Print `produced_unit.trailer_id` before and after |
| No broad updates | No `UPDATE produced_unit` without `WHERE id=27` |
| No personal data | Do not print customer name, phone, email |
| Read-only open by default | Open in `?mode=ro` unless `--apply` |
| Explicit confirmation | Print "DRY RUN — no changes applied" or "APPLIED id=27 trailer_id → 1001" |
| Exit codes | 0 = success/clean, 1 = pre-check failed, 2 = db error, 3 = already repaired |

---

## 9. Verification after repair

After applying the repair:

1. **Re-run audit script:**
   ```bash
   python scripts/audit_vin_production_integrity.py
   ```

2. **Expected results:**
   - `produced_unit_vin_assigned_no_trailer` count = **0** (was 1)
   - P0 BLOCKERS count = **0**
   - P1 WARNINGS count ≤ 15 (no new P1 introduced)

3. **Manual checks:**
   - `produced_unit id=27` → `trailer_id = 1001`
   - `trailer id=1001` → `status=SOLD, lifecycle_status=customer_shipped` (unchanged)
   - No duplicate VIN in `trailer` table
   - `vin_registry id=987` → `trailer_id = 1001, status=confirmed` (unchanged)

4. **Service check:**
   ```bash
   sudo systemctl status trailers.service
   ```
   Service must remain `active (running)`. Repair does not require service restart.

---

## 10. Rollback approach

If the repair introduces unexpected issues:

1. Stop immediately — do not attempt further fixes.
2. Restore from backup:
   ```bash
   # On production server:
   sudo systemctl stop trailers.service
   cp backups/trailers_before_repair_unit27_<timestamp>.db instance/trailers.db
   sudo systemctl start trailers.service
   ```
3. Verify service started with old data:
   ```bash
   sudo systemctl status trailers.service
   ```
4. Document the issue before retrying.

---

## 11. Forbidden actions

- No `UPDATE`, `DELETE`, or `INSERT` without explicit `--apply` flag.
- No changes beyond `produced_unit WHERE id=27`.
- No changes to `vin_registry`, `trailer`, `customer_order`, or any other table.
- No migration execution.
- No service restart as part of repair (data-only change, no code change).
- No deploy or push as part of repair.
- No changes to `views.py`, `models.py`, `forms.py`, or templates.
- No changes to Nginx or systemd config.
- No installation of new dependencies.
- Do not print customer name, phone, email, or personal information.
- Do not remove the backup after repair — keep for at least 30 days.

---

## 12. Next safe step: P0-FB — create dry-run repair script

**P0-FB:** Create `scripts/repair_produced_unit_27.py` in dry-run mode only (no `--apply` implementation yet).

The script must:
- open DB in read-only mode by default;
- run all pre-checks R1–R14;
- print proposed change;
- print "DRY RUN — no changes applied";
- exit 0 if all checks pass, exit 1 if any check fails.

`--apply` path may be added in P0-FC after dry-run output is reviewed and approved.

Do not create the `--apply` path without separate approval.
