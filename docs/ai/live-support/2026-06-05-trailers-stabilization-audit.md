# Trailers Live Stabilization Audit
**Date:** 2026-06-05
**Branch:** crm-roles-production-logistics
**Auditor:** Cursor Agent (live hotfix orchestrator)

---

## Project Structure

| Path | Description |
|------|-------------|
| `app.py` | Flask app factory (`create_app`), CLI commands, login manager |
| `wsgi.py` | WSGI entrypoint (`from app import create_app`) |
| `views.py` | 15 409 lines, 536 route functions, all in `main_bp` blueprint |
| `models.py` | 1 922 lines, SQLAlchemy models |
| `forms.py` | 891 lines, WTForms |
| `extensions.py` | Flask extensions init (db, migrate) |
| `inventory_service.py` | Inventory/warehouse business logic |
| `trailer_configurator.py` | Trailer config / allowed matrix logic |
| `kaspi_client.py` | Kaspi payment API client |
| `sigex_client.py` | SIGEX e-signing API client |
| `pdf_utils.py` | WeasyPrint contract PDF generation |
| `templates/` | 65 Jinja2 HTML templates |
| `static/` | CSS, JS static assets |
| `migrations/` | Flask-Migrate / Alembic, 35 migration files |
| `instance/trailers.db` | Live SQLite database |
| `venv_trailers/` | Python virtualenv |

## App Entrypoint

```
wsgi.py → app.py:create_app() → extensions (db, migrate, csrf) → views.main_bp
```

Flask-Login, Flask-WTF CSRF, Flask-SQLAlchemy, Flask-Migrate all initialized in `create_app`.

## Route Files

Single file: `views.py` — all routes registered on `main_bp`.
Key route groups:
- `/` — dashboard / workspace views (manager, director, logistics, production, purchaser)
- `/orders/` — customer orders, order lines, payments
- `/logistics/vin-registry` — VIN registry CRUD + confirm/void actions
- `/supply-needs/` — supply needs, production requests
- `/contracts/` — sales contracts, SIGEX e-signing
- `/inventory/` — warehouse receipts, transfers, movements
- `/trailers/` — trailer catalog, configurator
- `/customers/` — CRM customers, leads, conversations
- `/admin/` — admin-only management views

## Templates

65 templates. Key ones for live stabilization:
- `order_detail.html` — order lines, production needs, payments
- `vin_registry_list.html` — VIN upload + confirm actions
- `vin_registry_detail.html` — per-VIN actions
- `contract_sign.html` — NCALayer SIGEX signing flow
- `base.html` — nav, role-based menu

## Static Files

`static/css/`, `static/js/` — Bootstrap-based, custom styles and scripts.

## Config Files

- `.env` — secrets (DATABASE_URL, SIGEX credentials, KASPI token). **DO NOT TOUCH.**
- `migrations/alembic.ini` — Alembic config.

## Database / Migration Mechanism

- **Engine:** SQLite (`instance/trailers.db`) via Flask-SQLAlchemy.
- **Migrations:** Flask-Migrate (Alembic). 35 migration files in `migrations/versions/`.
- **Backups:** Multiple `.db` backup files in project root and `backups/`.
- **Migration state:** Last migration applied is `e7d8c9b0a1f2_unique_vin_registry_indexes.py`.

## Obvious Broken / Risk Areas

| Area | Issue | Severity |
|------|-------|----------|
| `vin_registry_list.html` line 80 | Confirm button guard `is_manager or is_director` missing `is_admin`; `role_required` bypasses for admin, so admin can POST confirm but UI hides button | P1 |
| `views.py` WeasyPrint | `WeasyPrint` import fails silently on server if not installed; PDF/SIGEX routes raise `RuntimeError` at runtime | P2 |
| `views.py` SIGEX | `sigex_expire_at` uses `datetime.utcnow()` (deprecated Python 3.12+) | P2 |
| `views.py` bare `except` | None found — all exceptions are typed | OK |
| Python syntax | All 5 main modules pass `ast.parse` | OK |

## Safe Check Commands Available

| Command | Status |
|---------|--------|
| `python -c "import ast; ast.parse(open('views.py').read())"` | PASSED |
| `git diff --check` | PASSED (no whitespace issues) |
| `python -m py_compile *.py` | PASSED (all .py files compile) |
| `flask routes` | SKIPPED — loading app reads `.env` with production DB/secrets; unsafe to run in this context |
| `pytest` | SKIPPED — no test suite found in project root |

## Forbidden Files (never stage or deploy)

- `.env`
- `instance/trailers.db`
- `backups/*.db`
- `trailers_backup_*.db`
- `migrations/versions/*.py` (no pending migration needed)
- `scripts/server_update.sh`
- `venv_trailers/`

## First 3 Low-Risk Fixes

1. **P1** — `vin_registry_list.html`: add `or current_user.is_admin` to confirm button guard (1-line template fix, reversible).
2. **P2** — `views.py`: replace `datetime.utcnow()` with `datetime.now(timezone.utc)` in SIGEX fields (Python 3.12 deprecation, ~4 occurrences).
3. **P2** — `views.py`: add a helper guard so `_prepare_sigex_contract_document` returns a user-friendly flash instead of raising `RuntimeError` when WeasyPrint unavailable.
