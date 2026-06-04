# Implementation Plan: Order need production button

Date: 2026-06-04
Status: implemented
Linked research brief: `docs/ai/research/2026-06-04-order-need-production-button.md`

## Objective

Дать менеджеру/директору возможность отдать уже созданную из заказа потребность в производство прямо из карточки заказа.

## Non-goals

- Не менять схему БД.
- Не менять права доступа.
- Не менять статусы VIN, документов, реализаций и перемещений.
- Не добавлять новый экран выбора склада.

## File touch list

| File | Change | Reason | Risk |
|---|---|---|---|
| `templates/order_detail.html` | Кнопка `В производство` в таблице потребностей заказа. | Закрыть отсутствующее действие в текущем сценарии. | low |
| `views.py` | Helper выбора `target_warehouse_id` и использование в `supply_need_create_production_request`. | Снизить риск пустого склада назначения. | medium |

## Step-by-step plan

### Step 1

- Files: `views.py`
- Action: добавить helper, который берет склад из `need.warehouse_id`, затем `need.order.warehouse_id`, затем активный склад текущего пользователя.
- Verification: проверить diff и compile.

### Step 2

- Files: `templates/order_detail.html`
- Action: добавить POST-кнопку на существующий маршрут для новых потребностей.
- Verification: проверить diff и compile шаблонов через общий `compileall`.

## Test plan

```bash
python -m compileall .
git -c core.quotepath=false diff --ignore-cr-at-eol -- views.py templates/order_detail.html docs/ai/research/2026-06-04-order-need-production-button.md docs/ai/plans/2026-06-04-order-need-production-button.md
```

## Acceptance criteria

- [x] В карточке заказа у новой потребности есть кнопка `В производство`.
- [x] Кнопка использует существующий маршрут создания производственной заявки.
- [x] При создании заявки склад назначения подставляется из доступного контекста, если он был пустым в потребности.
- [x] Нет изменений моделей, миграций, auth, документов, VIN и перемещений.

## Rollback plan

Удалить добавленный helper/его использование в `views.py` и вернуть блок потребностей в `templates/order_detail.html` к прежнему виду.

## Approval

Approved by user: yes
Date: 2026-06-04
Notes: Пользователь ответил "все" после предложения сделать UI-кнопку и backend-проверку/автоподстановку склада.
