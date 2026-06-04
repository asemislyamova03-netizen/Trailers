# Research Brief: Order need production button

Date: 2026-06-04
Status: approved

## Goal

Понять, почему из карточки заказа с уже созданной потребностью нельзя отдать позицию в производство, особенно когда склад заказа/источника не задан.

## Original request

Пользователь создал заказ менеджером на прицеп из производства со другого склада. В заказе не задан склад источника, текущий склад прицепа не задан. Потребность создалась из заказа, но нет кнопки отдать в производство.

## What I inspected

| File / Source | Finding |
|---|---|
| `views.py` | `supply_need_create_production_request` уже создает `ProductionRequest` из `SupplyNeed`, но ставит `target_warehouse_id=need.warehouse_id`. |
| `views.py` | `_create_production_need_for_line` создает потребность со складом `order.warehouse_id`, который может быть пустым. |
| `templates/order_detail.html` | В блоке потребностей карточки заказа есть только действие отмены производства, кнопки запуска в производство нет. |
| `templates/supply_needs_list.html` | В общем списке потребностей кнопка `В производство` уже есть для `NEW`. |
| `templates/logistics_workspace.html` | В директорском логистическом рабочем месте кнопка `В производство` уже есть для новых потребностей. |

## Current codebase facts

- В модели `SupplyNeed.warehouse_id` nullable.
- В модели `ProductionRequest.target_warehouse_id` nullable.
- Потребность заказа должна оставаться привязанной к `CustomerOrderLine`.
- Создание производственной заявки уже защищено от дубля через `_active_production_line_for_need` и idempotency.

## Constraints

- Не менять схему БД и миграции.
- Не менять роли и route-level permissions.
- Не переписывать order-level/line-level логику.
- Не менять VIN, документы, реализации и складские перемещения.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Производственная заявка без склада назначения | medium | Подставлять склад из потребности, заказа или склада пользователя, если он активен. |
| Дублирование производственной строки | medium | Использовать существующий маршрут и существующую проверку `_active_production_line_for_need`. |
| Поломка карточки заказа | medium | Менять только маленький UI-блок потребностей. |

## Recommendation

Добавить кнопку `В производство` в карточку заказа для новых потребностей и перед созданием `ProductionRequest` мягко определить склад назначения из доступного контекста.

## Expected file touch list

| File | Expected change | Why |
|---|---|---|
| `templates/order_detail.html` | Добавить кнопку `В производство` для `SupplyNeed.status == 'NEW'`. | Менеджер сможет продолжить сценарий из карточки заказа. |
| `views.py` | Добавить маленький helper для выбора склада назначения и использовать его при создании заявки. | Не создавать заявку без склада, если склад можно вывести из заказа/пользователя. |
| `docs/ai/plans/2026-06-04-order-need-production-button.md` | План реализации. | Выполнить project workflow. |

## Open questions

- Если у заказа, потребности и пользователя нет склада, бизнес должен выбрать склад вручную. В этом этапе не добавляется новая форма выбора склада.
