# Order Customer Replacement — Diagnosis, Plan & Hotfix Report

**Дата:** 2026-06-10
**Тип:** diagnosis + implementation (локально), **без** deploy / push / DB / migrations

---

## 1. Root cause (404 / misworkflow)

### Путаница действий

| Действие | Что делало UI | Что нужно бизнесу |
|----------|---------------|-------------------|
| **Заменить клиента в заказе** | Кнопка «Редактировать» → `/customers/<id>/edit?return_to=order_edit` | Выбрать другого клиента в `customer_search` и **сохранить шапку заказа** |
| **Редактировать карточку** | То же | Отдельная ссылка на `/customers/<id>/edit` **без** attach к заказу |

### Почему 404 у менеджера

1. Кнопка «Редактировать» в `order_header_form.html` / `order_form.html` вела на `customer_edit` с `return_to=order_edit`.
2. Это **не замена** клиента: после сохранения карточки `_attach_customer_to_order` привязывает **тот же** `customer_id`, а не нового из поиска.
3. 404 возникает при битой ссылке: если `data-id` в datalist не синхронизирован, URL может стать `/customers/undefined/edit` или невалидный id → `get_or_404`.
4. Для **замены** уже был POST `order_edit` (header-only branch, `views.py` ~10232), но UI направлял в `customer_edit`, а blockers для contract/docs/shipped **не проверялись**.

### Шаблон шапки

`order_edit` → при `lines.count() > 0` и `not _order_can_configure_primary_line(order)` → `_render_order_header_form` → **`templates/order_header_form.html`**.

---

## 2. Business rule (implemented)

Замена `order.customer_id` разрешена **manager-owner / admin** только если:

- нет `SalesContract` по `order_id`;
- нет `SalesRealization` по `order_id`;
- `documents_issued == False`;
- `is_shipped == False` и status не `shipped` / `customer_shipped`;
- заказ не `cancelled`.

Иначе — flash + форма без изменения `customer_id`, событие `customer_changed`.

Director **не обязателен** для workflow (не добавляли отдельный UI; `can_manage_order` для director сохранён на уровне `order_edit`).

---

## 3. Implementation summary

### A. UI (`order_header_form.html`, `order_form.html`)

- Подсказка: «Чтобы **заменить клиента в заказе**, найдите другого… и нажмите Сохранить».
- Кнопка «Редактировать» → **«Карточка»** → `/customers/<id>/edit` без `return_to=order_edit`.
- Alert с blockers, если замена недоступна.
- Кнопка «Создать» клиента с `return_to=order_edit` сохранена (создание нового клиента).

### B–E. Backend (`views.py`)

- `_order_customer_change_blockers(order)`
- `_can_replace_order_customer(order)` — admin + manager-owner
- `_apply_order_customer_id_change(order, new_customer_id)` — blockers + `add_order_event(..., 'customer_changed', ...)`
- Header-only и full `order_edit` POST вызывают helper вместо прямого `order.customer_id = ...`

### F. `customer_edit` не используется для замены

Ссылка «Карточка» без attach-flow; замена только через сохранение шапки.

---

## 4. Changed files

| File | Change |
|------|--------|
| `views.py` | blockers, apply helper, order_edit integration |
| `templates/order_header_form.html` | UI copy, card link, blockers alert |
| `templates/order_form.html` | same for full order form |
| `tests/test_order_customer_replacement.py` | **new** — 12 tests |
| `docs/ai/live-support/2026-06-10-trailers-order-customer-replacement-plan-and-hotfix-report.md` | this report |

**Не менялось:** models, migrations, VIN/configuration, DB, deploy.

---

## 5. Tests

```text
python -m py_compile views.py
python -m unittest discover -s tests -v
→ Ran 70 tests — OK
```

Покрытие `test_order_customer_replacement.py`:

- blockers: contract, realization, documents, shipped
- owner/admin POST replacement OK (snapshot copy)
- non-owner 403
- contract exists → blocked, customer_id unchanged
- header UI: replace hint, no customer_edit?return_to=order_edit for card link

---

## 6. Commit

`c9e9d01ad4a73fc33ce2e36c29c3c2fe1091b1da` — Add order header customer replacement with business blockers.

---

## 7. Deploy recommendation

**После predeploy verification** — scp:

1. `views.py`
2. `templates/order_header_form.html`
3. `templates/order_form.html`
4. `tests/test_order_customer_replacement.py` (optional)

**Manual smoke:**

- [ ] Manager-owner: шапка заказа → выбрать другого клиента в поиске → Сохранить → `customer_id` сменился
- [ ] «Карточка» открывает edit клиента, **не** меняет заказ
- [ ] Заказ с договором → замена blocked + сообщение
- [ ] Admin — замена до blockers OK

**Не делать без approval:** push, migrations, DB writes on production during deploy.

---

## 8. Relation to 9b28925 hotfix

Hotfix `9b28925` исправил guards на `customer_edit` в order context. Эта задача **отделяет** замену клиента в заказе от редактирования карточки. Оба изменения совместимы.
