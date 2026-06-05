# P0-H: Stock Release VIN / Reservation Integrity — Repair Plan

**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Prerequisite:** Read `2026-06-05-trailers-stock-release-vin-reservation-p0h-report.md` first.
**Status:** PLAN ONLY — no data has been modified

---

## Summary of cases requiring action

| ID | Case | Severity | Action type |
|----|------|----------|-------------|
| R1 | ORD-000078 — sold + docs, no trailer | P0-3 / watch | Operational follow-up + future script repair |
| R2 | Trailer 987 — placeholder VIN MX4000002T0000000 | P1-C | Manual VIN update when real VIN is known |
| R3 | Trailer 784 — placeholder VIN 00000000000000000000 | P1-C | Investigate + update or retire |

---

## R1: ORD-000078 — Sold order without physical trailer

### Affected records

| Record | id | Key fields |
|--------|----|-----------|
| customer_order | 78 | ORD-000078, status=sold_not_shipped, trailer_id=NULL, docs_issued=1 |
| customer_order_line | 80 | order_id=78, vin_registry_id=1042, trailer_id=NULL, fstatus=ready_for_documents |
| vin_registry | 1042 | vin=MX4000002T0002706, status=reserved, trailer_id=NULL |

### Context

- `fulfillment_source='later'` — the physical trailer is to be created via production.
- Documents were issued before the trailer was produced.
- VIN `MX4000002T0002706` is reserved for this order but not physically applied.
- No `produced_unit` or `reservation` exists yet.

### Required links to restore (when trailer is produced)

```
trailer (new)  ←→  customer_order.trailer_id = new_trailer.id
trailer (new)  ←→  customer_order_line.trailer_id = new_trailer.id
trailer (new)  ←→  vin_registry.trailer_id = new_trailer.id
vin_registry status: reserved → confirmed
produced_unit (new or existing) → trailer_id = new_trailer.id
```

### Safe repair procedure (when production is ready)

**Pre-conditions (verify before applying):**
1. A `Trailer` record exists with `vin='MX4000002T0002706'`.
2. `vin_registry id=1042` still has `trailer_id IS NULL` and `status='reserved'`.
3. `customer_order id=78` still has `trailer_id IS NULL`.

**Step 1 — Link vin_registry to trailer:**
```sql
UPDATE vin_registry
   SET trailer_id = <new_trailer_id>,
       status = 'confirmed',
       confirmed_at = CURRENT_TIMESTAMP
 WHERE id = 1042
   AND trailer_id IS NULL
   AND status = 'reserved';
```

**Step 2 — Link order and line to trailer:**
```sql
UPDATE customer_order
   SET trailer_id = <new_trailer_id>
 WHERE id = 78
   AND trailer_id IS NULL;

UPDATE customer_order_line
   SET trailer_id = <new_trailer_id>
 WHERE id = 80
   AND trailer_id IS NULL;
```

**Step 3 — Link or create produced_unit:**
If a `produced_unit` was created by production, update:
```sql
UPDATE produced_unit
   SET trailer_id = <new_trailer_id>
 WHERE order_id = 78
   AND trailer_id IS NULL;
```

**Step 4 — Verify:**
```sql
SELECT o.id, o.status, o.trailer_id,
       col.id, col.trailer_id,
       vr.status, vr.trailer_id
FROM customer_order o
JOIN customer_order_line col ON col.order_id = o.id
JOIN vin_registry vr ON vr.id = col.vin_registry_id
WHERE o.id = 78;
```

**Note:** This repair should be executed using the same dry-run/apply pattern
as `scripts/repair_produced_unit_27.py`. A new script
`scripts/repair_order_78_trailer_link.py` should be created when the
physical trailer is ready.

### Monitoring

Until the physical trailer is produced, run weekly:
```bash
python3 scripts/audit_stock_release_vin_reservation_integrity.py --db instance/trailers.db
```
and verify P0-3 count does not grow beyond 1.

---

## R2: Trailer 987 — Placeholder VIN (MX4000002T0000000)

### Affected records

| Record | id | Key fields |
|--------|----|-----------|
| trailer | 987 | vin=MX4000002T0000000, status=SOLD, warehouse_id=2 |
| vin_registry | 976 | vin_full=MX4000002T0000000, status=confirmed, trailer_id=987, order_id=27 |
| customer_order | 27 | ORD-000027, status=sold_not_shipped, trailer_id=987, docs_issued=1 |
| produced_unit | 18 | status=vin_assigned, trailer_id=987 |

### Context

- Trailer 987 was produced (produced_unit id=18, status=vin_assigned).
- The VIN `MX4000002T0000000` (serial7=`0000000`) is a placeholder.
- ORD-000027 has documents issued.
- The real physical VIN stamped on the trailer is not recorded.
- The P0-G1 guard (commit 6a8df58) now prevents new placeholder VINs via UI.

### Required action

When the real VIN is confirmed from the physical trailer:

**Step 1 — Update trailer VIN:**
```sql
UPDATE trailer
   SET vin = '<REAL_VIN>'
 WHERE id = 987
   AND vin = 'MX4000002T0000000';
```

**Step 2 — Update vin_registry:**
```sql
UPDATE vin_registry
   SET vin_full = '<REAL_VIN>',
       serial7 = '<LAST_7_OF_REAL_VIN>'
 WHERE id = 976
   AND vin_full = 'MX4000002T0000000';
```

**Pre-conditions:**
- Confirm the real VIN is not already used by another trailer or vin_registry entry.
- Use `_validate_trailer_vin` logic to verify uniqueness before applying.
- Take a DB backup before applying.

**Priority:** P1 — not an emergency, but must be resolved before
ORD-000027 can be shipped or VIN verification passes.

---

## R3: Trailer 784 — Placeholder VIN (00000000000000000000)

### Affected records

| Record | id | Key fields |
|--------|----|-----------|
| trailer | 784 | vin=00000000000000000000, status=SOLD, warehouse_id=3 |

### Context

- Trailer 784 is SOLD but has no order linked and no vin_registry entry.
- VIN is raw zeros (not even the MX4 prefix format).
- This appears to be a historical seed or test record.

### Required action

**Investigate:**
```sql
SELECT t.id, t.vin, t.status, t.warehouse_id,
       o.id, o.order_number
FROM trailer t
LEFT JOIN customer_order o ON o.trailer_id = t.id
WHERE t.id = 784;
```

Options:
1. If this is a real trailer: obtain the real VIN and update as in R2.
2. If this is a test/seed record: mark as `status='VOID'` or similar
   to exclude from active inventory (requires code review of status model).

**Priority:** P1 — lower urgency, no active order at risk.

---

## What was NOT proposed

- Deleting VINs or produced_units — as instructed, only link restoration is proposed.
- Deleting or voiding ORD-000078 — it is an active order in the sales workflow.
- Modifying reservations — all ACTIVE reservations correspond to open orders.

---

## Preventive measures already in place

| Guard | Status |
|-------|--------|
| `_validate_trailer_vin` in `trailer_edit` and `trailer_create` routes | Implemented (commit 6a8df58) |
| Placeholder VIN list (`_VIN_PLACEHOLDERS`) | Implemented (commit 6a8df58) |
| Minimum VIN length check (7 chars) | Implemented (commit 6a8df58) |
| Uniqueness check against Trailer + VinRegistry | Implemented (commit 6a8df58) |

**New guard needed (future P1):**
When `documents_issued=1` is set on a `later`-fulfillment order,
the system should emit a warning if `customer_order.trailer_id IS NULL`.
This would have surfaced ORD-000078 at the moment docs were issued.

---

## Next steps

1. **Operational**: Contact logistics/production to schedule production for ORD-000078.
2. **R2**: When the real VIN for trailer 987 is known, run a repair script (same pattern as `repair_produced_unit_27.py`).
3. **R3**: Investigate trailer 784 origin and either update VIN or retire.
4. **Monitoring**: Run `audit_stock_release_vin_reservation_integrity.py` weekly until all P1-C cases are resolved.
5. **Future guard**: Add warning when `documents_issued` is set for orders with `fulfillment_source='later'` and no trailer.
