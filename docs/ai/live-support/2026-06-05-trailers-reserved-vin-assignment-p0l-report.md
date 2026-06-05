# P0-L: Reserved VIN Assignment Flow Fix

**Date:** 2026-06-06
**Branch:** crm-roles-production-logistics
**Status:** IMPLEMENTED

---

## Problem

### Bug 1: `logistics_assign_vin` — only free VINs shown

When a produced_unit had a reserved VIN for its supply_need/order_line, the
`_vin_registry_for_order_or_need(order, need)` function found the reserved row
(via `customer_order_id` / `supply_need_id`), so `reserved_vin_row` was set and
`vin_options = [reserved_vin_row]` (1 option, auto-selected).

**However**, if the function returned `None` (edge case: VIN reserved only via
`order_line_id` with no matching `customer_order_id` / `supply_need_id`), the
fallback showed only `status='free'` VINs — completely hiding the reserved VIN.

Additionally the submit path used `reserved_vin_row or form.vin_registry_id.data`,
meaning the form selection was ignored when `reserved_vin_row` was set. This
prevented choosing a free VIN as an alternative.

### Bug 2: `_stock_replenishment_stats` — wrong classification

```python
# OLD — incorrect
needs_vin_units = [
    unit for unit in units
    if unit.status == 'produced_no_vin' and not unit.order_id and not unit.order_line_id
]
```

Units with `order_line_id` set (customer-order production runs) were excluded
from `needs_vin_units` and added to `problem_units`. The template then showed
"Удалить выпуск" instead of "Назначить VIN" — wrong action for a valid
`produced_no_vin` unit.

### Bug 3: Template — no VIN assignment action for problem units

Even in `problem_units` section, units with `status='produced_no_vin'` showed
only "Удалить выпуск" (or "Нужна очистка директором"). No link to assign VIN.

---

## Fix

### views.py — `_stock_replenishment_stats` (line ~8710)

Changed `needs_vin_units` filter:

```python
# NEW — correct
needs_vin_units = [
    unit for unit in units
    if unit.status == 'produced_no_vin' and not unit.order_id
    # P0-L: order_line_id is OK — it means the unit is for a customer-order
    # production run and needs VIN assignment, not deletion.
]
```

Effect: produced_units with `order_line_id` set are now classified as
`needs_vin_units` and get the "Присвоить VIN" button in the template.

### views.py — `logistics_assign_vin` VIN candidate logic (lines ~13684–13729)

Replaced the `if reserved_vin_row / else free-only` block with a two-stage
candidate builder:

**Stage 1: Context-reserved VINs (priority)**
- If `_produced_unit_context` found `reserved_vin_row` → use it as context VIN.
- Fallback: search by `order_line_id` for `status IN ('reserved', 'assigned')` +
  `trailer_id IS NULL`. This catches VINs reserved via `order_line_id` when the
  `customer_order_id` / `supply_need_id` lookup returned nothing.

**Stage 2: Free VINs (fallback)**
- `status='free'`, `trailer_id IS NULL`, no existing trailer with same VIN.
- Filtered by `vin_modification_code` if `expected_modification` is known.
- Limited to 50 most recent.

**Combined `vin_options`:** context VINs first, then free VINs (deduped by id).

**Choice labels:**
- `[Под этот заказ] MX4000002T0002708` for context/reserved VINs
- `[Свободный] MX4000002T0002566` for free VINs

**Submit validation:**
- Changed to always use `form.vin_registry_id.data` (not forced `reserved_vin_row`).
- New guard: if selected VIN has `status='reserved'` but is NOT in `context_vin_ids`,
  reject with clear error "Этот VIN зарезервирован под другой заказ".

### assign_vin_form.html

- Title/header: "Привязать VIN" when context VIN exists, else "Присвоить VIN".
- Info banner: shows "Зарезервированный VIN выбран автоматически" when context
  VIN exists; shows modification hint when only free VINs available.
- Empty state message distinguishes:
  - "Зарезервированный VIN недоступен..." (when `reserved_vin_row` was found but
    exhausted — e.g., already linked or void)
  - "Нет доступных VIN в реестре для этого заказа и модификации..." (general empty)

### stock_replenishment_list.html

Added "Назначить VIN" action for `problem_units` with `status='produced_no_vin'`:

```jinja
{% if problem_unit.status == 'produced_no_vin' %}
  <a class="btn btn-sm btn-orange" href="{{ url_for(...assign_vin...) }}">Назначить VIN</a>
{% elif current_user.is_admin or current_user.is_director %}
  <form ...delete...>
```

This ensures `produced_no_vin` problem units always show a safe forward action
instead of "Удалить выпуск".

---

## VIN Candidate Logic

| Case | VIN source | Shown in dropdown |
|------|-----------|-------------------|
| VIN reserved for same order/need (via `customer_order_id` / `supply_need_id`) | `_vin_registry_for_order_or_need` | `[Под этот заказ]` first |
| VIN reserved only via `order_line_id` | fallback `order_line_id` query | `[Под этот заказ]` first |
| VIN free, matching modification | `status='free'` query | `[Свободный]` after |
| VIN reserved for another order | excluded from candidates | Never shown; rejected if tampered |
| VIN already has trailer | excluded | Never shown |

---

## Files Changed

| File | Change |
|------|--------|
| `views.py` | `_stock_replenishment_stats` filter fix + `logistics_assign_vin` VIN candidate logic + submit path |
| `templates/assign_vin_form.html` | Labels, info messages, empty state |
| `templates/stock_replenishment_list.html` | "Назначить VIN" for `produced_no_vin` problem units |

---

## Impact on VINs 2659 / 2708

Both `produced_unit id=70` (VIN 2659) and `produced_unit id=80` (VIN 2708) will
now correctly show their reserved VINs in the assignment form:

- `unit.order_line_id` is set → triggers fallback `order_line_id` query if needed
- The reserved VIN row (`vr_id=979` / `vr_id=1041`) is found and labeled
  `[Под этот заказ]`
- User selects it → trailer created atomically (P0-G2 guard active)
- All links set: `trailer.id`, `produced_unit.trailer_id`, `vin_registry.trailer_id`,
  `order.trailer_id`, `order_line.trailer_id`

---

## Checks

```
python -m py_compile views.py   → SYNTAX OK
git diff --check                → OK (no whitespace errors)
application code changed:       yes (views.py)
production data changed:        no
migrations changed:             no
```
