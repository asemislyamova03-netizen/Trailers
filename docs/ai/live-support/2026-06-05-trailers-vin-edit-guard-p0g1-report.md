# P0-G1 VIN Edit Guard Implementation Report

- Date: 2026-06-05
- Branch: crm-roles-production-logistics
- Context: P0-E root cause identified missing VIN validation in trailer edit/create routes
- Status: IMPLEMENTED

## Problem

`views.py` trailer edit route (~line 2014) and create route (~line 1935) wrote
`trailer.vin` directly from `request.form` with only `.strip().upper()` normalization,
and no uniqueness check and no format or placeholder validation.

This allowed:
- placeholder VINs (`00000000000000000000`, `MX000002`) to be persisted
- VINs that bypass the `vin_registry` workflow to be set directly
- no guard against duplicate VIN being set on a trailer via admin edit

## Changes made

### File: views.py

#### 1. New helper `_validate_trailer_vin()` (added near `_parse_vin_full`)

```python
_VIN_PLACEHOLDERS = frozenset({
    '', '-', 'N/A', 'NA', 'TEST', 'VIN', 'TBD', 'UNKNOWN', 'NONE', 'NULL',
    '0' * 7, '0' * 17, '0' * 20, '1' * 17, 'XXXXXXXXXXXXXXXXX',
})

def _validate_trailer_vin(raw, exclude_trailer_id=None) -> tuple[str|None, str|None]:
    ...
```

Validation steps:
1. `strip().upper()` normalization
2. Reject empty or placeholder values
3. Reject internal spaces
4. Reject length < 7 characters
5. Uniqueness check: `Trailer.query.filter(vin == vin, id != exclude_trailer_id)`
6. Cross-check `vin_registry`: block if a non-void/non-free registry row is linked to a different trailer with the same `vin_full`

Does **not** enforce strict 17-char MX4 format — preserves existing legacy VINs.

#### 2. `trailer_edit` route (admin-only)

Before:
```python
trailer.vin = (request.form.get('vin') or '').strip().upper()
...
db.session.commit()
```

After:
```python
new_vin, vin_error = _validate_trailer_vin(request.form.get('vin'), exclude_trailer_id=trailer.id)
if vin_error:
    flash(vin_error, 'danger')
    return render_template(...)
trailer.vin = new_vin
...
try:
    db.session.commit()
except Exception:
    db.session.rollback()
    flash('VIN уже существует или произошла ошибка сохранения...', 'danger')
    return render_template(...)
```

#### 3. `trailer_create` route (admin-only)

Before:
```python
vin = (request.form.get('vin') or '').strip().upper()
if not vin or not warehouse_id:
    flash('Укажите VIN и склад.', 'danger')
...
db.session.commit()
```

After:
```python
if not warehouse_id:
    flash('Укажите склад.', 'danger')
    return render_template(...)
vin, vin_error = _validate_trailer_vin(request.form.get('vin'), exclude_trailer_id=None)
if vin_error:
    flash(vin_error, 'danger')
    return render_template(...)
...
try:
    db.session.commit()
except Exception:
    db.session.rollback()
    flash('VIN уже существует или произошла ошибка сохранения...', 'danger')
    return render_template(...)
```

## Guard behavior summary

| Input | Result |
|---|---|
| Empty VIN | Rejected: "VIN не может быть пустым..." |
| `N/A`, `TEST`, `TBD`, etc. | Rejected: placeholder |
| VIN with spaces | Rejected: "VIN не должен содержать пробелы" |
| VIN shorter than 7 chars | Rejected: "VIN слишком короткий" |
| VIN already on another trailer | Rejected: "уже принадлежит другому прицепу" |
| VIN in active vin_registry for another trailer | Rejected: "уже используется в реестре VIN" |
| Same VIN on same trailer (no change) | Allowed: exclude_trailer_id check |
| Valid new VIN | Accepted |
| DB UNIQUE constraint fires anyway | Caught by `except Exception`, flash error, rollback |

## Permissions

No role changes. Both routes are already `@login_required + is_admin` check (trailer_edit)
or admin-only (trailer_create). Guard is additive only.

## Checks

- `python -m py_compile views.py` — PASS
- `python -m py_compile forms.py` — not changed
- `git diff --check` — clean

## What was NOT changed

- No migration
- No template changes
- No forms.py changes
- No production data changes
- No deploy or push
- No service restart
- No change to vin_registry workflow
- No change to production release logic (P0-G2)

## Next step

**P0-G2:** Add guard to the production VIN assignment route to require non-NULL
`production_warehouse` before creating a `Trailer` record. This prevents the
`produced_unit.trailer_id=NULL` failure mode identified in P0-E root cause analysis.
