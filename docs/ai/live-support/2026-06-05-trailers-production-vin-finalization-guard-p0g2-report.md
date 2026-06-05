# P0-G2: Production VIN Finalization Guard — Implementation Report

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**File changed:** `views.py`
**Status:** IMPLEMENTED AND COMMITTED

---

## Context

P0-E root cause analysis identified that `produced_unit id=27` reached the state
`status='vin_assigned'` with `trailer_id=NULL` because the production VIN
finalization path did not have an atomic rollback wrapper around trailer
creation and link writing.

P0-FC repaired the single production data case.
P0-G1 guarded `trailer_edit` and `trailer_create` from placeholder/duplicate VINs.
P0-G2 closes the root cause B vulnerability in the production release path itself.

---

## Route Audited

### `logistics_assign_vin` (views.py ~line 13659)

The only production route that:
1. Creates a new `Trailer` record from a VIN registry entry.
2. Sets `produced_unit.status = 'vin_assigned'`.
3. Sets `produced_unit.trailer_id`, `vin_registry.trailer_id`, and
   `customer_order_line.trailer_id` in a single session.

### `_attach_produced_unit_to_existing_trailer` (views.py ~line 7209)

Helper for linking a produced_unit to an *existing* trailer.
Has pre-condition checks. Not currently called from any route — no changes needed.

### `vin_registry_assign`, `vin_registry_confirm` (views.py ~lines 11587, 11625)

These routes operate on existing trailers (trailer already exists, trailer.id
already known). They cannot create the broken state.
No changes needed.

---

## Existing Guards (already present before P0-G2)

| Guard | Location | Effect |
|-------|----------|--------|
| `unit.status != 'produced_no_vin'` | line 13690 | Rejects already-finalized units |
| `production_warehouse is None` check | line 13698 | Rejects missing warehouse with flash |
| VIN registry status/vin_full check | line 13714 | Rejects unavailable VIN row |
| `existing_trailer` VIN duplicate check | line 13722 | Rejects VIN already on another trailer |

---

## What P0-G2 Adds

### 1. Atomic try/except wrapper

All operations from `Trailer()` creation through `db.session.commit()` are now
wrapped in a single `try/except` block:

```python
try:
    trailer = Trailer(...)
    db.session.add(trailer)
    db.session.flush()            # assigns trailer.id
    _finish_idempotency(...)
    unit.status = 'vin_assigned'
    unit.trailer_id = trailer.id
    # ... all link writes ...
    vin_registry_row.trailer_id = trailer.id
    # ...
    db.session.commit()
except Exception:
    db.session.rollback()
    flash('Ошибка при выпуске VIN. Все изменения отменены...', 'danger')
    return render_template('assign_vin_form.html', ...)
```

**Effect:** Any exception between `Trailer()` creation and `commit()` —
including FK violations, unique constraint errors, unexpected AttributeError,
network/DB timeout — triggers a full rollback. The `produced_unit` cannot
reach `status='vin_assigned'` in the database without `trailer_id` being set.

### 2. Pre-commit integrity assertion

Immediately before `db.session.commit()`:

```python
if unit.trailer_id is None or vin_registry_row.trailer_id is None:
    raise RuntimeError('P0-G2: trailer_id link missing before commit')
```

**Effect:** If the link-writing logic ever skips setting `trailer_id` due to
a future code change or edge case, this assertion fires and triggers the
`except` branch, preventing a broken commit.

---

## What P0-G2 Does NOT Change

| Aspect | Decision |
|--------|---------|
| Success path behavior | Unchanged — same operations, same order |
| Existing validation guards | Unchanged — still present |
| VIN reservation without trailer (VALID-B) | Not affected — VIN reservation does not go through this route |
| Free VIN inventory (VALID-A) | Not affected |
| COMPONENT/GOODS items | Not applicable — all current items are TRAILER type; `requires_vin` is True for all |
| Templates | Not changed |
| Models / migrations | Not changed |
| `vin_registry_assign`, `vin_registry_confirm` routes | Not changed |

---

## VIN reservation without trailer remains allowed

The guard blocks only the **finalization** path in `logistics_assign_vin`.
VIN rows with `status='reserved'` and `trailer_id=NULL` continue to be
created freely by the reservation flow. This is the correct VALID-B state.

---

## produced_unit vin_assigned without trailer_id is now blocked

After P0-G2:
- If `db.session.flush()` fails → exception → rollback → `unit.status` never set
- If any link write fails → exception → rollback → `unit.status` rolled back
- If pre-commit assertion fires → exception → rollback → commit never happens
- If `db.session.commit()` fails → exception → rollback → no partial state in DB

The state `produced_unit.status='vin_assigned' AND trailer_id=NULL` cannot be
created by `logistics_assign_vin` after this change.

---

## Validation Checks Run

```
python -m py_compile views.py   → SYNTAX OK
git diff --check                → OK (no whitespace errors)
ReadLints views.py              → No linter errors
```

---

## Diff Summary

```
views.py | 101 lines changed (56 insertions, 45 deletions)
```

The 45 deletions + 56 insertions reflect re-indentation of existing code inside
the new `try:` block plus the 9 new lines (comment, try, assertion, except,
rollback, flash, return).

No other files were changed.

---

## Next Safe Steps

| Priority | Change | Description |
|----------|--------|-------------|
| P0 | P0-I | Shipment guard: check `trailer_id IS NOT NULL` for TRAILER lines before marking order shipped |
| P1 | P1-C | Repair placeholder VINs on trailer 784 and 987 (manual, when real VINs are known) |
| P1 | P0-J | Stock replenishment problem resolver UI in views.py |
| P1 | P1-A/B | Goods/component sales model and order line UI |
