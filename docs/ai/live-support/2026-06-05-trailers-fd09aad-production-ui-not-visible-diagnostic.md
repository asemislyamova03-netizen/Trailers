# Diagnostic: fd09aad production UI not visible
**Date:** 2026-06-08
**Branch:** crm-roles-production-logistics
**Reporter:** Production deploy verification auditor

---

## 1. Server git state

| Check | Result |
|---|---|
| Server HEAD | `b0bf1285f05f0cccb1709b927ddd6662c8365468` (b0bf128) |
| fd09aad present on server | **YES** — 4th commit back in log |
| Commits ahead of fd09aad on server | `28b09f9` Show manager pending VIN assignment tasks / `b0bf128` Allow sales from production warehouse stock |
| Branch | crm-roles-production-logistics |
| Working tree dirty | `M wsgi.py` only — expected (ProxyFix customization for nginx, not in repo) |

**fd09aad is on the server. Two additional commits (28b09f9, b0bf128) were deployed on top of it.**

---

## 2. Server file strings — all present

| String | File | Line | Found |
|---|---|---|---|
| `Производственные заказы — ожидают привязки VIN` | stock_replenishment_list.html | 225 | **YES** |
| `customer_order_vin_pending` | stock_replenishment_list.html | 223 | **YES** |
| `Привязать VIN к выпуску` | order_detail.html | 278 | **YES** |
| `Менеджер / директор назначит VIN` | logistics_workspace.html | 148 | **YES** |
| `context_vin_ids` | assign_vin_form.html | 2 | **YES** |
| `vin_assign_unit` | views.py | 9257+ | **YES** |
| `customer_order_vin_pending` | views.py | 1710, 8820 | **YES** |
| `_customer_order_vin_pending_rows` | views.py | 8774 | **YES** |

---

## 3. Service status

| Check | Result |
|---|---|
| Service name | trailers.service |
| Status | **active (running)** |
| Start time | Mon 2026-06-08 08:05:48 UTC |
| b0bf128 commit time | Mon 2026-06-08 08:00:38 UTC |
| Service started AFTER last deploy | **YES** (+5 minutes) |
| Working directory | `/home/ubuntu/Trailers` |
| gunicorn command | `venv_trailers/bin/gunicorn --workers 2 --threads 4 --bind 127.0.0.1:8003 --timeout 120 wsgi:application` |
| Service directory matches git pull | **YES** |

---

## 4. Logs

No errors since service restart at 08:05 UTC Jun 8. No 500 errors. No template errors.
Previous error from Jun 5 (`jinja2.exceptions.TemplateSyntaxError` in `inventory_balances_list.html`) is resolved.

---

## 5. Production DB — unit 70 and 80

### Produced unit 70 (VIN 2659 / ORD-000032)

| Field | Value |
|---|---|
| id | 70 |
| status | `produced_no_vin` ✓ |
| trailer_id | NULL ✓ |
| order_line_id | 32 ✓ |
| order_id | NULL (linked via order_line) |
| production_request_line_id | 19 |
| order_line.fulfillment_source | `production` ✓ |
| order_line.trailer_id | NULL ✓ |
| order.order_number | ORD-000032 |
| order.status | `produced_waiting_vin` ✓ |
| order.is_shipped | 0 ✓ |
| order.documents_issued | 1 |
| VIN registry id | 979 |
| VIN full | `MX4000002T0002659` |
| VIN status | `reserved` |
| VIN trailer_id | NULL |
| supply_need_id | 21 (CUSTOMER_ORDER, READY) |

### Produced unit 80 (VIN 2708 / ORD-000076)

| Field | Value |
|---|---|
| id | 80 |
| status | `produced_no_vin` ✓ |
| trailer_id | NULL ✓ |
| order_line_id | 78 ✓ |
| order_id | NULL (linked via order_line) |
| production_request_line_id | 66 |
| order_line.fulfillment_source | `production` ✓ |
| order_line.trailer_id | NULL ✓ |
| order.order_number | ORD-000076 |
| order.status | `produced_waiting_vin` ✓ |
| order.is_shipped | 0 ✓ |
| order.documents_issued | 1 |
| VIN registry id | 1041 |
| VIN full | `MX4000002T0002708` |
| VIN status | `reserved` |
| VIN trailer_id | NULL |
| supply_need_id | 77 (CUSTOMER_ORDER, READY) |

**Note:** Unit 81 also exists for ORD-000076 / order_line 78 with same status (duplicate unit, known P0-K issue).

---

## 6. Code conditions vs DB values

### stock_replenishment_list — `customer_order_vin_pending` section

**Query simulation result (run directly on production DB):**

```
Total rows: 3
unit_id=81, ORD-000076, produced_waiting_vin, is_shipped=0  ← VIN 2708
unit_id=80, ORD-000076, produced_waiting_vin, is_shipped=0  ← VIN 2708
unit_id=70, ORD-000032, produced_waiting_vin, is_shipped=0  ← VIN 2659
```

All 3 units pass all filter conditions. `customer_order_vin_pending` **will be non-empty** when the view is called.

### order_detail — `vin_assign_unit` condition for ORD-000032 (line 32)

```python
(is_manager or is_director or is_admin)         # True for manager users
and not is_logistics                             # True for manager role
and fulfillment_source == 'production'           # True: 'production'
and order.status == 'produced_waiting_vin'       # True: 'produced_waiting_vin'
and not order.is_shipped                         # True: is_shipped=0
and not line.trailer_id                          # True: trailer_id=None
```
→ `vin_assign_unit = produced_unit(id=70)` — **condition passes**.

### order_detail — `vin_assign_unit` condition for ORD-000076 (line 78)

Same analysis → `vin_assign_unit = produced_unit(id=80)` — **condition passes**.

### User roles

| Username | Role | is_manager | is_logistics |
|---|---|---|---|
| Viktoria, Artur, Asem, Igor | manager | True | **False** ✓ |
| Kuanysch | logistics | False | True |
| Director | director | False (is_director) | False |
| admin | admin | False (is_admin) | False |

Manager users: condition `not is_logistics` = True. Section is accessible.

---

## 7. Root cause analysis

**FINDING: The code is deployed correctly. All data conditions are satisfied. The UI elements WILL render when the page is loaded fresh.**

The most likely causes of "UI did not change" visible to the user:

### Cause A (Most likely): Browser cache
Jinja2 HTML output is served through nginx. The browser may be showing a previously cached page.
**Fix:** Hard refresh: `Ctrl+Shift+R` (Chrome/Firefox) or `Ctrl+F5`.

### Cause B (Likely): User checking wrong page or not scrolling
The new "Производственные заказы — ожидают привязки VIN" section appears at the **bottom** of `/trailers/stock-replenishment/` page (after all supply need cards).
**Fix:** Open `/trailers/stock-replenishment/` as manager and scroll to bottom.

### Cause C (Possible): Testing as logistics user on logistics workspace
The logistics workspace (`/trailers/logistics/workspace`) now shows `"Менеджер / директор назначит VIN"` for logistics users — this is the **intended behavior** of P0-L2. Logistics users no longer see the VIN assign button.
**Fix:** Log in as Viktoria / Artur / Asem / Igor (manager role) or Director.

### Cause D (Low probability): Jinja2 template bytecode cache stale
If any `.pyc` cache existed and wasn't cleared.
**Fix:** Service restart clears all in-memory caches. The service WAS restarted at 08:05 UTC after the last deploy.

### Not the cause:
- Code not deployed — RULED OUT (server is at b0bf128, all strings confirmed)
- Service running old code — RULED OUT (restarted at 08:05 UTC, after b0bf128 commit at 08:00 UTC)
- Data conditions fail — RULED OUT (query simulation returned 3 correct rows)
- Role condition blocks managers — RULED OUT (manager role has is_logistics=False)
- Python syntax error — RULED OUT (py_compile passed, no 500s in logs)

---

## 8. Next safe steps

1. **Manager opens `/trailers/stock-replenishment/` in browser, presses Ctrl+Shift+R (hard refresh), scrolls to bottom** — should see "Производственные заказы — ожидают привязки VIN" with units 70, 80, 81.

2. **Manager opens ORD-000032 detail page, presses Ctrl+Shift+R** — should see "Выпуск готов — нужно привязать VIN" with button "Привязать VIN к выпуску" for unit 70.

3. **Manager opens ORD-000076 detail page** — should see same for unit 80.

4. **If still not visible after hard refresh**: Check if proxy/CDN caching is active (nginx or upstream). Check nginx config for cache headers. This would require a separate investigation.

5. **Also check manager workspace** (`/trailers/`) — 28b09f9 added `customer_order_vin_pending` section there too (limit=10).
