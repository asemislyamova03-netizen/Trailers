#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class VinRegistryUploadPermissionsTests(unittest.TestCase):
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

    def test_production_warehouse_manager_allowed(self):
        from views import _can_manage_vin_registry_upload, _is_production_warehouse_manager

        user = _User(
            id=5,
            is_manager=True,
            is_admin=False,
            is_director=False,
            is_logistics=False,
            is_production=False,
            warehouse_id=3,
            warehouse=self._warehouse(),
        )
        self.assertTrue(_is_production_warehouse_manager(user))
        self.assertTrue(_can_manage_vin_registry_upload(user))

    def test_normal_manager_without_production_warehouse_blocked(self):
        from views import _can_manage_vin_registry_upload, _is_production_warehouse_manager

        user = _User(id=6, is_manager=True, warehouse_id=None, warehouse=None)
        self.assertFalse(_is_production_warehouse_manager(user))
        self.assertFalse(_can_manage_vin_registry_upload(user))

        sales_manager = _User(id=7, is_manager=True, warehouse_id=8, warehouse=self._warehouse(is_production=False))
        self.assertFalse(_is_production_warehouse_manager(sales_manager))
        self.assertFalse(_can_manage_vin_registry_upload(sales_manager))

    def test_admin_director_logistics_allowed(self):
        from views import _can_manage_vin_registry_upload

        for user in (
            _User(id=1, is_admin=True),
            _User(id=2, is_director=True),
            _User(id=3, is_logistics=True),
        ):
            self.assertTrue(_can_manage_vin_registry_upload(user))

    def test_production_role_not_allowed_for_upload_helper(self):
        from views import _can_manage_vin_registry_upload

        user = _User(id=9, is_production=True, is_manager=False)
        self.assertFalse(_can_manage_vin_registry_upload(user))

    def test_production_warehouse_manager_can_post_upload(self):
        from views import vin_registry_list

        user = _User(
            id=5,
            is_manager=True,
            warehouse_id=3,
            warehouse=self._warehouse(),
        )
        added = []

        class _VinQuery:
            def filter(self, *args, **kwargs):
                return self

            def filter_by(self, **kwargs):
                return self

            def first(self):
                return None

        with self.app.test_request_context('/logistics/vin-registry', method='POST', data={'vin_input': '0002632', 'source': 'manual'}):
            with patch('views.current_user', new=user), \
                 patch('views.VinRegistry.query', new=_VinQuery()), \
                 patch('views.Trailer', new=SimpleNamespace(query=_VinQuery())), \
                 patch('views._normalize_serial7', return_value=('0002632', None)), \
                 patch('views._add_vin_event', return_value=None), \
                 patch('views.db.session.add', side_effect=lambda row: added.append(row)), \
                 patch('views.db.session.flush', return_value=None), \
                 patch('views.db.session.commit', return_value=None), \
                 patch('views.flash', return_value=None):
                response = vin_registry_list.__wrapped__()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(len(added), 1)
                self.assertEqual(added[0].serial7, '0002632')

    def test_normal_manager_post_upload_forbidden(self):
        from views import vin_registry_list
        from werkzeug.exceptions import Forbidden

        user = _User(id=6, is_manager=True, warehouse=None, warehouse_id=None)
        with self.app.test_request_context('/logistics/vin-registry', method='POST', data={'vin_input': '0002632'}):
            with patch('views.current_user', new=user):
                with self.assertRaises(Forbidden):
                    vin_registry_list.__wrapped__()

    def test_duplicate_serial7_blocked(self):
        from views import vin_registry_list

        user = _User(id=1, is_admin=True)
        existing = SimpleNamespace(id=11, serial7='0002632')

        class _VinQuery:
            def filter(self, *args, **kwargs):
                return self

            def filter_by(self, **kwargs):
                return SimpleNamespace(first=lambda: existing)

            def first(self):
                return existing

        flashes = []
        with self.app.test_request_context('/logistics/vin-registry', method='POST', data={'vin_input': '0002632'}):
            with patch('views.current_user', new=user), \
                 patch('views.VinRegistry', new=SimpleNamespace(query=_VinQuery())), \
                 patch('views._normalize_serial7', return_value=('0002632', None)), \
                 patch('views.db.session.add') as add_mock, \
                 patch('views.db.session.commit', return_value=None), \
                 patch('views.flash', side_effect=lambda msg, cat: flashes.append(msg)):
                response = vin_registry_list.__wrapped__()
                self.assertEqual(response.status_code, 302)
                add_mock.assert_not_called()
                self.assertTrue(any('уже есть' in msg for msg in flashes))

    def test_vin_registry_list_template_uses_upload_helper(self):
        text = Path('templates/vin_registry_list.html').read_text(encoding='utf-8')
        self.assertIn('can_manage_vin_registry_upload()', text)
        self.assertNotIn('current_user.is_admin or current_user.is_director or current_user.is_logistics', text)


if __name__ == '__main__':
    unittest.main()
