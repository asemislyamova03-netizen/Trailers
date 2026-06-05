# P0-I: Type-Aware Shipment Guard — Implementation Report

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**File changed:** `views.py`
**Status:** IMPLEMENTED AND COMMITTED

---

## Context

P0-H audit identified ORD-000078: `sold_not_shipped` with `documents_issued=1`,
VIN reserved, but no physical trailer linked. The P0-I guard prevents this
pattern for stock/production fulfillment orders.

---

## Route Architecture

After audit, the actual document/shipment flow is:

| Route | State | Notes |
|-------|-------|-------|
| `order_issue_documents` (line 11747) | Sets `documents_issued=True` | **Active** — the real gate |
| `order_ship` (line 10631) | Deprecated | First line is `flash + return redirect` — dead code |
| `order_line_ship` (line 10682) | Deprecated | Same — dead code |
| `_apply_realization_shipment_effect` | Sets `is_shipped=True` | Called when realization is posted |

The only entry point for `documents_issued=True` is `order_issue_documents`,
which already calls `_order_lines_document_blockers(order)` and blocks if
blockers exist. P0-I adds a blocker to that function.

---

## What P0-I Adds

In `_order_lines_document_blockers` (views.py ~line 5903):

```python
# P0-I: for stock/production lines the physical trailer must exist before
# documents are issued. 'later' fulfillment is intentionally allowed here —
# it represents pre-production sales where the trailer is produced after the
# legal sale. GOODS/COMPONENT lines do not require a trailer at all.
if (line.line_type == 'TRAILER'
        and (line.fulfillment_source or '').lower() in ('stock', 'production')
        and not _trailers_for_order_line(line)):
    blockers.append(
        f'по позиции #{line.line_no} физический прицеп не привязан — '
        'завершите выпуск производства или привяжите прицеп из наличия.'
    )
```

### Trigger conditions

The blocker fires when ALL three are true:
1. `line.line_type == 'TRAILER'`
2. `fulfillment_source` is explicitly `'stock'` or `'production'`
3. `_trailers_for_order_line(line)` returns an empty list (no physical trailer)

`_trailers_for_order_line` checks three sources:
- Active VIN registry rows with `row.trailer` linked
- ACTIVE reservations with `reservation.trailer` linked
- Contract lines with `contract_line.trailer` linked

---

## Behavior Matrix

| Scenario | fulfillment_source | Has trailer | Blocked? | Correct? |
|----------|--------------------|-------------|----------|----------|
| Production order, VIN assigned, trailer created | `production` | Yes | No | ✓ |
| Production order, VIN assigned but no trailer | `production` | No | **Yes** | ✓ |
| Stock order, trailer reserved | `stock` | Yes | No | ✓ |
| Stock order, no trailer linked | `stock` | No | **Yes** | ✓ |
| Later-fulfillment order (pre-production sale) | `later` | No | No | ✓ (intended) |
| Later-fulfillment order with trailer | `later` | Yes | No | ✓ |
| No fulfillment_source set | `None` | No | No | ✓ (conservative) |
| GOODS/SERVICE line (future) | any | N/A | No | ✓ (line_type check) |

---

## ORD-000078 (current P0-3 watch case)

ORD-000078 has `fulfillment_source='later'`. The P0-I guard does NOT block it.
This is correct — the order was intentionally created as a pre-production sale
and documents were issued before the trailer was produced. The operational
follow-up (create trailer and link to this order) is tracked in the P0-H
repair plan.

---

## What P0-I Does NOT Change

- `fulfillment_source='later'` orders: still allowed to issue docs without trailer
- GOODS/COMPONENT lines: not affected (line_type check)
- Realization posting path: not changed (realization requires documents to
  already be issued, so P0-I prevents the bad state upstream)
- Permissions: unchanged
- Templates: unchanged

---

## Validation Checks Run

```
python -m py_compile views.py  → SYNTAX OK
git diff --check               → OK
ReadLints views.py             → No linter errors
```

## Diff Summary

```
views.py | 11 insertions(+)
```

11 lines added to `_order_lines_document_blockers`. No deletions.

---

## Guards Now in Place (all P0 production/VIN)

| Guard | Commit | Protects |
|-------|--------|---------|
| P0-G1 VIN edit guard | 6a8df58 | `trailer_edit`, `trailer_create` — no placeholder/duplicate VINs |
| P0-G2 finalization guard | 4cb2471 | `logistics_assign_vin` — atomic rollback, pre-commit assertion |
| P0-I shipment guard | this commit | `order_issue_documents` — no docs without physical trailer for stock/production |

---

## Next Safe Steps

| Priority | Change | Description |
|----------|--------|-------------|
| P1 | P1-C | Repair placeholder VINs — trailer 784 and 987 (manual when real VINs known) |
| P1 | P0-J | Stock replenishment problem resolver UI |
| P1 | P1-A/B | Goods/component sales model and order line UI |
