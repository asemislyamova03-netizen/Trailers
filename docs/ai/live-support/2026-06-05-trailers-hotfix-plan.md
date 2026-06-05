# Trailers Live Hotfix Plan
**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Session:** Stabilization / Codex closeout

---

## Already Committed Fixes (this branch)

| Commit | Files | Summary |
|--------|-------|---------|
| `039c80a` | `views.py`, `templates/order_detail.html` | Fix production request action for order needs — warehouse selection logic + "В производство" button guard |
| `071ab28` | `templates/vin_registry_list.html`, `templates/vin_registry_detail.html` | Clarify VIN confirmation actions — button visibility and action routing |

These fixes are **already in git**. Do not amend or rewrite.

---

## Remaining Issues

### P0 — Live-Breaking

*None identified at this time.*

---

### P1 — Client-Visible

#### VIN-001: Admin cannot see "Подтвердить" button on VIN list

**File:** `templates/vin_registry_list.html`, line 80
**Problem:** Button guard is `{% if current_user.is_manager or current_user.is_director %}`.
Route `vin_registry_confirm` has `@role_required('manager', 'director')`, but `role_required` always bypasses for `is_admin` (views.py line 96).
Admin can POST the confirm endpoint but never sees the button in the UI — invisible dead end.

**Fix:** Change line 80 from:
```jinja2
{% if current_user.is_manager or current_user.is_director %}
```
to:
```jinja2
{% if current_user.is_admin or current_user.is_manager or current_user.is_director %}
```

**Scope:** 1 line, 1 template file.
**Risk:** Low — only adds admin visibility, does not change route logic.
**Reversible:** Yes.

**Status:** APPLIED in this session — see commit below.

---

### P2 — Cleanup / Non-Blocking

#### P2-001: `datetime.utcnow()` deprecated (Python 3.12+)

**File:** `views.py`
**Pattern:** `datetime.utcnow()` used in SIGEX `sigex_expire_at` field assignments (~4 occurrences).
**Fix:** Replace with `datetime.now(timezone.utc)` (requires `from datetime import timezone` if not already imported).
**Status:** Deferred — not blocking, low urgency.

#### P2-002: WeasyPrint RuntimeError not user-friendly

**File:** `views.py` lines ~4087, ~4107
**Problem:** `raise RuntimeError("WeasyPrint is not available...")` surfaces as 500 instead of a flash message.
**Fix:** Catch at route level and return flash + redirect.
**Status:** Deferred — WeasyPrint is installed on live server per requirements.txt.

---

### P3 — Future Migration Mapping (Flexity industry_trailers)

| Area | Flexity Module |
|------|---------------|
| VIN registry | `industry_trailers.vin` |
| Production requests | `industry_trailers.production` |
| Sales contracts + SIGEX | `documents` (Flexity core) |
| Customer orders | `orders` (Flexity core) |
| Supply needs | `industry_trailers.supply` |
| Kaspi payments | `finance.integrations.kaspi` |
| Trailer configurator / catalog | `industry_trailers.catalog` |

---

## Dirty Working Tree — Intentionally NOT Staged

These files exist in the working tree but must remain uncommitted:

| File / Pattern | Reason |
|----------------|--------|
| `AGENTS.md` | Modified — AI context doc, not application code |
| `.cursor/` | Cursor IDE config — not application |
| `.cursorignore` | IDE config |
| `.gitattributes` | Unrelated line-ending config |
| `backup_before_*/` | Historical backups, never commit |
| `changed_files_*.diff`, `*.zip` | Generated artifacts |
| `chatgpt_changes_*` | AI session artifacts |
| `docs/ai/DO_NOT_TOUCH.md` | Already managed separately |
| `docs/ai/MIGRATION_TO_FLEXITY.md` | Future planning doc |
| `docs/ai/PROJECT_CONTEXT.md` | Context doc |
| `docs/ai/ROADMAP.md` | Roadmap doc |
| `docs/ai/handoffs/` | AI handoff docs |
| `docs/ai/research/2026-06-03-*` | Prior session research |
| `docs/adr/` | ADR templates |
| `README_WHAT_TO_COPY.md` | Reference doc |

---

## Session Commit

**Commit message:** `Stabilize Trailers live workflow actions`
**Files staged:**
- `templates/vin_registry_list.html` — P1 admin confirm button fix
- `docs/ai/live-support/2026-06-05-trailers-stabilization-audit.md` — audit
- `docs/ai/live-support/2026-06-05-trailers-hotfix-plan.md` — this plan

---

## Next Safe Steps

1. Review this commit locally (do not push yet).
2. Test VIN confirm flow in staging/dev with admin user.
3. Address P2-001 (`utcnow` deprecation) in a separate small commit.
4. When ready to deploy: follow `scripts/server_update.sh` protocol with manual approval.
