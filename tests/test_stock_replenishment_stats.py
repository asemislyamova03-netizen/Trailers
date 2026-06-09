#!/usr/bin/env python3
"""Unit tests for stock replenishment stats helpers in views.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from views import (  # noqa: E402
    _is_stock_replenishment_unit_reassigned,
    _stock_replenishment_metrics,
)


class _FakeTrailer:
    def __init__(
        self,
        *,
        id: int = 1,
        warehouse_id: int = 3,
        status: str = 'IN_STOCK',
        lifecycle_status: str | None = None,
        vin: str = 'MX4000002T0002706',
    ):
        self.id = id
        self.warehouse_id = warehouse_id
        self.status = status
        self.lifecycle_status = lifecycle_status
        self.vin = vin


class _FakeUnit:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.status = kwargs.get('status', 'vin_assigned')
        self.order_id = kwargs.get('order_id')
        self.order_line_id = kwargs.get('order_line_id')
        self.trailer = kwargs.get('trailer')
        self.target_warehouse_id = kwargs.get('target_warehouse_id')


class _FakeNeed:
    def __init__(self, *, id: int = 12, warehouse_id: int = 2, quantity: int = 1):
        self.id = id
        self.warehouse_id = warehouse_id
        self.quantity = quantity


class StockReplenishmentReassignedTests(unittest.TestCase):
    def test_reassigned_by_order_link(self):
        unit = _FakeUnit(
            order_id=78,
            order_line_id=80,
            trailer=_FakeTrailer(status='SOLD', lifecycle_status='customer_shipped'),
        )
        self.assertTrue(_is_stock_replenishment_unit_reassigned(unit))

    @patch('views._active_movement_for_trailer', return_value=None)
    @patch('views._trailer_has_sale_or_document_links', return_value=False)
    def test_sold_trailer_linked_to_order_not_actionable(self, _sale_links, _movement):
        need = _FakeNeed()
        trailer = _FakeTrailer(id=1050, warehouse_id=3, status='SOLD', lifecycle_status='customer_shipped')
        unit = _FakeUnit(
            id=93,
            status='vin_assigned',
            order_id=78,
            order_line_id=80,
            trailer=trailer,
            target_warehouse_id=2,
        )
        stats = _stock_replenishment_metrics(need, lines=[], units=[unit])

        self.assertEqual(stats['reassigned_qty'], 1)
        self.assertEqual(stats['reassigned_units'], [unit])
        self.assertEqual(stats['needs_movement_qty'], 0)
        self.assertEqual(stats['needs_vin_qty'], 0)
        self.assertEqual(stats['problem_qty'], 0)
        self.assertEqual(stats['produced_qty'], 0)
        self.assertEqual(stats['accepted_qty'], 0)
        self.assertEqual(stats['remaining_qty'], 1)

    @patch('views._active_movement_for_trailer', return_value=None)
    @patch('views._trailer_has_sale_or_document_links', return_value=False)
    def test_stock_unit_still_needs_movement(self, _sale_links, _movement):
        need = _FakeNeed()
        trailer = _FakeTrailer(id=200, warehouse_id=3, status='IN_STOCK')
        unit = _FakeUnit(
            id=10,
            status='vin_assigned',
            trailer=trailer,
            target_warehouse_id=2,
        )
        stats = _stock_replenishment_metrics(need, lines=[], units=[unit])

        self.assertEqual(stats['reassigned_qty'], 0)
        self.assertEqual(stats['needs_movement_qty'], 1)
        self.assertEqual(stats['produced_qty'], 1)


if __name__ == '__main__':
    unittest.main()
