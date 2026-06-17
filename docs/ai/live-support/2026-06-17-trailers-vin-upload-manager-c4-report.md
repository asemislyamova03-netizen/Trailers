# C4 Report — VIN upload for production warehouse manager

**Date:** 2026-06-17
**Project:** Trailers
**Slice:** C4 (code + tests, no deploy)

---

## Current access before fix (diagnosis)

### Route `/logistics/vin-registry`

- Decorator: `@role_required('logistics', 'director', 'manager')` (+ admin bypass inside decorator).
- **POST upload:** hard block for all managers:
  - `if request.method == 'POST' and current_user.is_manager: abort(403)`
- **GET list:** managers see only VIN rows linked to their own orders/lines.
- Upload validation unchanged: full VIN parse / serial7 normalize, duplicate checks for registry + trailer, source field.

### Upload UI (`templates/vin_registry_list.html`)

- Upload button/form visible only for:
  - `admin`, `director`, `logistics`
- Managers (including production warehouse) could not see upload UI.

### Navigation (`templates/base.html`)

- Manager menu had **no** VIN registry / upload links.
- Logistics menu already uses neutral labels: `VIN-реестр`, `Загрузка VIN` (not "логистика" as page title).

### Production warehouse manager identification

Canonical signal in codebase:
- `User.role == 'manager'`
- `User.warehouse_id` -> `Warehouse.is_production == True` and `is_active`
- Existing related helper pattern: `_default_production_warehouse()`.

### Production role

- `production` role is **not** in `role_required` for vin registry routes -> no registry upload access (unchanged).

---

## What changed

### A) Helpers (`views.py`)

- `_user_production_warehouse(user)`
- `_is_production_warehouse_manager(user)`
- `_can_manage_vin_registry_upload(user)`
  - allowed: `admin`, `director`, `logistics`, production-warehouse `manager`
- Jinja globals:
  - `is_production_warehouse_manager()`
  - `can_manage_vin_registry_upload()`

### B) Route behavior (`vin_registry_list`)

- POST upload guard switched from "block all managers" to `_can_manage_vin_registry_upload()`.
- GET list filter: regular managers remain order-scoped; **production-warehouse managers** see full registry list (needed to manage uploaded free VIN rows).

### C) Templates

- `templates/vin_registry_list.html`
  - upload UI uses `can_manage_vin_registry_upload()`
  - page title remains `Реестр VIN` / `Загрузка VIN`
- `templates/base.html`
  - for production-warehouse manager: added menu entries `VIN-реестр` and `Загрузка VIN`

### D) Tests (new)

- `tests/test_vin_registry_upload_permissions.py`
  - production-warehouse manager allowed by helper and POST upload
  - normal manager blocked
  - non-production warehouse manager blocked
  - admin/director/logistics allowed
  - production role blocked
  - duplicate serial7 still blocked
  - template uses upload helper (not logistics-only gate)

---

## Logistics compatibility decision

**Kept compatibility:** logistics role still has upload access via `_can_manage_vin_registry_upload()`.

Reason:
- minimal-risk transition;
- no forced regression for existing logistics workflows;
- business rule focuses on removing logistics as mandatory owner, not immediate hard revoke.

Future optional step: remove logistics from upload helper after operational confirmation.

---

## Changed files

- `views.py`
- `templates/vin_registry_list.html`
- `templates/base.html`
- `tests/test_vin_registry_upload_permissions.py` (new)
- `docs/ai/live-support/2026-06-17-trailers-vin-upload-manager-c4-report.md` (this report)

Intentionally not touched:
- `models.py`, migrations, DB/data
- `vin_registry_detail.html` reserve/assign actions (out of C4 upload scope)
- order/customer/config code

---

## Tests executed

- `python -m py_compile views.py` -> **OK**
- `python -m unittest discover -s tests -v` -> **OK**
  - `Ran 104 tests`
  - `OK`

---

## Deploy recommendation

**APPROVE** for controlled deploy after pre-deploy verification.

Deploy scope:
- `views.py`
- `templates/vin_registry_list.html`
- `templates/base.html`
- optional `tests/test_vin_registry_upload_permissions.py`

No migrations. No data repair.

Prerequisite check on server:
- at least one manager user must have `warehouse_id` pointing to `Warehouse.is_production=True` to use new upload path.
