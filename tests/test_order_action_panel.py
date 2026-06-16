#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _QueryResult:
    def __init__(self, item):
        self._item = item

    def first(self):
        return self._item

    def order_by(self, *args, **kwargs):
        return self


class _QueryStub:
    def __init__(self, item):
        self._item = item

    def filter_by(self, **kwargs):
        return _QueryResult(self._item)

    def filter(self, *args, **kwargs):
        return _QueryResult(self._item)


class OrderActionPanelTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.req = self.app.test_request_context('/orders/92')
        self.req.push()

    def tearDown(self):
        self.req.pop()
        self.ctx.pop()

    def _order(self, **kwargs):
        data = {
            'id': kwargs.get('id', 92),
            'status': kwargs.get('status', 'in_production'),
            'is_shipped': kwargs.get('is_shipped', False),
            'documents_issued': kwargs.get('documents_issued', False),
            'price': kwargs.get('price', 100000),
            'remaining_amount': kwargs.get('remaining_amount', 10000),
            'trailer': kwargs.get('trailer', None),
            'warehouse': kwargs.get('warehouse', None),
        }
        return SimpleNamespace(**data)

    @patch('views.SalesRealization', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views.SalesContract', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views._order_customer_change_blockers', return_value=[])
    @patch('views._realization_create_blockers', return_value=[])
    @patch('views._order_lines_document_blockers', return_value=['по позиции #1 не хватает VIN: 0/1'])
    def test_waiting_production_shows_path(self, *_mocks):
        from views import _build_order_action_panel

        order = self._order(status='in_production')
        rows = [
            {'shortage_qty': 1, 'supply_need': None, 'config_change': {'show_link': False, 'show_disabled': False}, 'vin_assign_unit': None}
        ]
        panel = _build_order_action_panel(order, rows, can_manage=True)
        actions = {a['code']: a for a in panel['actions']}
        self.assertIn('production_need', actions)
        self.assertTrue(actions['production_need']['enabled'])
        self.assertFalse(actions['issue_documents']['enabled'])
        self.assertIn('не хватает VIN', actions['issue_documents']['reason'])

    @patch('views.SalesRealization', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views.SalesContract', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views._order_customer_change_blockers', return_value=[])
    @patch('views._realization_create_blockers', return_value=[])
    @patch('views._order_lines_document_blockers', return_value=[])
    def test_produced_no_vin_shows_assign_vin_action(self, *_mocks):
        from views import _build_order_action_panel

        order = self._order(status='produced_waiting_vin')
        rows = [
            {'shortage_qty': 0, 'supply_need': None, 'config_change': {'show_link': False, 'show_disabled': False}, 'vin_assign_unit': SimpleNamespace(id=777)}
        ]
        panel = _build_order_action_panel(order, rows, can_manage=True)
        assign = next(a for a in panel['actions'] if a['code'] == 'assign_vin')
        self.assertTrue(assign['enabled'])
        self.assertIn('/logistics/produced-units/777/assign-vin', assign['url'])

    @patch('views.SalesRealization', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views.SalesContract', new=SimpleNamespace(query=_QueryStub(SimpleNamespace(id=1))))
    @patch('views._order_customer_change_blockers', return_value=[])
    @patch('views._realization_create_blockers', return_value=[])
    @patch('views._order_lines_document_blockers', return_value=[])
    def test_docs_and_realization_available_when_no_blockers(self, *_mocks):
        from views import _build_order_action_panel

        order = self._order(status='ready_to_ship', remaining_amount=0)
        rows = [
            {'shortage_qty': 0, 'supply_need': None, 'config_change': {'show_link': True, 'show_disabled': False}, 'vin_assign_unit': None}
        ]
        panel = _build_order_action_panel(order, rows, can_manage=True)
        actions = {a['code']: a for a in panel['actions']}
        self.assertTrue(actions['issue_documents']['enabled'])
        self.assertTrue(actions['create_realization']['enabled'])

    @patch('views.SalesRealization', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views.SalesContract', new=SimpleNamespace(query=_QueryStub(SimpleNamespace(id=1))))
    @patch('views._order_customer_change_blockers', return_value=['по заказу уже создан договор'])
    @patch('views._realization_create_blockers', return_value=['заказ уже отгружен'])
    @patch('views._order_lines_document_blockers', return_value=[])
    def test_shipped_order_only_disabled_safe_state(self, *_mocks):
        from views import _build_order_action_panel

        order = self._order(status='shipped', is_shipped=True, documents_issued=True, remaining_amount=0)
        panel = _build_order_action_panel(order, [], can_manage=True)
        self.assertEqual(panel['summary'], 'Заказ закрыт.')
        for action in panel['actions']:
            if action['code'] != 'final_status':
                self.assertFalse(action['enabled'])

    @patch('views.SalesRealization', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views.SalesContract', new=SimpleNamespace(query=_QueryStub(None)))
    @patch('views._order_customer_change_blockers', return_value=[])
    @patch('views._realization_create_blockers', return_value=[])
    @patch('views._order_lines_document_blockers', return_value=[])
    def test_no_vin_confirm_step_in_panel(self, *_mocks):
        from views import _build_order_action_panel

        panel = _build_order_action_panel(self._order(), [], can_manage=True)
        text = ' '.join(a['label'].lower() for a in panel['actions'])
        self.assertNotIn('подтвердить vin', text)
        self.assertNotIn('подтверждение vin', text)

    def test_no_manage_rights_returns_no_actions(self):
        from views import _build_order_action_panel

        panel = _build_order_action_panel(self._order(), [], can_manage=False)
        self.assertEqual(panel['actions'], [])
        self.assertIn('нет прав', panel['summary'])


if __name__ == '__main__':
    unittest.main()
