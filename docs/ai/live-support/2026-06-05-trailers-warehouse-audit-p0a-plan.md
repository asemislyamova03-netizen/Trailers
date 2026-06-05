# P0-A Plan: Read-only warehouse integrity audit

Date: 2026-06-05
Repository: Trailers
Source TZ: `docs/ai/live-support/2026-06-05-trailers-sales-production-workflow-tz.md`
Status: plan only

## 1. Task classification

This is a P0-A read-only data integrity audit plan for the legacy Flask Trailers project.

Purpose:

- detect records created without required warehouse/location context after production warehouse settings were removed;
- classify the impact by entity and workflow;
- prepare a separate P0-B read-only audit script/report.

This task is documentation-only. It does not authorize data fixes, guards, migrations, role changes, UI changes, deploy, or production database access.

Classification answers:

| Question | Answer |
|---|---|
| Temporary legacy Trailers fix? | No code fix yet; this is a legacy data audit plan. |
| Universal Flexity feature? | No. |
| Future `industry_trailers` logic? | Useful as reference mapping only. |
| Research/mapping without code change? | Yes. |

## 2. Entities, tables, and models to audit

Audit the current Flask models and table names, not target field names from the TZ.

| Area | Model | Table | Why audit |
|---|---|---|---|
| Customer orders without sales warehouse | `CustomerOrder` | `customer_order` | Order has no sales/working warehouse context. |
| Trailers without physical warehouse | `Trailer` | `trailer` | Physical stock cannot be located reliably. |
| Production requests without target warehouse | `ProductionRequest` | `production_request` | Production output has no destination warehouse. |
| Production context without production warehouse | `ProductionRequest`, `Warehouse` | `production_request`, `warehouse` | There is no `production_warehouse_id` field; current production context is derived from `Warehouse.is_production` / `_default_production_warehouse()`. |
| Supply needs without target warehouse | `SupplyNeed` | `supply_need` | Client-order and stock replenishment needs cannot resolve destination warehouse. |
| Order items with inconsistent source warehouse | `CustomerOrderLine` plus linked records | `customer_order_line`, `reservation`, `trailer`, `stock_movement`, `supply_need`, `produced_unit`, `customer_order` | Line source/destination is derived per fulfillment source and can be missing or contradictory. |

## 3. Exact fields to inspect

### `CustomerOrder`

Fields:

```text
CustomerOrder.id
CustomerOrder.order_number
CustomerOrder.status
CustomerOrder.fulfillment_source
CustomerOrder.customer_id
CustomerOrder.item_id
CustomerOrder.trailer_id
CustomerOrder.warehouse_id
CustomerOrder.source_warehouse_id
CustomerOrder.assigned_user_id
CustomerOrder.created_at
CustomerOrder.cancelled_at
CustomerOrder.is_shipped
CustomerOrder.documents_issued
```

Primary audit condition:

```text
CustomerOrder.warehouse_id IS NULL
```

Classification hint:

- active/open order without `warehouse_id` is P0;
- cancelled/closed/shipped historical order without `warehouse_id` is usually P2 unless it blocks reporting or linked active records.

### `Trailer`

Fields:

```text
Trailer.id
Trailer.vin
Trailer.item_id
Trailer.warehouse_id
Trailer.status
Trailer.lifecycle_status
Trailer.created_at
```

Primary audit condition:

```text
Trailer.warehouse_id IS NULL
```

Classification hint:

- trailer with saleable/stock/in-transfer/reserved lifecycle and no `warehouse_id` is P0;
- cancelled or obsolete records are P2 cleanup candidates.

### `ProductionRequest`

Fields:

```text
ProductionRequest.id
ProductionRequest.request_number
ProductionRequest.status
ProductionRequest.target_warehouse_id
ProductionRequest.created_at
ProductionRequest.note
```

Primary audit condition:

```text
ProductionRequest.target_warehouse_id IS NULL
```

Production warehouse context:

- there is no current `ProductionRequest.production_warehouse_id`;
- do not audit a non-existent field;
- inspect whether any active `Warehouse` has `is_production = true`;
- if no active production warehouse exists, classify as environment/config finding, not row-level missing data.

### `SupplyNeed`

Fields:

```text
SupplyNeed.id
SupplyNeed.need_type
SupplyNeed.status
SupplyNeed.order_id
SupplyNeed.order_line_id
SupplyNeed.item_id
SupplyNeed.warehouse_id
SupplyNeed.production_workshop_id
SupplyNeed.quantity
SupplyNeed.created_at
SupplyNeed.cancelled_at
```

Primary audit condition:

```text
SupplyNeed.warehouse_id IS NULL
```

Classification hint:

- active `CUSTOMER_ORDER` / client-order need without `warehouse_id` is P0 when linked order or line is still open;
- active stock replenishment need without `warehouse_id` is P0/P1 depending on whether production request already exists;
- cancelled needs without `warehouse_id` are P2 unless linked to active production lines or produced units.

### `CustomerOrderLine`

Fields:

```text
CustomerOrderLine.id
CustomerOrderLine.order_id
CustomerOrderLine.line_no
CustomerOrderLine.line_type
CustomerOrderLine.fulfillment_source
CustomerOrderLine.source_type
CustomerOrderLine.fulfillment_status
CustomerOrderLine.item_id
CustomerOrderLine.trailer_id
CustomerOrderLine.reservation_id
CustomerOrderLine.supply_need_id
CustomerOrderLine.production_request_line_id
CustomerOrderLine.stock_movement_id
CustomerOrderLine.status
CustomerOrderLine.quantity
```

Linked fields:

```text
CustomerOrder.warehouse_id
Reservation.status
Reservation.source_type
Reservation.trailer_id
Trailer.warehouse_id
SupplyNeed.warehouse_id
ProductionRequest.target_warehouse_id
ProductionRequestLine.production_request_id
ProducedUnit.target_warehouse_id
StockMovement.from_warehouse_id
StockMovement.to_warehouse_id
```

Per-source warehouse rules:

| `fulfillment_source` / state | Source warehouse check | Target warehouse check | Problem condition |
|---|---|---|---|
| `stock`, `stock_reserved` | `Reservation.trailer.warehouse_id` or `Trailer.warehouse_id` | `CustomerOrder.warehouse_id` | no linked trailer/reservation, trailer warehouse NULL, or order warehouse NULL |
| `other_warehouse`, `transfer`, `transit` | `StockMovement.from_warehouse_id` or selected `Trailer.warehouse_id` | `StockMovement.to_warehouse_id` and `CustomerOrder.warehouse_id` | no movement when required, movement warehouse NULL, target not equal to order warehouse when expected |
| `production`, `production_requested` | production output is expected from `SupplyNeed` / `ProductionRequestLine` / `ProducedUnit` | `SupplyNeed.warehouse_id`, `ProducedUnit.target_warehouse_id`, `CustomerOrder.warehouse_id` | no supply need, supply need warehouse NULL, produced unit target NULL, or target/order mismatch |
| `external`, `component`, assembly-only lines | not mandatory for trailer stock location | not mandatory unless linked to trailer/production | usually skip or P2 note |
| NULL / empty / `none` | no source selected | `CustomerOrder.warehouse_id` still required | record as `source_required`; P1 unless order is ready for fulfillment |

## 4. Proposed read-only audit queries and ORM checks

These checks are proposed for the next P0-B audit script/report. Do not execute them as part of this P0-A plan.

### SQL checks

Customer orders without warehouse:

```sql
SELECT id, order_number, status, fulfillment_source, customer_id, trailer_id,
       warehouse_id, source_warehouse_id, assigned_user_id, created_at,
       is_shipped, documents_issued, cancelled_at
FROM customer_order
WHERE warehouse_id IS NULL
ORDER BY created_at DESC, id DESC;
```

Trailers without warehouse:

```sql
SELECT id, vin, item_id, status, lifecycle_status, warehouse_id, created_at
FROM trailer
WHERE warehouse_id IS NULL
ORDER BY created_at DESC, id DESC;
```

Production requests without target warehouse:

```sql
SELECT id, request_number, status, target_warehouse_id, created_at, note
FROM production_request
WHERE target_warehouse_id IS NULL
ORDER BY created_at DESC, id DESC;
```

Production warehouse availability:

```sql
SELECT id, name, code, is_active, is_production
FROM warehouse
WHERE is_production = 1
ORDER BY is_active DESC, name ASC;
```

Supply needs without warehouse:

```sql
SELECT id, need_type, status, order_id, order_line_id, item_id,
       warehouse_id, production_workshop_id, quantity, created_at,
       cancelled_at
FROM supply_need
WHERE warehouse_id IS NULL
ORDER BY created_at DESC, id DESC;
```

Order lines with stock source but no usable trailer warehouse:

```sql
SELECT l.id AS line_id, l.order_id, l.line_no, l.fulfillment_source,
       l.source_type, l.fulfillment_status, l.trailer_id, l.reservation_id,
       o.order_number, o.warehouse_id AS order_warehouse_id,
       r.id AS reservation_id_joined, r.status AS reservation_status,
       r.trailer_id AS reservation_trailer_id,
       t.id AS trailer_id_joined, t.warehouse_id AS trailer_warehouse_id
FROM customer_order_line l
JOIN customer_order o ON o.id = l.order_id
LEFT JOIN reservation r
       ON r.order_line_id = l.id AND r.status = 'ACTIVE'
LEFT JOIN trailer t
       ON t.id = COALESCE(l.trailer_id, r.trailer_id)
WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('stock', 'stock_reserved')
  AND (t.id IS NULL OR t.warehouse_id IS NULL OR o.warehouse_id IS NULL)
ORDER BY o.created_at DESC, l.id DESC;
```

Order lines with transfer source but missing/incomplete movement:

```sql
SELECT l.id AS line_id, l.order_id, l.line_no, l.fulfillment_source,
       l.source_type, l.fulfillment_status, l.stock_movement_id,
       o.order_number, o.warehouse_id AS order_warehouse_id,
       m.id AS movement_id, m.status AS movement_status,
       m.from_warehouse_id, m.to_warehouse_id, m.trailer_id
FROM customer_order_line l
JOIN customer_order o ON o.id = l.order_id
LEFT JOIN stock_movement m
       ON m.id = l.stock_movement_id
       OR (m.order_line_id = l.id AND COALESCE(m.status, '') NOT IN ('cancelled', 'CANCELLED'))
WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('other_warehouse', 'transfer', 'transit')
  AND (
      m.id IS NULL
      OR m.from_warehouse_id IS NULL
      OR m.to_warehouse_id IS NULL
      OR o.warehouse_id IS NULL
      OR (m.to_warehouse_id IS NOT NULL AND o.warehouse_id IS NOT NULL AND m.to_warehouse_id != o.warehouse_id)
  )
ORDER BY o.created_at DESC, l.id DESC;
```

Order lines with production source but missing/inconsistent target warehouse:

```sql
SELECT l.id AS line_id, l.order_id, l.line_no, l.fulfillment_source,
       l.source_type, l.fulfillment_status, l.supply_need_id,
       l.production_request_line_id,
       o.order_number, o.warehouse_id AS order_warehouse_id,
       n.id AS need_id, n.warehouse_id AS need_warehouse_id,
       pr.id AS production_request_id, pr.target_warehouse_id,
       pu.id AS produced_unit_id, pu.target_warehouse_id AS produced_target_warehouse_id
FROM customer_order_line l
JOIN customer_order o ON o.id = l.order_id
LEFT JOIN supply_need n
       ON n.id = l.supply_need_id
       OR n.order_line_id = l.id
LEFT JOIN production_request_line prl
       ON prl.id = l.production_request_line_id
       OR prl.order_line_id = l.id
       OR prl.supply_need_id = n.id
LEFT JOIN production_request pr ON pr.id = prl.production_request_id
LEFT JOIN produced_unit pu
       ON pu.order_line_id = l.id
       OR pu.production_request_line_id = prl.id
WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('production', 'production_requested')
  AND (
      o.warehouse_id IS NULL
      OR n.id IS NULL
      OR n.warehouse_id IS NULL
      OR (pr.id IS NOT NULL AND pr.target_warehouse_id IS NULL)
      OR (pu.id IS NOT NULL AND pu.target_warehouse_id IS NULL)
      OR (n.warehouse_id IS NOT NULL AND o.warehouse_id IS NOT NULL AND n.warehouse_id != o.warehouse_id)
      OR (pu.target_warehouse_id IS NOT NULL AND o.warehouse_id IS NOT NULL AND pu.target_warehouse_id != o.warehouse_id)
  )
ORDER BY o.created_at DESC, l.id DESC;
```

Order lines without selected source:

```sql
SELECT l.id AS line_id, l.order_id, l.line_no, l.fulfillment_source,
       l.source_type, l.fulfillment_status, l.status,
       o.order_number, o.status AS order_status, o.warehouse_id AS order_warehouse_id
FROM customer_order_line l
JOIN customer_order o ON o.id = l.order_id
WHERE COALESCE(l.fulfillment_source, '') = ''
   OR lower(COALESCE(l.fulfillment_source, '')) IN ('none', 'source_required')
ORDER BY o.created_at DESC, l.id DESC;
```

### ORM checks

The P0-B script can use Flask app context and SQLAlchemy without committing:

```python
orders_without_warehouse = (
    CustomerOrder.query
    .filter(CustomerOrder.warehouse_id.is_(None))
    .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc())
    .all()
)
```

```python
trailers_without_warehouse = (
    Trailer.query
    .filter(Trailer.warehouse_id.is_(None))
    .order_by(Trailer.created_at.desc(), Trailer.id.desc())
    .all()
)
```

```python
production_requests_without_target = (
    ProductionRequest.query
    .filter(ProductionRequest.target_warehouse_id.is_(None))
    .order_by(ProductionRequest.created_at.desc(), ProductionRequest.id.desc())
    .all()
)
```

```python
supply_needs_without_warehouse = (
    SupplyNeed.query
    .filter(SupplyNeed.warehouse_id.is_(None))
    .order_by(SupplyNeed.created_at.desc(), SupplyNeed.id.desc())
    .all()
)
```

```python
production_warehouses = (
    Warehouse.query
    .filter(Warehouse.is_production.is_(True))
    .order_by(Warehouse.is_active.desc(), Warehouse.name.asc())
    .all()
)
```

For `CustomerOrderLine`, prefer explicit Python classification over one large mutation-prone query:

```python
for line in CustomerOrderLine.query.order_by(CustomerOrderLine.id.asc()).yield_per(200):
    source = (line.fulfillment_source or '').lower()
    order_warehouse_id = line.order.warehouse_id if line.order else None
    issues = []

    if source in ('stock', 'stock_reserved'):
        reservation = next((r for r in line.reservations if r.status == 'ACTIVE'), None)
        trailer = line.trailer or (reservation.trailer if reservation else None)
        if not trailer:
            issues.append('missing_stock_trailer')
        elif trailer.warehouse_id is None:
            issues.append('stock_trailer_warehouse_null')
        if order_warehouse_id is None:
            issues.append('order_warehouse_null')

    elif source in ('other_warehouse', 'transfer', 'transit'):
        movement = line.stock_movement or next(
            (m for m in line.order.movements if m.order_line_id == line.id and (m.status or '').lower() != 'cancelled'),
            None,
        )
        if not movement:
            issues.append('missing_stock_movement')
        else:
            if movement.from_warehouse_id is None:
                issues.append('movement_from_warehouse_null')
            if movement.to_warehouse_id is None:
                issues.append('movement_to_warehouse_null')
            if order_warehouse_id and movement.to_warehouse_id and movement.to_warehouse_id != order_warehouse_id:
                issues.append('movement_target_differs_from_order_warehouse')
        if order_warehouse_id is None:
            issues.append('order_warehouse_null')

    elif source in ('production', 'production_requested'):
        need = line.supply_need or next(iter(line.supply_needs), None)
        if not need:
            issues.append('missing_supply_need')
        elif need.warehouse_id is None:
            issues.append('supply_need_warehouse_null')
        if order_warehouse_id is None:
            issues.append('order_warehouse_null')
        for unit in line.produced_units:
            if unit.target_warehouse_id is None:
                issues.append('produced_unit_target_warehouse_null')
            elif order_warehouse_id and unit.target_warehouse_id != order_warehouse_id:
                issues.append('produced_unit_target_differs_from_order_warehouse')

    elif source in ('external', 'component'):
        pass
    else:
        issues.append('source_required')
```

The script must never call `db.session.commit()`, `db.session.flush()`, `db.session.delete()`, migration commands, or route handlers that mutate state.

## 5. Report format for results

P0-B should produce a read-only markdown report under `docs/ai/live-support/` or console output that can be copied into a report file.

Recommended report filename:

```text
docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0b-report.md
```

Report structure:

```text
# P0-B Warehouse Integrity Audit Report

Date:
Database/source:
Code branch/commit:
Read-only confirmation:

## Summary

| Severity | Count | Meaning |
|---|---:|---|
| P0 | 0 | Data integrity blockers |
| P1 | 0 | Workflow inconsistencies |
| P2 | 0 | Cleanup |

## Findings by entity

### CustomerOrder without warehouse
| severity | id | order_number | status | assigned_user_id | created_at | reason | recommended next step |

### Trailer without warehouse
| severity | id | vin | status | lifecycle_status | created_at | reason | recommended next step |

### ProductionRequest without target warehouse
| severity | id | request_number | status | created_at | reason | recommended next step |

### Production warehouse context
| severity | finding | warehouse_ids | reason | recommended next step |

### SupplyNeed without warehouse
| severity | id | need_type | status | order_id | order_line_id | created_at | reason | recommended next step |

### CustomerOrderLine source warehouse inconsistencies
| severity | line_id | order_id | line_no | fulfillment_source | issue_code | linked_record | reason | recommended next step |

## Totals by issue code

| issue_code | severity | count |

## Non-mutating recommendations

## Proposed P0-B/P0-C follow-ups
```

Each finding should include:

- entity/model;
- table;
- primary key;
- business identifier where available (`order_number`, VIN, `request_number`);
- current warehouse fields;
- linked record IDs;
- issue code;
- severity;
- reason;
- proposed next step without executing it.

## 6. Severity levels

### P0 data integrity blocker

Use P0 when the missing/inconsistent warehouse can cause wrong sale, wrong stock location, wrong production destination, duplicate reservation, or impossible fulfillment.

Examples:

- active `CustomerOrder` without `warehouse_id`;
- saleable/reserved/in-transfer `Trailer` without `warehouse_id`;
- active `ProductionRequest` without `target_warehouse_id`;
- active `SupplyNeed` without `warehouse_id` and linked to open order or production;
- production order line with produced unit whose `target_warehouse_id` is NULL;
- transfer order line with missing `StockMovement.from_warehouse_id` or `to_warehouse_id`;
- order line source requires stock/production/transfer but linked source record is missing.

### P1 workflow inconsistency

Use P1 when the record can confuse workflow/status/reporting but is not immediately proven to risk wrong physical stock.

Examples:

- `CustomerOrderLine.fulfillment_source` is NULL/empty for an open order that is not ready for fulfillment;
- order and produced unit target warehouse differ, but order is not yet shipped and no stock movement happened;
- stock movement target differs from order warehouse but movement is draft/pending;
- no active production warehouse configured while row-level target warehouses are present.

### P2 cleanup

Use P2 for historical or low-risk cleanup candidates.

Examples:

- cancelled/closed/shipped historical order without warehouse and no active linked records;
- cancelled supply need without warehouse;
- obsolete production request without target warehouse and no active lines/produced units;
- external/component line without warehouse where no trailer/production stock is involved.

## 7. Forbidden actions

This plan explicitly forbids:

- editing application code;
- editing templates;
- editing `models.py`;
- editing migrations or database schema;
- running migrations;
- running data-fix SQL;
- running route handlers or scripts that write data;
- calling `db.session.commit()`, `db.session.flush()`, or `db.session.delete()` in audit code;
- silently substituting `None` with a fallback warehouse;
- creating, updating, or deleting warehouse/order/trailer/production records;
- changing auth, role checks, route permissions, or UI visibility;
- installing dependencies;
- changing `.env`, secrets, Nginx, systemd, deploy scripts, or server state;
- deploying;
- pushing;
- deleting files;
- staging `.cursor`, backup folders, `changed_files_*.diff`, `changed_files_*.zip`, `chatgpt_changes_*`, or unrelated docs.

## 8. Next step after this plan

Create a P0-B read-only audit script/report.

Recommended scope:

- one script or command path that opens the Flask app context;
- read-only SQLAlchemy queries only;
- no commits to database;
- output markdown report with the format above;
- include counts and issue codes;
- stop after report generation.

Recommended P0-B output:

```text
docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0b-report.md
```

Do not implement P0-B guards, data repair, migrations, or production warehouse settings until the audit report is reviewed and approved.

## 9. Acceptance criteria

- Plan file exists at `docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0a-plan.md`.
- Plan is documentation-only.
- No application code was edited.
- No templates were edited.
- No database schema or migrations were edited.
- No dependency installation, deploy, push, Nginx/systemd, or database write was performed.
- The plan lists exact current models/tables to audit.
- The plan lists exact current fields to inspect.
- The plan includes read-only SQL and ORM checks to propose for P0-B.
- The plan defines report format for results.
- The plan defines P0/P1/P2 severity levels.
- The plan lists forbidden actions.
- The plan states the next step: P0-B read-only audit script/report.
- Any commit includes only this new docs file.
