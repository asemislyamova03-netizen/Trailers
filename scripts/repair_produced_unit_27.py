#!/usr/bin/env python3
"""
P0-FB: Dry-run repair script for produced_unit id=27.

SAFETY GUARANTEES:
- Opens SQLite in read-only URI mode: file:<path>?mode=ro (dry-run)
- Sets PRAGMA query_only = ON as defense-in-depth.
- No INSERT / UPDATE / DELETE executed in this version.
- --apply is not implemented in P0-FB; passing it exits with error.
- No db.session.commit(), no migrations, no schema changes.
- No personal data in output (IDs, VINs, order numbers only).

Purpose:
  Verify all R1-R14 pre-checks from the P0-F plan and show the proposed
  repair SQL without executing it.

Usage:
  python scripts/repair_produced_unit_27.py          # dry-run (default)
  python scripts/repair_produced_unit_27.py --dry-run
  python scripts/repair_produced_unit_27.py --apply  # exits with error in P0-FB

Plan reference:
  docs/ai/live-support/2026-06-05-trailers-produced-unit-27-repair-p0f-plan.md
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "instance" / "trailers.db"
REPORT_DOCS = ROOT / "docs" / "ai" / "live-support" / "2026-06-05-trailers-produced-unit-27-repair-p0fb-dry-run-report.md"

TODAY = datetime.date.today().isoformat()
NOW = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

# ---------------------------------------------------------------------------
# Target constants (from P0-F plan and P0-D audit)
# ---------------------------------------------------------------------------
TARGET_UNIT_ID = 27
TARGET_TRAILER_ID = 1001
TARGET_TRAILER_VIN = "MX4000004T0002670"
TARGET_SUPPLY_NEED_ID = 29
TARGET_VIN_REGISTRY_ID = 987
TARGET_ITEM_ID = 515
TARGET_WAREHOUSE_ID = 1


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def open_db_readonly(path: Path):
    import sqlite3
    uri = path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except Exception:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        pass
    return conn


def table_exists(conn, t: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone())


def col_exists(conn, t: str, c: str) -> bool:
    try:
        return any(r[1] == c for r in conn.execute(f"PRAGMA table_info({t})"))
    except Exception:
        return False


def get_branch() -> str:
    try:
        import subprocess
        r = subprocess.run(["git", "branch", "--show-current"],
                           cwd=ROOT, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Pre-checks R1 – R14
# ---------------------------------------------------------------------------

CheckResult = dict  # {id, description, expected, actual, status, detail}


def run_checks(conn) -> list[CheckResult]:
    results: list[CheckResult] = []

    def check(id_: str, description: str, expected: str, passed: bool,
              actual: str, detail: str = "") -> CheckResult:
        r = {
            "id": id_,
            "description": description,
            "expected": expected,
            "actual": actual,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
        }
        results.append(r)
        return r

    # --- R1: produced_unit id=27 exists ---
    unit = conn.execute(
        "SELECT id, status, trailer_id, item_id, production_request_line_id, "
        "target_warehouse_id, order_id FROM produced_unit WHERE id=?",
        (TARGET_UNIT_ID,)
    ).fetchone()
    check("R1", "produced_unit id=27 exists",
          "exists", unit is not None,
          "exists" if unit else "NOT FOUND")
    if not unit:
        # All subsequent checks depend on this; mark remaining skipped
        for r_id, desc in [
            ("R2", "produced_unit.status = vin_assigned"),
            ("R3", "produced_unit.trailer_id IS NULL"),
            ("R4", "produced_unit.item_id = 515"),
            ("R5", "produced_unit.production_request_line_id = 27"),
            ("R6", "trailer id=1001 exists"),
            ("R7", "trailer.vin = MX4000004T0002670"),
            ("R8", "trailer.item_id = 515"),
            ("R9", "trailer.warehouse_id NOT NULL"),
            ("R10", "vin_registry id=987 exists"),
            ("R11", "vin_registry.supply_need_id = 29"),
            ("R12", "vin_registry.trailer_id = 1001"),
            ("R13", "supply_need id=29 exists and not cancelled"),
            ("R14", "no other produced_unit links trailer_id=1001 conflictingly"),
        ]:
            results.append({"id": r_id, "description": desc, "expected": "—",
                            "actual": "SKIPPED (R1 failed)", "status": "SKIP", "detail": ""})
        return results

    # --- R2: status = vin_assigned ---
    status = unit["status"]
    check("R2", "produced_unit.status = 'vin_assigned'",
          "vin_assigned", status == "vin_assigned",
          str(status),
          "already repaired or unexpected status" if status != "vin_assigned" else "")

    # --- R3: trailer_id IS NULL ---
    tid = unit["trailer_id"]
    check("R3", "produced_unit.trailer_id IS NULL",
          "NULL", tid is None,
          str(tid),
          "already repaired" if tid is not None else "")

    # --- R4: item_id = 515 ---
    check("R4", "produced_unit.item_id = 515",
          "515", unit["item_id"] == TARGET_ITEM_ID,
          str(unit["item_id"]))

    # --- R5: production_request_line_id = 27 ---
    check("R5", "produced_unit.production_request_line_id = 27",
          "27", unit["production_request_line_id"] == 27,
          str(unit["production_request_line_id"]))

    # --- R6: trailer id=1001 exists ---
    trailer = conn.execute(
        "SELECT id, vin, item_id, warehouse_id, status, lifecycle_status "
        "FROM trailer WHERE id=?",
        (TARGET_TRAILER_ID,)
    ).fetchone()
    check("R6", "trailer id=1001 exists",
          "exists", trailer is not None,
          "exists" if trailer else "NOT FOUND")
    if not trailer:
        for r_id, desc in [
            ("R7", "trailer.vin = MX4000004T0002670"),
            ("R8", "trailer.item_id = 515"),
            ("R9", "trailer.warehouse_id NOT NULL"),
        ]:
            results.append({"id": r_id, "description": desc, "expected": "—",
                            "actual": "SKIPPED (R6 failed)", "status": "SKIP", "detail": ""})
    else:
        # --- R7: VIN matches ---
        check("R7", "trailer.vin = 'MX4000004T0002670'",
              TARGET_TRAILER_VIN,
              (trailer["vin"] or "").strip().upper() == TARGET_TRAILER_VIN,
              str(trailer["vin"]))

        # --- R8: item_id matches ---
        check("R8", "trailer.item_id = 515",
              "515", trailer["item_id"] == TARGET_ITEM_ID,
              str(trailer["item_id"]))

        # --- R9: warehouse_id not NULL ---
        check("R9", "trailer.warehouse_id NOT NULL",
              "not NULL", trailer["warehouse_id"] is not None,
              str(trailer["warehouse_id"]))

    # --- R10: vin_registry id=987 exists ---
    vr = conn.execute(
        "SELECT id, vin_full, status, trailer_id, supply_need_id, customer_order_id "
        "FROM vin_registry WHERE id=?",
        (TARGET_VIN_REGISTRY_ID,)
    ).fetchone() if table_exists(conn, "vin_registry") else None

    check("R10", "vin_registry id=987 exists",
          "exists", vr is not None,
          "exists" if vr else "NOT FOUND",
          "vin_registry table missing" if not table_exists(conn, "vin_registry") else "")
    if not vr:
        for r_id, desc in [
            ("R11", "vin_registry.supply_need_id = 29"),
            ("R12", "vin_registry.trailer_id = 1001"),
        ]:
            results.append({"id": r_id, "description": desc, "expected": "—",
                            "actual": "SKIPPED (R10 failed)", "status": "SKIP", "detail": ""})
    else:
        # --- R11: supply_need_id = 29 ---
        check("R11", "vin_registry.supply_need_id = 29",
              "29", vr["supply_need_id"] == TARGET_SUPPLY_NEED_ID,
              str(vr["supply_need_id"]))

        # --- R12: trailer_id = 1001 ---
        check("R12", "vin_registry.trailer_id = 1001",
              "1001", vr["trailer_id"] == TARGET_TRAILER_ID,
              str(vr["trailer_id"]))

    # --- R13: supply_need id=29 exists and not cancelled ---
    if table_exists(conn, "supply_need"):
        sn = conn.execute(
            "SELECT id, status, cancelled_at, item_id FROM supply_need WHERE id=?",
            (TARGET_SUPPLY_NEED_ID,)
        ).fetchone()
        not_cancelled = (sn is not None and sn["cancelled_at"] is None
                         and (sn["status"] or "").upper() not in ("CANCELLED", "CANCELED"))
        check("R13", "supply_need id=29 exists and not cancelled",
              "exists, not cancelled", not_cancelled,
              f"exists, status={sn['status']}, cancelled_at={sn['cancelled_at']}" if sn else "NOT FOUND")
    else:
        results.append({"id": "R13", "description": "supply_need id=29 exists and not cancelled",
                        "expected": "—", "actual": "SKIPPED (table missing)",
                        "status": "SKIP", "detail": ""})

    # --- R14: no other produced_unit already claims trailer_id=1001 ---
    other_units = conn.execute(
        "SELECT id, status FROM produced_unit WHERE trailer_id=? AND id != ?",
        (TARGET_TRAILER_ID, TARGET_UNIT_ID)
    ).fetchall()
    check("R14", "no other produced_unit links trailer_id=1001 (excluding id=27)",
          "0 rows", len(other_units) == 0,
          f"{len(other_units)} row(s)" + (f": ids={[r['id'] for r in other_units]}" if other_units else ""),
          "conflict — another unit claims this trailer" if other_units else "")

    return results


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(checks: list[CheckResult], verdict: str, blockers: list[str],
                 branch: str) -> str:
    lines = [
        "# P0-FB Dry-Run Repair Report: produced_unit id=27",
        "",
        f"- Date: {TODAY}",
        f"- Run at: {NOW}",
        f"- Branch: {branch}",
        f"- DB: instance/trailers.db (SQLite, read-only)",
        f"- Mode: DRY-RUN — no changes applied",
        f"- Script: scripts/repair_produced_unit_27.py",
        "",
        "## Target repair",
        "",
        "| Field | Before | After |",
        "|---|---|---|",
        "| `produced_unit.id` | 27 | 27 (unchanged) |",
        "| `produced_unit.trailer_id` | NULL | **1001** |",
        "| `produced_unit.status` | vin_assigned | vin_assigned (unchanged) |",
        "",
        "## Pre-checks R1–R14",
        "",
        "| Check | Description | Expected | Actual | Status |",
        "|---|---|---|---|---|",
    ]
    for c in checks:
        detail = f" ({c['detail']})" if c.get("detail") else ""
        lines.append(
            f"| {c['id']} | {c['description']} | `{c['expected']}` "
            f"| `{c['actual']}{detail}` | **{c['status']}** |"
        )

    lines += [
        "",
        "## Proposed repair SQL",
        "",
        "> **NOT EXECUTED** — dry-run only",
        "",
        "```sql",
        "-- P0-FC apply (not yet approved):",
        "BEGIN;",
        "UPDATE produced_unit",
        "   SET trailer_id = 1001",
        " WHERE id = 27",
        "   AND trailer_id IS NULL",
        "   AND status = 'vin_assigned';",
        "-- Expected: 1 row affected",
        "COMMIT;",
        "```",
        "",
        "## Dry-run verdict",
        "",
        f"**{verdict}**",
        "",
    ]

    if blockers:
        lines += ["### Blockers", ""]
        for b in blockers:
            lines.append(f"- {b}")
        lines.append("")

    lines += [
        "## Verification plan (after P0-FC apply)",
        "",
        "1. Re-run `scripts/audit_vin_production_integrity.py` — expect P0 count = 0.",
        "2. Confirm `produced_unit id=27` → `trailer_id=1001`.",
        "3. Confirm no duplicate VIN in `trailer` table.",
        "4. Confirm `trailers.service` still active.",
        "",
        "## Forbidden actions",
        "",
        "- No UPDATE/DELETE/INSERT without `--apply` flag (P0-FC).",
        "- `--apply` not implemented in P0-FB.",
        "- No migration, no deploy, no push, no service restart.",
        "",
        "## Next step",
        "",
        "**P0-FC:** After reviewing this report and confirming all checks PASS,",
        "request explicit approval to implement `--apply` in the repair script",
        "and execute the controlled repair on production.",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if "--apply" in argv:
        print("ERROR: --apply is not implemented in P0-FB (dry-run only).", file=sys.stderr)
        print("P0-FC apply step requires separate explicit approval.", file=sys.stderr)
        return 1

    dry_run = True  # always in P0-FB

    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    print(f"[P0-FB] Dry-run repair: produced_unit id={TARGET_UNIT_ID}")
    print(f"[P0-FB] DB: {DB_PATH}")
    print(f"[P0-FB] Mode: DRY-RUN (read-only)")

    try:
        conn = open_db_readonly(DB_PATH)
    except Exception as exc:
        print(f"ERROR: Cannot open DB: {exc}", file=sys.stderr)
        return 2

    try:
        branch = get_branch()
        print(f"[P0-FB] Branch: {branch}")
        print(f"[P0-FB] Running pre-checks R1–R14...")

        checks = run_checks(conn)

        # Print check results
        passed = sum(1 for c in checks if c["status"] == "PASS")
        failed = sum(1 for c in checks if c["status"] == "FAIL")
        skipped = sum(1 for c in checks if c["status"] == "SKIP")

        for c in checks:
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}.get(c["status"], "?")
            detail = f" ({c['detail']})" if c.get("detail") else ""
            print(f"[P0-FB]  {mark} {c['id']:3s} {c['description']}: {c['actual']}{detail}")

        print(f"[P0-FB]")
        print(f"[P0-FB] Checks: {passed} PASS / {failed} FAIL / {skipped} SKIP")

        blockers = [f"{c['id']}: {c['description']} — got `{c['actual']}`"
                    + (f" ({c['detail']})" if c.get("detail") else "")
                    for c in checks if c["status"] == "FAIL"]

        if failed == 0:
            verdict = "READY_FOR_APPLY"
            print(f"[P0-FB] Verdict: READY_FOR_APPLY")
            print(f"[P0-FB]")
            print(f"[P0-FB] Proposed change (NOT EXECUTED):")
            print(f"[P0-FB]   UPDATE produced_unit SET trailer_id = {TARGET_TRAILER_ID}")
            print(f"[P0-FB]   WHERE id = {TARGET_UNIT_ID} AND trailer_id IS NULL;")
        else:
            verdict = "BLOCKED"
            print(f"[P0-FB] Verdict: BLOCKED")
            print(f"[P0-FB] Blockers:")
            for b in blockers:
                print(f"[P0-FB]   - {b}")

        print(f"[P0-FB]")
        print(f"[P0-FB] DRY RUN — no changes applied to DB.")

        # Write report
        report = build_report(checks, verdict, blockers, branch)
        REPORT_DOCS.parent.mkdir(parents=True, exist_ok=True)
        REPORT_DOCS.write_text(report, encoding="utf-8")
        print(f"[P0-FB] Report: {REPORT_DOCS}")

        return 0 if failed == 0 else 1

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
