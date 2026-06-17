# C2 Report — contract with reserved VIN for production order

**Date:** 2026-06-17
**Project:** Trailers
**Slice:** C2 (code + tests, no deploy)

---

## Diagnosis result

### Route / contract line build (`views.py`)

- `order_contract_create` checks:
  - order status / documents_issued / is_shipped;
  - `_order_trailer_catalog_blockers(order)`;
  - `get_order_effective_vin(order)` (reserved/assigned/confirmed VIN accepted);
  - existing contract guards.
- **No `trailer_id` requirement** at contract-create stage.
- `_create_contract_lines_from_order` builds `SalesContractLine` with:
  - `vin_registry_id` + `vin_full` from reserved VIN row;
  - `trailer_id=None` when no physical trailer linked.

### Document / realization blockers (unchanged)

- `_order_lines_document_blockers` still requires physical trailer for `fulfillment_source in ('stock','production')`.
- `_realization_post_blockers` still extends document blockers.
- `order_issue_documents` still blocked without physical trailer.

### UI false blocker found and fixed

- `templates/order_detail.html` used `not document_blockers` to show **Создать договор**.
- Because `document_blockers` includes physical-trailer requirement, manager could not see contract action even when route would allow reserved-VIN contract create.
- Fix: separate `contract_create_blockers` via new helper `_order_contract_create_blockers(order)`.

---

## What changed

### Code

- `views.py`
  - added `_order_contract_create_blockers(order)`;
  - passed `contract_create_blockers` into `order_detail` context.

- `templates/order_detail.html`
  - create-contract button now gated by `contract_create_blockers`, not `document_blockers`;
  - separate hint lines for contract vs document blockers.

### Tests (new)

- `tests/test_order_contract_reserved_vin.py`
  - reserved VIN + production line + no trailer => contract blockers empty, document blockers still require trailer;
  - missing catalog blocks contract;
  - missing VIN blocks contract;
  - `order_contract_create` succeeds with reserved VIN and no `trailer_id` (mocked);
  - contract line includes `vin_registry_id` / `vin_full` without `trailer_id`;
  - `order_issue_documents` still blocked without physical trailer;
  - `_realization_post_blockers` still blocked without physical trailer;
  - no "подтвердить VIN" wording in contract blockers.

---

## Tests executed

- `python -m py_compile views.py` -> **OK**
- `python -m unittest discover -s tests -v` -> **OK**
  - `Ran 96 tests`
  - `OK`

---

## Changed files

- `views.py`
- `templates/order_detail.html`
- `tests/test_order_contract_reserved_vin.py` (new)
- `docs/ai/live-support/2026-06-17-trailers-contract-reserved-vin-c2-report.md` (this report)

---

## Code changed or tests-only?

**Code + tests** (minimal UI/helper fix + regression tests).

Route logic was already mostly correct; main gap was UI gating on document blockers.

---

## Deploy recommendation

**APPROVE** for controlled deploy after pre-deploy verification.

Scope:
- `views.py`
- `templates/order_detail.html`
- optional `tests/test_order_contract_reserved_vin.py`

No migrations. No data repair. Preserve prior runtime fixes (P0, config, VIN Phase A, customer replacement/search, B1 action panel, C3 tabs, C1 VIN reserve).
