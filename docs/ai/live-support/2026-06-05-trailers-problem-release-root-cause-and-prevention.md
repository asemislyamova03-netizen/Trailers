# Problem Release Root Cause and Prevention Plan

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Status:** Documentation only — no code or data changed
**Scope:** Stock replenishment and production release integrity

---

## 1. Why Problem Releases Appeared

### Root cause A — Missing warehouse guard in production release code

The production release path (the route that sets
`produced_unit.status = 'vin_assigned'`) did not verify that
`production_warehouse` was available before continuing.

When `production_warehouse` was `None` (e.g., because the production request
line had no warehouse context, or the warehouse was deleted/deactivated):

1. The code continued past the warehouse lookup without aborting.
2. A partial database state was written (produced_unit status changed to
   `vin_assigned`).
3. The code then failed before writing `produced_unit.trailer_id`.
4. The transaction was left in a partially committed state.
5. Result: `produced_unit.status = 'vin_assigned'` AND `trailer_id = NULL`.

**Repaired case:** produced_unit id=27 → trailer_id set to 1001 (P0-FC, commit 0cdb758).

---

### Root cause B — No atomic commit wrapping trailer creation + link writing

The production VIN finalization path creates a `Trailer` record and then links
it back to `produced_unit.trailer_id`, `vin_registry.trailer_id`, and
`customer_order_line.trailer_id` as separate operations.

If any step between `Trailer` creation and the link-writing commits failed
(exception, missing FK, flush error), the trailer could exist in the database
without being linked — or the produced_unit could be marked `vin_assigned`
without a trailer.

There was no single rollback point covering the entire "create trailer + assign
VIN + update produced_unit" sequence.

---

### Root cause C — No application-level VIN uniqueness/validity guard on trailer edit

The `trailer_edit` and `trailer_create` routes allowed direct writes to
`trailer.vin` without checking:
- Duplicate VINs against other Trailer records.
- Duplicate VINs against VinRegistry entries.
- Placeholder VINs (e.g., `MX4000002T0000000`, all-zeros serial).
- Minimum VIN length.

This allowed production operators to save trailers with placeholder or
duplicate VINs. Over time this created confirmed VIN registry entries with
placeholder values.

**Fixed by:** P0-G1 VIN edit guard (commit 6a8df58).

---

### Root cause D — Goods/component sales not implemented; future workaround risk

The current order system only supports `line_type = 'TRAILER'`. There is no
`GOODS` or `COMPONENT` order line type, no goods fulfillment flow, and no UI
to sell non-trailer items (accessories, spare parts, services) through an order.

**Current state:** As of 2026-06-05, all 79 production order lines are type
`TRAILER`. The single `COMPONENT` item in the catalog (id=497, тент) is not
linked to any order.

**Future risk:** When managers need to sell non-trailer goods, they may create
a fake trailer record with a placeholder VIN and process it through the
trailer order workflow. This would produce records that look like data
corruption but are actually intentional goods sales entries.

VIN guards must be **type-aware** (`item.requires_vin` check) so they do not
block these future cases.

---

## 2. Valid States

### VALID-1: Free VIN without trailer

```
vin_registry.status = 'free'
vin_registry.trailer_id = NULL
vin_registry.customer_order_id = NULL
vin_registry.supply_need_id = NULL
```

A VIN number allocated to the VIN pool but not yet reserved or assigned.
This is the standard initial state for all VIN inventory.

---

### VALID-2: Reserved VIN without trailer (before production release)

```
vin_registry.status = 'reserved'
vin_registry.trailer_id = NULL
vin_registry.customer_order_id = <order_id>   -- or supply_need_id
produced_unit.status IN ('produced_no_vin', 'produced_waiting_vin')
   OR produced_unit does not exist yet
```

A VIN reserved for a specific order or supply need, but the physical trailer
has not been produced/released yet. The absence of `trailer_id` is correct
here — the physical asset does not exist yet.

---

### VALID-3: Goods/components without VIN or trailer

```
customer_order_line.line_type = 'GOODS'   -- (future, not yet in schema)
item.requires_vin = False
trailer_id = NULL
vin_registry_id = NULL
produced_unit = NULL
```

A sales order line for a non-trailer item (tent, spare part, accessory, service).
VIN and produced_unit are not applicable. This state does not exist in the
current schema (no GOODS line type), but guards must accommodate it
**before** goods sales is implemented to avoid blocking it.

---

### VALID-4: VIN confirmed with trailer after production release

```
vin_registry.status = 'confirmed'
vin_registry.trailer_id = <trailer_id>
produced_unit.status = 'vin_assigned'
produced_unit.trailer_id = <same trailer_id>
customer_order_line.trailer_id = <same trailer_id>
```

The fully completed production release state. All links are intact.
This is the target state after a successful production VIN finalization.

---

## 3. Invalid States

### INVALID-P0-1: produced_unit released but trailer_id NULL

```
produced_unit.status IN ('vin_assigned', 'done', 'released', 'completed')
produced_unit.trailer_id = NULL
```

The produced_unit claims a VIN was assigned and the unit is complete, but
no physical trailer record is linked. This means the VIN assignment
partially succeeded and the transaction was left in a broken state.

**Current count (post P0-FC):** 0.

---

### INVALID-P0-2: vin_registry confirmed but trailer_id NULL

```
vin_registry.status IN ('confirmed', 'assigned')
vin_registry.trailer_id = NULL
```

The VIN registry entry is marked as physically applied to a trailer, but
there is no trailer record linked. Implies a failed or partial production
release.

**Current count:** 0.

---

### INVALID-P0-3: Order sold/shipped with VIN but no trailer_id

```
customer_order.status IN ('sold_not_shipped', 'shipped', 'done', 'customer_shipped')
customer_order.trailer_id = NULL
customer_order_line.vin_registry_id IS NOT NULL
customer_order_line.trailer_id = NULL
```

Documents were issued or the order was shipped, but no physical trailer
is linked. The customer received (or is about to receive) documents for
a trailer that cannot be traced in the system.

**Current count:** 1 (ORD-000078 — active workflow, docs issued, production
not yet started; see P0-H repair plan for monitoring instructions).

---

### INVALID-P1-C: SOLD trailer with placeholder VIN

```
trailer.vin GLOB '*0000000'   -- or all-zeros variant
trailer.status = 'SOLD'
```

A real trailer (item_type=TRAILER, requires_vin=1) was sold but the
physical VIN stamped on it was never recorded. The placeholder VIN is
not legally or technically valid for vehicle registration.

**Current count:** 2 (trailer 784, trailer 987).

---

## 4. Why "Удалить выпуск" Is Unsafe for Sold/Reserved Releases

The "delete release" action (удалить выпуск) is appropriate only when:
- The produced_unit is in an early state (`produced_no_vin`).
- No VIN has been assigned or confirmed.
- No order or reservation is linked.

It becomes unsafe when:
- `vin_registry.status = 'confirmed'` — the VIN is officially recorded.
- `customer_order.documents_issued = 1` — documents were issued to the customer.
- `customer_order.status` is `sold_not_shipped` or later.
- A `reservation` record exists for this produced_unit.
- The produced_unit has been physically shipped or delivered.

**Consequences of unsafe delete:**
- Customer has documents for a trailer that no longer exists in the system.
- VIN registry becomes orphaned (confirmed VIN with no trailer).
- Reservation record left dangling (blocking the order from completion).
- Stock counts become incorrect (trailer removed from inventory while order
  still references it).
- Legal risk: vehicle registration documents reference a VIN with no system record.

**Safe alternative:** Repair the missing links (restore `trailer_id` on
produced_unit, vin_registry, and order_line) rather than deleting.

---

## 5. Code Changes Required to Prevent Recurrence

### P0-G2: Production VIN finalization guard

**Target routes in views.py:**
- `logistics_assign_vin` (line ~13659)
- Stock replenishment release path
- Any route that sets `produced_unit.status = 'vin_assigned'`

**Guard logic (pseudocode):**
```python
# Before finalizing VIN assignment:

# 1. Verify item requires VIN (skip for COMPONENT/GOODS items)
item = Item.query.get(unit.item_id)
if not item or not item.requires_vin:
    # No VIN guard needed — goods/component item
    pass
else:
    # 2. Verify produced_unit exists and is in pre-final state
    assert unit.status in ('produced_no_vin', 'produced_waiting_vin')
    assert unit.trailer_id is None, 'produced_unit already has trailer_id'

    # 3. Verify warehouse exists
    warehouse = unit.target_warehouse or production_warehouse
    if warehouse is None:
        db.session.rollback()
        flash('Склад производства не найден. Выпуск не сохранён.', 'danger')
        return redirect(safe_page)

    # 4. Verify VIN registry row
    assert vin_row is not None
    assert vin_row.status not in ('void', 'confirmed')

    # 5. Verify VIN not already linked to another trailer
    if vin_row.trailer_id and vin_row.trailer_id != unit.trailer_id:
        flash('VIN уже привязан к другому прицепу.', 'danger')
        return redirect(safe_page)

    # 6. Create/find trailer FIRST, then write all links atomically
    try:
        trailer = _create_or_find_trailer(vin_row, warehouse, item)
        unit.trailer_id = trailer.id
        unit.status = 'vin_assigned'
        vin_row.trailer_id = trailer.id
        vin_row.status = 'confirmed'
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Ошибка при выпуске. Все изменения отменены.', 'danger')
        return redirect(safe_page)
```

**Type-aware pattern:**
```python
def _vin_required_for_unit(unit: ProducedUnit) -> bool:
    if not unit.item_id:
        return True  # conservative default for trailer items
    item = Item.query.get(unit.item_id)
    return bool(item and item.requires_vin)
```

---

### P0-J: Stock replenishment problem classifier / resolver

A management UI or admin route that:
- Lists all `produced_unit` records where `status = 'vin_assigned'`
  AND `trailer_id IS NULL` (should be 0 after P0-G2).
- Lists all `produced_unit` records where `status = 'produced_no_vin'`
  and has been in that state > N days.
- For each problematic record, offers:
  - "Привязать к существующему прицепу" — if the right trailer can be identified.
  - "Создать прицеп и привязать" — if no trailer exists yet.
  - "Отменить выпуск" — only if no order/reservation/docs are linked.

This addresses the operational resolution flow without requiring manual
SQL repairs.

---

### P0-I: Shipment guard (type-aware)

Before marking an order as shipped or issuing documents:
```python
for line in order.lines:
    if line.line_type == 'TRAILER':
        if line.trailer_id is None:
            flash(f'Позиция {line.line_no}: прицеп не выбран. Отгрузка невозможна.', 'danger')
            return redirect(safe_page)
    elif line.line_type == 'GOODS':
        if not line.quantity or line.quantity <= 0:
            flash(f'Позиция {line.line_no}: количество не указано.', 'danger')
            return redirect(safe_page)
```

Currently, the shipment route does not enforce `trailer_id IS NOT NULL` for
TRAILER lines before setting `order.is_shipped = 1`.

---

### P1-A: Goods/component sales flow

**New model elements needed:**
- `CustomerOrderLine.line_type` values: add `'GOODS'`, optionally `'SERVICE'`
- `Item.item_type` values: possibly add `'GOODS'` alongside `'TRAILER'` and `'COMPONENT'`
- Fulfillment flow for GOODS lines: warehouse pick → stock decrement → ship
- No VIN, no produced_unit, no production release required

**Migration scope:** Add `'GOODS'` and `'SERVICE'` to the `line_type` column
(currently a VARCHAR(30) with no enum constraint in SQLite — migration is
straightforward).

---

### P1-B: Type-aware order line creation UI

Manager order form gains a "Добавить позицию" dropdown with options:
- Прицеп (TRAILER) — existing flow
- Товар/комплектующее (GOODS) — new flow with SKU, quantity, price

Until P1-B is deployed, managers have no valid path for selling non-trailer
goods and may resort to the placeholder-VIN workaround.

---

## 6. Existing Changes That Already Help

| Change | Commit | What it prevents |
|--------|--------|-----------------|
| P0-FC data repair | 0cdb758 | Resolved the only known produced_unit with `vin_assigned` and `trailer_id=NULL` |
| P0-G1 VIN edit guard | 6a8df58 | Prevents new placeholder/duplicate VINs via `trailer_edit` and `trailer_create` routes |
| P0-H audit script | 1397f41 | Reusable monitor for VALID-A/B/C vs P0 states across all vin_registry rows |

---

## 7. Problems That Can Still Appear Until Further Changes Are Implemented

| Problem | Until fixed by | Risk level |
|---------|---------------|------------|
| Production release with missing warehouse → partial `vin_assigned` state | P0-G2 | P0 — can create broken produced_unit |
| Non-atomic trailer + produced_unit + vin_registry commit | P0-G2 | P0 — can leave partial links |
| Order marked shipped without `trailer_id` on TRAILER lines | P0-I | P0 — docs issued for untracked trailer |
| Goods sold via fake trailer + placeholder VIN | P1-A + P1-B | P1 → escalates to P0 if docs issued |
| Existing placeholder VINs on trailer 784 and 987 | P1-C (manual repair) | P1 — legal/registration risk |
| ORD-000078 `later` order stalled without production | Operational + P0-J | P1 → P0 if remains unresolved |

---

## 8. Recommended Implementation Order

```
Priority  Change   Description                                  Scope
--------  -------  -------------------------------------------  ---------
P0        P0-G2    Production VIN finalization guard            views.py only
P0        P0-I     Type-aware shipment guard                    views.py only
P1        P1-C     Placeholder VIN repair (trailer 784, 987)    data repair script
P1        P0-J     Stock replenishment problem resolver UI       views.py only
P1        P1-A     Goods sales data model                       models.py + migration
P1        P1-B     Goods order line creation UI                 views.py + templates
P2        P1-C+    Migrate historical goods-as-trailers (if any) data migration
```

### Why this order

1. **P0-G2 first:** Closes the production release vulnerability. Even one new
   problem release requires a manual SQL repair (P0-FC pattern).
2. **P0-I second:** Closes the shipment path. Currently an order can be marked
   shipped without a trailer_id on TRAILER lines.
3. **P1-C:** The two placeholder VINs are legal risks but not system blockers.
   They can be fixed independently when the real VINs are confirmed.
4. **P0-J:** Operational UX improvement. Until this exists, problem releases
   require manual SQL or admin database access to resolve.
5. **P1-A/B:** Structural improvement. Must be done before goods sales demand
   forces managers into the workaround pattern. Not urgent today (0 cases),
   but becomes urgent the first time a manager needs to sell an accessory.

---

## Related documents

- `2026-06-05-trailers-vin-production-root-cause-p0e.md` — P0-E root cause for produced_unit id=27
- `2026-06-05-trailers-produced-unit-27-repair-p0fc-apply-report.md` — P0-FC repair report
- `2026-06-05-trailers-vin-edit-guard-p0g1-report.md` — P0-G1 guard report
- `2026-06-05-trailers-stock-release-vin-reservation-p0h-report.md` — P0-H audit results
- `2026-06-05-trailers-stock-release-vin-reservation-p0h-repair-plan.md` — P0-H repair plan
- `2026-06-05-trailers-goods-sales-workaround-p1a-discovery.md` — P1-A goods sales discovery
