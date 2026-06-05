# P0-H: Stock Release VIN / Reservation Integrity Audit Report

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**DB:** production `/home/ubuntu/Trailers/instance/trailers.db`
**Mode:** READ-ONLY
**Audit script:** `scripts/audit_stock_release_vin_reservation_integrity.py`
**Status:** COMPLETE

---

## Context

Previous P0-FC repaired the only known case of a `produced_unit` with a final status but
`trailer_id=NULL` (produced_unit id=27 → trailer_id=1001).

This audit extends coverage to:
- All `produced_unit` records in final statuses without a trailer link (P0-1)
- All `vin_registry` records confirmed/applied but without a trailer link (P0-2)
- All `customer_order` records sold/shipped but without a trailer link (P0-3)
- Stale ACTIVE reservations pointing to trailers not matched by the order (P0-4)
- Orphan ACTIVE reservations, docs-without-trailer, placeholder VINs (P1-A/B/C)

**Important correction applied:**
`vin_registry.trailer_id IS NULL` is NOT inherently invalid.
Classification model used:

| Class | Meaning |
|-------|---------|
| VALID-A | VIN exists, trailer_id NULL, no order/reservation/supply_need |
| VALID-B | VIN exists, trailer_id NULL, linked to order/sn/line but not released/shipped |
| VALID-C | VIN exists and trailer_id is set (normal) |
| P0-1 | produced_unit final/vin_assigned but trailer_id NULL |
| P0-2 | vin_registry confirmed/assigned but trailer_id NULL |
| P0-3 | customer_order sold/shipped/ready_to_ship with VIN but no trailer_id |
| P0-4 | ACTIVE reservation for SOLD trailer with no matching order-trailer link |

---

## Table Counts

| Table | Rows |
|-------|------|
| produced_unit | 78 |
| vin_registry | 1039 |
| supply_need | 80 |
| reservation | 109 |
| customer_order_line | 79 |
| customer_order | 79 |
| trailer | 1038 |

---

## P0 Findings

### P0-1: produced_unit final status but trailer_id NULL

**Count: 0** — CLEAN

No produced_unit records in final statuses without a trailer link.
(P0-FC repair resolved the only prior case: produced_unit id=27.)

---

### P0-2: vin_registry confirmed/assigned but trailer_id NULL

**Count: 0** — CLEAN

All confirmed/assigned vin_registry rows have a linked trailer.

---

### P0-3: customer_order sold/shipped but trailer_id NULL

**Count: 1 — WATCH**

| Field | Value |
|-------|-------|
| ord_id | 78 |
| order_number | ORD-000078 |
| status | sold_not_shipped |
| fulfillment_source | later |
| documents_issued | 1 |
| is_shipped | 0 |
| order.trailer_id | NULL |
| created_at | 2026-06-05 |
| VIN reserved | MX4000002T0002706 (vr_id=1042, status=reserved) |
| order_line | id=80, fstatus=ready_for_documents, vin_registry_id=1042 |
| produced_unit | none |
| reservation | none |

**Context:**
- Order was created on 2026-06-05 (today) with `fulfillment_source='later'`.
- A VIN (`MX4000002T0002706`) was reserved for this order line.
- Documents were issued (`docs_issued_at=2026-06-05 19:13`).
- No physical trailer exists yet — production has not been initiated.
- The VIN registry status is `reserved` (not yet confirmed/applied).

**Classification:** P0-3 / active workflow watch.
This is not a historical broken link. It is an in-progress fulfillment where:
1. An order was sold and documents issued.
2. A VIN was reserved.
3. Production must still be scheduled to create the physical trailer.

**Risk:** If production is not initiated, this order will remain stuck with
issued documents but no physical asset to deliver.

---

### P0-4: ACTIVE reservation with no order-trailer match

**Count: 0** — CLEAN

All ACTIVE reservations for SOLD trailers correspond to orders that
correctly reference that trailer (`customer_order.trailer_id = reservation.trailer_id`).

Note: reservation res=32 for trailer 987 / ORD-000027 was reviewed and
confirmed VALID: order 27 has `trailer_id=987` — this is a normal
sold-not-yet-shipped lock.

---

## P1 Findings

### P1-A: ACTIVE reservation with cancelled/missing order

**Count: 0** — CLEAN

---

### P1-B: Documents issued for later-fulfillment order, no physical trailer

**Count: 1**

Same case as P0-3 above (ORD-000078).
Documents were issued for a `later`-source order line before a physical trailer
was produced. The vin_registry record has `docs_issued_at` set but VIN status
remains `reserved` (not confirmed as physically applied).

---

### P1-C: Trailer SOLD or active but has placeholder VIN

**Count: 2**

| trailer_id | VIN | status | order | vr_id | vr_status | confirmed_at |
|-----------|-----|--------|-------|-------|-----------|--------------|
| 987 | MX4000002T0000000 | SOLD | ORD-000027 (sold_not_shipped) | 976 | confirmed | 2026-05-13 |
| 784 | 00000000000000000000 | SOLD | none | none | — | — |

**Trailer 987:**
- VIN `MX4000002T0000000` is a placeholder (serial7 = `0000000`).
- The vin_registry entry (id=976) has `status=confirmed` — VIN was marked as
  physically applied on 2026-05-13.
- ORD-000027 is `sold_not_shipped`, docs issued, produced_unit id=18 is `vin_assigned`.
- The real physical VIN was not recorded when the trailer was produced.
- The P0-G1 guard (committed in 6a8df58) now prevents new placeholder VINs
  from being set via the UI; this is a pre-existing case.

**Trailer 784:**
- VIN `00000000000000000000` (raw zeros, not even the MX4 prefix).
- Status SOLD, no order linked, no vin_registry entry.
- Historical data quality issue.

---

## VALID State Summary

All 1039 vin_registry rows are classified:

| Class | Count |
|-------|-------|
| VALID-A (inventory, no links) | 3 |
| VALID-B (reserved+linked, not shipped) | 4 |
| VALID-C (trailer_id set) | 1032 |
| **Total** | **1039** |

**VALID-B details** (all expected in-progress orders):

| vr_id | VIN | vr_status | linked order | order status |
|-------|-----|-----------|-------------|--------------|
| 1042 | MX4000002T0002706 | reserved | ORD-000078 | sold_not_shipped |
| 1041 | MX4000002T0002708 | reserved | order 76 | produced_waiting_vin |
| 1039 | MX4000013T0000017 | reserved | order 72 | waiting_production |
| 979 | MX4000002T0002659 | reserved | order 32 | produced_waiting_vin |

---

## Verdict

```
P0-1 (produced_unit final, no trailer):        0
P0-2 (vin_registry confirmed, no trailer):     0
P0-3 (order sold/shipped, no trailer):         1  ← WATCH
P0-4 (stale reservation, no order link):       0
P1-A (orphan ACTIVE reservation):              0
P1-B (docs issued, later, no trailer):         1  ← WATCH
P1-C (placeholder VIN on SOLD trailer):        2  ← WARN

RESULT: P0_WATCH (1)
P1:     WARNINGS (3)
```

**No immediate data repair needed** (unlike P0-FC which required a script repair).

The single P0-3 case (ORD-000078) is an active in-progress workflow, not a
historical broken link. It requires operational follow-up rather than a repair script.

See `2026-06-05-trailers-stock-release-vin-reservation-p0h-repair-plan.md`
for repair instructions per case.
