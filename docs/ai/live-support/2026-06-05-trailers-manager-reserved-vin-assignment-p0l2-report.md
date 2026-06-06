# P0-L2: Manager Reserved VIN Assignment Flow

**Date:** 2026-06-06
**Branch:** crm-roles-production-logistics
**Status:** IMPLEMENTED

---

## Problem

P0-L fixed VIN candidate selection in `logistics_assign_vin` route
(`/logistics/produced-units/<id>/assign-vin`), but:

1. **Role confusion**: Logistics users saw a "Привязать VIN" / "Присвоить VIN"
   button in `logistics_workspace.html` that links to a route they cannot access
   (403). The route has always been `@role_required('manager', 'director')`.

2. **Not accessible from order detail**: For orders ORD-000032 and ORD-000076
   (`produced_waiting_vin`, `documents_issued=1`), the normal VIN attach flows
   in `order_detail.html` are ALL blocked by `and not order.documents_issued`.
   There was no path for the manager to assign the reserved VIN to the produced
   unit from the order detail page.

3. **Not visible in stock replenishment**: VINs 2659/2708 belong to CUSTOMER_ORDER
   supply_needs (not STOCK_REPLENISHMENT), so they never appeared in
   `/stock-replenishment`. Managers had no way to see them from that view.

---

## Business Rule

| Role | Can upload/issue VINs to registry | Can link/confirm VIN to trailer |
|------|-----------------------------------|---------------------------------|
| Logistics | YES | NO |
| Manager | NO | YES |
| Director | NO | YES |
| Admin | NO | YES |

The `logistics_assign_vin` route was already correctly restricted to
`@role_required('manager', 'director')`. P0-L2 enforces this at the UI level.

---

## Changes

### 1. `logistics_workspace.html` — Hide VIN assign button from logistics

```jinja
{# BEFORE #}
<a ...>{{ 'Привязать VIN' if row.vin_to_apply else 'Присвоить VIN' }}</a>

{# AFTER #}
{% elif current_user.is_manager or current_user.is_director or current_user.is_admin %}
  <a ...>{{ 'Привязать VIN' if row.vin_to_apply else 'Присвоить VIN' }}</a>
{% else %}
  <span class="text-muted small">Менеджер / директор назначит VIN</span>
{% endif %}
```

Logistics users now see an informational note instead of a dead link.

### 2. `views.py` — Add `vin_assign_unit` to order_detail line context

In the `order_detail` view, for each order line, added a lookup for
`vin_assign_unit` — the `ProducedUnit` with `status='produced_no_vin'` linked to
this order line, visible ONLY when:

- User is manager/director/admin (NOT logistics)
- Line `fulfillment_source == 'production'`
- Order `status == 'produced_waiting_vin'`
- Order is not shipped
- Line has no trailer_id yet

```python
vin_assign_unit = None
if (
    (current_user.is_manager or current_user.is_director or current_user.is_admin)
    and not getattr(current_user, 'is_logistics', False)
    and (line.fulfillment_source or '').lower() == 'production'
    and order.status == 'produced_waiting_vin'
    and not order.is_shipped
    and not line.trailer_id
):
    vin_assign_unit = ProducedUnit.query.filter_by(
        order_line_id=line.id,
        status='produced_no_vin',
        trailer_id=None,
    ).first()
```

### 3. `order_detail.html` — Show "Привязать VIN к выпуску" for manager

Added a new section in the order line block that appears when `row.vin_assign_unit`
is set:

```html
{% if row.vin_assign_unit %}
<div class="border-top pt-2">
  <div class="small fw-semibold mb-1 text-warning">Выпуск готов — нужно привязать VIN</div>
  <div class="d-flex gap-2 align-items-center flex-wrap">
    <span class="small text-muted">Единица #N: выпущена без VIN, документы уже выданы.</span>
    <a class="btn btn-sm btn-orange" href="{{ url_for('main.logistics_assign_vin', ...) }}">Привязать VIN к выпуску</a>
  </div>
</div>
{% endif %}
```

### 4. `views.py` + `stock_replenishment_list.html` — Manager production VIN pending section

Added `customer_order_vin_pending` to the stock_replenishment_list view:
- Queries `ProducedUnit` with `status='produced_no_vin'`, `trailer_id=NULL`,
  `order_line_id` set
- Filters to orders with `status='produced_waiting_vin'`, not shipped
- Shown at the bottom of `/stock-replenishment` as a separate table
- Only visible to manager/director/admin
- Each row shows: order number → link to order_detail, line number, unit id,
  reserved VIN (if any), and "Привязать VIN" button → `logistics_assign_vin`
- Section header explicitly states: "Логист НЕ выполняет это действие."

---

## VIN Assign Action Accessibility

| UI location | Visible to | Condition |
|-------------|-----------|-----------|
| `order_detail.html` | manager/director/admin | order=`produced_waiting_vin`, line has `produced_no_vin` unit |
| `/stock-replenishment` (new section) | manager/director/admin | Any `produced_no_vin` unit with `order_line_id` for `produced_waiting_vin` order |
| `logistics_workspace.html` | manager/director/admin only | Was visible to logistics — now hidden |
| `stock_replenishment_list.html` (problem section, P0-L) | manager/director/admin | STOCK_REPLENISHMENT supply_need with `produced_no_vin` problem unit |
| `manager_workspace.html` | manager | Existing — not changed |
| `production_request_detail.html` | All with access | Existing — not changed |

---

## Impact on VINs 2659 / 2708

Manager navigates to `/stock-replenishment` → sees table "Производственные заказы
— ожидают привязки VIN" with rows for ORD-000032 / ORD-000076 → clicks
"Привязать VIN" → opens `logistics_assign_vin` form → reserved VIN shown with
label `[Под этот заказ]` (P0-L candidate logic) → assigns VIN → P0-G2 guard
commits all links atomically.

Alternatively, manager opens ORD-000032 or ORD-000076 order detail → sees
"Выпуск готов — нужно привязать VIN" section → clicks "Привязать VIN к выпуску".

---

## Files Changed

| File | Change |
|------|--------|
| `views.py` | `stock_replenishment_list` — added `customer_order_vin_pending` |
| `views.py` | `order_detail` — added `vin_assign_unit` to line context |
| `templates/order_detail.html` | Added VIN assign section for `produced_waiting_vin` |
| `templates/stock_replenishment_list.html` | Added pending VIN table at bottom |
| `templates/logistics_workspace.html` | Hid VIN assign button from logistics |

## Checks

```
python -m py_compile views.py  → SYNTAX OK
git diff --check               → OK
migrations changed:            no
requirements changed:          no
application code changed:      yes (views.py)
production data changed:       no
```
