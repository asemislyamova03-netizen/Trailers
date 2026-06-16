# Phase B Slice B1 — Order Action Panel Report

**Дата:** 2026-06-16
**Тип:** implementation (UI/view-model only, без изменения бизнес-guard)
**План-источник:** `docs/ai/live-support/2026-06-16-trailers-order-centric-action-panel-phase-b-plan.md`

---

## 1. Что реализовано

Цель Slice B1 выполнена: в карточке заказа добавлен блок **«Следующие действия»**, который показывает менеджеру/директору/админу последовательные шаги по заказу с состоянием:

- доступно (active link);
- недоступно (disabled + причина).

Сделано только через view-model/helper + UI, без новых маршрутов и без новых POST-действий.

---

## 2. Изменённые файлы

1. `views.py`
   - добавлен helper: `_build_order_action_panel(order, order_line_rows, can_manage)`;
   - в `order_detail()` передаётся `order_action_panel` в шаблон.

2. `templates/order_detail.html`
   - добавлен новый UI-блок «Следующие действия» у верхней части карточки заказа;
   - рендерит действия из view-model с reason для disabled state.

3. `tests/test_order_action_panel.py` (новый)
   - unit-тесты helper’а для сценариев Slice B1.

---

## 3. Что покрывает action panel (B1)

- редактировать шапку / заменить клиента;
- выбрать источник / подобрать прицеп;
- создать или проверить потребность в производство;
- комплектация строки (как шаг/ссылка на existing UI);
- присвоить VIN (если есть `produced_no_vin` unit);
- перемещение (если нужен другой склад);
- выдать документы (с учётом blockers);
- создать реализацию или открыть draft реализацию;
- финальный статус (информирующий).

Важно:

- шага «подтвердить VIN» нет;
- логистика не показывается как обязательный процессный шаг;
- production-only операции не добавлялись как manager POST CTA.

---

## 4. Проверки и результаты

### 4.1 Compile

```bash
python -m py_compile views.py
```

Результат: **OK**.

### 4.2 New tests (Slice B1)

```bash
python -m unittest discover -s tests -p "test_order_action_panel.py" -v
```

Результат: **6/6 OK**.

Покрыто:

- waiting production path;
- produced_no_vin -> assign VIN action;
- docs/realization availability без blockers;
- shipped order -> safe disabled state;
- отсутствие VIN confirm step;
- отсутствие actions при `can_manage=False`.

### 4.3 Full regression suite

```bash
python -m unittest discover -s tests -v
```

Результат: **78 tests, OK**.

Примечание: есть существующие предупреждения `LegacyAPIWarning` и `WeasyPrint` (известные, не связаны с этим изменением).

### 4.4 HTML smoke (local test_client)

- user 4, `/orders/80`: HTTP 200, блок «Следующие действия» присутствует, текста про VIN confirm нет.
- user 1, `/orders/80`: HTTP 200, блок присутствует, VIN confirm текста нет.
- user 4, `/orders/72`: HTTP 403 (ожидаемо по доступу).

---

## 5. Ограничения scope соблюдены

Не менялось:

- DB/data;
- migrations;
- deploy/push;
- production business routes;
- VIN/config/customer replacement logic;
- `production_workspace`/`logistics_workspace`/`stock_replenishment` templates.

---

## 6. Риски

Низкие/средние:

- Панель B1 даёт orchestration-level view, но не заменяет все существующие формы внизу страницы.
- Возможна частичная визуальная «дубликация» CTA (в панели + в существующих секциях), это ожидаемо для B1.

---

## 7. Рекомендация по deploy

**Можно готовить deploy после ручного UI review**:

1. Проверить 3-4 заказа разного состояния (open / waiting_transfer / shipped / documents_issued).
2. Убедиться, что причины disabled читаемы и корректны.
3. После approval — отдельный deploy approval (scp-only flow как в проекте).
