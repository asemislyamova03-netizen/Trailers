#!/usr/bin/env python3
"""
P0-K: Specific VIN Linkage Audit
----------------------------------
Read-only. Pass --db /path/to/trailers.db to override default.

Checks two groups of VIN suffixes reported by user:

GROUP_A (SOLD_WITHOUT_TRAILER):
  2681 2680 2674 2566 2524 2670 2637 2414 2578 2544 2655

GROUP_B (MISSING_SOURCE_WAREHOUSE / PRODUCTION_BLOCKED):
  2706 2708 2659

For each suffix finds all matching records in:
  vin_registry, trailer, produced_unit, supply_need,
  production_request_line, reservation, customer_order, customer_order_line
"""

import sqlite3
import sys
import os
import argparse
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(__file__), '..', 'instance', 'trailers.db')

GROUP_A = ['2681', '2680', '2674', '2566', '2524', '2670', '2637', '2414', '2578', '2544', '2655']
GROUP_B = ['2706', '2708', '2659']
ALL_SUFFIXES = sorted(set(GROUP_A + GROUP_B))

SOLD_STATUSES = ('sold_not_shipped', 'shipped', 'done', 'customer_shipped', 'ready_to_ship', 'delivered', 'closed')

# ---------------------------------------------------------------------------

def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f'[ERROR] DB not found: {db_path}', file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def column_exists(conn, table: str, col: str) -> bool:
    return col in [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]


def section(title: str):
    print()
    print('=' * 70)
    print(title)
    print('=' * 70)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_vin_rows(conn, suffix: str) -> list:
    return conn.execute(
        "SELECT id, vin_full, serial7, status, trailer_id, "
        "customer_order_id, supply_need_id, order_line_id, "
        "assigned_at, confirmed_at, docs_issued_at "
        "FROM vin_registry WHERE vin_full LIKE ? ORDER BY id",
        (f'%{suffix}',)
    ).fetchall()


def fetch_trailer_by_vin_suffix(conn, suffix: str) -> list:
    return conn.execute(
        "SELECT id, vin, status, warehouse_id, item_id "
        "FROM trailer WHERE vin LIKE ? ORDER BY id",
        (f'%{suffix}',)
    ).fetchall()


def fetch_produced_unit_for_trailer(conn, trailer_id: int) -> list:
    return conn.execute(
        "SELECT id, status, trailer_id, order_id, item_id, "
        "target_warehouse_id, production_request_line_id, created_at "
        "FROM produced_unit WHERE trailer_id=? ORDER BY id",
        (trailer_id,)
    ).fetchall()


def fetch_produced_unit_for_order(conn, order_id: int) -> list:
    return conn.execute(
        "SELECT id, status, trailer_id, order_id, item_id, "
        "target_warehouse_id, production_request_line_id, created_at "
        "FROM produced_unit WHERE order_id=? ORDER BY id",
        (order_id,)
    ).fetchall()


def fetch_order(conn, order_id: int):
    return conn.execute(
        "SELECT id, order_number, status, trailer_id, warehouse_id, "
        "documents_issued, is_shipped, fulfillment_source, created_at "
        "FROM customer_order WHERE id=?",
        (order_id,)
    ).fetchone()


def fetch_order_lines_for_order(conn, order_id: int) -> list:
    return conn.execute(
        "SELECT id, order_id, line_no, line_type, fulfillment_source, "
        "fulfillment_status, status, trailer_id, vin_registry_id, "
        "supply_need_id, reservation_id "
        "FROM customer_order_line WHERE order_id=? ORDER BY line_no",
        (order_id,)
    ).fetchall()


def fetch_supply_need(conn, sn_id: int):
    if not table_exists(conn, 'supply_need'):
        return None
    cols = [r[1] for r in conn.execute('PRAGMA table_info(supply_need)')]
    wh_col = 'warehouse_id' if 'warehouse_id' in cols else None
    src_col = 'source_warehouse_id' if 'source_warehouse_id' in cols else None
    select = ['id', 'status', 'need_type']
    if wh_col:
        select.append(wh_col)
    if src_col:
        select.append(src_col)
    row = conn.execute(
        f"SELECT {','.join(select)} FROM supply_need WHERE id=?", (sn_id,)
    ).fetchone()
    return dict(row) if row else None


def fetch_prl_for_supply_need(conn, sn_id: int) -> list:
    if not table_exists(conn, 'production_request_line'):
        return []
    cols = [r[1] for r in conn.execute('PRAGMA table_info(production_request_line)')]
    src_col = 'source_warehouse_id' if 'source_warehouse_id' in cols else None
    select = ['id', 'status', 'supply_need_id']
    if src_col:
        select.append(src_col)
    if 'production_request_id' in cols:
        select.append('production_request_id')
    if 'quantity' in cols:
        select.append('quantity')
    return conn.execute(
        f"SELECT {','.join(select)} FROM production_request_line WHERE supply_need_id=? ORDER BY id",
        (sn_id,)
    ).fetchall()


def fetch_all_produced_units_for_sn(conn, sn_id: int) -> list:
    if not table_exists(conn, 'produced_unit'):
        return []
    cols = [r[1] for r in conn.execute('PRAGMA table_info(produced_unit)')]
    if 'supply_need_id' in cols:
        return conn.execute(
            "SELECT id, status, trailer_id, order_id, item_id, "
            "target_warehouse_id, production_request_line_id, created_at "
            "FROM produced_unit WHERE supply_need_id=? ORDER BY id",
            (sn_id,)
        ).fetchall()
    # fallback: via production_request_line
    prl_ids = [r['id'] for r in fetch_prl_for_supply_need(conn, sn_id)]
    if not prl_ids:
        return []
    ph = ','.join('?' * len(prl_ids))
    return conn.execute(
        f"SELECT id, status, trailer_id, order_id, item_id, "
        f"target_warehouse_id, production_request_line_id, created_at "
        f"FROM produced_unit WHERE production_request_line_id IN ({ph}) ORDER BY id",
        prl_ids
    ).fetchall()


def fetch_reservations_for_order(conn, order_id: int) -> list:
    return conn.execute(
        "SELECT id, trailer_id, status, source_type, order_id, order_line_id "
        "FROM reservation WHERE order_id=? ORDER BY id",
        (order_id,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Classify helper
# ---------------------------------------------------------------------------

def classify(suffix: str, group: str, vr_rows, trailer_rows,
             order_data, all_pus, all_sns, prl_rows, reservations) -> str:
    """Return classification letter A-G."""
    has_trailer = bool(trailer_rows)
    trailer_linked_to_vr = any(vr.get('trailer_id') or vr['trailer_id'] for vr in (dict(r) for r in vr_rows) if vr.get('trailer_id'))

    # G: duplicate — multiple distinct orders/trailers for same suffix
    distinct_trailers = {r['id'] for r in trailer_rows}
    distinct_orders = set()
    for r in vr_rows:
        if r['customer_order_id']:
            distinct_orders.add(r['customer_order_id'])
    if len(distinct_trailers) > 1 or len(distinct_orders) > 1:
        return 'G'

    # D: order is sold/shipped but trailer missing in vr/order
    if order_data:
        o = dict(order_data)
        if o['status'] in SOLD_STATUSES and not o['trailer_id']:
            return 'D'
        # sold + vr has no trailer_id
        for vr in vr_rows:
            vd = dict(vr)
            if vd['status'] in ('confirmed', 'assigned') and not vd['trailer_id']:
                return 'D'

    # A: trailer exists but some links are missing
    if has_trailer:
        # check if produced_unit.trailer_id matches
        for t in trailer_rows:
            for pu in all_pus:
                pud = dict(pu)
                if pud.get('trailer_id') != t['id'] and pud.get('status') in ('vin_assigned',):
                    return 'A'
        # vr.trailer_id missing but trailer exists
        for vr in vr_rows:
            vd = dict(vr)
            if not vd['trailer_id'] and has_trailer:
                return 'A'
        return 'A'

    # E: missing source warehouse blocks production (GROUP_B)
    if group == 'B':
        for sn in all_sns:
            snd = dict(sn) if sn else {}
            if not snd.get('warehouse_id') and not snd.get('source_warehouse_id'):
                return 'E'
        if not all_sns and prl_rows:
            # prl exists but no supply need
            for prl in prl_rows:
                prld = dict(prl)
                if not prld.get('source_warehouse_id'):
                    return 'E'
        return 'E'

    # B: no trailer but enough data to create
    if vr_rows and order_data:
        return 'B'

    # C: reserved, valid watch
    for vr in vr_rows:
        vd = dict(vr)
        if vd['status'] == 'reserved':
            return 'C'

    return 'F'


# ---------------------------------------------------------------------------
# Main audit per suffix
# ---------------------------------------------------------------------------

def audit_suffix(conn, suffix: str, group: str):
    print(f'\n{"─"*70}')
    print(f'SUFFIX: {suffix}  (GROUP {group})')
    print(f'{"─"*70}')

    # --- vin_registry ---
    vr_rows = fetch_vin_rows(conn, suffix)
    print(f'\n  vin_registry matches: {len(vr_rows)}')
    for vr in vr_rows:
        print(f'    vr_id={vr["id"]} vin={vr["vin_full"]} s7={vr["serial7"]} '
              f'status={vr["status"]} trailer_id={vr["trailer_id"]} '
              f'order={vr["customer_order_id"]} sn={vr["supply_need_id"]} '
              f'line={vr["order_line_id"]} docs={vr["docs_issued_at"]}')

    # --- trailer ---
    trailer_rows = fetch_trailer_by_vin_suffix(conn, suffix)
    print(f'\n  trailer matches: {len(trailer_rows)}')
    for t in trailer_rows:
        print(f'    trailer_id={t["id"]} vin={t["vin"]} status={t["status"]} '
              f'wh={t["warehouse_id"]} item={t["item_id"]}')

    # Collect order IDs from VIN rows
    order_ids = set()
    sn_ids = set()
    for vr in vr_rows:
        if vr['customer_order_id']:
            order_ids.add(vr['customer_order_id'])
        if vr['supply_need_id']:
            sn_ids.add(vr['supply_need_id'])

    # --- orders ---
    orders = {}
    for oid in order_ids:
        o = fetch_order(conn, oid)
        if o:
            orders[oid] = o
    if orders:
        print(f'\n  orders: {len(orders)}')
        for oid, o in orders.items():
            print(f'    order_id={o["id"]} num={o["order_number"]} '
                  f'status={o["status"]} trailer={o["trailer_id"]} '
                  f'wh={o["warehouse_id"]} docs={o["documents_issued"]} '
                  f'shipped={o["is_shipped"]} fs={o["fulfillment_source"]}')
            # order lines
            lines = fetch_order_lines_for_order(conn, oid)
            for ln in lines:
                print(f'      line_id={ln["id"]} line_no={ln["line_no"]} '
                      f'type={ln["line_type"]} fs={ln["fulfillment_source"]} '
                      f'status={ln["status"]} trailer={ln["trailer_id"]} '
                      f'vr={ln["vin_registry_id"]} sn={ln["supply_need_id"]}')
            # reservations
            ress = fetch_reservations_for_order(conn, oid)
            for r in ress:
                print(f'      res_id={r["id"]} trailer={r["trailer_id"]} '
                      f'status={r["status"]} src={r["source_type"]}')

    # --- supply needs ---
    all_sns = []
    all_prls = []
    for sn_id in sn_ids:
        sn = fetch_supply_need(conn, sn_id)
        if sn:
            all_sns.append(sn)
            print(f'\n  supply_need: id={sn["id"]} status={sn["status"]} '
                  f'type={sn["need_type"]} wh={sn.get("warehouse_id")} '
                  f'src_wh={sn.get("source_warehouse_id")}')
            prls = fetch_prl_for_supply_need(conn, sn_id)
            all_prls.extend(prls)
            for prl in prls:
                prld = dict(prl)
                print(f'    prl_id={prld["id"]} status={prld["status"]} '
                      f'src_wh={prld.get("source_warehouse_id")} '
                      f'pr={prld.get("production_request_id")} '
                      f'qty={prld.get("quantity")}')

    # --- produced units ---
    all_pus = []
    for t in trailer_rows:
        pus = fetch_produced_unit_for_trailer(conn, t['id'])
        all_pus.extend(pus)
    for oid in order_ids:
        pus = fetch_produced_unit_for_order(conn, oid)
        for pu in pus:
            if pu['id'] not in {p['id'] for p in all_pus}:
                all_pus.append(pu)
    for sn_id in sn_ids:
        pus = fetch_all_produced_units_for_sn(conn, sn_id)
        for pu in pus:
            if pu['id'] not in {p['id'] for p in all_pus}:
                all_pus.append(pu)

    if all_pus:
        print(f'\n  produced_units: {len(all_pus)}')
        for pu in all_pus:
            pud = dict(pu)
            print(f'    pu_id={pud["id"]} status={pud["status"]} '
                  f'trailer={pud["trailer_id"]} order={pud["order_id"]} '
                  f'item={pud["item_id"]} wh={pud["target_warehouse_id"]} '
                  f'prl={pud["production_request_line_id"]} '
                  f'created={pud["created_at"]}')

    # --- classify ---
    first_order = next(iter(orders.values()), None)
    cls = classify(suffix, group, vr_rows, trailer_rows, first_order,
                   all_pus, all_sns, all_prls, [])

    print(f'\n  CLASSIFICATION: {cls}')

    # --- blocker analysis ---
    blockers = []
    if group == 'B':
        if not all_sns:
            blockers.append('no supply_need found')
        for sn in all_sns:
            snd = dict(sn)
            if not snd.get('warehouse_id') and not snd.get('source_warehouse_id'):
                blockers.append(f'supply_need id={snd["id"]}: warehouse_id=NULL AND source_warehouse_id=NULL')
        for prl in all_prls:
            prld = dict(prl)
            if not prld.get('source_warehouse_id'):
                blockers.append(f'prl id={prld["id"]}: source_warehouse_id=NULL')

    if group == 'A':
        for o in orders.values():
            od = dict(o)
            if od['status'] in SOLD_STATUSES and not od['trailer_id']:
                blockers.append(f'order {od["order_number"]} is {od["status"]} but trailer_id=NULL')

    if blockers:
        print('  BLOCKERS:')
        for b in blockers:
            print(f'    - {b}')

    return {
        'suffix': suffix,
        'group': group,
        'classification': cls,
        'vr_count': len(vr_rows),
        'trailer_count': len(trailer_rows),
        'order_ids': list(order_ids),
        'pu_count': len(all_pus),
        'sn_ids': list(sn_ids),
        'blockers': blockers,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='P0-K specific VIN linkage audit — read-only')
    parser.add_argument('--db', default=DEFAULT_DB)
    args = parser.parse_args()

    print(f'P0-K Audit  |  {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC')
    print(f'DB: {os.path.abspath(args.db)}')
    print('MODE: READ-ONLY')
    print(f'GROUP_A (sold/no trailer): {GROUP_A}')
    print(f'GROUP_B (missing warehouse/blocked): {GROUP_B}')

    conn = connect(args.db)
    results = []

    section('GROUP A — SOLD WITHOUT TRAILER')
    for suffix in sorted(set(GROUP_A)):
        r = audit_suffix(conn, suffix, 'A')
        results.append(r)

    section('GROUP B — MISSING SOURCE WAREHOUSE / PRODUCTION BLOCKED')
    for suffix in GROUP_B:
        r = audit_suffix(conn, suffix, 'B')
        results.append(r)

    section('SUMMARY')
    by_cls: dict[str, list] = {}
    for r in results:
        by_cls.setdefault(r['classification'], []).append(r['suffix'])

    total_p0 = len([r for r in results if r['classification'] in ('D', 'E')])
    print(f'  Total suffixes audited: {len(results)}')
    print(f'  Total P0 cases (D or E): {total_p0}')
    print()
    for cls in sorted(by_cls):
        print(f'  Class {cls}: {by_cls[cls]}')

    conn.close()


if __name__ == '__main__':
    main()
