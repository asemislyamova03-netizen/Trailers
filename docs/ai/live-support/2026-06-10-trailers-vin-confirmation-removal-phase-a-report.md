# VIN Confirmation Removal — Phase A Report

**Дата:** 2026-06-10
**План:** `docs/ai/live-support/2026-06-10-trailers-order-centric-vin-and-configuration-plan.md`
**Статус:** **code complete** (local), deploy/push **не выполнялись**

---

## Summary

Phase A убирает обязательный шаг «подтвердить нанесение VIN» из UI, статусов заказов и blockers. Новый assign VIN сразу переводит реестр в финальное состояние (`confirmed`). Старые записи `vin_registry.status='assigned'` считаются финальными в UI наравне с `confirmed`.

**DB backfill не делался** — по scope owner.

---

## Changed files

| File | Change |
|------|--------|
| `views.py` | `_vin_registry_status_is_final()`; убран `waiting_vin_confirm` из filters/state; assign → `confirmed`; confirm route idempotent; убраны confirm feeds |
| `templates/manager_workspace.html` | убран confirm block; обновлены чеклисты/ссылки |
| `templates/logistics_workspace.html` | убрана секция «VIN назначен, не подтверждён» |
| `templates/vin_registry_detail.html` | confirm form → info «VIN присвоен» |
| `templates/vin_registry_list.html` | confirm button → badge «VIN присвоен» |
| `templates/director_dashboard.html` | убран attention bucket и таблица unconfirmed VIN |
| `tests/test_vin_assign_final.py` | **новый** — 7 unit tests |

**Не менялись:** `models.py`, migrations, DB, deploy scripts, `.env`.

---

## UI blocks removed / replaced

| Location | Before | After |
|----------|--------|-------|
| Manager workspace top card | «VIN назначен, ждёт подтверждения» + POST confirm | **удалён** |
| Manager «Что на сегодня» | badge «ждут подтверждения VIN» | **удалён** |
| Manager quick links | «Что нужно подтвердить?» → `waiting_vin_confirm` | «Где ждут VIN?» → `waiting_vin` |
| Logistics workspace | таблица assigned-not-confirmed | **удалена** |
| VIN registry list | кнопка «Подтвердить» для `assigned` | badge «VIN присвоен» |
| VIN registry detail | форма «Подтвердить нанесение» | info «VIN присвоен» |
| Director dashboard | bucket + table «VIN назначен, не подтверждён» | **удалены** |
| Orders list filter | «Ждут подтверждения VIN» | **удалён** |

---

## Backend behavior changes

1. **`logistics_assign_vin`:** `vin_registry.status='confirmed'`, `confirmed_at/by` set; line `vin_confirmed`.
2. **`vin_registry_assign`:** сразу `confirmed` + line `vin_confirmed`.
3. **`vin_registry_confirm`:** route сохранён; если уже final (`assigned`/`confirmed`) → info flash, redirect (no-op).
4. **`_order_list_state`:** ветка `waiting_vin_confirm` удалена.
5. **`_link_vin_registry_to_order_line`:** `assigned` и `confirmed` → line `vin_confirmed`.

---

## Tests

```bash
python -m py_compile views.py
python -m unittest discover -s tests -p "test_*.py" -v
```

| Result | Count |
|--------|-------|
| **OK** | **44 tests** (7 new + 37 regression) |

New tests cover:

- `_vin_registry_status_is_final` for assigned/confirmed/reserved/free
- `_order_list_state` не возвращает `waiting_vin_confirm` при наличии trailer
- `ORDER_LIST_FILTERS` без `waiting_vin_confirm`
- `waiting_vin` по-прежнему работает при `missing_vin`

---

## Risks

| Risk | Level | Note |
|------|-------|------|
| Legacy `assigned` rows in DB | low | UI treats as final; no backfill |
| Old bookmarks `?status=waiting_vin_confirm` | low | filter removed; shows empty/all |
| Confirm route still callable via direct POST | low | idempotent for final states |

---

## Deploy recommendation

### **APPROVE deploy** (views.py + 5 templates bundle)

**Пакет для scp (как Phase 2 UI pattern):**

- `views.py`
- `templates/manager_workspace.html`
- `templates/logistics_workspace.html`
- `templates/vin_registry_detail.html`
- `templates/vin_registry_list.html`
- `templates/director_dashboard.html`
- `tests/test_vin_assign_final.py` (optional on server)

**Pre-deploy:**

1. Backup runtime `views.py` + templates
2. `python3 -m py_compile views.py`
3. `python3 -m unittest discover -s tests -p "test_*.py" -v`
4. `systemctl restart trailers.service`

**Post-deploy smoke (manager login):**

1. Список заказов — нет фильтра «Ждут подтверждения VIN»
2. Manager workspace — нет жёлтого confirm-блока
3. Assign VIN на produced unit — заказ не показывает «подтвердить нанесение»
4. VIN registry list — `assigned` показывает «VIN присвоен», не кнопку confirm

**Не входит в deploy:** DB backfill, migrations.

---

## Next steps

| # | Step | Approval |
|---|------|----------|
| 1 | Deploy Phase A bundle | separate |
| 2 | **Phase B** — order-centric action panel | after deploy smoke |
| 3 | **NEXT HOTFIX** — 403 при редактировании клиента из шапки заказа | separate ticket |
| 4 | Push to origin | separate |

---

## Handoff note

Phase A complete locally. Recommended: deploy → manual GET smoke → Phase B planning.
