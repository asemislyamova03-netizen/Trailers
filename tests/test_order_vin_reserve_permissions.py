#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _OrderQuery:
    def __init__(self, order):
        self._order = order

    def get_or_404(self, _id):
        return self._order


class _OrderLineQuery:
    def __init__(self, line):
        self._line = line

    def filter_by(self, **kwargs):
        return SimpleNamespace(first_or_404=lambda: self._line)


class _CurrentUser:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.is_admin = kwargs.get('is_admin', False)
        self.is_director = kwargs.get('is_director', False)
        self.is_manager = kwargs.get('is_manager', False)
        self.is_production = kwargs.get('is_production', False)
        self.is_logistics = kwargs.get('is_logistics', False)
        self.is_warehouse = kwargs.get('is_warehouse', False)
        self.is_viewer = kwargs.get('is_viewer', False)


class OrderVinReservePermissionsTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    @staticmethod
    def _order(order_id=90, assigned_user_id=1, status='in_production'):
        return SimpleNamespace(
            id=order_id,
            assigned_user_id=assigned_user_id,
            created_by_user_id=assigned_user_id,
            is_shipped=False,
            documents_issued=False,
            status=status,
        )

    @staticmethod
    def _line(order_id=90):
        return SimpleNamespace(id=901, order_id=order_id, line_no=1)

    def test_manager_owner_can_reserve_order_vin(self):
        from views import order_reserve_vin

        order = self._order()
        line = self._line(order.id)
        with self.app.test_request_context('/orders/90/reserve-vin', method='POST', data={'year_code': 'T'}):
            with patch('views.current_user', new=_CurrentUser(id=1, is_manager=True)), \
                 patch('views.CustomerOrder', new=SimpleNamespace(query=_OrderQuery(order))), \
                 patch('views.CustomerOrderLine', new=SimpleNamespace(query=_OrderLineQuery(line))), \
                 patch('views._primary_order_line', return_value=line), \
                 patch('views._reserve_vin_for_order_line', return_value=SimpleNamespace(vin_full='MX4000002T0000001')), \
                 patch('views._refresh_order_status', return_value=None), \
                 patch('views.add_order_event', return_value=None), \
                 patch('views.db.session.commit', return_value=None):
                response = order_reserve_vin.__wrapped__(order.id)
                self.assertEqual(response.status_code, 302)
                self.assertIn(f'/orders/{order.id}', response.location)

    def test_manager_non_owner_blocked(self):
        from views import order_reserve_vin
        from werkzeug.exceptions import Forbidden

        order = self._order(assigned_user_id=7)
        with self.app.test_request_context('/orders/90/reserve-vin', method='POST', data={'year_code': 'T'}):
            with patch('views.current_user', new=_CurrentUser(id=1, is_manager=True)), \
                 patch('views.CustomerOrder', new=SimpleNamespace(query=_OrderQuery(order))):
                with self.assertRaises(Forbidden):
                    order_reserve_vin.__wrapped__(order.id)

    def test_admin_and_director_allowed_for_line_reserve(self):
        from views import order_line_reserve_vin

        for user in (_CurrentUser(id=3, is_admin=True), _CurrentUser(id=4, is_director=True)):
            order = self._order(assigned_user_id=99)
            line = self._line(order.id)
            with self.app.test_request_context('/orders/90/lines/901/reserve-vin', method='POST', data={'year_code': 'T'}):
                with patch('views.current_user', new=user), \
                     patch('views.CustomerOrder', new=SimpleNamespace(query=_OrderQuery(order))), \
                     patch('views.CustomerOrderLine', new=SimpleNamespace(query=_OrderLineQuery(line))), \
                     patch('views._reserve_vin_for_order_line', return_value=SimpleNamespace(vin_full='MX4000002T0000002')), \
                     patch('views._refresh_order_status', return_value=None), \
                     patch('views.add_order_event', return_value=None), \
                     patch('views.db.session.commit', return_value=None):
                    response = order_line_reserve_vin.__wrapped__(order.id, line.id)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(f'/orders/{order.id}', response.location)

    def test_production_and_logistics_blocked(self):
        from views import order_line_reserve_vin
        from werkzeug.exceptions import Forbidden

        order = self._order(assigned_user_id=1)
        line = self._line(order.id)
        blocked_users = (
            _CurrentUser(id=1, is_production=True),
            _CurrentUser(id=1, is_logistics=True),
        )
        for user in blocked_users:
            with self.app.test_request_context('/orders/90/lines/901/reserve-vin', method='POST', data={'year_code': 'T'}):
                with patch('views.current_user', new=user), \
                     patch('views.CustomerOrder', new=SimpleNamespace(query=_OrderQuery(order))), \
                     patch('views.CustomerOrderLine', new=SimpleNamespace(query=_OrderLineQuery(line))):
                    with self.assertRaises(Forbidden):
                        order_line_reserve_vin.__wrapped__(order.id, line.id)

    def test_existing_capacity_or_modification_blockers_preserved(self):
        from views import order_line_reserve_vin

        order = self._order()
        line = self._line(order.id)
        with self.app.test_request_context('/orders/90/lines/901/reserve-vin', method='POST', data={'year_code': 'T'}):
            with patch('views.current_user', new=_CurrentUser(id=1, is_manager=True)), \
                 patch('views.CustomerOrder', new=SimpleNamespace(query=_OrderQuery(order))), \
                 patch('views.CustomerOrderLine', new=SimpleNamespace(query=_OrderLineQuery(line))), \
                 patch('views._reserve_vin_for_order_line', side_effect=ValueError('По выбранной позиции уже зарезервированы все VIN по количеству.')):
                response = order_line_reserve_vin.__wrapped__(order.id, line.id)
                self.assertEqual(response.status_code, 302)
                self.assertIn(f'/orders/{order.id}', response.location)


if __name__ == '__main__':
    unittest.main()
