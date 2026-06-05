# P0-E VIN & Production Integrity — Root Cause Analysis

- Date: 2026-06-05
- Branch: crm-roles-production-logistics
- Linked audit: docs/ai/live-support/2026-06-05-trailers-vin-production-integrity-p0d-report.md
- Status: root cause only — no code changes made

## 1. Summary of findings from P0-D

| Finding | Severity | Count |
|---|---|---|
| produced_unit vin_assigned but no trailer | P0 | 1 |
| production request done but no produced_unit | P1 | 7 |
| trailer.vin not in vin_registry | P1 | 6 |
| suspicious VIN values (placeholder, short) | P1 | 2 |

No exact duplicate VINs found. DB UNIQUE constraints on `trailer.vin`, `vin_registry.vin_full`, and `vin_registry.serial7` are present and working.

## 2. Root cause: produced_unit id=27 — vin_assigned with trailer_id=NULL

### Flow reconstruction

The VIN assignment for a stock replenishment unit (no customer order) uses the route:

```
/logistics/vin-registry/<vin_id>/assign-produced-unit
```

Or the `assign_vin_form` flow at approximately `views.py:13646`:

```python
existing_trailer = Trailer.query.filter_by(vin=vin).first()
# idempotency check...
if existing_trailer:
    # error returned, no trailer created
trailer = Trailer(vin=vin, item_id=unit.item_id, warehouse_id=production_warehouse.id, ...)
db.session.add(trailer)
db.session.flush()
_finish_idempotency(idem_key, 'Trailer', trailer.id)
unit.status = 'vin_assigned'
unit.trailer_id = trailer.id
# ...
db.session.commit()
```

### Likely failure mode for unit_id=27

**Scenario A — production_warehouse is NULL at assignment time:**
The code at line 13667 uses `production_warehouse.id`. If `production_warehouse` was None (no active production warehouse configured), this line would raise `AttributeError`, crashing the request before `db.session.commit()` is called. The idempotency key would have been written via `db.session.flush()` (line 13670), but the `trailer` and `unit.trailer_id` updates would be rolled back.

However, if `_finish_idempotency` was called with `db.session.flush()` (not commit), a subsequent retry may have been blocked by idempotency.

**Scenario B — exception between flush and commit:**
`db.session.flush()` writes the trailer to the DB but `commit()` completes the transaction. If an exception occurred between flush and commit — a network error, application exception, or timeout — the `produced_unit.trailer_id` assignment would be lost even if the flush succeeded (since SQLite transactions roll back on close).

**Scenario C — old code path for stock replenishment:**
Unit_id=27 has `order_id=NULL` — this is a stock replenishment production, not a customer order. The assign flow for stock units may have had a code path that set `unit.status='vin_assigned'` but failed to create the trailer record due to missing `production_warehouse` context.

**Most probable cause:** Missing or NULL `production_warehouse` at the time of VIN assignment in May 2026, causing a mid-transaction failure. The idempotency key may have been committed early (via flush), preventing retry.

## 3. Root cause: trailer.vin not in vin_registry (6 trailers)

Trailers 229, 232, 250 were created on 2025-12-08 — before the VIN registry feature existed. The `vin_registry` table was introduced later. These trailers have valid real VINs but were created through the pre-registry direct-entry flow.

Trailers 784 and 959 have placeholder/test VINs (`00000000000000000000`, `MX000002`) — these were manually entered via the trailer edit form with no registry validation.

**Root cause for 784 and 959:** The trailer edit route (views.py ~line 2014):

```python
trailer.vin = (request.form.get('vin') or '').strip().upper()
```

This sets `trailer.vin` directly **without**:
- checking if the VIN already exists in another trailer
- checking if the VIN exists in `vin_registry`
- validating VIN format (length, prefix)
- requiring a VIN registry row

This is the primary **missing guard** in the application. Although the DB UNIQUE constraint prevents exact duplicates, it does not prevent:
- Placeholder values being committed
- VINs bypassing the registry entirely

## 4. Missing validation points

| Location | File | Line | Missing check |
|---|---|---|---|
| Trailer edit route | views.py | ~2014 | No uniqueness check against other trailers |
| Trailer edit route | views.py | ~2014 | No format/length validation |
| Trailer edit route | views.py | ~2014 | No vin_registry lookup |
| Trailer create route | views.py | ~1941 | Verify — may also bypass registry |
| VIN assign (production) | views.py | ~13654 | EXISTS check present but depends on production_warehouse not being NULL |
| VIN admin-edit | views.py | ~11629 | Checks vin_registry only, not trailer.vin collision |

## 5. Missing DB constraints / indexes

| Table | Column | Current state | Required |
|---|---|---|---|
| `trailer` | `vin` | UNIQUE index present | ✓ OK |
| `vin_registry` | `vin_full` | UNIQUE index present | ✓ OK |
| `vin_registry` | `serial7` | UNIQUE index present | ✓ OK |
| `trailer` | `vin` (format) | No CHECK constraint | P0-C: consider CHECK length |

DB-level constraints are sufficient for exact duplicates. The problem is at the application layer.

## 6. Missing VIN normalization

The `trailer.vin` edit path normalizes via `.strip().upper()` only. It does not:
- Validate length (17 or valid serial7)
- Remove internal spaces or hyphens
- Check VIN prefix pattern
- Require non-placeholder values

The VIN upload route (`vin_registry_list` POST) does call `_parse_vin_full()` which validates length and format. But this guard is absent in the trailer direct-edit path.

## 7. Role/action flow problems

| Role | Action | Guard present |
|---|---|---|
| Logistics | Upload VIN to registry | ✓ `_parse_vin_full()` validation |
| Manager/Director | Assign VIN to trailer (from registry) | ✓ Checks `Trailer.query.filter_by(vin=vin)` |
| Manager/Director | Confirm VIN application | ✓ Checks `trailer.vin == registry.vin_full` |
| Admin/Manager/Director | Edit trailer directly (vin field) | ✗ **No uniqueness check, no format check** |
| Admin | Admin-edit VIN registry | ✓ Checks registry, but not `trailer.vin` cross-table |
| System | Production VIN assignment | ~ Checks `Trailer.filter_by(vin)` but can fail if `production_warehouse` is None |

## 8. Whether production release needs guard

Yes. The `assign_vin_form` flow must verify `production_warehouse` is not None before attempting `Trailer(warehouse_id=production_warehouse.id)`. If production_warehouse is None, the function should return an error to the user and not partially modify the session.

Current code does not have this guard:

```python
# views.py ~13630 area
# production_warehouse is resolved earlier in the function
# If it's None here, the next line will raise AttributeError
trailer = Trailer(... warehouse_id=production_warehouse.id ...)
```

## 9. Whether existing schema supports safe unique constraint

Yes. `trailer.vin` already has a UNIQUE index in production. The 6 historical trailers without registry entries and the 2 trailers with placeholder VINs do not violate uniqueness.

The `vin_registry.vin_full` and `vin_registry.serial7` UNIQUE indexes are also present.

No migration is needed for uniqueness. The gap is at the application guard level only.

## 10. Proposed next implementation plans

### P0-F: Data repair plan (requires separate approval)

1. `produced_unit id=27`:
   - Find the VIN registry row that was used during the failed VIN assignment (check `vin_registry` rows with `supply_need_id=29` or `production_request_line_id=27`).
   - If a matching trailer exists: set `produced_unit.trailer_id = <trailer.id>`.
   - If no matching trailer exists: reset `produced_unit.status = 'produced_no_vin'` and re-run VIN assignment.
   - Use a one-off repair script with explicit backup before execution.

2. Trailers 784 and 959 (placeholder VINs):
   - Both are SOLD + customer_shipped — no active reservation or order link.
   - Historical records only. No immediate repair needed.
   - P2 note: update to correct VIN if real VIN is known; otherwise mark lifecycle_status=decommissioned.

3. Trailers 229, 232, 250, 828 (pre-registry VINs):
   - All SOLD + customer_shipped.
   - If real VIN registry entries are needed for audit/reporting: create registry rows with status=confirmed and link to these trailers.
   - Not blocking current operations.

### P0-G: Guard implementation plan (requires separate approval)

1. **Trailer edit route** (`trailer_edit`, views.py ~line 1990):
   - Add uniqueness check: `Trailer.query.filter(Trailer.vin == new_vin, Trailer.id != trailer.id).first()`
   - Add format validation: reject VINs not matching 17-char pattern or valid 7-char serial.
   - Add VIN registry cross-check: warn if VIN not in registry.
   - Restrict who can set VIN via direct edit (director/admin only, or remove field entirely).

2. **Production VIN assignment** (`assign_vin_form`, views.py ~line 13580):
   - Add explicit guard: if `production_warehouse is None` → flash error and return, do not proceed.
   - Wrap `db.session.flush()` + `db.session.commit()` in try/except with rollback on failure.

3. **Trailer create route** (views.py ~line 1941):
   - Audit whether VIN can be set on creation without registry check.
   - Add the same uniqueness + format validation as the edit route.

## 11. Acceptance criteria for P0-F and P0-G

- `produced_unit id=27` has either `trailer_id != NULL` or `status='produced_no_vin'`.
- Trailer edit route rejects VINs already assigned to other trailers.
- Trailer edit route rejects VINs shorter than 7 chars or with obvious placeholder patterns.
- Production VIN assignment returns error if production_warehouse is None.
- All app-level changes are covered by manual test of the affected flows.
- No new migration required (existing UNIQUE indexes are sufficient).
