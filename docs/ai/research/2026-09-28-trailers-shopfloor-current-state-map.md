# Research Brief + Migration Map: текущее состояние участков, смен, выпуска и ТМЦ в Trailers

Date: 2026-09-28
Status: read-only correction
Baseline: `crm-roles-production-logistics` @ `c1c3f51cda58f9c326b1d3608c0e32746dcad078`
Docs branch: `docs/public-cloud-context-candidate`
Mode: documentation only. Code, DB, production, dirty local `views.py` not inspected as a source of truth and not modified.

## Task classification

- Project: Trailers Flask (legacy/reference)
- Category: `research_only` + `migration_map`
- Risk: low (docs only)
- Intended scope: this file
- Forbidden scope: `views.py`, `models.py`, migrations, `inventory_service.py`, templates, `.env`, production DB/deploy, dirty local tree on `crm-roles-production-logistics`

## Correction of the previous read-only report

Previous mapping was **wrong** if it said Trailers has no shopfloor model and that workshops / employees / shifts must be designed from scratch.

On the approved baseline `c1c3f51` the following **already exist** and are wired into UI:

| Entity | Table | Status |
|---|---|---|
| `ProductionWorkshop` | `production_workshop` | seeded + used |
| `ProductionEmployee` | `production_employee` | auto-created from `User` |
| `ProductionShift` | `production_shift` | open / close in UI |
| `ProductionShiftOutput` | `production_shift_output` | qty + defect in UI |
| Trailer production pipeline | `supply_need` → `production_request_line` → `produced_unit` | live operational loop |
| TMZ loop | `inventory_balance` / `inventory_operation` / BOM consumption on trailer `+1` | live operational loop |

Also stale in `docs/planning/CURRENT_STATE.md`:

- Implemented section correctly says кнопка `+1` списывает BOM.
- “Что не реализовано” still says выпуск не списывает комплектующие. **This second sentence is outdated.** At `c1c3f51`, `POST /production/lines/<id>/produce-one` calls `apply_production_consumption()` when an active BOM exists.

What is **still missing** is not the entities themselves, but the **business rules** that connect shopfloor shifts to TMZ and to finished trailers.

---

## Context

Источник запроса шефа — `docs/planning/NEXT_ACTIONS.md`:

1. Проверить приход ТМЦ, роль `warehouse`, приход по плану, фильтр номенклатуры по участку.
2. Следующий этап: **смены лазера/гибки с фактическим списанием и сверхплановым выпуском**, затем синхронизация с 1С.
3. Бизнес-задача: **подтвердить новую модель производства** — участки, склады комплектующих/полуфабрикатов, сотрудники, смены и результаты месяца для директора.

Этот файл отвечает: что уже есть в Flask, чего нет как правила, что нельзя дописывать во Flask как вторую ERP, и какие вопросы нужно задать шефу до любого кода.

---

## What I inspected (read-only, committed baseline)

| Source | Finding |
|---|---|
| `docs/ai/DO_NOT_TOUCH.md` | Производство и ТМЦ — запретная зона без plan+approval. |
| `docs/ai/MIGRATION_TO_FLEXITY.md` | Target = Flexity `industry_trailers`, not a second Flask ERP. |
| `docs/ai/ROADMAP.md` | Этап 3 = производство+ТМЦ; этап 6 = кадры/смены; универсальные HR/склад/производство во Flexity. |
| `docs/ai/PROJECT_CONTEXT.md` | Модели workshop/employee/shift/output уже перечислены. |
| `docs/planning/CURRENT_STATE.md` | Контур ТМЦ и производства описан; часть пунктов про BOM устарела. |
| `docs/planning/NEXT_ACTIONS.md` | Запрос шефа по сменам лазера/гибки и модели участков. |
| `models.py` @ `c1c3f51` | Полный shopfloor + TMZ + trailer production graph. |
| `views.py` @ `c1c3f51` | Workspace tabs `shifts`/`performance`; produce-one; shift open/close/output. Dirty local `views.py` ignored. |
| `inventory_service.py` @ `c1c3f51` | Receipt, transfer, BOM check/consume/reverse, deficit. |
| `templates/production_workspace.html` | Tabs including Смены and Производительность. |
| `migrations/versions/f9a1b2c3d4e5_order_lines_production_foundation.py` | Seed 12 workshops + COMPONENTS/SEMI_FINISHED areas. |
| Flexity `docs/ai/PRODUCT_ARCHITECTURE.md` | Universal `inventory`/`production` still future; `industry_trailers` not started. |

---

## Current state map

### 1. Участки (`ProductionWorkshop`)

Seed from migration `f9a1b2c3d4e5`:

| code | name | workshop_type (seed) |
|---|---|---|
| `LIGHT_TRAILERS` | Легковые прицепы | `trailer_light` |
| `CARGO_TRAILERS` | Грузовые прицепы | `trailer_cargo` |
| `LASER` | Лазерная резка | `semi_finished` |
| `BENDING` | Листогиб | `semi_finished` |
| `WELDING` | Сварочный блок | `welding` |
| `TENTS` | Тенты | `tent` |
| `FRAMES` | Каркасы | `frame` |
| `TENT_FRAMES` | Тент-каркасы | `tent_frame` |
| `ELECTRIC` | Автоэлектрика | `electric` |
| `CONSTRUCTION` | Стройка | `construction` |
| `CARGO_VEHICLES` | Цех грузовых автомобилей | `cargo_vehicle` (later rename pass in `e8f0a1b2c3d4` maps some types toward `cargo_trailer`) |
| `OTHER_TASKS` | Прочие поручения | `other` |

Facts:

- There is **no CRUD screen** for workshops. They are a seeded catalog.
- Workspace quick-start buttons match workshops by substring in `code`/`name`: Лазер, Листогиб, Сварка, Сборка, Тенты/каркасы. “Сборка” has no dedicated seeded workshop code `ASSEMBLY`; it only lights up if a name/code contains `assembly`.
- `production_workshop_id` already hangs off `CustomerOrderLine`, `SupplyNeed`, `ProductionRequestLine`, `ProducedUnit`, BOM lines, inventory operations. Filling it is optional and not a required step of `produce-one`.

### 2. Сотрудники (`ProductionEmployee`)

- Profile is created on demand by `_get_or_create_production_employee(user)` from the logged-in `User`.
- Fields exist: `full_name`, `employee_code` (`U{user.id}`), `phone`, `default_workshop_id`, `primary_role`, `is_active`.
- There is **no HR directory UI**, no positions catalog, no payroll, no attendance except shift open/close.
- Roles `laser_operator` and `bending_operator` exist on `User.role` and can open/close shifts and add shift output.
- Trailer `+1` / `В работу` are `@role_required('production')` only. Laser/bending operators **cannot** emit a `ProducedUnit`.

### 3. Смены (`ProductionShift`)

UI: `/production/workspace?tab=shifts`

- Open: workshop optional, `work_area` defaults to workshop code or `production`.
- One open shift per employee.
- Close: own shift, or admin/director.
- Manager and director are also allowed on shift routes. That is broader than “цеховой сотрудник”.

This is a **labor/time card**, not a production order.

### 4. Два разных «выпуска»

Trailers currently has **two unconnected output loops**.

#### Loop A — готовый прицеп (операционный контур продаж)

```text
SupplyNeed (клиент или пополнение склада)
  -> ProductionRequest / ProductionRequestLine
  -> POST /production/lines/<id>/start
  -> POST /production/lines/<id>/produce-one
       creates ProducedUnit status=produced_no_vin
       if BOM exists: InventoryOperation production_issue + InventoryBalance decrease
       if no BOM: unit is created, warning flash, no TMZ write
  -> manager assigns VIN -> Trailer appears
  -> stock movement to sales warehouse
```

Director report `/reports/production` counts **this** loop (`ProducedUnit`), not shift output.

#### Loop B — цеховой факт смены (учёт выработки)

```text
User -> ProductionEmployee
  -> open ProductionShift on a workshop
  -> POST /production/shifts/<id>/output
       writes ProductionShiftOutput (quantity, defect_quantity, optional item_id)
       does NOT consume TMZ
       does NOT create ProducedUnit / Trailer
       does NOT set order_line_id / production_request_line_id
         (columns exist on the model, handler leaves them null)
  -> tab=performance aggregates qty/defect/hours by employee+workshop+item for a month
```

`ItemProductionRoute` / `ItemProductionRouteStep` exist in `models.py` and are **unused in views**.
`TrailerAssemblyOperation` exists as a schema stub for later комплектация and is **unused in views**.

### 5. ТМЦ

Operational now:

| Capability | Route / function | Bound to |
|---|---|---|
| Receipt | `/inventory/receipts/new` | warehouse + storage area + COMPONENT |
| Balances | `/inventory/balances`, workspace tab `materials` | production warehouses, COMPONENT only |
| Deficit | `/inventory/deficit` | open PR lines × BOM vs balances |
| Transfer | `/inventory/transfers/new` | production warehouses/areas |
| Suppliers / receipt plans | `/suppliers`, `/inventory/receipt-plans`, `/purchaser/dashboard` | role `warehouse` (+ legacy `purchaser`) |
| BOM edit | `/items/<id>/bom` | director; TRAILER header only |
| Consume on trailer `+1` | `apply_production_consumption` | required BOM lines, production warehouse of the branch |
| Reverse on admin delete of unit | `reverse_production_consumption` | director cleanup |

Storage-area inconsistency (important for the шеф model):

- Migration seeds `COMPONENTS` on **all** warehouses and `SEMI_FINISHED` on **production** warehouses.
- Runtime fallback `DEFAULT_STORAGE_AREAS` is different: `MAIN`, `METAL`, `ASSEMBLY`.
- UI grouping of balances (`металл / заготовки / комплектующие`) is a **name heuristic** in `tmz_group_code()`, not a hard link to area or workshop.
- `Item.component_category` and `is_controlled` exist from purchasing iteration; they do not drive shift consumption.

Shift output does **not** write `InventoryBalance`. Laser/bending “фактическое списание” from NEXT_ACTIONS is **not implemented**.

### 6. Roles that touch this map

| Role | Trailer jobs / `+1` | Shifts | TMZ |
|---|---|---|---|
| `production` | yes | yes | materials tab (read) |
| `laser_operator` / `bending_operator` | no | yes | no dedicated TMZ desk |
| `warehouse` / `purchaser` | no | no | receipt, balances, deficit, plans |
| `manager` | start production from CRM needs; VIN after unit | can open shifts (route allows) | no |
| `director` / `admin` | oversight; BOM; delete unit | shifts + monthly performance | TMZ + reports |

---

## Map to the шеф request in NEXT_ACTIONS

| Request | Already in Flask @ `c1c3f51` | Gap |
|---|---|---|
| Приход ТМЦ на производственный склад / зону | Yes (`/inventory/receipts/new`) | Needs live smoke, not new entities |
| Роль кладовщик+закупщик `warehouse` | Yes | Live account/check |
| Приход по плану | Yes | Live smoke |
| Фильтр номенклатуры по участку склада | Started in purchasing iteration | Confirm area catalog vs heuristic grouping |
| Смены лазера/гибки | Yes as time card + free-form qty | No metal/semi-finished consume; no over-plan rule |
| Фактическое списание на смене | Only on trailer `+1` via BOM | Shift output ignores inventory |
| Сверхплановый выпуск | No model | Need a rule: extra sheets? extra trailers? scrap? |
| Синхронизация с 1С | Fields only on `SalesRealization` | No production/1C export |
| Участки | Seeded catalog | No confirmation that this list matches the real shop |
| Склады комплектующих/полуфабрикатов | Storage areas, not separate warehouse master | Physical vs logical still unsigned |
| Сотрудники | Shadow of `User` | No HR master |
| Результаты месяца для директора | Two screens: `/reports/production` (finished units) and workspace `performance` (shift qty) | Which one is “результат месяца”? They can disagree |

---

## Classification: Flask hotfix vs Flexity universal vs industry_trailers

### A. Critical Flask hotfix (only if live is broken)

Allowed in Trailers **only** as a tiny approved live hotfix:

- Trailer `+1` blocked incorrectly, BOM reverse broken, workspace 500, warehouse receipt cannot post, deficit clearly wrong for a live job.
- Do **not** invent a second production ERP inside `views.py`.

Not a hotfix:

- Super-plan output.
- Laser/bending material consume.
- HR employees, payroll, 1C production export.
- Workshop CRUD, route engine, assembly BOM-delta.

Dirty local `views.py` on the working tree of `crm-roles-production-logistics` must not be mixed into this discussion or committed here.

### B. Universal Flexity modules (do not duplicate in Trailers)

| Domain | Flexity target | Why not Flask |
|---|---|---|
| количественный склад ТМЦ | future `inventory` | universal for any tenant |
| производственные задания / выпуск / маршруты как платформа | future `production` | universal |
| сотрудники, должности, начисления | future HR / payroll | explicit Flexity future; DO_NOT_TOUCH forbids HR module in Trailers |
| закупки / поставщики как платформа | future purchasing / inventory | Trailers already has a thin local slice |
| документы, оплаты, CRM | `documents` / `finance` / `workflows` | already forbidden to grow in Trailers |
| 1C / ЭСФ as platform integration | Flexity integrations | realization already has stub fields |

Flexity code today: `inventory` and `production` are **named future modules**, not implemented packages. Do not start them from this Trailers request.

### C. industry_trailers (Flexity package, later)

Trailer-specific, after a signed map and after Flexity universal inventory/production exist:

- VIN / ОТТС / конфигуратор / комплектация прицепа.
- Recipe: which workshop sequence turns metal → frame → trailer.
- Trailer BOM semantics (required vs substitute, tent/frame kits).
- `ProducedUnit` without VIN until manager assigns registry VIN.
- Production-warehouse rule: `Warehouse.is_production` does **not** exclude the warehouse from sales.

Copying Flask tables into Flexity as-is is forbidden.

### D. Tenant customization (not now)

Shop names, local area labels, 1C account mapping per company. Change Request only.

---

## Architecture risks

| Risk | Severity | Note |
|---|---|---|
| Two outputs that look like “выпуск” | high | Shift qty can be reported while no `ProducedUnit` exists, or vice versa. |
| Stale CURRENT_STATE about BOM | medium | Agents may “implement” consume that already exists. |
| Area seed vs DEFAULT_STORAGE_AREAS | medium | Metal/assembly/components catalogs can diverge per warehouse. |
| Manager can open shifts | medium | Mixes CRM role into shopfloor time cards. |
| `ItemProductionRoute` unused | medium | Looks like a route engine; it is schema only. |
| 1C mentioned next to laser shifts | high | Can pull tax/document contour into shopfloor work. |
| Wide `views.py` | high | Any shopfloor consume feature is a high-risk edit. |

---

## Open questions for the шеф (missing business rules)

1. **Что считает смена лазера/гибки?** Заготовки (кг/шт листа), детали по артикулу, или только часы/выработку человека?
2. **Откуда списывать металл?** Зона `METAL`, зона `COMPONENTS`, склад полуфабрикатов, или отдельный цеховой остаток, которого ещё нет?
3. **Сверхплан — это что?** Лист/деталь сверх BOM заявки, лишний готовый прицеп, или допустимый припуск на брак?
4. **Брак:** уменьшает остаток ТМЦ, идёт в отдельную статью, или только статистика `defect_quantity` как сейчас?
5. **Связь с заявкой:** смена обязана выбрать `ProductionRequestLine`, или лазер работает на общий задел без заказа?
6. **Результат месяца для директора:** готовые прицепы (`ProducedUnit`), выработка смен, расход металла, или все три отдельно?
7. **Список цехов:** 12 seeded names — это фактическая карта завода? Нужны ли «Стройка» и «Цех грузовых автомобилей»?
8. **Сборка:** отдельный участок или только зона склада `ASSEMBLY`?
9. **Кто сотрудник:** учётная запись `User`, отдельный кадровый список, или табель из 1С?
10. **Роли лазерщик/гибщик:** они никогда не жмут `+1` по прицепу, или это временное ограничение кода?
11. **1С:** что выгружать — списание ТМЦ, смены, или только реализации? Production export must not start from Flask.
12. **Комплектующие vs полуфабрикаты:** разные склады, разные зоны одного производственного склада, или разные `item_type` (сейчас TMZ = только `COMPONENT`)?

Until these are answered, no implementation plan for laser consume / over-plan should be written as code.

---

## Recommendation

1. Treat shopfloor **entities as already present**. Do not design a new workshop/employee/shift schema in Trailers.
2. Treat trailer `+1` + BOM consume as **already present**. Do not re-implement it.
3. Treat laser/bending actual consume, over-plan, and 1C as **unsigned business rules**, then:
   - if needed for live factory this month → tiny Flask hotfix plan, one loop only (almost certainly Loop B consume against TMZ), no HR, no 1C;
   - otherwise capture as Flexity `inventory` + `production` + `industry_trailers` after W3, not in Flask.
4. First live checks from NEXT_ACTIONS remain smoke of receipt / warehouse role / materials tab — they do not require code.

## Do not touch

- `views.py` (including dirty local edits)
- `models.py`, migrations, SQLite
- production server, systemd, `.env`
- Flexity production/inventory code (does not exist yet; do not start)
- Trailers as a place for universal HR / warehouse / production modules

## Next safe step

Get answers to questions 1–6 from the шеф. After that, either:

- a **live-hotfix plan** limited to linking `ProductionShiftOutput` to TMZ for LASER/BENDING, or
- a Flexity research brief for universal `inventory`/`production` using this map as reference.

No code until that choice is explicit.

## Final checks

- No code changes.
- No migrations.
- No deploy.
- No destructive git commands in the working tree of `crm-roles-production-logistics`.
- Dirty `views.py` not used as baseline.
