# Новый архитектурный статус Trailers

С 03.06.2026 проект Trailers больше не развивается как самостоятельная ERP-платформа.

Trailers используется как:

1. legacy/reference-проект;
2. источник бизнес-логики по производству и продаже прицепов;
3. будущий отраслевой пакет Flexity: `industry_trailers`.

Целевая архитектура:

```text
Flexity = основная FastAPI multi-tenant ERP-платформа
Trailers Flask = legacy/reference
industry_trailers = будущий отраслевой пакет внутри Flexity
```

## Главный запрет

Без отдельного архитектурного решения нельзя развивать Trailers как вторую ERP-платформу.

Нельзя добавлять в Trailers новые универсальные модули:

* универсальная CRM;
* подписки;
* AI foundation;
* налоговый модуль;
* зарплатный модуль;
* кадровый модуль;
* универсальный складской модуль;
* универсальный производственный модуль;
* универсальные документы;
* универсальные финансы;
* универсальный модуль подписания документов.

Если задача универсальная — её нужно проектировать во Flexity.

Trailers можно менять только для:

1. критичных исправлений текущей legacy-системы;
2. анализа бизнес-логики;
3. подготовки миграции в Flexity;
4. специфичных функций прицепов, если есть отдельный approved plan;
5. составления карты `Trailers Flask → Flexity industry_trailers`.

Перед любой новой задачей AI-агент обязан определить:

```text
1. Это временная правка legacy Trailers?
2. Это универсальная функция Flexity?
3. Это будущая функция industry_trailers?
4. Это только research/mapping без изменения кода?
```

Если ответ неясен — код не менять.
# Do Not Touch Without Approval — Trailers

Этот файл защищает проект от случайных изменений. AI-агент не должен менять перечисленные зоны без отдельного research brief, implementation plan и явного approval пользователя.

## Абсолютно запрещено без отдельного approval

### Секреты и окружение

- `.env`
- `.env.*`
- production secrets
- токены Kaspi, SigeX, NCALayer, webhook/API tokens
- SSH/deploy credentials

Причина: утечка или изменение секретов может сломать production и безопасность.

### База данных и миграции

- `models.py`
- `migrations/versions/*.py`
- `migrations/env.py`
- `migrations/alembic.ini`
- `trailers.db`
- `instance/trailers.db`
- `*.db`
- `backups/*.db`

Разрешено менять только после отдельного плана миграции:

1. текущая версия базы;
2. migration head;
3. новая миграция;
4. rollback plan;
5. тест на локальной копии базы.

### Auth / login / permissions

- `User.role`
- `role_required`
- `admin_required`
- route-level access checks
- `templates/base.html` в части ролевого меню
- любые проверки `current_user.role`, `is_admin`, `is_manager`, `is_director`, `is_production`, `is_logistics`, `is_warehouse`

Причина: ошибка в ролях может открыть коммерческие данные производству/логистике или закрыть доступ менеджерам.

### Деньги, документы, продажи

- `OrderPayment`
- `SalesContract`
- `SalesContractLine`
- `SalesRealization`
- `SalesRealizationLine`
- маршруты договоров `/contracts/*`
- маршруты оплат `/orders/<id>/payments/*`
- маршруты выдачи документов `/orders/<id>/issue-documents`
- маршруты реализаций `/realizations/*`
- шаблоны договоров и печати: `templates/contract_*`, `pdf_utils.py`

Причина: влияет на юридические документы, оплаты, отчетность и будущий налоговый модуль.

### VIN и физический прицеп

- `Trailer`
- `VinRegistry`
- `VinRegistryEvent`
- маршруты `/logistics/vin-registry/*`
- присвоение VIN выпущенной единице
- подтверждение нанесения VIN
- блокировки повторного VIN

Причина: VIN — юридически и складски критичная сущность.

### Заказы и многострочная модель

- `CustomerOrder`
- `CustomerOrderLine`
- order-level / line-level привязки
- поля `order_id`, `order_line_id`
- маршруты `/orders/*/lines/*`
- логика `reserve`, `source`, `production`, `stock`, `realization`, `shipping`

Причина: в проекте уже идет переход от шапки заказа к многострочным позициям. Нельзя возвращать старую order-level логику.

### Склад и логистика

- `StockMovement`
- `Warehouse`
- `WarehouseStorageArea`
- lifecycle/status прицепов
- приемка/отправка перемещений
- складские фильтры менеджера

Причина: ошибка может привести к повторной продаже, неправильному складу или потере прицепа в статусах.

### Производство и ТМЦ

- `SupplyNeed`
- `ProductionRequest`
- `ProductionRequestLine`
- `ProducedUnit`
- `ItemBillOfMaterials`
- `InventoryBalance`
- `InventoryOperation`
- `InventoryTransaction`
- `inventory_service.py`
- `production_workspace`
- BOM-списание и возврат материалов

Причина: ошибка ломает потребности, выпуск, склад, остатки и себестоимость.

### Интеграции

- `kaspi_client.py`
- `sigex_client.py`
- `/integrations/kaspi/*`
- `/contracts/<id>/sigex/*`
- `/webhooks/*`
- `static/js/ncalayer-client.js`

Причина: интеграции зависят от внешних API, токенов, статусов и форматов.

### Deployment / server

- `scripts/server_update.sh`
- systemd service name
- server paths
- production database
- server-only fixes
- remote migration commands

Причина: можно сломать рабочий сервер.

## Нельзя трогать как контекст для обычной разработки

Эти файлы/папки не должны использоваться Cursor как основной код:

- `venv_trailers/`
- `__pycache__/`
- `*.pyc`
- `backup_before_*`
- `backups/`
- `changed_files_*.diff`
- `changed_files_*.zip`
- `chatgpt_changes_*.diff`
- `*.db`
- `*.xlsx`

Их можно читать только по отдельному запросу: например, чтобы восстановить старую версию или сравнить diff.

## Что делать, если изменение все-таки нужно

Перед изменением высокорисковой зоны:

1. Создать research brief в `docs/ai/research/`.
2. Создать implementation plan в `docs/ai/plans/`.
3. Для архитектуры/схемы/налогового/финансового контура создать ADR в `docs/adr/`.
4. Указать точный file touch list.
5. Указать migration/rollback plan, если затронута база.
6. Получить явное approval пользователя.
7. Менять только один маленький шаг за раз.
