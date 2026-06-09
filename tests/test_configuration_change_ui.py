#!/usr/bin/env python3
"""Unit tests for configuration change UI helpers in views.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from views import (  # noqa: E402
    _configuration_change_ui_for_order_line,
    _configuration_change_ui_for_produced_unit,
    _configuration_change_ui_for_trailer,
    _user_can_use_configuration_change_ui,
)


class _FakeTrailer:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1039)
        self.vin = kwargs.get('vin', 'MX4000004T0002678')
        self.status = kwargs.get('status', 'IN_STOCK')
        self.lifecycle_status = kwargs.get('lifecycle_status')
        self.item = kwargs.get('item')


class _FakePu:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 112)
        self.status = kwargs.get('status', 'produced_no_vin')
        self.order_id = kwargs.get('order_id')
        self.order_line_id = kwargs.get('order_line_id')
        self.trailer = kwargs.get('trailer')
        self.item = kwargs.get('item')


class _FakeLine:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 80)
        self.line_type = kwargs.get('line_type', 'TRAILER')
        self.trailer = kwargs.get('trailer')
        self.trailer_id = kwargs.get('trailer_id')


class _RoleUser:
    def __init__(self, role: str):
        self.is_authenticated = True
        self.role = role

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    @property
    def is_manager(self) -> bool:
        return self.role == 'manager'

    @property
    def is_director(self) -> bool:
        return self.role == 'director'

    @property
    def is_production(self) -> bool:
        return self.role == 'production'


class ConfigurationChangeUITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.request_context = self.app.test_request_context()
        self.request_context.push()
        self.director = _RoleUser('director')
        self.production = _RoleUser('production')

    def tearDown(self):
        self.request_context.pop()
        self.app_context.pop()

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    @patch('views._can_change_produced_unit_item', return_value=(True, ''))
    def test_produced_unit_allowed_link(self, _mock_allow, _mock_user):
        unit = _FakePu(id=112)
        state = _configuration_change_ui_for_produced_unit(unit)
        self.assertTrue(state['allowed'])
        self.assertTrue(state['show_link'])
        self.assertFalse(state['show_disabled'])
        self.assertIn('/logistics/produced-units/112/change-item', state['url'])
        self.assertEqual(state['entry_point'], 'produced_unit')

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    @patch(
        'views._can_change_produced_unit_item',
        return_value=(False, 'Комплектацию нельзя менять: прицеп уже отгружен клиенту.'),
    )
    def test_produced_unit_blocked_disabled(self, _mock_allow, _mock_user):
        unit = _FakePu(id=93)
        state = _configuration_change_ui_for_produced_unit(unit)
        self.assertFalse(state['allowed'])
        self.assertIsNone(state['url'])
        self.assertTrue(state['show_disabled'])
        self.assertFalse(state['show_link'])
        self.assertIn('отгружен', state['block_message'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('manager'))
    @patch('views._can_change_trailer_item', return_value=(True, ''))
    def test_trailer_allowed_link(self, _mock_allow, _mock_user):
        trailer = _FakeTrailer(id=1039)
        state = _configuration_change_ui_for_trailer(trailer)
        self.assertTrue(state['show_link'])
        self.assertIn('/trailers/1039/change-item', state['url'])
        self.assertEqual(state['entry_point'], 'trailer')

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    @patch(
        'views._can_change_trailer_item',
        return_value=(False, 'Комплектацию нельзя менять: прицеп уже отгружен клиенту.'),
    )
    def test_trailer_customer_shipped_blocked(self, _mock_allow, _mock_user):
        trailer = _FakeTrailer(id=1050, lifecycle_status='customer_shipped', status='SOLD')
        state = _configuration_change_ui_for_trailer(trailer)
        self.assertTrue(state['show_disabled'])
        self.assertFalse(state['show_link'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    @patch('views._can_change_produced_unit_item', return_value=(True, ''))
    @patch('views.ProducedUnit')
    def test_order_line_prefers_allowed_produced_unit(self, mock_pu_model, _mock_allow, _mock_user):
        unit = _FakePu(id=73, order_line_id=80)
        line = _FakeLine(id=80)
        mock_pu_model.query.filter_by.return_value.order_by.return_value.first.return_value = unit
        state = _configuration_change_ui_for_order_line(line)
        self.assertTrue(state['show_link'])
        self.assertEqual(state['entry_point'], 'produced_unit')
        self.assertIn('/logistics/produced-units/73/change-item', state['url'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('manager'))
    @patch('views._can_change_trailer_item', return_value=(True, ''))
    @patch('views._can_change_produced_unit_item', return_value=(False, 'blocked pu'))
    @patch('views.ProducedUnit')
    def test_order_line_falls_back_to_trailer(self, mock_pu_model, _mock_pu_allow, _mock_tr_allow, _mock_user):
        trailer = _FakeTrailer(id=1039)
        unit = _FakePu(id=73, trailer=trailer)
        line = _FakeLine(id=80, trailer=trailer)
        mock_pu_model.query.filter_by.return_value.order_by.return_value.first.return_value = unit
        state = _configuration_change_ui_for_order_line(line)
        self.assertTrue(state['show_link'])
        self.assertEqual(state['entry_point'], 'trailer')
        self.assertIn('/trailers/1039/change-item', state['url'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    @patch(
        'views._can_change_produced_unit_item',
        return_value=(False, 'Комплектацию нельзя менять: выпуск #98 переназначен в заказ.'),
    )
    def test_reassigned_produced_unit_disabled(self, _mock_allow, _mock_user):
        unit = _FakePu(id=98, order_id=83)
        state = _configuration_change_ui_for_produced_unit(unit)
        self.assertTrue(state['show_disabled'])
        self.assertFalse(state['show_link'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('production'))
    def test_production_role_hides_action(self, _mock_user):
        self.assertFalse(_user_can_use_configuration_change_ui())
        with patch('views._can_change_produced_unit_item', return_value=(True, '')):
            state = _configuration_change_ui_for_produced_unit(_FakePu(id=112))
        self.assertFalse(state['show_action'])
        self.assertFalse(state['show_link'])
        self.assertFalse(state['show_disabled'])

    @patch('views.current_user', new_callable=lambda: _RoleUser('director'))
    def test_non_trailer_line_has_no_action(self, _mock_user):
        line = _FakeLine(line_type='COMPONENT')
        state = _configuration_change_ui_for_order_line(line)
        self.assertFalse(state['show_action'])
        self.assertFalse(state['show_link'])


if __name__ == '__main__':
    unittest.main()
