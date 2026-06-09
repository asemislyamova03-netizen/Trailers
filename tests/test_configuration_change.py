#!/usr/bin/env python3
"""Unit tests for configuration change helpers in views.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from views import (  # noqa: E402
    _ConfigurationChain,
    _configuration_change_blockers,
    _is_active_production_request_line,
    _is_active_supply_need,
    _normalize_vin_modification_code,
    _should_sync_order_header_for_configuration,
    _vin_modification_compatible,
)


class _FakeItem:
    def __init__(self, *, id: int = 1, article: str = 'ART', axle_count: int = 1, group_code: str = '002', body_size_code: str = '2513'):
        self.id = id
        self.article = article
        self.axle_count = axle_count
        self.group_code = group_code
        self.body_size_code = body_size_code


class _FakeOrder:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.documents_issued = kwargs.get('documents_issued', False)
        self.is_shipped = kwargs.get('is_shipped', False)
        self.status = kwargs.get('status', 'in_production')
        self.trailer_id = kwargs.get('trailer_id')
        self.sales_realizations = kwargs.get('sales_realizations', [])
        self.lines = kwargs.get('lines', [])


class _FakeLine:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 80)
        self.line_type = kwargs.get('line_type', 'TRAILER')
        self.trailer_id = kwargs.get('trailer_id')
        self.realization_lines = kwargs.get('realization_lines', [])
        self.order = kwargs.get('order')


class _FakeTrailer:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1050)
        self.vin = kwargs.get('vin')
        self.status = kwargs.get('status', 'IN_STOCK')
        self.lifecycle_status = kwargs.get('lifecycle_status')
        self.item = kwargs.get('item')


class _FakePu:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 93)
        self.status = kwargs.get('status', 'produced_no_vin')
        self.order_id = kwargs.get('order_id')
        self.order_line_id = kwargs.get('order_line_id')
        self.trailer = kwargs.get('trailer')
        self.item = kwargs.get('item')


class _FakeNeed:
    def __init__(self, status: str = 'IN_PRODUCTION'):
        self.status = status


class _FakePrl:
    def __init__(self, status: str = 'in_production'):
        self.status = status


class _FakeRealization:
    def __init__(self, status: str = 'posted'):
        self.status = status


class VinModificationTests(unittest.TestCase):
    def test_compatible_same_mod(self):
        self.assertTrue(_vin_modification_compatible('MX4000002T0002706', {'vin_modification_code': '002'}))

    def test_incompatible_mod(self):
        self.assertFalse(_vin_modification_compatible('MX4000002T0002706', {'vin_modification_code': '999'}))

    def test_no_vin_always_compatible(self):
        self.assertTrue(_vin_modification_compatible(None, {'vin_modification_code': '999'}))


class ActiveEntityTests(unittest.TestCase):
    def test_closed_need_inactive(self):
        self.assertFalse(_is_active_supply_need(_FakeNeed('CLOSED')))

    def test_ready_need_active(self):
        self.assertTrue(_is_active_supply_need(_FakeNeed('READY')))

    def test_closed_prl_inactive(self):
        self.assertFalse(_is_active_production_request_line(_FakePrl('closed')))


class OrderHeaderSyncTests(unittest.TestCase):
    def test_single_trailer_line(self):
        line = _FakeLine(id=1)
        order = _FakeOrder(lines=[line])
        self.assertTrue(_should_sync_order_header_for_configuration(order, line))

    def test_multi_line_no_sync(self):
        line = _FakeLine(id=2)
        order = _FakeOrder(lines=[_FakeLine(id=1), line])
        self.assertFalse(_should_sync_order_header_for_configuration(order, line))


class ConfigurationBlockerTests(unittest.TestCase):
    def setUp(self):
        patcher_movement = patch('views._active_movement_for_trailer', return_value=None)
        patcher_posted = patch('views._posted_realization_for_trailer', return_value=None)
        patcher_reassigned = patch('views._produced_unit_reassigned_from_stock_replenishment', return_value=False)
        patcher_orders = patch('views.CustomerOrder')
        patcher_vin = patch('views.VinRegistry')
        self.addCleanup(patcher_movement.stop)
        self.addCleanup(patcher_posted.stop)
        self.addCleanup(patcher_reassigned.stop)
        self.addCleanup(patcher_orders.stop)
        self.addCleanup(patcher_vin.stop)
        patcher_movement.start()
        patcher_posted.start()
        patcher_reassigned.start()
        mock_order = patcher_orders.start()
        mock_order.query.filter_by.return_value.all.return_value = []
        mock_vin = patcher_vin.start()
        mock_vin.query.filter.return_value.first.return_value = None

    def test_documents_issued_blocks(self):
        order = _FakeOrder(documents_issued=True)
        chain = _ConfigurationChain(order=order)
        blockers = _configuration_change_blockers(chain)
        self.assertTrue(any('выданы документы' in b for b in blockers))

    def test_posted_realization_blocks(self):
        order = _FakeOrder(sales_realizations=[_FakeRealization('posted')])
        chain = _ConfigurationChain(order=order)
        blockers = _configuration_change_blockers(chain)
        self.assertTrue(any('проведена реализация' in b for b in blockers))

    def test_customer_shipped_blocks(self):
        trailer = _FakeTrailer(lifecycle_status='customer_shipped')
        chain = _ConfigurationChain(trailer=trailer)
        with patch('views._trailer_is_customer_shipped', return_value=True):
            blockers = _configuration_change_blockers(chain)
        self.assertTrue(any('отгружен' in b for b in blockers))

    def test_reassigned_pu_blocks(self):
        pu = _FakePu(order_id=78)
        chain = _ConfigurationChain(produced_units=[pu])
        with patch('views._produced_unit_reassigned_from_stock_replenishment', return_value=True):
            blockers = _configuration_change_blockers(chain)
        self.assertTrue(any('переназначен' in b for b in blockers))

    def test_vin_assigned_allowed_without_other_blockers(self):
        pu = _FakePu(status='vin_assigned', trailer=_FakeTrailer(vin=None))
        chain = _ConfigurationChain(produced_units=[pu])
        blockers = _configuration_change_blockers(chain)
        self.assertEqual(blockers, [])

    def test_vin_mod_mismatch_blocks(self):
        trailer = _FakeTrailer(vin='MX4000002T0002706')
        chain = _ConfigurationChain(trailer=trailer)
        blockers = _configuration_change_blockers(
            chain,
            snapshot={'vin_modification_code': '999'},
        )
        self.assertTrue(any('несовместимая модификация VIN' in b for b in blockers))

    @patch('views._item_base_matches_locked_item', return_value=False)
    def test_platform_mismatch_blocks(self, _match):
        old = _FakeItem()
        new = _FakeItem(id=2)
        chain = _ConfigurationChain()
        blockers = _configuration_change_blockers(chain, locked_item=old, new_item=new)
        self.assertTrue(any('осей' in b for b in blockers))


class NormalizeVinModTests(unittest.TestCase):
    def test_normalize_last_three_digits(self):
        self.assertEqual(_normalize_vin_modification_code('000002'), '002')


if __name__ == '__main__':
    unittest.main()
