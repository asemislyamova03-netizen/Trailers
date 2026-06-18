# Pre-Deploy — год выпуска в спецификации договора из VIN

**Date:** 2026-06-18
**Project:** Trailers (legacy Flask)
**Mode:** local verification, **no deploy yet**

---

## Проблема

В `contract_print.html` год выпуска брался из даты договора:

```jinja
{% set year_str = contract.contract_date.year if contract.contract_date else '' %}
```

Пример: договор № 2228, VIN `MX4000002S0002411` (10-й символ `S` → **2025**), но в спецификации показывалось **2026** (год даты договора 18.06.2026).

---

## Исправление

| Файл | Изменение |
|------|-----------|
| `views.py` | `_manufacture_year_from_vin()` — маппинг 10-го символа VIN (ISO 3779, 2010–2030); `manufacture_year` в `_build_contract_context()` |
| `templates/contract_print.html` | `year_str` ← `manufacture_year` |
| `tests/test_vin_manufacture_year.py` | unit-тесты |

Маппинг: `S`→2025, `T`→2026, `R`→2024, `P`→2023 и т.д.

---

## Тесты

```text
python -m unittest tests.test_vin_manufacture_year tests.test_order_line_next_actions -q
→ OK
```

---

## Важно: смешанный diff в `views.py`

Локальный `views.py` также содержит **не задеплоенные** изменения B2.2 (`_build_order_line_ui_state`).
Для **изолированного** hotfix на прод лучше задеплоить только hunks года выпуска, либо сначала закоммитить/разделить изменения.

Минимальный набор для hotfix года:
- `views.py` (только `_manufacture_year_from_vin` + 2 строки в `_build_contract_context`)
- `templates/contract_print.html`

---

## План deploy (после approval)

1. Backup на сервере: `views.py`, `contract_print.html`
2. `scp` двух файлов
3. `sudo systemctl restart trailers.service`
4. Smoke: `/contracts/1028/print` → «Год выпуска **2025** г.»

Rollback: восстановить из backup + restart.

---

## Риски

- **Низкий:** только печать договора, без БД и миграций.
- Если VIN короткий/битый — год не показывается (пусто), не подставляется дата договора.
