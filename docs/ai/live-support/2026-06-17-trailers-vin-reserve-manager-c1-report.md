# C1 Report — VIN wording cleanup + manager-owner reserve access

**Дата:** 2026-06-17
**Проект:** Trailers
**Slice:** C1 (без deploy)

---

## Root cause (diagnosis)

1. **Manager-owner не мог резервировать VIN** из заказа, потому что:
   - `order_reserve_vin` и `order_line_reserve_vin` имели жёсткий guard только для `admin/director`;
   - UI-флаг `can_reserve_vin` в `order_detail` также ограничивался `admin/director`.

2. **Logistics wording** оставался в пользовательском пути менеджера:
   - `order_detail`: «логист / директор»;
   - `assign_vin_form`: «Обратитесь к логисту», «Логист должен загрузить VIN»;
   - `stock_replenishment_list`: тексты с обязательным участием логиста.

---

## Что изменено

### A) Wording cleanup

- `templates/order_detail.html`
  - `Зарезервировать новый VIN (логист / директор)` -> `Зарезервировать VIN для заказа`.

- `templates/assign_vin_form.html`
  - Убраны формулировки «обратитесь к логисту / логист должен...».
  - Заменены на нейтральные: проверить VIN-реестр / обратиться к ответственному за VIN-реестр.

- `templates/stock_replenishment_list.html`
  - Убраны тексты, где логист описан как обязательный исполнитель.
  - Подчёркнут order-centric менеджерский путь.

### B) Manager-owner reserve access

- `views.py`
  - `order_reserve_vin`:
    - было: manual role check `admin/director` + `_ensure_can_access_order`;
    - стало: `_ensure_can_manage_order(order)`.
  - `order_line_reserve_vin`:
    - было: manual role check `admin/director` + `_ensure_can_access_order`;
    - стало: `_ensure_can_manage_order(order)`.
  - `order_detail` (`can_reserve_vin`):
    - было: `(current_user.is_admin or current_user.is_director)`;
    - стало: `can_manage_order(order)`.

Это даёт:
- manager-owner: allowed;
- manager non-owner: blocked;
- admin/director: allowed;
- production/logistics/warehouse: blocked (через `_block_production_commercial_access` и `can_manage_order`).

---

## Изменённые файлы

- `views.py`
- `templates/order_detail.html`
- `templates/assign_vin_form.html`
- `templates/stock_replenishment_list.html`
- `tests/test_order_vin_reserve_permissions.py` (new)

---

## Тесты

Executed:

- `python -m py_compile views.py` -> **OK**
- `python -m unittest discover -s tests -v` -> **OK**
  - `Ran 88 tests`
  - `OK`

New tests in `tests/test_order_vin_reserve_permissions.py` cover:
- manager-owner can reserve VIN;
- manager non-owner blocked;
- admin/director allowed;
- production/logistics blocked;
- capacity/modification blocker path still returns with existing validation flow (`ValueError` branch).

Regression guard:
- existing suite still includes `test_no_vin_confirm_step_in_panel` (no return of VIN confirm-step wording).

---

## Remaining logistics wording (in requested scope)

After C1 changes and scan of requested templates:

- No remaining matches for:
  - `логистика`
  - `обратитесь к логисту`
  - `логист / директор`
- One expected contextual mention remains in `templates/stock_replenishment_list.html`:
  - «...без обязательного участия логиста.»
- `templates/logistics_workspace.html` still contains page title `Логистика` (expected for dedicated logistics workspace).

---

## Risk / recommendation

- Risk level: **low-medium** (permission guard + UI text changes).
- Business blockers/capacity/modification logic preserved.
- **Deploy recommendation:** approve for pre-deploy verification and then controlled deploy (no migrations/data changes).
