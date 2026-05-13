import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ORDER_ID = 2
CONTRACT_ID = 754
OLD_TRAILER_ID = 786
SUPPLY_NEED_ID = 2

OLD_VIN_IN_DB = "MX4000002S0002645"
OLD_VIN_CORRECTED = "MX4000002T0002645"
NEW_ORDER_VIN = "MX4000002T0002635"


def parse_vin(vin):
    return {
        "vin_full": vin,
        "prefix": vin[:3],
        "vin_modification_code": vin[3:9],
        "year_code": vin[9],
        "serial7": vin[-7:],
    }


def fetch_one(cur, query, params=()):
    return cur.execute(query, params).fetchone()


def backup_db(db_path):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_backup_before_vin_2635_2645_{stamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def already_fixed(cur):
    order = fetch_one(cur, "select trailer_id from customer_order where id=?", (ORDER_ID,))
    contract = fetch_one(cur, "select trailer_id from sales_contract where id=?", (CONTRACT_ID,))
    new_trailer = fetch_one(cur, "select id from trailer where vin=?", (NEW_ORDER_VIN,))
    old_trailer = fetch_one(cur, "select status from trailer where id=? and vin=?", (OLD_TRAILER_ID, OLD_VIN_CORRECTED))
    return bool(order and contract and new_trailer and old_trailer and order["trailer_id"] == new_trailer["id"] and contract["trailer_id"] == new_trailer["id"])


def apply_fix(db_path, dry_run=False):
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    backup_path = None
    if not dry_run:
        backup_path = backup_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")

    try:
        cur.execute("BEGIN")
        if already_fixed(cur):
            conn.rollback()
            return {"status": "already_fixed", "backup": str(backup_path) if backup_path else None}

        old = fetch_one(cur, "select * from trailer where id=?", (OLD_TRAILER_ID,))
        if not old:
            raise RuntimeError("Old trailer 786 not found")
        if old["vin"] not in (OLD_VIN_IN_DB, OLD_VIN_CORRECTED):
            raise RuntimeError(f"Unexpected old trailer VIN: {old['vin']}")

        order = fetch_one(cur, "select * from customer_order where id=?", (ORDER_ID,))
        if not order:
            raise RuntimeError("Order 2 not found")
        contract = fetch_one(cur, "select * from sales_contract where id=? and order_id=?", (CONTRACT_ID, ORDER_ID))
        if not contract:
            raise RuntimeError("Contract 754 for order 2 not found")

        existing_new = fetch_one(cur, "select id from trailer where vin=?", (NEW_ORDER_VIN,))
        if existing_new:
            raise RuntimeError(f"{NEW_ORDER_VIN} already exists but order/contract are not both linked to it")
        if fetch_one(cur, "select id from vin_registry where serial7=? or vin_full=?", (NEW_ORDER_VIN[-7:], NEW_ORDER_VIN)):
            raise RuntimeError(f"VIN registry row for {NEW_ORDER_VIN} already exists")
        if fetch_one(cur, "select id from trailer where vin=? and id<>?", (OLD_VIN_CORRECTED, OLD_TRAILER_ID)):
            raise RuntimeError(f"{OLD_VIN_CORRECTED} exists on another trailer")

        old_vin = parse_vin(OLD_VIN_CORRECTED)
        cur.execute(
            "update trailer set vin=?, status=?, lifecycle_status=? where id=?",
            (OLD_VIN_CORRECTED, "IN_STOCK", "in_stock", OLD_TRAILER_ID),
        )
        cur.execute(
            """
            update vin_registry
               set vin_full=?, prefix=?, vin_modification_code=?, year_code=?,
                   customer_order_id=NULL, supply_need_id=NULL, sales_contract_id=NULL,
                   docs_issued_order_id=NULL, docs_issued_at=NULL, updated_at=?
             where trailer_id=? and serial7=?
            """,
            (
                old_vin["vin_full"],
                old_vin["prefix"],
                old_vin["vin_modification_code"],
                old_vin["year_code"],
                now,
                OLD_TRAILER_ID,
                OLD_VIN_CORRECTED[-7:],
            ),
        )

        cur.execute(
            """
            insert into trailer (vin, item_id, warehouse_id, manufacture_date, created_at, status, comment, otts_id, lifecycle_status)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                NEW_ORDER_VIN,
                old["item_id"],
                old["warehouse_id"],
                old["manufacture_date"],
                now,
                "SOLD",
                "Created by VIN correction: replace T0002645 with T0002635 for ORD-000002",
                old["otts_id"],
                "customer_shipped",
            ),
        )
        new_trailer_id = cur.lastrowid

        new_vin = parse_vin(NEW_ORDER_VIN)
        cur.execute(
            """
            insert into vin_registry (
                vin_full, prefix, vin_modification_code, year_code, serial7, status,
                customer_order_id, supply_need_id, trailer_id, sales_contract_id, docs_issued_order_id,
                reserved_by_user_id, assigned_by_user_id, confirmed_by_user_id, void_by_user_id,
                reserved_at, assigned_at, confirmed_at, docs_issued_at, void_at,
                source, comment, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                new_vin["vin_full"],
                new_vin["prefix"],
                new_vin["vin_modification_code"],
                new_vin["year_code"],
                new_vin["serial7"],
                ORDER_ID,
                SUPPLY_NEED_ID,
                new_trailer_id,
                CONTRACT_ID,
                now,
                now,
                "manual_order_trailer_replace",
                "Replacement VIN for ORD-000002",
                now,
                now,
            ),
        )
        new_registry_id = cur.lastrowid

        cur.execute("update customer_order set trailer_id=?, updated_at=? where id=?", (new_trailer_id, now, ORDER_ID))
        cur.execute("update sales_contract set trailer_id=? where id=?", (new_trailer_id, CONTRACT_ID))
        cur.execute("update produced_unit set trailer_id=? where trailer_id=?", (new_trailer_id, OLD_TRAILER_ID))
        cur.execute("update reservation set trailer_id=? where order_id=? and trailer_id=?", (new_trailer_id, ORDER_ID, OLD_TRAILER_ID))
        cur.execute("update stock_movement set trailer_id=? where order_id=? and trailer_id=?", (new_trailer_id, ORDER_ID, OLD_TRAILER_ID))

        cur.execute(
            """
            insert into vin_registry_event (
                vin_registry_id, event_type, old_status, new_status,
                customer_order_id, sales_contract_id, trailer_id, user_id, comment, created_at
            )
            values (?, 'created', NULL, 'confirmed', ?, ?, ?, NULL, ?, ?)
            """,
            (
                new_registry_id,
                ORDER_ID,
                CONTRACT_ID,
                new_trailer_id,
                "Created by VIN correction for ORD-000002",
                now,
            ),
        )
        old_registry = fetch_one(
            cur,
            "select id from vin_registry where trailer_id=? and serial7=?",
            (OLD_TRAILER_ID, OLD_VIN_CORRECTED[-7:]),
        )
        if old_registry:
            cur.execute(
                """
                insert into vin_registry_event (
                    vin_registry_id, event_type, old_status, new_status,
                    customer_order_id, sales_contract_id, trailer_id, user_id, comment, created_at
                )
                values (?, 'reservation_cancelled', 'confirmed', 'confirmed', NULL, NULL, ?, NULL, ?, ?)
                """,
                (
                    old_registry["id"],
                    OLD_TRAILER_ID,
                    f"Detached from ORD-000002 during replacement with {NEW_ORDER_VIN}",
                    now,
                ),
            )
        cur.execute(
            """
            insert into order_event (order_id, user_id, event_type, old_value, new_value, comment, created_at)
            values (?, NULL, 'trailer_assigned', ?, ?, ?, ?)
            """,
            (
                ORDER_ID,
                OLD_VIN_CORRECTED,
                NEW_ORDER_VIN,
                "Data correction: synchronized order, contract, produced unit, shipments, and VIN registry.",
                now,
            ),
        )

        if dry_run:
            conn.rollback()
            return {"status": "dry_run_ok", "new_trailer_id": new_trailer_id, "new_registry_id": new_registry_id}

        conn.commit()
        return {
            "status": "fixed",
            "backup": str(backup_path),
            "new_trailer_id": new_trailer_id,
            "new_registry_id": new_registry_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Fix ORD-000002 trailer replacement from VIN 2645 to VIN 2635.")
    parser.add_argument("--db", default="trailers.db", help="Path to trailers.db")
    parser.add_argument("--dry-run", action="store_true", help="Validate and rollback without writing")
    args = parser.parse_args()

    result = apply_fix(Path(args.db), dry_run=args.dry_run)
    print(result)


if __name__ == "__main__":
    main()
