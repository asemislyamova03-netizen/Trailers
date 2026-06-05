# P1-A Discovery: Goods / Component Sales Workaround

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Status:** Read-only discovery — no code changed
**Triggered by:** Correction before P0-G2 implementation

---

## Background

Before implementing the P0-G2 production VIN finalization guard, the user raised
an important business correction:

> Some records that look like trailers with placeholder VINs are not real light
> trailers. They are components/goods that managers entered through the
> trailer/order workflows because goods sales for non-trailer items was not
> implemented.

This document records the findings from a read-only audit to verify whether this
pattern exists in production and to define the future model.

---

## Current State (production DB snapshot — 2026-06-05)

### Item catalog

| item_type | count |
|-----------|-------|
| TRAILER   | 530   |
| COMPONENT | 1     |

The single COMPONENT item is `id=497`, name=`тент 2 выс 60`, `requires_vin=False`,
`is_sellable=True`. It is **not linked to any order line or produced_unit** in
the production database.

### Order lines

| line_type | count |
|-----------|-------|
| TRAILER   | 79    |

**All 79 customer_order_line records have `line_type='TRAILER'`.** There are no
`GOODS`, `COMPONENT`, or `SERVICE` order line types in the current schema or data.

### Placeholder-VIN trailers (P1-C candidates from P0-H audit)

| trailer_id | vin | item_id | item_type | item_name | requires_vin |
|-----------|-----|---------|-----------|-----------|-------------|
| 784 | 00000000000000000000 | 19 | TRAILER | Прицеп 2×1,25м, борт 30см, R13 с опорным колесом с колёсами | 1 |
| 987 | MX4000002T0000000   | 504 | TRAILER | Прицеп легковой одноосный 2,0×1,25м, борт 50см, R14, с опорным колесом | 1 |

**Both placeholder-VIN trailers are genuine light trailers** (item_type=TRAILER,
requires_vin=1). They are NOT goods/components entered via a workaround.
Their placeholder VINs represent a data quality issue: the physical VIN stamped
on the trailer was never recorded in the system.

---

## Conclusion: Is the goods-as-trailers workaround happening NOW?

**No.** As of 2026-06-05, all production orders contain only TRAILER line types.
No goods or components have been sold through the trailer workflow.

The user's concern is a **forward-looking risk**, not a current data problem.

---

## Why this risk exists

The current data model supports:
- `ItemTypeEnum = ('TRAILER', 'COMPONENT')` — two item types in `item.item_type`
- `item.requires_vin` — boolean flag per item
- `CustomerOrderLine.line_type = 'TRAILER'` (hardcoded default)

What is **missing**:
- No `GOODS` or `SERVICE` value in `CustomerOrderLine.line_type`
- No goods/component order line creation UI
- No fulfillment flow for non-trailer items (no warehouse pick, no stock movement)
- No way to sell a single accessory, tent, spare part, or service through an order

When a manager needs to sell a `тент` (tent) or a spare part to a customer today,
they have no supported path. The most likely workaround would be to:
1. Create a dummy `Trailer` record for the item.
2. Process it through the trailer order workflow.
3. Assign a placeholder VIN (since VIN is required for trailer items).
4. "Sell" the item via a trailer order.

This workaround would produce exactly the pattern the user described:
trailer records with placeholder VINs, no real production lifecycle, no real VIN.

---

## Item model analysis

The `Item` model already has infrastructure for non-trailer items:

```python
ItemTypeEnum = Enum('TRAILER', 'COMPONENT', name='item_type')

class Item(db.Model):
    item_type       = db.Column(ItemTypeEnum)      # TRAILER or COMPONENT
    requires_vin    = db.Column(db.Boolean)        # False for components
    is_sellable     = db.Column(db.Boolean)        # True if can be sold
    is_internal_bom_item = db.Column(db.Boolean)  # BOM/assembly only
    component_category   = db.Column(db.String)   # e.g. 'tent', 'wheel'
    is_controlled        = db.Column(db.Boolean)  # controlled stock item
```

The `item.requires_vin` flag is the correct discriminator for VIN guards.

---

## Gap: CustomerOrderLine has no GOODS type

`CustomerOrderLine.line_type` defaults to `'TRAILER'` in both the model
(default) and views.py logic. All existing VIN and production guards check
`if line.line_type != 'TRAILER': return` before proceeding — meaning they
already **skip non-TRAILER lines by design**.

However, there is no:
- UI to create a non-TRAILER order line
- Fulfillment flow for GOODS lines
- Stock movement for GOODS lines
- Warehouse pick logic for GOODS lines

---

## Affected validation points

| Validation | Current behavior | Correct future behavior |
|-----------|-----------------|------------------------|
| VIN required on order | Only checked for TRAILER lines | Unchanged — correct |
| trailer_id required | Only set for TRAILER lines | Unchanged — correct |
| produced_unit creation | Only for TRAILER lines (production source) | Unchanged — correct |
| P0-G1 (trailer_edit guard) | `trailer_edit`/`trailer_create` routes only | Correct — goods never use these routes |
| P0-G2 (production VIN finalization) | Must check `item.requires_vin` or `line_type` | See type-aware revision doc |
| Shipment validation (P0-I) | Must check line_type before requiring trailer_id | Must be type-aware |

---

## Placeholder-VIN repair is still needed (P1-C)

Even though trailer 784 and 987 are genuine trailer records, their placeholder
VINs represent a data quality issue that predates the P0-G1 guard:

| trailer_id | order | status | issue |
|-----------|-------|--------|-------|
| 784 | none | SOLD | Real VIN never recorded; no vin_registry entry |
| 987 | ORD-000027 (sold) | SOLD | Real VIN never recorded; vin_registry confirmed with placeholder |

Repair plan: see
`2026-06-05-trailers-stock-release-vin-reservation-p0h-repair-plan.md` (R2, R3).

---

## Future phases

### P1-A: Goods/component sales model

Define `CustomerOrderLine.line_type` values:
- `TRAILER` — existing; requires VIN, trailer_id, production flow
- `GOODS` — new; requires SKU/article, quantity, warehouse; no VIN, no produced_unit
- `SERVICE` — optional; requires description, price; no warehouse, no VIN

Add `GOODS` and `SERVICE` to the `line_type` column (migration).

### P1-B: Goods/component order line UI

Add ability to create GOODS order lines in the manager order form:
- Select item with `item_type=COMPONENT` or new `GOODS` item type
- Enter quantity, unit price, warehouse
- No VIN, no production release required

### P1-C: Migrate legacy goods records

If goods have been sold as fake trailers before P1-B is deployed:
- Identify them by: placeholder VIN, item name indicating component, no real
  production lifecycle, manual warehouse note
- Convert to GOODS order lines or mark as `is_goods_workaround=True`
- Remove from trailer inventory to avoid stock count distortion

### P0-G (type-aware VIN guards)

All VIN-related guards must check `item.requires_vin` before enforcing:
```python
# Pattern for type-aware guard
item = Item.query.get(produced_unit.item_id)
if item and not item.requires_vin:
    # skip VIN validation — not a VIN-required item
    pass
```

### P0-I: Type-aware shipment guards

When validating `trailer_id IS NOT NULL` before shipping, check:
```python
if line.line_type == 'TRAILER':
    assert line.trailer_id is not None, 'Trailer order line must have trailer_id'
elif line.line_type == 'GOODS':
    assert line.quantity > 0, 'Goods order line must have quantity'
```

---

## Summary

| Finding | Value |
|---------|-------|
| Goods-as-trailers workaround in production NOW | No |
| Placeholder-VIN trailers are real trailers | Yes (trailer 784, 987) |
| Item model has requires_vin flag | Yes |
| Order line model has GOODS line type | No — missing |
| Existing guards already skip non-TRAILER lines | Yes (line_type check in views.py) |
| P0-G2 can proceed safely for TRAILER items | Yes — with item.requires_vin check |
| P0-G1 guard needs updating | No — it targets trailer routes only |
| Placeholder VIN repair still needed | Yes — P1-C, R2/R3 in P0-H plan |
