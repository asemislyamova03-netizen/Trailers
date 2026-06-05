#!/usr/bin/env python3
"""
P0-FC: Controlled production data repair for produced_unit id=27.

SAFETY GUARANTEES:
- Default mode: read-only (dry-run). No data changes.
- --apply requires BOTH --apply AND --i-understand-production-data-change.
- Before apply: all R1-R14 pre-checks must pass.
- Before apply: creates a timestamped DB backup and verifies it.
- Apply uses BEGIN IMMEDIATE transaction + PRAGMA foreign_keys=ON.
- Only ONE UPDATE is permitted:
    UPDATE produced_unit SET trailer_id = 1001
    WHERE id = 27 AND trailer_id IS NULL AND status = 'vin_assigned';
- Post-update verification before COMMIT; rolls back on mismatch.
- No other UPDATE/INSERT/DELETE statements.
- No db.session.commit(), no migrations, no schema changes.
- No personal data in output (IDs, VINs, order numbers only).

Usage:
  python scripts/repair_produced_unit_27.py                         # dry-run
  python scripts/repair_produced_unit_27.py --dry-run               # dry-run
  python scripts/repair_produced_unit_27.py --apply                 # error: missing confirmation
  python scripts/repair_produced_unit_27.py --apply --i-understand-production-data-change

Plan reference:
  docs/ai/live-support/2026-06-05-trailers-produced-unit-27-repair-p0f-plan.md
  docs/ai/live-support/2026-06-05-trailers-produced-unit-27-repair-p0fb-dry-run-report.md
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

PREFIX = "[P0-FC]"


def do_apply(branch: str) -> tuple[str, list[CheckResult], int, str, list[str], str]:
    """
    Execute the controlled production data repair.
    Returns: (backup_path, checks, rowcount, post_check_status, errors, sql_executed)
    """
    import shutil
    import sqlite3 as _sqlite3

    errors: list[str] = []
    sql_executed = (
        "UPDATE produced_unit "
        f"SET trailer_id = {TARGET_TRAILER_ID} "
        f"WHERE id = {TARGET_UNIT_ID} "
        "AND trailer_id IS NULL "
        "AND status = 'vin_assigned';"
    )

    # --- Pre-checks (read-only) ---
    print(f"{PREFIX} Running pre-checks R1–R14 before apply...")
    ro_conn = open_db_readonly(DB_PATH)
    try:
        checks = run_checks(ro_conn)
    finally:
        ro_conn.close()

    failed = [c for c in checks if c["status"] == "FAIL"]
    for c in checks:
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}.get(c["status"], "?")
        print(f"{PREFIX}  {mark} {c['id']:3s} {c['description']}: {c['actual']}")

    if failed:
        errs = [f"{c['id']}: {c['description']} — {c['actual']}" for c in failed]
        print(f"{PREFIX} PRE-CHECK FAILED — aborting apply:")
        for e in errs:
            print(f"{PREFIX}   ✗ {e}")
        return "", checks, 0, "PRE_CHECK_FAILED", errs, sql_executed

    print(f"{PREFIX} All {len(checks)} pre-checks PASSED.")

    # --- Backup ---
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"trailers_before_produced_unit_27_repair_{ts}.db"
    backup_path = DB_PATH.parent.parent / "backups" / backup_name
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"{PREFIX} Creating backup: {backup_path.name}")
    try:
        shutil.copy2(str(DB_PATH), str(backup_path))
    except Exception as exc:
        err = f"Backup failed: {exc}"
        print(f"{PREFIX} ERROR: {err}", file=sys.stderr)
        return str(backup_path), checks, 0, "BACKUP_FAILED", [err], sql_executed

    backup_size = backup_path.stat().st_size
    if backup_size == 0:
        err = "Backup file is 0 bytes — aborting"
        print(f"{PREFIX} ERROR: {err}", file=sys.stderr)
        return str(backup_path), checks, 0, "BACKUP_EMPTY", [err], sql_executed

    print(f"{PREFIX} Backup OK: {backup_path.name} ({backup_size:,} bytes)")

    # --- Apply transaction ---
    print(f"{PREFIX} Opening DB for write...")
    try:
        rw_conn = _sqlite3.connect(str(DB_PATH), timeout=10)
        rw_conn.row_factory = _sqlite3.Row
    except Exception as exc:
        err = f"Cannot open DB for write: {exc}"
        print(f"{PREFIX} ERROR: {err}", file=sys.stderr)
        return str(backup_path), checks, 0, "DB_OPEN_FAILED", [err], sql_executed

    rowcount = 0
    post_status = "UNKNOWN"
    try:
        rw_conn.execute("PRAGMA foreign_keys = ON")
        rw_conn.execute("BEGIN IMMEDIATE")

        cursor = rw_conn.execute(
            "UPDATE produced_unit "
            "SET trailer_id = ? "
            "WHERE id = ? AND trailer_id IS NULL AND status = 'vin_assigned'",
            (TARGET_TRAILER_ID, TARGET_UNIT_ID),
        )
        rowcount = cursor.rowcount
        print(f"{PREFIX} UPDATE rowcount: {rowcount}")

        if rowcount != 1:
            err = f"Expected rowcount=1, got {rowcount} — rolling back"
            print(f"{PREFIX} ERROR: {err}")
            rw_conn.rollback()
            return str(backup_path), checks, rowcount, "ROWCOUNT_MISMATCH", [err], sql_executed

        # Post-update verification before COMMIT
        row = rw_conn.execute(
            "SELECT id, trailer_id, status FROM produced_unit WHERE id=?",
            (TARGET_UNIT_ID,)
        ).fetchone()

        if row is None:
            err = "Post-update: produced_unit id=27 not found — rolling back"
            print(f"{PREFIX} ERROR: {err}")
            rw_conn.rollback()
            return str(backup_path), checks, rowcount, "POST_VERIFY_FAILED", [err], sql_executed

        if row["trailer_id"] != TARGET_TRAILER_ID:
            err = f"Post-update: trailer_id={row['trailer_id']} != expected {TARGET_TRAILER_ID} — rolling back"
            print(f"{PREFIX} ERROR: {err}")
            rw_conn.rollback()
            return str(backup_path), checks, rowcount, "POST_VERIFY_FAILED", [err], sql_executed

        if row["status"] != "vin_assigned":
            err = f"Post-update: status={row['status']} unexpected — rolling back"
            print(f"{PREFIX} ERROR: {err}")
            rw_conn.rollback()
            return str(backup_path), checks, rowcount, "POST_VERIFY_FAILED", [err], sql_executed

        print(f"{PREFIX} Post-update verification: produced_unit.trailer_id={row['trailer_id']} ✓")
        rw_conn.commit()
        post_status = "VERIFIED_OK"
        print(f"{PREFIX} COMMITTED.")

    except Exception as exc:
        try:
            rw_conn.rollback()
        except Exception:
            pass
        err = f"Transaction error: {exc}"
        print(f"{PREFIX} ERROR: {err}", file=sys.stderr)
        return str(backup_path), checks, rowcount, "TRANSACTION_ERROR", [err], sql_executed
    finally:
        rw_conn.close()

    # --- Post-commit read-only verify ---
    print(f"{PREFIX} Post-commit read-only verification...")
    verify_conn = open_db_readonly(DB_PATH)
    try:
        row2 = verify_conn.execute(
            "SELECT id, trailer_id, status FROM produced_unit WHERE id=?",
            (TARGET_UNIT_ID,)
        ).fetchone()
        if row2 and row2["trailer_id"] == TARGET_TRAILER_ID:
            print(f"{PREFIX} ✓ Post-commit: produced_unit id=27, trailer_id={row2['trailer_id']}, status={row2['status']}")
        else:
            actual = row2["trailer_id"] if row2 else "MISSING"
            err = f"Post-commit read-only check failed: trailer_id={actual}"
            print(f"{PREFIX} ERROR: {err}")
            errors.append(err)
            post_status = "POST_COMMIT_VERIFY_FAILED"
    finally:
        verify_conn.close()

    return str(backup_path), checks, rowcount, post_status, errors, sql_executed


def main(argv: list[str]) -> int:
    apply_mode = "--apply" in argv
    confirmed = "--i-understand-production-data-change" in argv

    if apply_mode and not confirmed:
        print("ERROR: --apply requires --i-understand-production-data-change flag.", file=sys.stderr)
        print("This confirms you understand you are modifying production data.", file=sys.stderr)
        print("Usage: python scripts/repair_produced_unit_27.py --apply --i-understand-production-data-change",
              file=sys.stderr)
        return 1

    dry_run = not (apply_mode and confirmed)
    tag = "DRY-RUN" if dry_run else "APPLY"

    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    print(f"{PREFIX} produced_unit id={TARGET_UNIT_ID} repair — {tag}")
    print(f"{PREFIX} DB: {DB_PATH}")
    print(f"{PREFIX} Mode: {tag}")

    try:
        conn = open_db_readonly(DB_PATH)
    except Exception as exc:
        print(f"ERROR: Cannot open DB: {exc}", file=sys.stderr)
        return 2

    branch = get_branch()
    print(f"{PREFIX} Branch: {branch}")

    if dry_run:
        try:
            print(f"{PREFIX} Running pre-checks R1–R14...")
            checks = run_checks(conn)
            passed = sum(1 for c in checks if c["status"] == "PASS")
            failed = sum(1 for c in checks if c["status"] == "FAIL")
            skipped = sum(1 for c in checks if c["status"] == "SKIP")
            for c in checks:
                mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}.get(c["status"], "?")
                detail = f" ({c['detail']})" if c.get("detail") else ""
                print(f"{PREFIX}  {mark} {c['id']:3s} {c['description']}: {c['actual']}{detail}")
            print(f"{PREFIX}")
            print(f"{PREFIX} Checks: {passed} PASS / {failed} FAIL / {skipped} SKIP")
            blockers = [f"{c['id']}: {c['description']} — got `{c['actual']}`"
                        + (f" ({c['detail']})" if c.get("detail") else "")
                        for c in checks if c["status"] == "FAIL"]
            if failed == 0:
                verdict = "READY_FOR_APPLY"
                print(f"{PREFIX} Verdict: READY_FOR_APPLY")
                print(f"{PREFIX}")
                print(f"{PREFIX} Proposed change (NOT EXECUTED):")
                print(f"{PREFIX}   UPDATE produced_unit SET trailer_id = {TARGET_TRAILER_ID}")
                print(f"{PREFIX}   WHERE id = {TARGET_UNIT_ID} AND trailer_id IS NULL;")
            else:
                verdict = "BLOCKED"
                print(f"{PREFIX} Verdict: BLOCKED")
                for b in blockers:
                    print(f"{PREFIX}   - {b}")
            print(f"{PREFIX}")
            print(f"{PREFIX} DRY RUN — no changes applied.")
            report = build_report(checks, verdict, blockers, branch)
            REPORT_DOCS.parent.mkdir(parents=True, exist_ok=True)
            REPORT_DOCS.write_text(report, encoding="utf-8")
            print(f"{PREFIX} Report: {REPORT_DOCS}")
            return 0 if failed == 0 else 1
        finally:
            conn.close()
    else:
        conn.close()
        # Apply path
        backup_path, checks, rowcount, post_status, errors, sql_executed = do_apply(branch)
        success = (post_status == "VERIFIED_OK" and rowcount == 1 and not errors)
        print(f"{PREFIX}")
        if success:
            print(f"{PREFIX} ✓ REPAIR APPLIED SUCCESSFULLY.")
            print(f"{PREFIX}   produced_unit id={TARGET_UNIT_ID}: trailer_id NULL → {TARGET_TRAILER_ID}")
        else:
            print(f"{PREFIX} ✗ REPAIR FAILED or INCOMPLETE.")
            for e in errors:
                print(f"{PREFIX}   ERROR: {e}")
        print(f"{PREFIX} Backup: {backup_path}")
        return 0 if success else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
