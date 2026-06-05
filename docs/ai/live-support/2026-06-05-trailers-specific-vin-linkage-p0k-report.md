# P0-K: Specific VIN Linkage Audit Report

**Date:** 2026-06-05 22:05 UTC
**Branch:** crm-roles-production-logistics
**Script:** `scripts/audit_specific_vin_linkage.py`
**DB:** production `/home/ubuntu/Trailers/instance/trailers.db`
**Mode:** READ-ONLY

---

## VIN Suffixes Checked

**GROUP A — Reported as sold/reserved without linked trailer:**
2681, 2680, 2674, 2566, 2524, 2670, 2637, 2414, 2578, 2544, 2655 (11 suffixes)

**GROUP B — Reported as missing source warehouse / cannot proceed to production:**
2706, 2708, 2659 (3 suffixes)

**Total audited: 14 suffixes**

---

## Summary Table

| Suffix | Full VIN | vr_id | vr_status | Trailer | t_status | Order | ord_status | docs | shipped | Class |
|--------|----------|-------|-----------|---------|----------|-------|------------|------|---------|-------|
| 2414 | MX4000002T0002414 | 985 | confirmed | 994 | SOLD | ORD-000057 | shipped | 1 | 1 | **A** |
| 2524 | MX4000002T0002524 | 1026 | confirmed | 1029 | SOLD | ORD-000065 | shipped | 1 | 1 | **A** |
| 2544 | MX4000002T0002544 | 981 | confirmed | 990 | SOLD | ORD-000034 | shipped | 1 | 1 | **A** |
| 2566 | MX4000002T0002566 | 1010 | confirmed | 1030 | IN_TRANSIT | — | — | — | — | **A** |
| 2578 | MX4000002T0002578 | 983 | confirmed | 992 | SOLD | ORD-000046 | shipped | 1 | 1 | **A** |
| 2637 | MX4000002T0002637 | 984 | confirmed | 993 | SOLD | ORD-000056 | shipped | 1 | 1 | **A** |
| 2655 | MX4000002T0002655 | 977 | confirmed | 988 | SOLD | ORD-000028 | shipped | 1 | 1 | **A** |
| 2670 | MX4000004T0002670 | 987 | confirmed | 1001 | SOLD | ORD-000045 | shipped | 1 | 1 | **A** |
| 2674 | MX4000002T0002674 | 996 | confirmed | 1036 | SOLD | ORD-000044 | shipped | 1 | 1 | **A** |
| 2680 | MX4000002T0002680 | 1036 | confirmed | 1040 | SOLD | ORD-000069 | shipped | 1 | 1 | **A** |
| 2681 | MX4000002T0002681 | 1037 | confirmed | 1042 | SOLD | ORD-000058 | shipped | 1 | 1 | **A** |
| 2706 | MX4000002T0002706 | 1042 | reserved | **NULL** | — | ORD-000078 | sold_not_shipped | 1 | 0 | **D** |
| 2708 | MX4000002T0002708 | 1041 | reserved | **NULL** | — | ORD-000076 | produced_waiting_vin | 1 | 0 | **E** |
| 2659 | MX4000002T0002659 | 979 | reserved | **NULL** | — | ORD-000032 | produced_waiting_vin | 1 | 0 | **E** |

---

## Classification Counts

| Class | Meaning | Count |
|-------|---------|-------|
| A | Trailer exists but some links missing (order_line.trailer_id=NULL) | 11 |
| D | Order sold/docs issued, VIN reserved, no physical trailer | 1 |
| E | Production blocked: prl.source_warehouse_id=NULL | 2 |

**True P0 cases: 3** (D=1, E=2)

---

## CLASS A — Detailed Findings (11 VINs)

### What the user reported vs. what was found

The user reported these VINs as "sold without linked trailer". Audit result:
**All 11 trailers EXIST in the system, are linked to vin_registry and orders.**

The actual inconsistency is at the **order line level**:
`customer_order_line.trailer_id = NULL` for most shipped orders,
even though `customer_order.trailer_id` is correctly set and
`vin_registry.trailer_id` is correctly set.

This is a **data completeness P1** issue, not a blocking P0.

### ORDER_LINE.trailer_id = NULL (COSMETIC / P1)

| Suffix | Order | line_id | line.trailer_id | order.trailer_id | line.vr | line.status |
|--------|-------|---------|----------------|-----------------|---------|-------------|
| 2414 | ORD-000057 | 58 | **NULL** | 994 | 985 | shipped |
| 2524 | ORD-000065 | 67 | **NULL** | 1029 | 1026 | shipped |
| 2544 | ORD-000034 | 34 | **NULL** | 990 | 981 | shipped |
| 2578 | ORD-000046 | 46 | **NULL** | 992 | 983 | shipped |
| 2637 | ORD-000056 | 57 | **NULL** | 993 | 984 | shipped |
| 2655 | ORD-000028 | 28 | 988 (OK) | 988 | 977 | shipped |
| 2670 | ORD-000045 | 45 | **NULL** | 1001 | 987 | **waiting_payment** ← |
| 2674 | ORD-000044 | 44 | **NULL** | 1036 | 996 | shipped |
| 2680 | ORD-000069 | 71 | 1040 (OK) | 1040 | 1036 | shipped |
| 2681 | ORD-000058 | 59 | **NULL** | 1042 | 1037 | shipped |
| 2566 | — | — | N/A | N/A | N/A | in_transit |

**2670 (ORD-000045):** Line status is `waiting_payment` but order is `shipped`. This is a line-status/sync inconsistency.

### STALE ACTIVE RESERVATIONS (P1)

For shipped orders, ACTIVE reservations should be CLOSED:

| Suffix | Order | res_id | trailer | res_status |
|--------|-------|--------|---------|------------|
| 2414 | ORD-000057 | 78 | 994 | **ACTIVE** ← stale |
| 2637 | ORD-000056 | 72 | 993 | **ACTIVE** ← stale |
| 2670 | ORD-000045 | 52 | 1001 | CLOSED (OK) |
| 2674 | ORD-000044 | 66 | 1036 | **ACTIVE** ← stale |
| 2681 | ORD-000058 | 81 | 1042 | **ACTIVE** ← stale |

### 2674 — TWO PRODUCED UNITS (P1 review)

| pu_id | status | trailer | order | prl | note |
|-------|--------|---------|-------|-----|------|
| 66 | vin_assigned | **1036** | None | 39 | Correct — VIN 2674 on trailer 1036 |
| 61 | vin_assigned | 1032 | **44** | 40 | Linked to ORD-000044 but trailer 1032 (VIN 2657), different trailer |

pu_id=61 is for trailer 1032 (VIN 2657), created as part of the same supply batch
(prl 40) and linked to order 44. The order was eventually fulfilled by trailer 1036
(prl 39). pu_id=61 is now orphan-linked to an order that was shipped with a
different trailer. Needs clarification: was pu_id=61 a cancelled/superseded attempt?

### 2524 — LARGE BATCH PRODUCTION (INFO)

supply_need id=39 has prl_id=35 with qty=26. All 25 produced_units (pu_id 34–58)
are `vin_assigned` to different trailers from the batch. Trailer 1029 (VIN 2524)
is one of them, correctly sold in ORD-000065. No P0 issue here.

---

## CLASS D — VIN 2706 (ORD-000078)

| Field | Value |
|-------|-------|
| Full VIN | MX4000002T0002706 |
| vr_id | 1042 |
| vr_status | reserved |
| trailer_id | NULL |
| Order | ORD-000078 |
| order_status | sold_not_shipped |
| documents_issued | 1 |
| fulfillment_source | **later** |
| produced_unit | none |
| supply_need | none |

**Classification D / WATCH:**
Documents were issued for an order with `fulfillment_source='later'`. No physical
trailer exists yet. This is a pre-production sale where the trailer will be
produced after the legal sale.

This case was identified in P0-H (vr_id=1042) and documented in the P0-H repair
plan as "R1: ORD-000078". It is **not a surprise** and is the **intended
`later`-fulfillment workflow**.

Blocking state: not blocked today. Production must be initiated, trailer created,
and all links restored when production is complete.

---

## CLASS E — VIN 2708 (ORD-000076)

| Field | Value |
|-------|-------|
| Full VIN | MX4000002T0002708 |
| vr_id | 1041 |
| vr_status | reserved |
| trailer_id | NULL |
| Order | ORD-000076 |
| order_status | produced_waiting_vin |
| line_status | sold_not_shipped |
| documents_issued | 1 |
| fulfillment_source | production |
| supply_need | id=77, CUSTOMER_ORDER, wh=3, src_wh=NULL |
| prl | id=66, status=ready, src_wh=NULL, qty=1 |
| produced_unit | pu_id=80, status=produced_no_vin, trailer=NULL, wh=3 |

**Blocker:** `prl id=66 source_warehouse_id = NULL`

The physical unit (pu_id=80) is ready (`produced_no_vin`) and the VIN is reserved.
The production warehouse needs to be set to allow `logistics_assign_vin` to proceed.

**Special case (user-reported 1+1 quantity mismatch):**
The audit found only 1 produced_unit (pu_id=80) and 1 PRL (qty=1). No second
produced_unit was found via supply_need id=77 or order id=76. The "1+1 mismatch"
may refer to a production request at a different level or a now-deleted/cancelled
record. Requires deeper investigation in repair plan.

**P0-I guard relevance:** ORD-000076 has `fulfillment_source='production'`.
The P0-I guard will now BLOCK document issuance for production orders without a
trailer. Since documents are already issued here, the guard prevents future
recurrence but does not block the current order from being resolved.

---

## CLASS E — VIN 2659 (ORD-000032)

| Field | Value |
|-------|-------|
| Full VIN | MX4000002T0002659 |
| vr_id | 979 |
| vr_status | reserved |
| trailer_id | NULL |
| Order | ORD-000032 |
| order_status | produced_waiting_vin |
| line_status | sold_not_shipped |
| documents_issued | 1 |
| fulfillment_source | production |
| supply_need | id=21, CUSTOMER_ORDER, wh=3, src_wh=NULL |
| prl | id=19, status=ready, src_wh=NULL, qty=1 |
| produced_unit | pu_id=70, status=produced_no_vin, trailer=NULL, wh=3 |

**Blocker:** `prl id=19 source_warehouse_id = NULL`

Same pattern as 2708. Physical unit ready (`produced_no_vin`), VIN reserved.
Production warehouse assignment needed to unblock VIN finalization.

---

## Verdict

```
CLASS A (trailer exists, minor link gaps):   11 VINs — P1, no immediate data repair
CLASS D (sold, docs issued, no trailer):      1 VIN  — WATCH (later workflow, known P0-H R1)
CLASS E (production blocked, no src_wh):     2 VINs  — P0/P1, source_warehouse repair needed

True P0 blockers requiring action:           3
Application code touched:                   No
Production data touched:                    No
```
