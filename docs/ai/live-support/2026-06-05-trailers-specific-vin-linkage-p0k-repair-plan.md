# P0-K: Specific VIN Linkage Repair Plan

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Prerequisite:** Read `2026-06-05-trailers-specific-vin-linkage-p0k-report.md` first.
**Status:** PLAN ONLY — no data has been modified

---

## Overview

| Priority | VIN Suffix | Classification | Action |
|----------|-----------|---------------|--------|
| WATCH | 2706 | D — later-fulfillment, no trailer | Monitor; create trailer when production ready |
| P0 | 2708 | E — production blocked, src_wh=NULL | Set source_warehouse → unblock VIN assignment |
| P0 | 2659 | E — production blocked, src_wh=NULL | Set source_warehouse → unblock VIN assignment |
| P1 | 2414,2524,2544,2578,2637,2655,2670,2674,2680,2681 | A — line.trailer_id=NULL | Sync order_line.trailer_id after reviewing stale reservations |
| P1 | 2566 | A — IN_TRANSIT, no order | No action until trailer moves to final warehouse |

---

## Section 1: VIN 2706 (ORD-000078) — D/WATCH

### State
- VIN `MX4000002T0002706` reserved (vr_id=1042)
- Order ORD-000078: `sold_not_shipped`, `documents_issued=1`
- `fulfillment_source='later'` — trailer production AFTER legal sale
- No produced_unit, no supply_need, no reservation

### Action required
This is the known P0-H R1 case. No immediate data repair needed.

**When logistics initiates production:**
1. Create supply_need for this order (wh=2, CUSTOMER_ORDER type).
2. Create production_request_line linked to the supply_need.
3. Produce the trailer via `logistics_assign_vin` route.
4. The P0-G2 guard will ensure atomic commit of all links.
5. After production: `vin_registry.trailer_id`, `produced_unit.trailer_id`,
   `order.trailer_id`, `order_line.trailer_id` will all be set.

**Monitoring:** Run `scripts/audit_stock_release_vin_reservation_integrity.py`
weekly to track this case.

---

## Section 2: VIN 2708 (ORD-000076) — E/P0

### State
- VIN `MX4000002T0002708` reserved (vr_id=1041)
- Order ORD-000076: `produced_waiting_vin`, `documents_issued=1`, `fs=production`
- `order_line id=78`: `sold_not_shipped`, `trailer_id=NULL`, `vr=1041`, `sn=77`
- `supply_need id=77`: CUSTOMER_ORDER, `wh=3`, `source_warehouse_id=NULL`
- `production_request_line id=66`: `status=ready`, `source_warehouse_id=NULL`, `qty=1`
- `produced_unit id=80`: `produced_no_vin`, `trailer_id=NULL`, `wh=3`

### Blocker
`prl id=66 source_warehouse_id = NULL`

The `logistics_assign_vin` route calls `_default_production_warehouse()` globally
(not per-PRL), so if the global production warehouse is configured, VIN assignment
via the UI should already work. The `source_warehouse_id=NULL` on the PRL is a
data gap but may not actually block the UI flow.

**Recommended test first (before any repair):**
1. In production, navigate to the produced_unit id=80 VIN assignment page.
2. Verify whether the "Assign VIN" form appears with `MX4000002T0002708` as option.
3. If it does → proceed with VIN assignment through UI (no data repair needed).
4. If blocked → confirm exactly which guard is failing.

### Special case: 1+1 produced_unit mismatch

The user reported: "production request created for 1+1 units; both units are
released/created without correct VIN/trailer link."

Audit found: only 1 PRL (qty=1) and 1 produced_unit (pu_id=80) for supply_need 77.
No second produced_unit or PRL was found linked to ORD-000076.

**Investigation needed:** Is there a second production_request or supply_need
linked to order 76 via a different path? Suggested query:
```sql
SELECT * FROM produced_unit WHERE order_id=76;
SELECT * FROM production_request_line WHERE order_line_id=78;
SELECT * FROM supply_need WHERE order_line_id IN
    (SELECT id FROM customer_order_line WHERE order_id=76);
```
Run these read-only on production before planning any repair.

### Repair if UI assignment is blocked

**Pre-conditions:**
1. Verify `_default_production_warehouse()` is set (warehouse with `is_production=True`).
2. Verify `pu_id=80` is still `produced_no_vin` and `trailer_id=NULL`.
3. Verify `vr_id=1041` still `status='reserved'` and `trailer_id=NULL`.

**Option A: Set source_warehouse_id on PRL (if that's the actual blocker)**
```sql
-- DRY RUN ONLY — do not apply without explicit approval
-- UPDATE production_request_line
--    SET source_warehouse_id = 3  -- warehouse 3 = wh where supply_need is for
--  WHERE id = 66
--    AND source_warehouse_id IS NULL;
```

**Option B: Assign VIN through UI**
After verifying no double-produced_unit:
1. Navigate to produced_unit id=80 VIN assignment.
2. Select VIN `MX4000002T0002708`.
3. System creates Trailer, links all records atomically (P0-G2 guard active).

**Required links after repair:**
```
trailer (new)       ← new record with vin=MX4000002T0002708, wh=3
produced_unit 80    ← trailer_id = new_trailer.id, status='vin_assigned'
vin_registry 1041   ← trailer_id = new_trailer.id, status='assigned'
order_line 78       ← trailer_id = new_trailer.id
order 76            ← trailer_id = new_trailer.id
```

---

## Section 3: VIN 2659 (ORD-000032) — E/P0

### State
- VIN `MX4000002T0002659` reserved (vr_id=979)
- Order ORD-000032: `produced_waiting_vin`, `documents_issued=1`, `fs=production`
- `order_line id=32`: `sold_not_shipped`, `trailer_id=NULL`, `vr=979`, `sn=21`
- `supply_need id=21`: CUSTOMER_ORDER, `wh=3`, `source_warehouse_id=NULL`
- `production_request_line id=19`: `status=ready`, `source_warehouse_id=NULL`, `qty=1`
- `produced_unit id=70`: `produced_no_vin`, `trailer_id=NULL`, `wh=3`

**Identical pattern to VIN 2708.** Same repair path applies.

**Pre-conditions:**
1. Verify `pu_id=70` still `produced_no_vin`, `trailer_id=NULL`.
2. Verify `vr_id=979` still `reserved`, `trailer_id=NULL`.

**Recommended test:** Navigate to produced_unit id=70 VIN assignment page.
If `MX4000002T0002659` appears as an option, assign through UI.

**Required links after repair:**
```
trailer (new)       ← new record with vin=MX4000002T0002659, wh=3
produced_unit 70    ← trailer_id = new_trailer.id, status='vin_assigned'
vin_registry 979    ← trailer_id = new_trailer.id, status='assigned'
order_line 32       ← trailer_id = new_trailer.id
order 32            ← trailer_id = new_trailer.id
```

---

## Section 4: Class A — order_line.trailer_id sync (P1)

### What is wrong
For 9 of 11 CLASS A VINs, `customer_order_line.trailer_id = NULL` even though:
- `customer_order.trailer_id` is correctly set
- `vin_registry.trailer_id` is correctly set
- The order is `shipped`

This is a data completeness gap in line-level denormalization. It does not
block any current operation (orders are shipped), but it breaks line-level
reports and may cause confusion in future order-line checks.

### Root cause
The `_sync_order_line_workflow_links` function is supposed to propagate
trailer_id to order lines. It may not have been called for some historic
orders, or was called before `trailer_id` was set on the line.

### Repair per line

For each affected order line, the repair is:
```sql
UPDATE customer_order_line
   SET trailer_id = <order.trailer_id>
 WHERE id = <line_id>
   AND trailer_id IS NULL
   AND order_id = <order_id>;
```

**Per-case values:**

| line_id | order_id | correct trailer_id |
|---------|----------|-------------------|
| 58 | 57 | 994 |
| 67 | 65 | 1029 |
| 34 | 34 | 990 |
| 46 | 46 | 992 |
| 57 | 56 | 993 |
| 45 | 45 | 1001 |
| 44 | 44 | 1036 |
| 59 | 58 | 1042 |

**Note 2670 (line_id=45):** Line status is `waiting_payment` but order status
is `shipped`. After setting trailer_id, also update line status:
```sql
UPDATE customer_order_line SET status='shipped' WHERE id=45 AND order_id=45;
```

### Stale ACTIVE reservations (P1)

For shipped orders, ACTIVE reservations should be CLOSED:

| res_id | order | trailer | action |
|--------|-------|---------|--------|
| 78 | ORD-000057 | 994 | Set status='CLOSED' |
| 72 | ORD-000056 | 993 | Set status='CLOSED' |
| 66 | ORD-000044 | 1036 | Set status='CLOSED' |
| 81 | ORD-000058 | 1042 | Set status='CLOSED' |

```sql
UPDATE reservation SET status='CLOSED'
 WHERE id IN (78, 72, 66, 81) AND status='ACTIVE';
```

### 2674 — extra produced_unit pu_id=61 (P1 review)

`pu_id=61` is linked to order 44 with trailer=1032 (VIN 2657). The order was
ultimately fulfilled with trailer=1036 (VIN 2674). Possible scenarios:
- The order was initially going to use trailer 1032, then switched to 1036.
- `pu_id=61` was a cancelled/abandoned attempt; `order_id=44` on this pu is stale.

**Action:** Verify whether trailer 1032 (VIN 2657) has its own order and produced_unit.
If `pu_id=61` correctly belongs to trailer 1032 under a different order, simply
set `pu_id=61.order_id = NULL` to detach it from order 44.
```sql
-- Only if confirmed that pu_id=61 should not belong to order 44:
-- UPDATE produced_unit SET order_id=NULL, order_line_id=NULL WHERE id=61;
```

---

## Section 5: Dry-Run Repair Script Requirements (P0-KB)

Create `scripts/repair_specific_vin_linkage_p0kb.py` with:

1. **Dry-run mode default** — no writes unless `--apply` is passed.
2. **One VIN at a time** — separate `--vin 2708` or `--vin 2659` flags.
3. **Pre-checks** before apply:
   - Verify produced_unit status is `produced_no_vin` (not already finalized).
   - Verify vr_row status is `reserved` (not already assigned).
   - Verify no Trailer with this VIN exists.
   - Verify order still open (not cancelled).
   - Verify production_warehouse exists globally.
4. **Timestamped DB backup** before any write.
5. **Single transaction** for all link writes.
6. **Post-apply verification** — re-query all affected records.
7. **Script NOT for UI-assignable cases** — if the UI can do it, use the UI.

**Highest confidence cases for P0-KB:**
- VIN 2659 (ORD-000032) — simple 1:1 produced_unit → VIN → order
- VIN 2708 (ORD-000076) — same, but verify 1+1 mismatch first

---

## Section 6: Forbidden Actions

- No `DELETE` on VINs, produced_units, reservations, or orders.
- No bulk UPDATE across all CLASS A lines in one statement.
- No repair without per-case review and explicit approval.
- No repair of VIN 2708 until the 1+1 produced_unit question is resolved.
- No repair of stale reservations until line_trailer_id is synced first.

---

## Section 7: Implementation Order

```
Step  Action                                                      Confidence
────  ──────────────────────────────────────────────────────────  ──────────
1     Test VIN 2659 assignment through UI (produced_unit id=70)  HIGH
2     Test VIN 2708 assignment through UI (produced_unit id=80)  HIGH
3     Investigate 2708 extra produced_unit question              REQUIRED before any repair
4     If UI blocked for 2659/2708: create P0-KB dry-run script   MEDIUM
5     Sync order_line.trailer_id for CLASS A lines (P1)          MEDIUM — not urgent
6     Close stale ACTIVE reservations (P1)                       MEDIUM
7     Fix 2670 line status (P1 cosmetic)                         LOW
8     Review pu_id=61 / VIN 2674 orphan link                     LOW
9     Monitor VIN 2706 (ORD-000078 later-fulfillment)            ONGOING
```

---

## Related documents

- `2026-06-05-trailers-stock-release-vin-reservation-p0h-report.md` — VIN 2706 (R1)
- `2026-06-05-trailers-stock-release-vin-reservation-p0h-repair-plan.md` — R1 plan
- `2026-06-05-trailers-problem-release-root-cause-and-prevention.md` — root causes
- `2026-06-05-trailers-production-vin-finalization-guard-p0g2-report.md` — P0-G2 guard
- `2026-06-05-trailers-shipment-guard-p0i-report.md` — P0-I guard
