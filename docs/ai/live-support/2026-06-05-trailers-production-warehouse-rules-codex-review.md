# Codex Review: Production Warehouse Rules

Date: 2026-06-09
Repository: Trailers
Reviewed documents:

- `docs/ai/PROJECT_CONTEXT.md`
- `docs/ai/live-support/2026-06-05-trailers-production-warehouse-rules.md`
- `docs/ai/live-support/2026-06-05-trailers-sales-production-workflow-tz.md`
- `docs/ai/live-support/2026-06-05-trailers-workflow-tz-codex-review.md`

## Verdict

approve

The production warehouse rules document is clear enough to use as canonical context before continuing P0-M manager VIN workflow. It fixes the core ambiguity that caused prior regressions: `Warehouse.is_production` is an attribute of a normal warehouse, not an exclusion flag for sales, stock, orders, availability, or transfers.

## Checklist Review

| Check | Result | Notes |
|---|---|---|
| 1. Production warehouse is still a warehouse | approve | Section 2.1 states this directly and repeats that `is_production=True` remains part of warehouse and commercial flows. |
| 2. Prevents excluding `is_production` warehouses from sales, stock, orders, transfers | approve | Sections 2.1, 2.2, 5, 8, 9.2, 9.3, and 10 explicitly prohibit this regression. |
| 3. Defines internal areas: components, semi-finished goods, WIP, finished goods | approve | Section 3 defines all four areas and frames them as internal organization of the production warehouse. |
| 4. Explains finished trailers appear in finished goods area after production | approve | Section 4.1 states the finished trailer appears in finished goods after production and then participates in stock/sales. |
| 5. Separates trailers from goods/components | approve | Section 6 separates trailer workflow from goods/component lines; Section 7 documents the future non-trailer sales flow. |
| 6. States goods/components must not require VIN/trailer_id | approve | Section 6.2 explicitly says VIN, `trailer_id`, `vin_registry`, and `produced_unit` are not required for components/goods. |
| 7. Protects future P0-M/P0-L/P0-G/P1-A work from the same mistake | approve | Sections 8 and 9.3 give agent-facing guardrails that apply to VIN assignment, logistics, production guards, and future goods/component work. |
| 8. Contradictions with existing Trailers workflow TZ | approve | No blocking contradiction found. The TZ uses `Warehouse.is_production` / `_default_production_warehouse()` as production context and defers settings/schema work to P0-C; the new document clarifies that this must not exclude production warehouses from sales/stock/order/transfer flows. |
| 9. Safe to proceed to P0-M manager VIN workflow after this context is committed | approve | Yes, with the constraint that P0-M must preserve the trailer vs goods/component split and must not add `is_production` exclusion filters. |

## Notes

- The document correctly aligns with `PROJECT_CONTEXT.md`, which now contains the same core rule in short form.
- The document should be treated as documentation-only canonical context, not as approval for migrations, production data changes, deployment, role changes, or broad warehouse refactoring.
- Future P0-M implementation should cite this file when touching manager VIN selection, stock availability, production release handoff, or warehouse dropdown/filter logic.

## Next Safe Step

Proceed to P0-M manager VIN workflow planning/implementation only after this review is committed, keeping the change small and verifying that no production warehouse is excluded from relevant sales, stock, order, transfer, or VIN candidate flows.
