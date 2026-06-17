# VIN Registry “Открыть” Hotfix Report

**Date:** 2026-06-17
**Project:** Trailers (legacy Flask)
**Type:** diagnosis + code fix (no deploy)

---

## Symptom

In production VIN registry (`/logistics/vin-registry`), clicking **«Открыть»** for production-warehouse manager appeared to do nothing.

---

## Diagnosis (read-only)

### 1) Rendered HTML — link is valid

On production (`flexity` / `ubuntu@3.67.82.83`), rendered list contains proper anchors, not buttons:

```html
<a class="btn btn-sm btn-soft" href="/logistics/vin-registry/1065">Открыть</a>
```

- Element: `<a>` with real `href`
- No `href="#"`
- No `disabled` / `pointer-events` on link (CSS `pointer-events: none` in `app.css` applies elsewhere, not to this link)
- Not a JS-only button

**Conclusion:** not an HTML/template link bug.

### 2) Route / guard mismatch (root cause)

| Actor | GET list | First «Открыть» detail GET |
|-------|----------|----------------------------|
| prod manager (u2) | 200, 299 links | **403 Forbidden** |
| normal manager (u3) | 200, 21 links | **200** (own-order VIN) |
| director (u6) | 200 | **200** |
| admin (u1) | 200 | **200** |

C4 (`905872f`) expanded **list** visibility for production-warehouse manager:

```python
if current_user.is_manager and not _is_production_warehouse_manager():
    # order-scoped filter only for normal managers
```

But **detail** route still used old guard:

```python
if current_user.is_manager:
    order = _vin_registry_order(row)
    if not order or order.assigned_user_id != current_user.id:
        abort(403)
```

Effect: prod manager sees free/unassigned VIN rows in list (no `assigned_user_id` match) → click navigates → **403**. Browser shows forbidden page; user perceives “nothing opens”.

### 3) Not caused by

- `row.id` / `url_for` typo — correct in template
- Upload collapse overlay — link is in table below form; href works
- C4 upload helper — upload UI unrelated to detail GET
- Admin actions on detail page — still gated by `current_user.is_admin` / logistics / director in template

### 4) Access log interpretation

Request **is sent** (not HTML/JS/CSS block). Response **403** → route guard bug, not broken href.

---

## Fix

Added `_can_view_vin_registry_row(row)` mirroring list visibility:

- admin / director / logistics → allow
- production-warehouse manager → allow (full registry read)
- normal manager → only if VIN linked to manager-owned order
- production role → unchanged (still blocked at route decorator)

Updated `vin_registry_detail` to use helper instead of legacy manager-only check.

**Intentionally not changed:**

- upload POST guard
- reserve/assign/void/admin-edit/delete routes
- templates (link already correct)

---

## Changed files

- `views.py` — `_can_view_vin_registry_row`, detail guard
- `tests/test_vin_registry_open_link_permissions.py` — new regression tests
- `docs/ai/live-support/2026-06-17-trailers-vin-registry-open-link-hotfix-report.md` — this report

---

## Tests

| Check | Result |
|-------|--------|
| `python -m py_compile views.py` | OK |
| `python -m unittest discover -s tests -v` | **112 tests, OK** |
| New file `test_vin_registry_open_link_permissions.py` | **8/8 OK** |

Coverage:

- prod manager helper + detail GET 200 for free VIN
- normal manager own VIN allowed, foreign VIN 403
- director/admin allowed
- list template href pattern valid

---

## Deploy recommendation

**APPROVE** urgent hotfix deploy.

Scope:
- `views.py` only (templates unchanged)

Risk: **low** — aligns detail read guard with existing list read rule from C4; no new admin/logistics actions exposed.

Prerequisite: deploy on top of C4 runtime (`905872f` already on server).

---

## Business rules preserved

| Rule | Status |
|------|--------|
| prod-warehouse manager opens VIN cards from full registry | **fixed** |
| normal manager opens only own-order VINs | preserved |
| upload right does not grant void/admin-delete/admin-edit | preserved (template + route guards unchanged) |
| logistics/production routes unchanged | preserved |
