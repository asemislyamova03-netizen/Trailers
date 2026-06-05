#!/usr/bin/env python3
"""
P0-H: Stock Release VIN / Reservation Integrity Audit
------------------------------------------------------
READ-ONLY.  Pass --db /path/to/trailers.db to override the default path.

Classification model
====================
VALID-A  VIN exists, trailer_id NULL, no order / reservation / supply_need.
VALID-B  VIN exists, trailer_id NULL, linked to order/sn/line but production
         not released and order not shipped/sold.
VALID-C  VIN exists and trailer_id is set (normal state).

P0-1  produced_unit status vin_assigned/done/released but trailer_id NULL.
P0-2  vin_registry confirmed/assigned as physically applied but trailer_id NULL.
P0-3  customer_order sold/shipped/ready_to_ship with VIN but no trailer_id.
P0-4  ACTIVE reservation references a trailer that is SOLD but
      the order itself has no matching delivered/shipped state (stale lock).

P1-A  ACTIVE reservation for a trailer with no matching current order
      (orphan reservation — trailer locked but no order context).
P1-B  order line has vin_registry_id set but order status is sold_not_shipped
      and docs_issued=1 with fulfillment_source='later' and no physical
      trailer yet (docs issued before production — watch only).
"""

import sqlite3
import sys
import os
import argparse
from datetime import datetime

# ---------------------------------------------------------------------------
DEFAULT_DB = os.path.join(os.path.dirname(__file__), '..', 'instance', 'trailers.db')

PRODUCED_UNIT_FINAL_STATUSES = ('vin_assigned', 'done', 'released', 'completed', 'confirmed')

ORDER_SOLD_STATUSES = ('sold_not_shipped', 'shipped', 'done', 'customer_shipped',
                       'ready_to_ship', 'delivered', 'closed')

# ---------------------------------------------------------------------------


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f'[ERROR] DB not found: {db_path}', file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
    return col in cols


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------

def section(title: str):
    print()
    print('=' * 60)
    print(title)
    print('=' * 60)


def row_dict(row) -> dict:
    return dict(row)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_table_overview(conn: sqlite3.Connection):
    section('TABLE ROW COUNTS')
    tables = ['produced_unit', 'vin_registry', 'supply_need',
              'reservation', 'customer_order_line', 'customer_order', 'trailer']
    for t in tables:
        if table_exists(conn, t):
            n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            print(f'  {t}: {n} rows')
        else:
            print(f'  {t}: TABLE MISSING')


def check_produced_unit_status(conn: sqlite3.Connection):
    section('produced_unit STATUS DISTRIBUTION')
    for r in conn.execute('SELECT status, COUNT(*) c FROM produced_unit GROUP BY status ORDER BY c DESC'):
        print(f'  {r[0]}: {r[1]}')


def check_vin_registry_status(conn: sqlite3.Connection):
    section('vin_registry STATUS DISTRIBUTION')
    for r in conn.execute('SELECT status, COUNT(*) c FROM vin_registry GROUP BY status ORDER BY c DESC'):
        print(f'  {r[0]}: {r[1]}')


# ---------------------------------------------------------------------------
# P0 checks
# ---------------------------------------------------------------------------

def check_p0_1(conn: sqlite3.Connection) -> list:
    """P0-1: produced_unit vin_assigned/done but trailer_id NULL."""
    section('P0-1: produced_unit FINAL STATUS but trailer_id NULL')
    placeholders = ','.join('?' * len(PRODUCED_UNIT_FINAL_STATUSES))
    rows = conn.execute(
        f'''SELECT id, status, order_id, target_warehouse_id,
                production_request_line_id, created_at
            FROM produced_unit
            WHERE trailer_id IS NULL
              AND status IN ({placeholders})
            ORDER BY created_at DESC''',
        PRODUCED_UNIT_FINAL_STATUSES
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P0-1 | pu_id={d["id"]} status={d["status"]} '
              f'order_id={d["order_id"]} wh={d["target_warehouse_id"]} '
              f'prl={d["production_request_line_id"]} created={d["created_at"]}')
        results.append(d)
    if not rows:
        print('  OK — no P0-1 cases found')
    return results


def check_p0_2(conn: sqlite3.Connection) -> list:
    """P0-2: vin_registry confirmed/assigned but trailer_id NULL."""
    section('P0-2: vin_registry confirmed/assigned but trailer_id NULL')
    rows = conn.execute(
        '''SELECT id, vin_full, serial7, status,
                  customer_order_id, supply_need_id, order_line_id,
                  assigned_at, confirmed_at, docs_issued_at
           FROM vin_registry
           WHERE trailer_id IS NULL
             AND status NOT IN ("free","void","voided","reserved")
           ORDER BY id DESC'''
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P0-2 | vr_id={d["id"]} vin={d["vin_full"]} status={d["status"]} '
              f'order={d["customer_order_id"]} sn={d["supply_need_id"]} '
              f'line={d["order_line_id"]} confirmed={d["confirmed_at"]}')
        results.append(d)
    if not rows:
        print('  OK — no P0-2 cases found')
    return results


def check_p0_3(conn: sqlite3.Connection) -> list:
    """P0-3: order sold/shipped with VIN but trailer_id NULL."""
    section('P0-3: customer_order sold/shipped but trailer_id NULL')
    placeholders = ','.join('?' * len(ORDER_SOLD_STATUSES))
    rows = conn.execute(
        f'''SELECT o.id, o.order_number, o.status, o.trailer_id,
                   o.warehouse_id, o.documents_issued, o.is_shipped,
                   o.fulfillment_source, o.created_at
            FROM customer_order o
            WHERE o.trailer_id IS NULL
              AND lower(o.status) IN ({placeholders})
            ORDER BY o.id DESC''',
        ORDER_SOLD_STATUSES
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        # Look up VIN links for this order
        lines = conn.execute(
            '''SELECT id, fulfillment_source, fulfillment_status, status,
                      trailer_id, vin_registry_id
               FROM customer_order_line WHERE order_id=?''',
            (d['id'],)
        ).fetchall()
        vin_links = [l['vin_registry_id'] for l in lines if l['vin_registry_id']]
        print(f'  P0-3 | ord_id={d["id"]} num={d["order_number"]} '
              f'status={d["status"]} docs_issued={d["documents_issued"]} '
              f'is_shipped={d["is_shipped"]} fs={d["fulfillment_source"]} '
              f'vin_links={vin_links} created={d["created_at"]}')
        d['_vin_links'] = vin_links
        d['_lines'] = [row_dict(l) for l in lines]
        results.append(d)
    if not rows:
        print('  OK — no P0-3 cases found')
    return results


def check_p0_4(conn: sqlite3.Connection) -> list:
    """P0-4: ACTIVE reservation where the order does NOT have this trailer linked.
    Excludes normal ACTIVE reservations where customer_order.trailer_id = reservation.trailer_id
    (those are valid sold-not-yet-shipped locks).
    """
    section('P0-4: ACTIVE reservation — order has NO trailer_id match (stale/orphan lock)')
    rows = conn.execute(
        '''SELECT r.id, r.trailer_id, r.order_id, r.order_line_id,
                  r.status r_status, r.source_type,
                  t.vin, t.status t_status,
                  o.order_number, o.status o_status,
                  o.trailer_id o_trailer_id,
                  o.documents_issued, o.is_shipped
           FROM reservation r
           LEFT JOIN trailer t ON t.id = r.trailer_id
           LEFT JOIN customer_order o ON o.id = r.order_id
           WHERE r.status = "ACTIVE"
             AND lower(t.status) = "sold"
             AND (o.trailer_id IS NULL OR o.trailer_id != r.trailer_id)
           ORDER BY r.id DESC'''
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P0-4 | res_id={d["id"]} trailer={d["trailer_id"]}({d["vin"]},status={d["t_status"]}) '
              f'order={d["order_id"]}({d["order_number"]}) ord_status={d["o_status"]} '
              f'ord_trailer={d["o_trailer_id"]} docs={d["documents_issued"]}')
        results.append(d)
    if not rows:
        print('  OK — no P0-4 cases found')
    return results


def check_p1_c_placeholder_vin(conn: sqlite3.Connection) -> list:
    """P1-C: trailer has a placeholder/zero VIN but is SOLD or linked to an order."""
    section('P1-C: trailer SOLD or in confirmed order but has placeholder VIN')
    # Known placeholder patterns: all-zeros last 7, or literal 0000000
    rows = conn.execute(
        '''SELECT t.id, t.vin, t.status, t.warehouse_id,
                  o.id o_id, o.order_number, o.status o_status,
                  vr.id vr_id, vr.vin_full, vr.status vr_status, vr.confirmed_at
           FROM trailer t
           LEFT JOIN customer_order o ON o.trailer_id = t.id
           LEFT JOIN vin_registry vr ON vr.trailer_id = t.id
           WHERE (t.vin LIKE "%0000000" OR t.vin IS NULL OR t.vin = "")
             AND lower(t.status) IN ("sold","reserved","in_stock","in_transit")
           ORDER BY t.id DESC'''
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P1-C | trailer={d["id"]} vin={d["vin"]} t_status={d["status"]} wh={d["warehouse_id"]} '
              f'order={d["o_id"]}({d["o_status"]}) '
              f'vr_id={d["vr_id"]} vr_status={d["vr_status"]} confirmed={d["confirmed_at"]}')
        results.append(d)
    if not rows:
        print('  OK — no P1-C cases found')
    return results


# ---------------------------------------------------------------------------
# P1 checks
# ---------------------------------------------------------------------------

def check_p1_a(conn: sqlite3.Connection) -> list:
    """P1-A: ACTIVE reservation where the order no longer exists."""
    section('P1-A: ACTIVE reservation with missing/cancelled order')
    rows = conn.execute(
        '''SELECT r.id, r.trailer_id, r.order_id, r.source_type,
                  t.vin, t.status t_status,
                  o.order_number, o.status o_status
           FROM reservation r
           LEFT JOIN trailer t ON t.id = r.trailer_id
           LEFT JOIN customer_order o ON o.id = r.order_id
           WHERE r.status = "ACTIVE"
             AND (o.id IS NULL OR lower(o.status) IN ("cancelled","void","deleted"))
           ORDER BY r.id DESC'''
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P1-A | res_id={d["id"]} trailer={d["trailer_id"]}({d["vin"]}) '
              f'order={d["order_id"]}({d["order_number"]}) ord_status={d["o_status"]}')
        results.append(d)
    if not rows:
        print('  OK — no P1-A cases found')
    return results


def check_p1_b(conn: sqlite3.Connection) -> list:
    """P1-B: docs issued for a 'later' order where no physical trailer exists yet."""
    section('P1-B: docs issued for later-fulfillment order, no physical trailer')
    rows = conn.execute(
        '''SELECT o.id, o.order_number, o.status, o.trailer_id,
                  o.documents_issued, o.is_shipped, o.fulfillment_source,
                  col.id line_id, col.vin_registry_id,
                  vr.vin_full, vr.status vr_status, vr.docs_issued_at
           FROM customer_order o
           JOIN customer_order_line col ON col.order_id = o.id
           LEFT JOIN vin_registry vr ON vr.id = col.vin_registry_id
           WHERE o.documents_issued = 1
             AND o.trailer_id IS NULL
             AND lower(col.fulfillment_source) = "later"
             AND col.trailer_id IS NULL
           ORDER BY o.id DESC'''
    ).fetchall()
    results = []
    for r in rows:
        d = row_dict(r)
        print(f'  P1-B | ord_id={d["id"]} num={d["order_number"]} '
              f'status={d["status"]} docs={d["documents_issued"]} '
              f'vr_id={d["vin_registry_id"]} vin={d["vin_full"]} '
              f'vr_status={d["vr_status"]} docs_issued_at={d["docs_issued_at"]}')
        results.append(d)
    if not rows:
        print('  OK — no P1-B cases found')
    return results


# ---------------------------------------------------------------------------
# VALID classification
# ---------------------------------------------------------------------------

def report_valid_summary(conn: sqlite3.Connection):
    section('VALID STATE SUMMARY (vin_registry)')

    valid_a = conn.execute(
        '''SELECT COUNT(*) FROM vin_registry
           WHERE trailer_id IS NULL
             AND (status IN ("free","void","voided")
                  OR (status="reserved"
                      AND customer_order_id IS NULL
                      AND supply_need_id IS NULL
                      AND order_line_id IS NULL))'''
    ).fetchone()[0]

    valid_b = conn.execute(
        '''SELECT COUNT(*) FROM vin_registry vr
           LEFT JOIN customer_order o ON o.id = vr.customer_order_id
           WHERE vr.trailer_id IS NULL
             AND vr.status NOT IN ("free","void","voided","confirmed","assigned")
             AND (vr.customer_order_id IS NOT NULL
                  OR vr.supply_need_id IS NOT NULL
                  OR vr.order_line_id IS NOT NULL)
             AND (o.id IS NULL OR o.is_shipped = 0)'''
    ).fetchone()[0]

    valid_c = conn.execute(
        'SELECT COUNT(*) FROM vin_registry WHERE trailer_id IS NOT NULL'
    ).fetchone()[0]

    total = conn.execute('SELECT COUNT(*) FROM vin_registry').fetchone()[0]

    print(f'  VALID-A (inventory / no links):       {valid_a}')
    print(f'  VALID-B (reserved+linked, not final): {valid_b}')
    print(f'  VALID-C (trailer_id set):             {valid_c}')
    print(f'  TOTAL:                                {total}')
    print(f'  Accounted:                            {valid_a + valid_b + valid_c}')

    if valid_b > 0:
        print()
        print('  VALID-B details:')
        rows = conn.execute(
            '''SELECT vr.id, vr.vin_full, vr.status,
                      vr.customer_order_id, vr.supply_need_id, vr.order_line_id,
                      o.status o_status, o.is_shipped
               FROM vin_registry vr
               LEFT JOIN customer_order o ON o.id = vr.customer_order_id
               WHERE vr.trailer_id IS NULL
                 AND vr.status NOT IN ("free","void","voided","confirmed","assigned")
                 AND (vr.customer_order_id IS NOT NULL
                      OR vr.supply_need_id IS NOT NULL
                      OR vr.order_line_id IS NOT NULL)
                 AND (o.id IS NULL OR o.is_shipped = 0)
               ORDER BY vr.id DESC'''
        ).fetchall()
        for r in rows:
            d = row_dict(r)
            print(f'    vr_id={d["id"]} vin={d["vin_full"]} vr_status={d["status"]} '
                  f'order={d["customer_order_id"]} sn={d["supply_need_id"]} '
                  f'line={d["order_line_id"]} ord_status={d["o_status"]} shipped={d["is_shipped"]}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='P0-H audit — read-only')
    parser.add_argument('--db', default=DEFAULT_DB, help='path to trailers.db')
    args = parser.parse_args()

    print(f'P0-H Audit  |  {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC')
    print(f'DB: {os.path.abspath(args.db)}')
    print('MODE: READ-ONLY')

    conn = connect(args.db)

    check_table_overview(conn)
    check_produced_unit_status(conn)
    check_vin_registry_status(conn)

    p0_1 = check_p0_1(conn)
    p0_2 = check_p0_2(conn)
    p0_3 = check_p0_3(conn)
    p0_4 = check_p0_4(conn)

    p1_a = check_p1_a(conn)
    p1_b = check_p1_b(conn)
    p1_c = check_p1_c_placeholder_vin(conn)

    report_valid_summary(conn)

    # ---- verdict ----
    total_p0 = len(p0_1) + len(p0_2) + len(p0_3) + len(p0_4)
    total_p1 = len(p1_a) + len(p1_b) + len(p1_c)

    section('VERDICT')
    print(f'  P0-1 (produced_unit final, no trailer):       {len(p0_1)}')
    print(f'  P0-2 (vin_registry confirmed, no trailer):    {len(p0_2)}')
    print(f'  P0-3 (order sold/shipped, no trailer):        {len(p0_3)}')
    print(f'  P0-4 (stale reservation, no order link):      {len(p0_4)}')
    print(f'  P1-A (orphan ACTIVE reservation):             {len(p1_a)}')
    print(f'  P1-B (docs issued, later, no trailer):        {len(p1_b)}')
    print(f'  P1-C (placeholder VIN on SOLD/active trailer):{len(p1_c)}')
    print()
    if total_p0 == 0:
        print('  RESULT: NO_P0_BLOCKERS')
    else:
        print(f'  RESULT: P0_WATCH ({total_p0})')
    if total_p1 == 0:
        print('  P1:    CLEAN')
    else:
        print(f'  P1:    WARNINGS ({total_p1})')

    conn.close()


if __name__ == '__main__':
    main()
