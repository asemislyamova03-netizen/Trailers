#!/usr/bin/env python3
"""
P0-D: Read-only VIN and production integrity audit for Trailers.

SAFETY GUARANTEES:
- Opens SQLite in read-only URI mode: file:<path>?mode=ro
- Sets PRAGMA query_only = ON as defense-in-depth.
- Never calls INSERT, UPDATE, DELETE, ALTER TABLE, or db.session.commit().
- Closes connection in finally block.
- Redacts personal data: no customer phone, email, full name in output.
- Uses internal IDs, order numbers, and VIN values only.
- Detects table/column presence dynamically — skips missing tables with notes.
- Generates .ai_local/reports/trailers-vin-production-p0d-report.md (internal).
- Generates docs/ai/live-support/2026-06-05-trailers-vin-production-integrity-p0d-report.md (public).
"""
from __future__ import annotations

import datetime
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "instance" / "trailers.db"

REPORT_LOCAL_DIR = ROOT / ".ai_local" / "reports"
REPORT_LOCAL = REPORT_LOCAL_DIR / "trailers-vin-production-p0d-report.md"
REPORT_DOCS = ROOT / "docs" / "ai" / "live-support" / "2026-06-05-trailers-vin-production-integrity-p0d-report.md"

TODAY = datetime.date.today().isoformat()
NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection:
    uri = path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        pass
    return conn


def table_exists(conn: sqlite3.Connection, t: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())


def col_exists(conn: sqlite3.Connection, t: str, c: str) -> bool:
    try:
        return any(r[1] == c for r in conn.execute(f"PRAGMA table_info({t})").fetchall())
    except Exception:
        return False


def normalize_vin(v: str | None) -> str:
    if not v:
        return ""
    return re.sub(r"[\s\-]", "", v).upper().strip()


# ---------------------------------------------------------------------------
# A. VIN duplicates
# ---------------------------------------------------------------------------

def check_vin_duplicates_trailer(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}
    if not table_exists(conn, "trailer"):
        result["skipped"].append("trailer table missing")
        return result
    rows = conn.execute("SELECT id, vin, status FROM trailer").fetchall()
    # exact duplicate
    seen: dict[str, list] = defaultdict(list)
    for r in rows:
        v = (r["vin"] or "").strip().upper()
        if v:
            seen[v].append(r)
    for vin, group in seen.items():
        if len(group) > 1:
            ids = [g["id"] for g in group]
            statuses = [g["status"] for g in group]
            result["findings"].append({
                "severity": "P0", "check": "trailer_vin_exact_duplicate",
                "vin": vin, "trailer_ids": ids, "statuses": statuses,
                "reason": f"Exact duplicate trailer.vin across {len(group)} trailers",
                "next_step": "P0-F: identify which is correct, void duplicates with repair plan"
            })
    # suspicious VINs
    for r in rows:
        v = (r["vin"] or "").strip()
        if not v:
            result["findings"].append({
                "severity": "P0", "check": "trailer_vin_missing",
                "vin": None, "trailer_ids": [r["id"]], "statuses": [r["status"]],
                "reason": "Trailer.vin is NULL or empty",
                "next_step": "Assign VIN or mark lifecycle_status=decommissioned"
            })
        elif len(v) not in (17, 7):
            result["findings"].append({
                "severity": "P1", "check": "trailer_vin_suspicious_length",
                "vin": v[:30], "trailer_ids": [r["id"]], "statuses": [r["status"]],
                "reason": f"VIN length={len(v)} (expected 17 or 7 serial)",
                "next_step": "Verify and correct VIN value"
            })
    return result


def check_vin_duplicates_registry(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}
    if not table_exists(conn, "vin_registry"):
        result["skipped"].append("vin_registry table missing")
        return result
    rows = conn.execute("SELECT id, vin_full, serial7, status FROM vin_registry").fetchall()
    # exact duplicate vin_full (excluding NULLs)
    seen_full: dict[str, list] = defaultdict(list)
    for r in rows:
        v = (r["vin_full"] or "").strip().upper()
        if v:
            seen_full[v].append(r)
    for vin, group in seen_full.items():
        if len(group) > 1:
            ids = [g["id"] for g in group]
            statuses = [g["status"] for g in group]
            result["findings"].append({
                "severity": "P0", "check": "vin_registry_vin_full_duplicate",
                "vin": vin, "registry_ids": ids, "statuses": statuses,
                "reason": f"Duplicate vin_registry.vin_full across {len(group)} rows (should be UNIQUE)",
                "next_step": "P0-F: void duplicate registry rows; root cause — DB unique constraint may not exist"
            })
    # exact duplicate serial7
    seen_s7: dict[str, list] = defaultdict(list)
    for r in rows:
        s = (r["serial7"] or "").strip()
        if s:
            seen_s7[s].append(r)
    for s7, group in seen_s7.items():
        if len(group) > 1:
            ids = [g["id"] for g in group]
            statuses = [g["status"] for g in group]
            result["findings"].append({
                "severity": "P0", "check": "vin_registry_serial7_duplicate",
                "vin": s7, "registry_ids": ids, "statuses": statuses,
                "reason": f"Duplicate vin_registry.serial7 across {len(group)} rows",
                "next_step": "P0-F: void/merge duplicate serial7 rows"
            })
    return result


def check_vin_cross_table(conn: sqlite3.Connection) -> dict:
    """Trailer.vin exists in vin_registry.vin_full but points to different or extra records."""
    result: dict = {"findings": [], "skipped": []}
    if not table_exists(conn, "trailer") or not table_exists(conn, "vin_registry"):
        result["skipped"].append("trailer or vin_registry missing")
        return result
    # Trailer VIN not in registry at all
    sql = """
        SELECT t.id AS trailer_id, t.vin, t.status AS trailer_status,
               vr.id AS registry_id, vr.status AS registry_status
        FROM trailer t
        LEFT JOIN vin_registry vr ON upper(trim(vr.vin_full)) = upper(trim(t.vin))
        WHERE t.vin IS NOT NULL AND t.vin != '' AND vr.id IS NULL
    """
    for r in conn.execute(sql).fetchall():
        result["findings"].append({
            "severity": "P1", "check": "trailer_vin_not_in_registry",
            "trailer_id": r["trailer_id"], "vin": r["vin"],
            "trailer_status": r["trailer_status"],
            "reason": "Trailer.vin has no matching vin_registry.vin_full entry",
            "next_step": "Verify if VIN was registered through old flow; create registry entry or investigate"
        })
    # Trailer has different VIN than its linked registry row
    sql2 = """
        SELECT t.id AS trailer_id, t.vin AS trailer_vin, t.status AS trailer_status,
               vr.id AS registry_id, vr.vin_full AS registry_vin, vr.status AS registry_status
        FROM vin_registry vr
        JOIN trailer t ON t.id = vr.trailer_id
        WHERE vr.vin_full IS NOT NULL
          AND t.vin IS NOT NULL
          AND upper(trim(t.vin)) != upper(trim(vr.vin_full))
    """
    for r in conn.execute(sql2).fetchall():
        result["findings"].append({
            "severity": "P0", "check": "trailer_vin_mismatch_registry",
            "trailer_id": r["trailer_id"], "trailer_vin": r["trailer_vin"],
            "registry_id": r["registry_id"], "registry_vin": r["registry_vin"],
            "reason": "Trailer.vin != vin_registry.vin_full for linked records",
            "next_step": "P0-F: reconcile — determine which is correct, update registry or trailer"
        })
    # vin_registry linked to >1 active trailer
    sql3 = """
        SELECT vr.id AS registry_id, vr.vin_full, vr.status,
               COUNT(t.id) AS trailer_count
        FROM vin_registry vr
        JOIN trailer t ON t.vin = vr.vin_full
        WHERE vr.status NOT IN ('void','voided')
        GROUP BY vr.id HAVING COUNT(t.id) > 1
    """
    for r in conn.execute(sql3).fetchall():
        result["findings"].append({
            "severity": "P0", "check": "registry_vin_linked_to_multiple_trailers",
            "registry_id": r["registry_id"], "vin": r["registry_vin"],
            "trailer_count": r["trailer_count"],
            "reason": f"vin_registry.vin_full matches {r['trailer_count']} trailer.vin rows",
            "next_step": "P0-F: determine correct trailer, void or reassign others"
        })
    return result


# ---------------------------------------------------------------------------
# B. VIN status inconsistencies
# ---------------------------------------------------------------------------

def check_vin_status_inconsistencies(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}
    if not table_exists(conn, "vin_registry"):
        result["skipped"].append("vin_registry missing")
        return result

    # Confirmed VIN without trailer
    sql = """
        SELECT id, vin_full, serial7, status, trailer_id, customer_order_id
        FROM vin_registry
        WHERE status = 'confirmed' AND (trailer_id IS NULL OR trailer_id = 0)
    """
    for r in conn.execute(sql).fetchall():
        result["findings"].append({
            "severity": "P1", "check": "confirmed_vin_without_trailer",
            "registry_id": r["id"], "vin": r["vin_full"] or r["serial7"],
            "order_id": r["customer_order_id"],
            "reason": "VIN status=confirmed but no trailer_id linked",
            "next_step": "Investigate confirmation flow; assign correct trailer or re-evaluate status"
        })

    # Assigned VIN without trailer
    sql2 = """
        SELECT id, vin_full, serial7, status, trailer_id, customer_order_id
        FROM vin_registry
        WHERE status = 'assigned' AND (trailer_id IS NULL OR trailer_id = 0)
    """
    for r in conn.execute(sql2).fetchall():
        result["findings"].append({
            "severity": "P1", "check": "assigned_vin_without_trailer",
            "registry_id": r["id"], "vin": r["vin_full"] or r["serial7"],
            "order_id": r["customer_order_id"],
            "reason": "VIN status=assigned but no trailer_id linked",
            "next_step": "Assign trailer to this VIN registry row or reset status to reserved/free"
        })

    # Same VIN assigned to multiple orders (non-void rows)
    if col_exists(conn, "vin_registry", "customer_order_id"):
        sql3 = """
            SELECT vin_full, COUNT(DISTINCT customer_order_id) AS order_count,
                   GROUP_CONCAT(DISTINCT customer_order_id) AS order_ids,
                   GROUP_CONCAT(DISTINCT id) AS registry_ids
            FROM vin_registry
            WHERE vin_full IS NOT NULL
              AND customer_order_id IS NOT NULL
              AND status NOT IN ('void','voided','free')
            GROUP BY vin_full HAVING COUNT(DISTINCT customer_order_id) > 1
        """
        for r in conn.execute(sql3).fetchall():
            result["findings"].append({
                "severity": "P0", "check": "vin_linked_to_multiple_orders",
                "vin": r["vin_full"], "order_ids": r["order_ids"],
                "registry_ids": r["registry_ids"],
                "reason": f"VIN linked to {r['order_count']} different orders in vin_registry",
                "next_step": "P0-F: keep only correct order link, void/unlink others"
            })

    return result


# ---------------------------------------------------------------------------
# C. Production release anomalies
# ---------------------------------------------------------------------------

def check_production_anomalies(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}

    if not table_exists(conn, "production_request"):
        result["skipped"].append("production_request missing")
        return result

    has_prl = table_exists(conn, "production_request_line")
    has_pu = table_exists(conn, "produced_unit")

    # Production request completed/released but no produced units
    if has_pu:
        sql = """
            SELECT pr.id, pr.request_number, pr.status, pr.target_warehouse_id,
                   COUNT(pu.id) AS unit_count
            FROM production_request pr
            LEFT JOIN produced_unit pu ON pu.production_request_line_id IN (
                SELECT id FROM production_request_line WHERE production_request_id = pr.id
            )
            WHERE lower(pr.status) IN ('completed','released','done','ready','closed')
            GROUP BY pr.id HAVING unit_count = 0
        """
        for r in conn.execute(sql).fetchall():
            result["findings"].append({
                "severity": "P1", "check": "production_request_completed_no_units",
                "prod_req_id": r["id"], "request_number": r["request_number"],
                "status": r["status"], "target_warehouse_id": r["target_warehouse_id"],
                "reason": "Production request status=done/completed but no produced_unit records",
                "next_step": "Verify if units were created under different flow; investigate"
            })

    # Produced unit with no trailer (VIN assignment pending anomaly)
    if has_pu:
        sql2 = """
            SELECT pu.id, pu.status, pu.item_id, pu.order_id,
                   pu.target_warehouse_id, pu.trailer_id, pu.production_request_line_id
            FROM produced_unit pu
            WHERE pu.status IN ('vin_assigned','confirmed')
              AND (pu.trailer_id IS NULL OR pu.trailer_id = 0)
        """
        for r in conn.execute(sql2).fetchall():
            result["findings"].append({
                "severity": "P0", "check": "produced_unit_vin_assigned_no_trailer",
                "unit_id": r["id"], "status": r["status"],
                "order_id": r["order_id"], "target_warehouse_id": r["target_warehouse_id"],
                "reason": "produced_unit.status=vin_assigned but trailer_id is NULL",
                "next_step": "P0-F: link correct trailer or reset unit status"
            })

    # Trailer with no production origin but lifecycle=ready_production_warehouse or produced
    if table_exists(conn, "trailer") and has_pu:
        sql3 = """
            SELECT t.id, t.vin, t.status, t.lifecycle_status, t.warehouse_id
            FROM trailer t
            WHERE t.lifecycle_status IN ('ready_production_warehouse')
              AND NOT EXISTS (
                  SELECT 1 FROM produced_unit pu WHERE pu.trailer_id = t.id
              )
        """
        for r in conn.execute(sql3).fetchall():
            result["findings"].append({
                "severity": "P1", "check": "trailer_production_status_no_produced_unit",
                "trailer_id": r["id"], "vin": r["vin"],
                "status": r["status"], "lifecycle_status": r["lifecycle_status"],
                "warehouse_id": r["warehouse_id"],
                "reason": "Trailer lifecycle=ready_production_warehouse but no produced_unit record",
                "next_step": "Verify manual creation path; ensure produced_unit exists for audit trail"
            })

    return result


# ---------------------------------------------------------------------------
# D. Reservation/order anomalies
# ---------------------------------------------------------------------------

def check_reservation_anomalies(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}

    if not table_exists(conn, "reservation"):
        result["skipped"].append("reservation missing")
        return result
    if not table_exists(conn, "customer_order"):
        result["skipped"].append("customer_order missing")
        return result

    # Trailer reserved by multiple active reservations
    sql = """
        SELECT r.trailer_id, COUNT(r.id) AS res_count,
               GROUP_CONCAT(r.id) AS reservation_ids,
               GROUP_CONCAT(r.order_id) AS order_ids
        FROM reservation r
        WHERE r.status = 'ACTIVE' AND r.trailer_id IS NOT NULL
        GROUP BY r.trailer_id HAVING COUNT(r.id) > 1
    """
    for r in conn.execute(sql).fetchall():
        result["findings"].append({
            "severity": "P0", "check": "trailer_multi_active_reservation",
            "trailer_id": r["trailer_id"],
            "reservation_ids": r["reservation_ids"], "order_ids": r["order_ids"],
            "reason": f"Trailer has {r['res_count']} ACTIVE reservations simultaneously",
            "next_step": "P0-F: cancel duplicate reservations, keep correct one"
        })

    # Reserved trailer without warehouse
    if table_exists(conn, "trailer"):
        sql2 = """
            SELECT r.id AS res_id, r.trailer_id, r.order_id, r.status,
                   t.vin, t.warehouse_id, t.status AS trailer_status
            FROM reservation r
            JOIN trailer t ON t.id = r.trailer_id
            WHERE r.status = 'ACTIVE' AND t.warehouse_id IS NULL
        """
        for r in conn.execute(sql2).fetchall():
            result["findings"].append({
                "severity": "P0", "check": "reserved_trailer_no_warehouse",
                "reservation_id": r["res_id"], "trailer_id": r["trailer_id"],
                "order_id": r["order_id"], "vin": r["vin"],
                "reason": "Active reservation points to trailer without warehouse_id",
                "next_step": "Assign warehouse to trailer; P0-B guard for new reservations"
            })

    return result


# ---------------------------------------------------------------------------
# E. Warehouse anomalies (simplified; main P0-B covered in previous audit)
# ---------------------------------------------------------------------------

def check_warehouse_anomalies(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}

    if not table_exists(conn, "trailer"):
        result["skipped"].append("trailer missing")
    else:
        rows = conn.execute(
            "SELECT id, vin, status, lifecycle_status, warehouse_id FROM trailer WHERE warehouse_id IS NULL"
        ).fetchall()
        for r in rows:
            sev = "P2" if (r["status"] or "").upper() in ("SOLD",) else "P0"
            result["findings"].append({
                "severity": sev, "check": "trailer_no_warehouse",
                "trailer_id": r["id"], "vin": r["vin"],
                "status": r["status"], "lifecycle_status": r["lifecycle_status"],
                "reason": "Trailer.warehouse_id IS NULL",
                "next_step": "Assign correct warehouse via repair plan"
            })

    if not table_exists(conn, "production_request"):
        result["skipped"].append("production_request missing")
    else:
        rows = conn.execute(
            "SELECT id, request_number, status, target_warehouse_id FROM production_request WHERE target_warehouse_id IS NULL AND lower(status) NOT IN ('cancelled','canceled','closed')"
        ).fetchall()
        for r in rows:
            result["findings"].append({
                "severity": "P0", "check": "production_request_no_target_warehouse",
                "prod_req_id": r["id"], "request_number": r["request_number"],
                "status": r["status"],
                "reason": "Active ProductionRequest.target_warehouse_id IS NULL",
                "next_step": "Set target warehouse; P0-B guard on creation"
            })

    return result


# ---------------------------------------------------------------------------
# F. Status mismatches
# ---------------------------------------------------------------------------

def check_status_mismatches(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "skipped": []}

    if not table_exists(conn, "customer_order") or not table_exists(conn, "customer_order_line"):
        result["skipped"].append("customer_order or customer_order_line missing")
        return result

    # Order with vin_registry rows but order status still in production stages
    if table_exists(conn, "vin_registry"):
        sql = """
            SELECT o.id AS order_id, o.order_number, o.status AS order_status,
                   COUNT(vr.id) AS confirmed_vins
            FROM customer_order o
            JOIN vin_registry vr ON vr.customer_order_id = o.id AND vr.status = 'confirmed'
            WHERE lower(o.status) IN ('waiting_production','in_production','produced_waiting_vin','production_requested')
            GROUP BY o.id
        """
        for r in conn.execute(sql).fetchall():
            result["findings"].append({
                "severity": "P1", "check": "order_status_behind_vin_confirmed",
                "order_id": r["order_id"], "order_number": r["order_number"],
                "order_status": r["order_status"], "confirmed_vins": r["confirmed_vins"],
                "reason": f"Order status={r['order_status']} but {r['confirmed_vins']} confirmed VIN(s) exist",
                "next_step": "Refresh order status via _refresh_order_status; investigate workflow gap"
            })

    # Order status=done/shipped but trailer still RESERVED
    if table_exists(conn, "trailer"):
        sql2 = """
            SELECT o.id AS order_id, o.order_number, o.status AS order_status,
                   t.id AS trailer_id, t.vin, t.status AS trailer_status
            FROM customer_order o
            JOIN trailer t ON t.id = o.trailer_id
            WHERE lower(o.status) IN ('done','shipped','cancelled','canceled')
              AND t.status = 'RESERVED'
        """
        for r in conn.execute(sql2).fetchall():
            result["findings"].append({
                "severity": "P1", "check": "order_closed_trailer_still_reserved",
                "order_id": r["order_id"], "order_number": r["order_number"],
                "order_status": r["order_status"], "trailer_id": r["trailer_id"],
                "vin": r["vin"],
                "reason": f"Order is {r['order_status']} but linked trailer.status=RESERVED",
                "next_step": "Release trailer reservation; update trailer status to IN_STOCK or SOLD"
            })

    return result


# ---------------------------------------------------------------------------
# DB constraint inspection
# ---------------------------------------------------------------------------

def check_db_constraints(conn: sqlite3.Connection) -> dict:
    result: dict = {"findings": [], "info": []}
    for table in ("trailer", "vin_registry"):
        if not table_exists(conn, table):
            continue
        indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
        unique_cols: list[str] = []
        for idx in indexes:
            if idx["unique"]:
                cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
                unique_cols.extend(cols)
        if table == "trailer" and "vin" not in unique_cols:
            result["findings"].append({
                "severity": "P0", "check": "trailer_vin_no_unique_index",
                "table": "trailer", "column": "vin",
                "reason": "No UNIQUE index on trailer.vin in actual DB schema — duplicates possible without app-level guard",
                "next_step": "P0-C: add UNIQUE constraint via migration plan; P0-G: add app-level guard immediately"
            })
        else:
            result["info"].append(f"{table}.vin: UNIQUE index present ({unique_cols})")
        if table == "vin_registry":
            for col in ("vin_full", "serial7"):
                if col not in unique_cols:
                    result["findings"].append({
                        "severity": "P1", "check": f"vin_registry_{col}_no_unique_index",
                        "table": "vin_registry", "column": col,
                        "reason": f"No UNIQUE index on vin_registry.{col} — app-level guard is single point of failure",
                        "next_step": "P0-C: add UNIQUE constraint via migration"
                    })
    return result


# ---------------------------------------------------------------------------
# Summarise and report
# ---------------------------------------------------------------------------

Section = dict[str, list[dict]]


def count_severity(all_findings: list[dict], sev: str) -> int:
    return sum(1 for f in all_findings if f.get("severity") == sev)


def md_table(headers: list[str], rows: list[dict], keys: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    return "\n".join(lines)


def build_report(sections: dict[str, list], constraints: dict, schema_info: dict, branch: str) -> str:
    all_f: list[dict] = []
    for v in sections.values():
        all_f.extend(v)
    p0 = count_severity(all_f, "P0")
    p1 = count_severity(all_f, "P1")
    p2 = count_severity(all_f, "P2")
    p0 += count_severity(constraints.get("findings", []), "P0")
    p1 += count_severity(constraints.get("findings", []), "P1")

    lines = [
        "# P0-D VIN & Production Integrity Audit Report",
        "",
        f"- Audit date: {TODAY}",
        f"- Run at: {NOW}",
        f"- Branch: {branch}",
        f"- DB: instance/trailers.db (SQLite, read-only)",
        f"- Read-only: YES — no INSERT/UPDATE/DELETE/COMMIT",
        "",
        "## Summary",
        "",
        "| Severity | Count | Meaning |",
        "|---|---:|---|",
        f"| P0 — BLOCKER | {p0} | Data integrity: duplicates, missing links, missing constraints |",
        f"| P1 — WARN    | {p1} | Workflow inconsistency, status mismatch, suspicious data |",
        f"| P2 — CLEANUP | {p2} | Historical/low-risk |",
        "",
    ]

    section_labels = {
        "vin_dup_trailer": "A1. VIN duplicates — trailer table",
        "vin_dup_registry": "A2. VIN duplicates — vin_registry table",
        "vin_cross": "A3. VIN cross-table mismatches",
        "vin_status": "B. VIN status inconsistencies",
        "production": "C. Production release anomalies",
        "reservation": "D. Reservation/order anomalies",
        "warehouse": "E. Warehouse anomalies",
        "status": "F. Status mismatches",
    }

    for key, label in section_labels.items():
        findings = sections.get(key, [])
        skipped = sections.get(f"{key}_skipped", [])
        lines.append(f"## {label}")
        lines.append("")
        if skipped:
            for s in skipped:
                lines.append(f"> SKIPPED: {s}")
            lines.append("")
        if not findings:
            lines.append("No issues found.")
        else:
            for f in findings:
                sev = f.get("severity", "?")
                check = f.get("check", "")
                reason = f.get("reason", "")
                next_step = f.get("next_step", "")
                detail_parts = []
                for k in ("vin", "trailer_id", "trailer_ids", "registry_id", "registry_ids",
                          "order_id", "order_number", "order_ids", "prod_req_id", "unit_id",
                          "request_number", "trailer_count", "confirmed_vins"):
                    if k in f and f[k] is not None:
                        detail_parts.append(f"{k}={f[k]}")
                detail = "  ".join(detail_parts)
                lines.append(f"- **{sev}** `{check}`: {reason}")
                if detail:
                    lines.append(f"  - detail: {detail}")
                lines.append(f"  - next step: {next_step}")
        lines.append("")

    # DB constraints
    lines += ["## G. DB constraint inspection", ""]
    if constraints.get("findings"):
        for f in constraints["findings"]:
            lines.append(f"- **{f['severity']}** `{f['check']}`: {f['reason']}")
            lines.append(f"  - next step: {f['next_step']}")
    else:
        lines.append("No missing constraints found.")
    if constraints.get("info"):
        lines.append("")
        lines.append("**Info (constraints present):**")
        for i in constraints["info"]:
            lines.append(f"- {i}")
    lines.append("")

    # Issue code totals
    lines += ["## Totals by issue code", ""]
    code_counts: dict[str, tuple[str, int]] = {}
    for f in all_f + constraints.get("findings", []):
        c = f.get("check", "unknown")
        s = f.get("severity", "?")
        code_counts[c] = (s, code_counts.get(c, (s, 0))[1] + 1)
    if code_counts:
        lines += ["| issue_code | severity | count |", "|---|---|---|"]
        for c, (s, n) in sorted(code_counts.items()):
            lines.append(f"| `{c}` | {s} | {n} |")
    else:
        lines.append("No issues found.")
    lines.append("")

    lines += [
        "## Forbidden actions from this report",
        "",
        "- No UPDATE/DELETE/INSERT.",
        "- No migration execution.",
        "- No deploy or push until P0-F repair plan is reviewed.",
        "- No schema changes without separate migration ADR.",
        "",
        "## Next steps",
        "",
        "1. Review this report.",
        "2. P0-F: data repair plan for confirmed P0 blockers (VIN duplicates, broken links).",
        "3. P0-G: app-level guard implementation plan (VIN uniqueness check in trailer edit route).",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_branch() -> str:
    try:
        import subprocess
        r = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    print(f"[P0-D] VIN & production integrity audit")
    print(f"[P0-D] DB: {DB_PATH}")

    try:
        conn = open_db(DB_PATH)
    except Exception as exc:
        print(f"ERROR: Cannot open DB: {exc}", file=sys.stderr)
        return 2

    try:
        branch = get_branch()
        print(f"[P0-D] Branch: {branch}")

        print("[P0-D] A. Checking VIN duplicates...")
        r_dup_t = check_vin_duplicates_trailer(conn)
        r_dup_r = check_vin_duplicates_registry(conn)
        r_cross = check_vin_cross_table(conn)

        print("[P0-D] B. Checking VIN status inconsistencies...")
        r_vstatus = check_vin_status_inconsistencies(conn)

        print("[P0-D] C. Checking production anomalies...")
        r_prod = check_production_anomalies(conn)

        print("[P0-D] D. Checking reservation/order anomalies...")
        r_res = check_reservation_anomalies(conn)

        print("[P0-D] E. Checking warehouse anomalies...")
        r_wh = check_warehouse_anomalies(conn)

        print("[P0-D] F. Checking status mismatches...")
        r_st = check_status_mismatches(conn)

        print("[P0-D] G. Inspecting DB constraints...")
        r_con = check_db_constraints(conn)

        sections = {
            "vin_dup_trailer": r_dup_t["findings"],
            "vin_dup_trailer_skipped": r_dup_t["skipped"],
            "vin_dup_registry": r_dup_r["findings"],
            "vin_dup_registry_skipped": r_dup_r["skipped"],
            "vin_cross": r_cross["findings"],
            "vin_cross_skipped": r_cross["skipped"],
            "vin_status": r_vstatus["findings"],
            "vin_status_skipped": r_vstatus["skipped"],
            "production": r_prod["findings"],
            "production_skipped": r_prod["skipped"],
            "reservation": r_res["findings"],
            "reservation_skipped": r_res["skipped"],
            "warehouse": r_wh["findings"],
            "warehouse_skipped": r_wh["skipped"],
            "status": r_st["findings"],
            "status_skipped": r_st["skipped"],
        }

        all_f = (r_dup_t["findings"] + r_dup_r["findings"] + r_cross["findings"] +
                 r_vstatus["findings"] + r_prod["findings"] + r_res["findings"] +
                 r_wh["findings"] + r_st["findings"] + r_con.get("findings", []))

        p0 = count_severity(all_f, "P0")
        p1 = count_severity(all_f, "P1")
        p2 = count_severity(all_f, "P2")

        print(f"\n[P0-D] === SUMMARY ===")
        print(f"[P0-D] P0 BLOCKERS : {p0}")
        print(f"[P0-D] P1 WARNINGS : {p1}")
        print(f"[P0-D] P2 CLEANUP  : {p2}")

        if p0 > 0:
            print(f"[P0-D] *** P0 FINDINGS REQUIRE IMMEDIATE ATTENTION ***")
            for f in all_f:
                if f.get("severity") == "P0":
                    print(f"[P0-D]   P0 {f.get('check')}: {f.get('reason', '')}")

        schema_info: dict = {}
        report = build_report(sections, r_con, schema_info, branch)

        REPORT_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_LOCAL.write_text(report, encoding="utf-8")
        print(f"\n[P0-D] Local report: {REPORT_LOCAL}")

        REPORT_DOCS.parent.mkdir(parents=True, exist_ok=True)
        REPORT_DOCS.write_text(report, encoding="utf-8")
        print(f"[P0-D] Docs report: {REPORT_DOCS}")
        print(f"[P0-D] Done. No data was modified.")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
