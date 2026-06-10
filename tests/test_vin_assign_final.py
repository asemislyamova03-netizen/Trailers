#!/usr/bin/env python3
"""Unit tests for Phase A: VIN assign is final (no separate confirm step)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from views import (  # noqa: E402
    ORDER_LIST_FILTERS,
    _order_list_state,
    _vin_registry_status_is_final,
)


class VinRegistryFinalStatusTests(unittest.TestCase):
    def test_assigned_is_final(self):
        self.assertTrue(_vin_registry_status_is_final('assigned'))

    def test_confirmed_is_final(self):
        self.assertTrue(_vin_registry_status_is_final('confirmed'))

    def test_reserved_is_not_final(self):
        self.assertFalse(_vin_registry_status_is_final('reserved'))

    def test_free_is_not_final(self):
        self.assertFalse(_vin_registry_status_is_final('free'))


class OrderListStateVinConfirmRemovalTests(unittest.TestCase):
    def _make_order(self):
        order = MagicMock()
        order.status = 'confirmed'
        order.is_shipped = False
        order.fulfillment_source = 'production'
        order.remaining_amount = 0
        order.document_status = 'pending'
        order.realization_status = 'not_started'
        order.lines.count.return_value = 1
        order.source_warehouse = None
        return order

    @patch('views.SalesContract')
    @patch('views._order_primary_line_state')
    def test_assigned_vin_does_not_wait_for_confirm(self, mock_line_state, mock_contract):
        mock_line_state.return_value = {
            'vin_rows': [],
            'trailers': [MagicMock(warehouse=None)],
            'missing_vin': False,
            'active_needs': [],
            'active_movements': [],
        }
        mock_contract.query.filter_by.return_value.first.return_value = MagicMock()
        state = _order_list_state(self._make_order())
        self.assertNotEqual(state['code'], 'waiting_vin_confirm')
        self.assertNotIn('подтвердить', (state['next_action'] or '').lower())
        self.assertNotIn('не подтвержд', (state['blocker'] or '').lower())

    @patch('views.SalesContract')
    @patch('views._order_primary_line_state')
    def test_missing_vin_still_shows_waiting_vin(self, mock_line_state, mock_contract):
        mock_line_state.return_value = {
            'vin_rows': [],
            'trailers': [],
            'missing_vin': True,
            'active_needs': [],
            'active_movements': [],
        }
        mock_contract.query.filter_by.return_value.first.return_value = None
        with patch('views._order_has_produced_units_waiting_vin', return_value=True):
            state = _order_list_state(self._make_order())
        self.assertEqual(state['code'], 'waiting_vin')
        self.assertIn('VIN', state['label'])


class OrderListFiltersTests(unittest.TestCase):
    def test_waiting_vin_confirm_filter_removed(self):
        codes = [code for code, _label in ORDER_LIST_FILTERS]
        self.assertNotIn('waiting_vin_confirm', codes)


if __name__ == '__main__':
    unittest.main()
