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


class _User(SimpleNamespace):
    def __init__(self, **kwargs):
        defaults = {
            'is_authenticated': True,
            'is_admin': False,
            'is_director': False,
            'is_logistics': False,
            'is_manager': False,
            'is_production': False,
            'warehouse_id': None,
            'warehouse': None,
        }
        defaults.update(kwargs)
        super().__init__(**defaults)


class _Order(SimpleNamespace):
    pass


class _VinRow(SimpleNamespace):
    pass


class VinRegistryOpenLinkPermissionsTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    @staticmethod
    def _warehouse(is_production=True, is_active=True):
        return SimpleNamespace(id=3, is_active=is_active, is_production=is_production)

    def test_prod_manager_can_view_free_vin_row(self):
        from views import _can_view_vin_registry_row

        user = _User(id=2, is_manager=True, warehouse_id=3, warehouse=self._warehouse())
        row = _VinRow(id=1065, customer_order=None, order_line=None)
        self.assertTrue(_can_view_vin_registry_row(row, user))

    def test_normal_manager_cannot_view_unrelated_vin(self):
        from views import _can_view_vin_registry_row

        user = _User(id=3, is_manager=True, warehouse_id=1, warehouse=self._warehouse(is_production=False))
        row = _VinRow(
            id=1065,
            customer_order=_Order(id=10, assigned_user_id=99),
            order_line=None,
        )
        self.assertFalse(_can_view_vin_registry_row(row, user))

    def test_normal_manager_can_view_own_order_vin(self):
        from views import _can_view_vin_registry_row

        user = _User(id=3, is_manager=True, warehouse_id=1, warehouse=self._warehouse(is_production=False))
        row = _VinRow(
            id=987,
            customer_order=_Order(id=10, assigned_user_id=3),
            order_line=None,
        )
        self.assertTrue(_can_view_vin_registry_row(row, user))

    def test_director_admin_can_view_any_vin(self):
        from views import _can_view_vin_registry_row

        row = _VinRow(id=1, customer_order=None, order_line=None)
        for user in (_User(id=1, is_admin=True), _User(id=6, is_director=True)):
            self.assertTrue(_can_view_vin_registry_row(row, user))

    def test_prod_manager_detail_route_returns_200_for_visible_vin(self):
        from models import VinRegistry

        client = self.app.test_client()
        row = VinRegistry.query.filter_by(status='free').first()
        self.assertIsNotNone(row)
        with client.session_transaction() as sess:
            sess.clear()
            sess['_user_id'] = '2'
            sess['_fresh'] = True
        response = client.get(f'/logistics/vin-registry/{row.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Карточка VIN', response.get_data(as_text=True))

    def test_normal_manager_detail_route_forbidden_for_foreign_vin(self):
        from views import vin_registry_detail
        from werkzeug.exceptions import Forbidden

        user = _User(id=3, is_manager=True, warehouse_id=1, warehouse=self._warehouse(is_production=False))
        row = _VinRow(
            id=1065,
            customer_order=_Order(id=10, assigned_user_id=99),
            order_line=None,
        )

        class _Query:
            def get_or_404(self, vin_id):
                return row

        with self.app.test_request_context('/logistics/vin-registry/1065'):
            with patch('views.current_user', new=user), \
                 patch('views.VinRegistry', new=SimpleNamespace(query=_Query())):
                with self.assertRaises(Forbidden):
                    vin_registry_detail.__wrapped__(1065)

    def test_list_template_open_links_have_detail_href(self):
        text = Path('templates/vin_registry_list.html').read_text(encoding='utf-8')
        self.assertIn("url_for('main.vin_registry_detail', vin_id=row.id)", text)
        self.assertRegex(text, r'<a[^>]+href="\{\{ url_for\(\'main\.vin_registry_detail\', vin_id=row\.id\) \}\}"[^>]*>Открыть</a>')

    def test_rendered_list_contains_detail_href_pattern(self):
        from models import User

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.clear()
            sess['_user_id'] = '2'
            sess['_fresh'] = True
        html = client.get('/logistics/vin-registry').get_data(as_text=True)
        links = re.findall(r'href="(/logistics/vin-registry/\d+)"[^>]*>Открыть', html)
        self.assertTrue(links, 'expected at least one Открыть link with detail href')
        self.assertNotIn('href="#"', html.split('Открыть')[0][-200:])


if __name__ == '__main__':
    unittest.main()
