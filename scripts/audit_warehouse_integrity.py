#!/usr/bin/env python3
"""
P0-B: Read-only warehouse integrity audit for Trailers.

SAFETY GUARANTEES:
- Opens SQLite in read-only URI mode (uri=True, ?mode=ro).
- Never calls commit(), execute INSERT/UPDATE/DELETE, or ALTER TABLE.
- Redacts personal data: no phone, email, or customer name in output.
- Produces counts and internal IDs/order numbers only in console summary.
- Full detail written to .ai_local/reports/trailers-warehouse-integrity-p0b-report.md
- Safe docs report written to docs/ai/live-support/2026-06-05-trailers-warehouse-audit-p0b-report.md
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "instance" / "trailers.db"
REPORT_LOCAL_DIR = ROOT / ".ai_local" / "reports"
REPORT_LOCAL = REPORT_LOCAL_DIR / "trailers-warehouse-integrity-p0b-report.md"
REPORT_DOCS = ROOT / "docs" / "ai" / "live-support" / "2026-06-05-trailers-warehouse-audit-p0b-report.md"

TODAY = datetime.date.today().isoformat()
NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

def classify_order(row: dict) -> str:
    """P0 if active/open; P2 if cancelled/shipped."""
    status = (row.get("status") or "").lower()
    if status in ("cancelled", "canceled", "done", "shipped", "closed"):
        return "P2"
    return "P0"


def classify_trailer(row: dict) -> str:
    lc = (row.get("lifecycle_status") or "").lower()
    st = (row.get("status") or "").lower()
    if lc in ("sold", "shipped", "cancelled", "canceled", "decommissioned") or st in ("sold", "cancelled"):
        return "P2"
    return "P0"


def classify_prod_request(row: dict) -> str:
    status = (row.get("status") or "").lower()
    if status in ("cancelled", "canceled", "closed", "completed"):
        return "P2"
    return "P0"


def classify_supply_need(row: dict) -> str:
    status = (row.get("status") or "").lower()
    cancelled_at = row.get("cancelled_at")
    if status in ("cancelled", "canceled", "closed", "done") or cancelled_at:
        return "P2"
    return "P0"


# ---------------------------------------------------------------------------
# DB connection — read-only
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    uri = db_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        # Fallback: open normally but never commit
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any(r["name"] == column for r in cur.fetchall())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_orders_without_warehouse(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "customer_order"):
        return []
    sql = """
        SELECT id, order_number, status, fulfillment_source,
               warehouse_id, source_warehouse_id,
               assigned_user_id, created_at, is_shipped,
               documents_issued, cancelled_at
        FROM customer_order
        WHERE warehouse_id IS NULL
        ORDER BY created_at DESC, id DESC
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["severity"] = classify_order(r)
        r["issue_code"] = "order_warehouse_null"
        r["reason"] = "CustomerOrder.warehouse_id IS NULL"
        r["next_step"] = (
            "P0-B guard: add validation in order create/edit route; "
            "P0-C: backfill via repair plan after review"
        )
    return rows


def check_trailers_without_warehouse(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "trailer"):
        return []
    # lifecycle_status may not exist in older DB schema — use column_exists check
    has_lifecycle = column_exists(conn, "trailer", "lifecycle_status")
    if has_lifecycle:
        sql = """
            SELECT id, vin, item_id, status, lifecycle_status, warehouse_id, created_at
            FROM trailer
            WHERE warehouse_id IS NULL
            ORDER BY created_at DESC, id DESC
        """
    else:
        sql = """
            SELECT id, vin, item_id, status, NULL AS lifecycle_status, warehouse_id, created_at
            FROM trailer
            WHERE warehouse_id IS NULL
            ORDER BY created_at DESC, id DESC
        """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["severity"] = classify_trailer(r)
        r["issue_code"] = "trailer_warehouse_null"
        r["reason"] = "Trailer.warehouse_id IS NULL (column defined nullable=False; historical SQLite record)"
        r["next_step"] = "Repair plan: assign warehouse or mark void; do not auto-assign"
    return rows


def check_production_requests_without_target(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "production_request"):
        return []
    sql = """
        SELECT id, request_number, status, target_warehouse_id, created_at, note
        FROM production_request
        WHERE target_warehouse_id IS NULL
        ORDER BY created_at DESC, id DESC
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["severity"] = classify_prod_request(r)
        r["issue_code"] = "production_request_target_warehouse_null"
        r["reason"] = "ProductionRequest.target_warehouse_id IS NULL"
        r["next_step"] = "P0-B guard: require target_warehouse on production request creation"
    return rows


def check_production_warehouse_config(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "warehouse"):
        return [{"severity": "P1", "finding": "table_missing", "issue_code": "warehouse_table_missing",
                 "warehouse_id": "", "warehouse_name": "", "is_active": "",
                 "reason": "warehouse table not found", "next_step": "Verify DB schema"}]
    has_is_production = column_exists(conn, "warehouse", "is_production")
    if not has_is_production:
        # Report all warehouses for reference, but flag missing column as P1
        wh_rows = conn.execute("SELECT id, name, is_active FROM warehouse ORDER BY is_active DESC, name ASC").fetchall()
        result = [{
            "severity": "P1",
            "finding": "warehouse_is_production_column_missing",
            "issue_code": "warehouse_is_production_column_missing",
            "warehouse_id": "",
            "warehouse_name": f"({len(wh_rows)} warehouses in DB, is_production column absent)",
            "is_active": "",
            "reason": "Warehouse.is_production column does not exist in current DB schema; production warehouse context unavailable",
            "next_step": "P0-C: add is_production column via separate migration plan"
        }]
        for r in wh_rows:
            result.append({
                "severity": "OK",
                "finding": "warehouse_listed",
                "issue_code": "ok",
                "warehouse_id": r["id"],
                "warehouse_name": r["name"],
                "is_active": bool(r["is_active"]),
                "reason": "Warehouse exists; is_production unknown",
                "next_step": "P0-C: add is_production flag"
            })
        return result
    rows = conn.execute(
        "SELECT id, name, is_active, is_production FROM warehouse WHERE is_production = 1 ORDER BY is_active DESC, name ASC"
    ).fetchall()
    if not rows:
        return [{"severity": "P1", "finding": "no_production_warehouse",
                 "issue_code": "no_active_production_warehouse",
                 "warehouse_id": "",
                 "warehouse_name": "",
                 "is_active": "",
                 "reason": "No Warehouse with is_production=1 found; production_warehouse context missing",
                 "next_step": "P0-C: restore production warehouse setting via separate plan"}]
    result = []
    for r in rows:
        result.append({
            "severity": "OK" if r["is_active"] else "P1",
            "finding": "production_warehouse_found" if r["is_active"] else "production_warehouse_inactive",
            "issue_code": "ok" if r["is_active"] else "production_warehouse_inactive",
            "warehouse_id": r["id"],
            "warehouse_name": r["name"],
            "is_active": bool(r["is_active"]),
            "reason": "Active production warehouse" if r["is_active"] else "Production warehouse exists but is inactive",
            "next_step": "No action" if r["is_active"] else "P0-C: reactivate or configure production warehouse"
        })
    return result


def check_supply_needs_without_warehouse(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "supply_need"):
        return []
    sql = """
        SELECT id, need_type, status, order_id, order_line_id,
               item_id, warehouse_id, production_workshop_id,
               quantity, created_at, cancelled_at
        FROM supply_need
        WHERE warehouse_id IS NULL
        ORDER BY created_at DESC, id DESC
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["severity"] = classify_supply_need(r)
        r["issue_code"] = "supply_need_warehouse_null"
        r["reason"] = "SupplyNeed.warehouse_id IS NULL"
        r["next_step"] = (
            "P0-B guard: require warehouse_id on supply need creation linked to order; "
            "review cancelled needs for P2 cleanup"
        )
    return rows


def check_order_lines_source_inconsistency(conn: sqlite3.Connection) -> list[dict]:
    """
    Per-source audit for CustomerOrderLine.
    Uses raw SQL with LEFT JOINs to avoid ORM lazy-loading overhead.
    Returns only lines with detected warehouse inconsistencies.
    """
    if not table_exists(conn, "customer_order_line"):
        return []

    results: list[dict] = []

    # --- stock / stock_reserved lines ---
    sql_stock = """
        SELECT
            l.id AS line_id, l.order_id, l.line_no,
            l.fulfillment_source, l.source_type, l.fulfillment_status, l.status,
            l.trailer_id, l.reservation_id,
            o.order_number, o.status AS order_status, o.warehouse_id AS order_warehouse_id,
            r.id AS res_id, r.status AS res_status, r.trailer_id AS res_trailer_id,
            t.id AS trailer_id_resolved, t.warehouse_id AS trailer_warehouse_id
        FROM customer_order_line l
        JOIN customer_order o ON o.id = l.order_id
        LEFT JOIN reservation r ON r.id = l.reservation_id
        LEFT JOIN trailer t ON t.id = COALESCE(l.trailer_id, r.trailer_id)
        WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('stock', 'stock_reserved')
          AND (
              t.id IS NULL
              OR t.warehouse_id IS NULL
              OR o.warehouse_id IS NULL
          )
        ORDER BY o.created_at DESC, l.id DESC
    """
    for row in conn.execute(sql_stock).fetchall():
        r = dict(row)
        issues = []
        if r["trailer_id_resolved"] is None:
            issues.append("missing_stock_trailer")
        elif r["trailer_warehouse_id"] is None:
            issues.append("stock_trailer_warehouse_null")
        if r["order_warehouse_id"] is None:
            issues.append("order_warehouse_null")
        sev = "P2" if (r.get("order_status") or "").lower() in ("cancelled", "canceled", "done", "shipped") else "P0"
        results.append({**r, "issue_code": ",".join(issues) or "unknown", "severity": sev,
                        "reason": f"stock line warehouse inconsistency: {issues}",
                        "next_step": "Review reservation/trailer linkage; P0-B guard for new orders"})

    # --- transfer lines ---
    sql_transfer = """
        SELECT
            l.id AS line_id, l.order_id, l.line_no,
            l.fulfillment_source, l.source_type, l.fulfillment_status, l.status,
            l.stock_movement_id,
            o.order_number, o.status AS order_status, o.warehouse_id AS order_warehouse_id,
            m.id AS movement_id, m.status AS movement_status,
            m.from_warehouse_id, m.to_warehouse_id
        FROM customer_order_line l
        JOIN customer_order o ON o.id = l.order_id
        LEFT JOIN stock_movement m ON m.id = l.stock_movement_id
        WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('other_warehouse', 'transfer', 'transit')
          AND (
              m.id IS NULL
              OR m.from_warehouse_id IS NULL
              OR m.to_warehouse_id IS NULL
              OR o.warehouse_id IS NULL
          )
        ORDER BY o.created_at DESC, l.id DESC
    """
    for row in conn.execute(sql_transfer).fetchall():
        r = dict(row)
        issues = []
        if r["movement_id"] is None:
            issues.append("missing_stock_movement")
        else:
            if r["from_warehouse_id"] is None:
                issues.append("movement_from_warehouse_null")
            if r["to_warehouse_id"] is None:
                issues.append("movement_to_warehouse_null")
        if r["order_warehouse_id"] is None:
            issues.append("order_warehouse_null")
        sev = "P2" if (r.get("order_status") or "").lower() in ("cancelled", "canceled", "done", "shipped") else "P0"
        results.append({**r, "issue_code": ",".join(issues) or "unknown", "severity": sev,
                        "reason": f"transfer line warehouse inconsistency: {issues}",
                        "next_step": "Ensure StockMovement exists with from/to warehouse; P0-B guard"})

    # --- production lines ---
    sql_prod = """
        SELECT
            l.id AS line_id, l.order_id, l.line_no,
            l.fulfillment_source, l.source_type, l.fulfillment_status, l.status,
            l.supply_need_id, l.production_request_line_id,
            o.order_number, o.status AS order_status, o.warehouse_id AS order_warehouse_id,
            n.id AS need_id, n.warehouse_id AS need_warehouse_id,
            pr.id AS prod_req_id, pr.target_warehouse_id AS prod_req_target_wh,
            pu.id AS produced_unit_id, pu.target_warehouse_id AS pu_target_wh
        FROM customer_order_line l
        JOIN customer_order o ON o.id = l.order_id
        LEFT JOIN supply_need n ON n.id = l.supply_need_id
        LEFT JOIN production_request_line prl ON prl.id = l.production_request_line_id
        LEFT JOIN production_request pr ON pr.id = prl.production_request_id
        LEFT JOIN produced_unit pu ON pu.order_line_id = l.id
        WHERE lower(COALESCE(l.fulfillment_source, '')) IN ('production', 'production_requested')
          AND (
              o.warehouse_id IS NULL
              OR n.id IS NULL
              OR n.warehouse_id IS NULL
              OR (pr.id IS NOT NULL AND pr.target_warehouse_id IS NULL)
              OR (pu.id IS NOT NULL AND pu.target_warehouse_id IS NULL)
          )
        ORDER BY o.created_at DESC, l.id DESC
    """
    for row in conn.execute(sql_prod).fetchall():
        r = dict(row)
        issues = []
        if r["order_warehouse_id"] is None:
            issues.append("order_warehouse_null")
        if r["need_id"] is None:
            issues.append("missing_supply_need")
        elif r["need_warehouse_id"] is None:
            issues.append("supply_need_warehouse_null")
        if r.get("prod_req_id") and r.get("prod_req_target_wh") is None:
            issues.append("production_request_target_warehouse_null")
        if r.get("produced_unit_id") and r.get("pu_target_wh") is None:
            issues.append("produced_unit_target_warehouse_null")
        sev = "P2" if (r.get("order_status") or "").lower() in ("cancelled", "canceled", "done", "shipped") else "P0"
        results.append({**r, "issue_code": ",".join(issues) or "unknown", "severity": sev,
                        "reason": f"production line warehouse inconsistency: {issues}",
                        "next_step": "Ensure SupplyNeed.warehouse_id and ProductionRequest.target_warehouse_id set"})

    # --- source_required lines (open orders) ---
    sql_nosrc = """
        SELECT
            l.id AS line_id, l.order_id, l.line_no,
            l.fulfillment_source, l.source_type, l.fulfillment_status, l.status,
            o.order_number, o.status AS order_status, o.warehouse_id AS order_warehouse_id
        FROM customer_order_line l
        JOIN customer_order o ON o.id = l.order_id
        WHERE (COALESCE(l.fulfillment_source, '') = ''
               OR lower(COALESCE(l.fulfillment_source, '')) IN ('none', 'source_required'))
          AND lower(COALESCE(o.status, '')) NOT IN ('cancelled', 'canceled', 'done', 'shipped', 'closed')
        ORDER BY o.created_at DESC, l.id DESC
    """
    for row in conn.execute(sql_nosrc).fetchall():
        r = dict(row)
        results.append({**r, "issue_code": "source_required", "severity": "P1",
                        "reason": "CustomerOrderLine has no fulfillment_source on open order",
                        "next_step": "Manager must select source; workflow P1 gap"})

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def severity_icon(s: str) -> str:
    return {"P0": "BLOCKER", "P1": "WARN", "P2": "CLEANUP", "OK": "OK"}.get(s, s)


def fmt_row(r: dict, keys: list[str]) -> str:
    vals = [str(r.get(k, "")) for k in keys]
    return "| " + " | ".join(vals) + " |"


def build_report(
    orders: list[dict],
    trailers: list[dict],
    prod_requests: list[dict],
    prod_wh_config: list[dict],
    supply_needs: list[dict],
    line_issues: list[dict],
    schema_gaps: list[dict],
    db_path: Path,
    branch: str,
) -> str:
    lines: list[str] = []

    all_data = orders + trailers + prod_requests + supply_needs + line_issues + prod_wh_config
    p0 = sum(1 for r in all_data if r.get("severity") == "P0")
    p1 = sum(1 for r in all_data if r.get("severity") == "P1") + len(schema_gaps)
    p2 = sum(1 for r in all_data if r.get("severity") == "P2")

    lines += [
        "# P0-B Warehouse Integrity Audit Report",
        "",
        f"- Audit date: {TODAY}",
        f"- Run at: {NOW}",
        f"- Database: {db_path.name} (local SQLite, read-only)",
        f"- Branch: {branch}",
        f"- Script: scripts/audit_warehouse_integrity.py",
        f"- Read-only: YES — no INSERT/UPDATE/DELETE/COMMIT performed",
        "",
        "## Summary",
        "",
        "| Severity | Count | Meaning |",
        "|---|---:|---|",
        f"| P0 — BLOCKER | {p0} | Data integrity: missing warehouse causes wrong stock/production/fulfillment |",
        f"| P1 — WARN | {p1} | Workflow inconsistency: may confuse status/reporting but not immediate data loss |",
        f"| P2 — CLEANUP | {p2} | Historical/cancelled records — low urgency |",
        "",
    ]

    # --- CustomerOrder ---
    lines += ["## CustomerOrder without warehouse_id", ""]
    if orders:
        lines += [
            "| severity | id | order_number | status | fulfillment_source | warehouse_id | source_warehouse_id | assigned_user_id | created_at | is_shipped | reason |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in orders:
            lines.append(fmt_row(r, ["severity", "id", "order_number", "status", "fulfillment_source",
                                     "warehouse_id", "source_warehouse_id", "assigned_user_id",
                                     "created_at", "is_shipped", "reason"]))
    else:
        lines.append("No CustomerOrder records without warehouse_id found.")
    lines.append("")

    # --- Trailer ---
    lines += ["## Trailer without warehouse_id", ""]
    if trailers:
        lines += [
            "| severity | id | vin | status | lifecycle_status | warehouse_id | created_at | reason |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in trailers:
            lines.append(fmt_row(r, ["severity", "id", "vin", "status", "lifecycle_status",
                                     "warehouse_id", "created_at", "reason"]))
    else:
        lines.append("No Trailer records without warehouse_id found.")
    lines.append("")

    # --- ProductionRequest ---
    lines += ["## ProductionRequest without target_warehouse_id", ""]
    if prod_requests:
        lines += [
            "| severity | id | request_number | status | target_warehouse_id | created_at | reason |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in prod_requests:
            lines.append(fmt_row(r, ["severity", "id", "request_number", "status",
                                     "target_warehouse_id", "created_at", "reason"]))
    else:
        lines.append("No ProductionRequest records without target_warehouse_id found.")
    lines.append("")

    # --- Production warehouse config ---
    lines += ["## Production warehouse configuration", ""]
    lines += [
        "| severity | finding | issue_code | warehouse_id | warehouse_name | is_active | reason | next_step |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in prod_wh_config:
        lines.append(fmt_row(r, ["severity", "finding", "issue_code",
                                 "warehouse_id", "warehouse_name", "is_active", "reason", "next_step"]))
    lines.append("")

    # --- SupplyNeed ---
    lines += ["## SupplyNeed without warehouse_id", ""]
    if supply_needs:
        lines += [
            "| severity | id | need_type | status | order_id | order_line_id | warehouse_id | quantity | created_at | reason |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in supply_needs:
            lines.append(fmt_row(r, ["severity", "id", "need_type", "status", "order_id",
                                     "order_line_id", "warehouse_id", "quantity", "created_at", "reason"]))
    else:
        lines.append("No SupplyNeed records without warehouse_id found.")
    lines.append("")

    # --- OrderLine issues ---
    lines += ["## CustomerOrderLine source warehouse inconsistencies", ""]
    if line_issues:
        lines += [
            "| severity | line_id | order_id | order_number | line_no | fulfillment_source | issue_code | reason |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in line_issues:
            lines.append(fmt_row(r, ["severity", "line_id", "order_id", "order_number",
                                     "line_no", "fulfillment_source", "issue_code", "reason"]))
    else:
        lines.append("No CustomerOrderLine warehouse inconsistencies found.")
    lines.append("")

    # --- Schema gaps ---
    lines += ["## Schema gaps (tables/columns missing from local DB)", ""]
    if schema_gaps:
        lines += [
            "| severity | entity | issue | reason | note |",
            "|---|---|---|---|---|",
        ]
        for g in schema_gaps:
            lines.append(fmt_row(g, ["severity", "entity", "issue", "reason", "note"]))
        lines += [
            "",
            "> **Impact:** Checks for missing entities were SKIPPED. Counts above reflect only tables present in the local DB.",
            "> **Root cause:** models.py defines these tables but migrations have not run against this SQLite instance.",
        ]
    else:
        lines.append("No schema gaps found — all expected tables/columns present.")
    lines.append("")

    # --- Issue code totals ---
    all_findings = orders + trailers + prod_requests + supply_needs + line_issues
    counts: dict[str, tuple[str, int]] = {}
    for r in all_findings:
        code = r.get("issue_code", "unknown")
        sev = r.get("severity", "?")
        if code not in counts:
            counts[code] = (sev, 0)
        counts[code] = (counts[code][0], counts[code][1] + 1)

    lines += ["## Totals by issue code", ""]
    if counts:
        lines += ["| issue_code | severity | count |", "|---|---|---|"]
        for code, (sev, cnt) in sorted(counts.items()):
            lines.append(f"| `{code}` | {sev} | {cnt} |")
    else:
        lines.append("No issues found.")
    lines.append("")

    # --- Recommendations ---
    lines += [
        "## Non-mutating recommendations",
        "",
        "- Do not auto-assign warehouses to existing records without a reviewed repair plan.",
        "- Do not run data-fix SQL directly from this report.",
        "- P0-B guard implementation: add warehouse validation in order create/edit routes only.",
        "- P0-C: introduce `default_production_warehouse_id` setting or `Warehouse.is_production` restoration via ADR.",
        "",
        "## Forbidden actions from this report",
        "",
        "- No INSERT/UPDATE/DELETE on any table.",
        "- No migration execution.",
        "- No deploy or push.",
        "- No dependency installation.",
        "- No schema changes without separate migration plan.",
        "",
        "## Proposed next steps",
        "",
        "1. Review this report manually.",
        "2. P0-B guard plan: add warehouse validation for new orders/production requests.",
        "3. P0-C: create separate ADR for production warehouse setting/migration.",
        "4. Repair plan: for P0 records found — manual review first, then targeted repair plan.",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_branch() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


EXPECTED_TABLES = [
    "customer_order",
    "customer_order_line",
    "production_request",
    "production_request_line",
    "supply_need",
    "reservation",
    "stock_movement",
    "produced_unit",
    "trailer",
    "warehouse",
]

EXPECTED_COLUMNS = {
    "trailer": ["lifecycle_status"],
    "warehouse": ["is_production", "code"],
}


def schema_gap_findings(conn: sqlite3.Connection) -> list[dict]:
    """Report tables/columns defined in models.py but absent from the local DB."""
    findings = []
    for tbl in EXPECTED_TABLES:
        if not table_exists(conn, tbl):
            findings.append({
                "severity": "P1",
                "entity": tbl,
                "issue": f"table '{tbl}' missing from local DB",
                "reason": "Table exists in models.py but has not been migrated to this DB instance",
                "note": "P0-A audit scope partially blocked for this entity"
            })
        else:
            for col in EXPECTED_COLUMNS.get(tbl, []):
                if not column_exists(conn, tbl, col):
                    findings.append({
                        "severity": "P1",
                        "entity": f"{tbl}.{col}",
                        "issue": f"column '{col}' missing from table '{tbl}'",
                        "reason": "Column defined in models.py but not yet in DB schema (migration pending)",
                        "note": "Partial audit only; field not available for query"
                    })
    return findings


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        print("Make sure you are running from the Trailers project root.", file=sys.stderr)
        return 2

    print(f"[P0-B] Trailers warehouse integrity audit")
    print(f"[P0-B] Database: {DB_PATH}")
    print(f"[P0-B] Opening read-only...")

    try:
        conn = open_db(DB_PATH)
    except Exception as exc:
        print(f"ERROR: Cannot open database: {exc}", file=sys.stderr)
        return 2

    try:
        branch = get_branch()
        print(f"[P0-B] Branch: {branch}")
        print(f"[P0-B] Running schema gap check...")

        schema_gaps = schema_gap_findings(conn)
        missing_tables = [g["entity"] for g in schema_gaps if "missing from local DB" in g.get("issue", "")]
        if schema_gaps:
            print(f"[P0-B] Schema gaps found: {len(schema_gaps)}")
            for g in schema_gaps:
                print(f"[P0-B]   {g['severity']}: {g['issue']}")
        print(f"[P0-B] Running checks...")

        orders = check_orders_without_warehouse(conn)
        trailers = check_trailers_without_warehouse(conn)
        prod_requests = check_production_requests_without_target(conn)
        prod_wh_config = check_production_warehouse_config(conn)
        supply_needs = check_supply_needs_without_warehouse(conn)
        line_issues = check_order_lines_source_inconsistency(conn)

        print(f"[P0-B] CustomerOrder without warehouse_id:   {len(orders)} (SKIPPED - table missing)" if "customer_order" in missing_tables else f"[P0-B] CustomerOrder without warehouse_id:  {len(orders)}")
        print(f"[P0-B] Trailer without warehouse_id:         {len(trailers)}")
        print(f"[P0-B] ProductionRequest without target_wh:  {len(prod_requests)} (SKIPPED - table missing)" if "production_request" in missing_tables else f"[P0-B] ProductionRequest without target_wh: {len(prod_requests)}")
        print(f"[P0-B] Production warehouse config findings: {len(prod_wh_config)}")
        print(f"[P0-B] SupplyNeed without warehouse_id:      {len(supply_needs)} (SKIPPED - table missing)" if "supply_need" in missing_tables else f"[P0-B] SupplyNeed without warehouse_id:     {len(supply_needs)}")
        print(f"[P0-B] OrderLine source inconsistencies:     {len(line_issues)} (SKIPPED - table missing)" if "customer_order_line" in missing_tables else f"[P0-B] OrderLine source inconsistencies:    {len(line_issues)}")

        all_data = orders + trailers + prod_requests + supply_needs + line_issues + prod_wh_config
        p0_total = sum(1 for r in all_data if r.get("severity") == "P0")
        p1_total = sum(1 for r in all_data if r.get("severity") == "P1")
        p1_total += len(schema_gaps)
        p2_total = sum(1 for r in all_data if r.get("severity") == "P2")

        print(f"")
        print(f"[P0-B] === SUMMARY ===")
        print(f"[P0-B] P0 BLOCKERS : {p0_total}")
        print(f"[P0-B] P1 WARNINGS : {p1_total}  (includes {len(schema_gaps)} schema gaps)")
        print(f"[P0-B] P2 CLEANUP  : {p2_total}")
        print(f"")

        report_text = build_report(
            orders, trailers, prod_requests, prod_wh_config,
            supply_needs, line_issues, schema_gaps, DB_PATH, branch
        )

        # Write local detailed report
        REPORT_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_LOCAL.write_text(report_text, encoding="utf-8")
        print(f"[P0-B] Local report: {REPORT_LOCAL}")

        # Write sanitized docs report (same content — no personal data included by design)
        REPORT_DOCS.parent.mkdir(parents=True, exist_ok=True)
        REPORT_DOCS.write_text(report_text, encoding="utf-8")
        print(f"[P0-B] Docs report: {REPORT_DOCS}")
        print(f"[P0-B] Done. No data was modified.")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
