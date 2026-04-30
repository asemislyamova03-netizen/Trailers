# README_CHANGELOG

## Фактическая структура

Проект остается монолитным Flask-приложением:

- `app.py` - app factory `create_app()`, регистрация `main_bp`, `db`, `migrate`, `csrf`.
- `models.py` - SQLAlchemy-модели.
- `forms.py` - WTForms.
- `views.py` - один Blueprint `main_bp` со всеми маршрутами.
- `templates/` - Jinja-шаблоны.
- `migrations/versions/` - Alembic/Flask-Migrate миграции.

## Добавленные модели

- `Lead`
- `LeadMessage`
- `CustomerOrder`
- `OrderPayment`
- `Reservation`
- `SupplyNeed`
- `ProductionRequest`
- `ProductionRequestLine`
- `StockMovement`

Дополнительно:

- `SalesContract.order_id` связывает договор с заказом.
- `Trailer.lifecycle_status` хранит производственный цикл будущего/перемещаемого товара.
- `Lead.channel`, `Lead.source_name`, `Lead.source_payload`, `LeadMessage.payload` добавлены для омниканальности.
- `StockMovement.departure_date`, `StockMovement.arrival_date` добавлены как даты документа перемещения.

## Добавленные маршруты

- `/leads`, `/leads/new`, `/leads/<id>`, `/leads/<id>/edit`, `/leads/<id>/delete`
- `/orders`, `/orders/new`, `/orders/<id>`, `/orders/<id>/edit`
- `/orders/<id>/payments/new`
- `/orders/<id>/contract/new`
- `/orders/<id>/ship`
- `/supply-needs`, `/supply-needs/new`, `/supply-needs/<id>/edit`
- `/production-requests`, `/production-requests/new`, `/production-requests/<id>`, `/production-requests/<id>/edit`
- `/stock-movements`, `/stock-movements/new`, `/stock-movements/<id>/edit`
- `/webhooks/site`, `/webhooks/telegram`, `/webhooks/instagram`, `/webhooks/whatsapp`

## Добавленные шаблоны

- `templates/leads_list.html`
- `templates/lead_form.html`
- `templates/lead_detail.html`
- `templates/orders_list.html`
- `templates/order_form.html`
- `templates/order_detail.html`
- `templates/order_payment_form.html`
- `templates/supply_needs_list.html`
- `templates/supply_need_form.html`
- `templates/production_requests_list.html`
- `templates/production_request_form.html`
- `templates/production_request_detail.html`
- `templates/stock_movements_list.html`
- `templates/stock_movement_form.html`

## Миграция

Новая миграция:

- `migrations/versions/6f2b7d9c1a04_add_crm_orders_production_movements.py`

Команды:

```powershell
$env:FLASK_APP = "app:create_app"
flask db upgrade
```

Если используется локальный venv проекта:

```powershell
.\venv_trailers\Scripts\Activate.ps1
$env:FLASK_APP = "app:create_app"
flask db upgrade
```

## Ручная проверка

1. Заявка из сайта/бота:
   - POST JSON на `/webhooks/site` или `/webhooks/telegram`.
   - Открыть `/leads`, убедиться, что создана заявка и сообщение.

2. Заказ из наличия:
   - Открыть заявку.
   - Нажать "Создать заказ".
   - Выбрать конкретный `Trailer`.
   - Сохранить заказ.
   - Проверить `Reservation` в карточке заказа и статус прицепа `RESERVED`.

3. Заказ под производство:
   - Создать заказ без конкретного `Trailer`.
   - Проверить созданную `SupplyNeed`.
   - Создать `ProductionRequest` и добавить позицию из потребности.

4. Предоплата 30%:
   - В карточке заказа добавить платеж `PREPAYMENT` на 30%.
   - Проверить статус `prepaid` или `waiting_production` для заказа под производство.

5. Полная оплата:
   - Добавить платеж `FULL` или финальную доплату до полной суммы.
   - Для заказа с VIN проверить переход к `ready_to_ship`.

6. Прибытие на склад:
   - Создать `StockMovement` с `status=arrived`, `trailer_id`, `to_warehouse_id`, `order_id`.
   - Проверить смену склада у `Trailer` и статус заказа `ready_to_ship`.

7. Создание договора из заказа:
   - В карточке заказа с выбранным VIN нажать "Создать договор".
   - Проверить договор в `/contracts`.
   - Прицеп при этом не переводится в `SOLD`.

8. Отгрузка:
   - В карточке заказа нажать "Отгрузить".
   - Проверить статус заказа `done`, закрытие активного резерва и перевод прицепа в `SOLD`.
