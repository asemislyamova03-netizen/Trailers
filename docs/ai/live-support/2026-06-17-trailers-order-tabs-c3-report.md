# C3 Report — Order Tabs Persistence

**Дата:** 2026-06-17
**Project:** Trailers
**Slice:** C3 (tab persistence only)
**Scope:** `views.py`, `templates/order_detail.html`, `tests/test_order_tabs_c3.py`

---

## Что сделано

Реализована фиксация/сохранение вкладки `documents` для contract/document flow без изменения бизнес-логики:

1. Добавлен helper redirect в `views.py`:
   - `_normalize_order_detail_tab(...)`
   - `_redirect_order_detail(order_id, tab=None, default_tab='overview')`

2. `order_detail` теперь принимает `?tab=...` и передаёт в шаблон `active_tab` с безопасным fallback на `overview`.

3. В `templates/order_detail.html`:
   - серверная активация nav/tab-pane по `active_tab_key`;
   - в формы блока документов добавлен hidden `tab=documents`:
     - `order_mark_invoice_sent`
     - `order_mark_contract_ready`
     - `order_mark_documents_ready`
     - `order_issue_documents`
     - `order_contract_create`
     - `contract_delete`
   - ссылки на `contract_edit` и `contract_sign` из вкладки документов теперь включают `tab=documents`.

4. В `views.py` переведены redirect’ы contract/document actions на tab-aware возврат:
   - `order_contract_create` (успех + blockers + duplicate branch)
   - `_mark_order_document` (все выходы, включая duplicate)
   - `order_issue_documents` (успех + blockers + duplicate branch)
   - `contract_edit` (access deny + success branch с order)
   - `contract_delete` (blocked + success branch с order)
   - `_duplicate_redirect` для `CustomerOrder`/`SalesContract` теперь учитывает `tab` из запроса.

---

## Exact redirect behavior

- Если `tab=documents` передан из формы/ссылки, возврат идёт на:
  - `/orders/<id>?tab=documents`
- Если `tab` отсутствует или невалиден:
  - fallback на поведение по умолчанию (`/orders/<id>`, т.е. `overview`).
- Для contract/document веток применён `default_tab='documents'`, чтобы даже blocker/duplicate возвращали пользователя в документы.
- `PDF/Печать` с `target="_blank"` не изменялись и работают как раньше.

---

## Проверки

### Compile
- `python -m py_compile views.py` — **OK**

### Unit tests
- `python -m unittest discover -s tests -v` — **OK**
- Итог: **83 tests, OK**

Добавлены C3-тесты в `tests/test_order_tabs_c3.py`:
- normalize/fallback tab;
- redirect helper с `tab=documents`;
- blocker redirect для `order_contract_create`;
- blocker redirect для `order_issue_documents`;
- blocker redirect для `_mark_order_document`.

---

## Ограничения scope (соблюдены)

- Бизнес-логика договоров не менялась.
- DB/data не менялись.
- Новые routes не добавлялись.
- VIN/reserve permissions не трогались.
- Миграции/deploy/push не выполнялись.

---

## Риски

- Низкий риск: UI/redirect слой.
- Возможен только UX-эффект в навигации вкладок; fallback на `overview` сохранён.

---

## Deploy recommendation

**Рекомендовано к deploy** как отдельный безопасный hotfix slice C3:
- изменения локальны и покрыты тестами;
- затрагивают только tab-navigation/redirect UX для contract/document сценариев.
