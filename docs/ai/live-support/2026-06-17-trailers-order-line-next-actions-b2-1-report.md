# B2.1 Report — Line-Level Next Actions in Order Detail

**Date:** 2026-06-17
**Project:** Trailers (legacy Flask)
**Slice:** B2.1 (view-model + presentation only)
**Mode:** code + tests, no deploy

Plan: `docs/ai/live-support/2026-06-17-trailers-order-line-next-actions-b2-plan.md`

---

## What was implemented

### View-model helpers (`views.py`)

- `_line_next_action_item` — unified action dict (label, href/anchor, enabled, reason)
- `_order_line_is_closed` / `_order_line_needs_transfer` / `_order_line_source_unset`
- `_build_order_line_stage(order, row)`
- `_build_order_line_next_action(order, row, …)`
- `_build_order_line_secondary_actions(order, row, …)`
- `_enrich_order_line_rows_actions(…)` — called from `order_detail` after `order_line_rows` built

Each `order_line_rows` item now includes: `stage`, `next_action`, `secondary_actions`.

### Templates

- `templates/_order_line_next_action.html` — new partial (stage badge, primary CTA, disabled reason, secondary chips)
- `templates/order_detail.html`:
  - column **«Следующий шаг»** for TRAILER lines
  - anchor ids on existing forms: `line-{id}-actions`, `-create-need`, `-reserve-vin`, `-link-vin`, `-attach`, `-source`, `-transfer`
  - existing second-row POST forms **preserved**

### Not changed

- POST routes / business guards
- B1 order-level action panel logic
- DB / migrations / deploy

---

## Business rules compliance

| Rule | Status |
|------|--------|
| One primary next step per TRAILER line | yes |
| No new POST | yes |
| Existing routes/anchors | yes |
| Disabled shows reason | yes |
| No VIN confirmation step | yes (grep in tests) |
| No mandatory logistics wording | yes |
| Second-row forms kept | yes |

---

## Tests

| Check | Result |
|-------|--------|
| `python -m py_compile views.py` | OK |
| `python -m unittest discover -s tests -v` | **122 tests, OK** |
| `tests/test_order_line_next_actions.py` | **10/10 OK** |

Coverage highlights:
- missing catalog / unset source
- production shortage → create need
- in production → wait (informational)
- produced_no_vin → assign VIN link
- shipped → completed disabled
- non-manager → view only
- VIN ready → contract step
- HTTP smoke: `line-next-action` marker on `order_detail`

---

## HTML / test_client smoke

- Manager-owned order with lines renders column «Следующий шаг»
- `data-testid="line-next-action"` present per TRAILER line
- No `подтвердить VIN` / `обратитесь к логист` in page HTML

---

## Deploy recommendation

**APPROVE** controlled deploy after brief predeploy.

Deploy scope:
- `views.py`
- `templates/order_detail.html`
- `templates/_order_line_next_action.html`
- optional `tests/test_order_line_next_actions.py`

Risk: **low–medium** (presentation only; existing forms unchanged).

Manual check: open open-order with production line — primary CTA scrolls to existing form via anchor.

---

## Next step (B2.2, not in this slice)

- Optionally collapse duplicate inline forms once line CTA proven in production UI
- Harmonize B1 panel labels with line primaries
