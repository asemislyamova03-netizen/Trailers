# Order Customer Edit 403 — Hotfix Report

**Дата:** 2026-06-10
**Тип:** UI/code hotfix (локально), **без** deploy / push / DB / migrations
**Связанный deploy:** Phase A VIN (`docs/ai/live-support/2026-06-10-trailers-vin-confirmation-removal-phase-a-deploy-report.md`)

---

## 1. Root cause

Цепочка: **Заказ → Редактировать шапку → Редактировать клиента → сохранить**.

| Слой | Проблема |
|------|----------|
| `customer_edit` / `customer_create` | `@role_required('manager')` — **director** не проходил guard, хотя `order_edit` разрешён через `can_manage_order` |
| Order context | `return_to=order_edit` + `order_id` **не проверяли** `can_manage_order` на GET/POST |
| `_attach_customer_to_order` | Менял `order.customer_id` **без** проверки прав на заказ (security gap) |
| POST | `return_to` / `order_id` читались только из `request.args`; при потере query string контекст заказа терялся |

**Итог:** director (и сценарии с чужим `order_id`) получали отказ; attach клиента к заказу был возможен без `can_manage_order`.

---

## 2. Fix summary

1. `@role_required('manager', 'director')` на `customer_edit` и `customer_create` (admin по-прежнему bypass).
2. `_customer_order_context_from_request()` — читает `return_to` / `order_id` из `request.values` (GET + POST).
3. `_ensure_customer_order_edit_access()` — при `return_to=order_edit` загружает заказ и вызывает `_ensure_can_manage_order`; для всех customer routes — `_block_production_commercial_access()`.
4. `_attach_customer_to_order()` — `_ensure_can_manage_order(order)` перед изменением `customer_id`.
5. `customer_form.html` — hidden fields `return_to` / `order_id` для надёжного POST.

**Не менялось:** DB, migrations, auth architecture, VIN/configuration, unrelated templates, customer_create business logic.

---

## 3. Changed files

| File | Change |
|------|--------|
| `views.py` | Guards, helpers, role decorator, attach permission |
| `templates/customer_form.html` | Hidden `return_to` / `order_id` |
| `tests/test_order_customer_edit_access.py` | **new** — 14 tests |
| `docs/ai/live-support/2026-06-10-trailers-order-customer-edit-403-hotfix-report.md` | this report |

---

## 4. Tests

```text
python -m py_compile views.py
python -m unittest discover -s tests -v
→ Ran 58 tests — OK
```

Новые сценарии (`test_order_customer_edit_access.py`):

- owning manager GET/POST from order context — OK
- director GET from order context — OK
- other manager + `order_id` — 403
- production / logistics — forbidden (302/403)
- `_attach_customer_to_order` without permission — no DB commit
- standalone customer edit for manager — OK

---

## 5. Commit

*(заполняется после commit)*

---

## 6. Deploy recommendation

**Рекомендация:** scp-deploy **после** manual smoke на staging/local:

1. `views.py`
2. `templates/customer_form.html`
3. `tests/test_order_customer_edit_access.py` (опционально на сервер)

**Smoke checklist:**

- [ ] Director: заказ → шапка → редактировать клиента → сохранить → redirect на `/orders/<id>/edit`
- [ ] Manager-владелец заказа: тот же сценарий
- [ ] Manager чужого заказа: 403 при `return_to=order_edit&order_id=...`
- [ ] Production/logistics: нет доступа к `/customers/*/edit`
- [ ] Standalone `/customers/<id>/edit` без `order_id` — работает для manager/director

**Не делать без approval:** push, migrations, DB changes.

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| Director ранее не мог редактировать клиента из заказа | Исправлено добавлением `director` в `role_required` |
| Чужой manager мог привязать клиента через `order_id` | `_ensure_can_manage_order` на GET/POST и в `_attach_customer_to_order` |
| Regression standalone edit | Тест + smoke без `return_to` |

---

## 8. Next safe step

1. Manual UI smoke (director + owning manager).
2. scp deploy hotfix files (по образцу Phase A).
3. Phase B: order-centric action panel (отдельная задача).
