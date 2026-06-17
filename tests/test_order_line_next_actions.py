#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Line(SimpleNamespace):
    pass


def _row(**kwargs):
    line = kwargs.pop('line', _Line(id=85, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production', status='new'))
    defaults = {
        'line': line,
        'vin_rows': [],
        'vin_count': 0,
        'shortage_qty': 0,
        'production_qty': 0,
        'supply_need': None,
        'trailer': None,
        'can_reserve_vin': False,
        'vin_link_candidates': [],
        'can_create_vin_from_trailer': False,
        'vin_link_message': '',
        'vin_assign_unit': None,
        'attachable_produced_units': [],
        'attachable_ready_trailers': [],
        'can_change_source': False,
        'source_change_message': '',
        'can_delete': False,
        'delete_message': '',
        'config_change': {'show_link': False, 'show_disabled': False},
    }
    defaults.update(kwargs)
    return defaults


class OrderLineNextActionsTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.req = self.app.test_request_context('/orders/83')
        self.req.push()

    def tearDown(self):
        self.req.pop()
        self.ctx.pop()

    def _order(self, **kwargs):
        data = {
            'id': kwargs.get('id', 83),
            'status': kwargs.get('status', 'waiting_production'),
            'is_shipped': kwargs.get('is_shipped', False),
            'documents_issued': kwargs.get('documents_issued', False),
            'warehouse': kwargs.get('warehouse', SimpleNamespace(id=1)),
            'warehouse_id': 1,
        }
        return SimpleNamespace(**data)

    def test_missing_catalog_shows_fill_catalog(self):
        from views import _build_order_line_next_action, _build_order_line_stage

        line = _Line(id=10, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        row = _row(line=line)
        order = self._order()
        with patch('views._trailer_line_catalog_blockers', return_value=['по позиции #1 не заполнена номенклатура']):
            stage = _build_order_line_stage(order, row)
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(stage['code'], 'no_catalog')
        self.assertEqual(action['code'], 'fill_catalog')
        self.assertTrue(action['enabled'])

    def test_unset_source_shows_pick_source(self):
        from views import _build_order_line_next_action

        line = _Line(id=11, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='later')
        row = _row(line=line)
        order = self._order()
        with patch('views._trailer_line_catalog_blockers', return_value=[]):
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(action['code'], 'pick_source')
        self.assertEqual(action['anchor'], 'line-11-actions')

    def test_production_shortage_shows_create_need(self):
        from views import _build_order_line_next_action

        line = _Line(id=12, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        row = _row(line=line, shortage_qty=1)
        order = self._order()
        with patch('views._trailer_line_catalog_blockers', return_value=[]):
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(action['code'], 'create_need')
        self.assertEqual(action['anchor'], 'line-12-create-need')

    def test_in_production_shows_wait_production(self):
        from views import _build_order_line_next_action, _build_order_line_stage

        need = SimpleNamespace(id=7, status='IN_PRODUCTION')
        line = _Line(id=13, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        row = _row(line=line, supply_need=need, production_qty=1, shortage_qty=0)
        order = self._order()
        with patch('views._trailer_line_catalog_blockers', return_value=[]), \
             patch('views._line_production_pipeline_blockers', return_value=['ожидается выпуск']):
            stage = _build_order_line_stage(order, row)
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(stage['code'], 'in_production')
        self.assertEqual(action['code'], 'wait_production')
        self.assertTrue(action['informational'])

    def test_produced_no_vin_shows_assign_vin(self):
        from views import _build_order_line_next_action

        unit = SimpleNamespace(id=501)
        line = _Line(id=14, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        row = _row(line=line, vin_assign_unit=unit)
        order = self._order(status='produced_waiting_vin')
        with patch('views._trailer_line_catalog_blockers', return_value=[]):
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(action['code'], 'assign_vin')
        self.assertIn('/assign-vin', action['url'])

    def test_shipped_line_shows_completed_disabled(self):
        from views import _build_order_line_next_action

        line = _Line(id=15, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='stock', status='shipped')
        row = _row(line=line, vin_count=1, vin_rows=[SimpleNamespace(status='confirmed', vin_full='MX4')])
        order = self._order(status='shipped', is_shipped=True)
        action = _build_order_line_next_action(
            order, row, can_manage=True, has_contract=True,
            contract_create_blockers=[], document_blockers=[], realization_blockers=[],
        )
        self.assertEqual(action['code'], 'completed')
        self.assertFalse(action['enabled'])

    def test_non_manager_gets_view_only(self):
        from views import _build_order_line_next_action

        line = _Line(id=16, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        row = _row(line=line, shortage_qty=1)
        order = self._order()
        action = _build_order_line_next_action(
            order, row, can_manage=False, has_contract=False,
            contract_create_blockers=[], document_blockers=[], realization_blockers=[],
        )
        self.assertEqual(action['code'], 'view_only')
        self.assertFalse(action['enabled'])

    def test_vin_ready_shows_contract_step(self):
        from views import _build_order_line_next_action

        line = _Line(id=17, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        vin = SimpleNamespace(status='reserved', vin_full='MX4000002T0001234', serial7='0001234')
        row = _row(line=line, vin_count=1, vin_rows=[vin])
        order = self._order(status='ready_to_ship')
        with patch('views._trailer_line_catalog_blockers', return_value=[]), \
             patch('views._order_line_needs_transfer', return_value=False):
            action = _build_order_line_next_action(
                order, row, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertEqual(action['code'], 'ready_contract')
        self.assertTrue(action['enabled'])

    def test_order_detail_renders_line_next_action_marker(self):
        from models import CustomerOrder

        order = CustomerOrder.query.filter(CustomerOrder.lines.any()).order_by(CustomerOrder.id.desc()).first()
        self.assertIsNotNone(order)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.clear()
            sess['_user_id'] = str(order.assigned_user_id or 2)
            sess['_fresh'] = True
        html = client.get(f'/orders/{order.id}').get_data(as_text=True)
        self.assertIn('line-next-action', html)
        self.assertIn('Следующий шаг', html)
        self.assertNotRegex(html, re.compile(r'подтвердить\s+VIN', re.I))
        self.assertNotRegex(html, re.compile(r'обратитесь\s+к\s+логист', re.I))

    def test_enrich_attaches_stage_and_secondary(self):
        from views import _enrich_order_line_rows_actions

        line = _Line(id=20, line_no=1, line_type='TRAILER', quantity=1, fulfillment_source='production')
        rows = [_row(line=line, can_delete=True, can_change_source=True)]
        order = self._order()
        with patch('views._trailer_line_catalog_blockers', return_value=[]):
            _enrich_order_line_rows_actions(
                order, rows, can_manage=True, has_contract=False,
                contract_create_blockers=[], document_blockers=[], realization_blockers=[],
            )
        self.assertIn('stage', rows[0])
        self.assertIn('next_action', rows[0])
        self.assertIn('secondary_actions', rows[0])


if __name__ == '__main__':
    unittest.main()
