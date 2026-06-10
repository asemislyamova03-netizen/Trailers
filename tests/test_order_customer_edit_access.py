#!/usr/bin/env python3
"""Tests for order-context customer edit access (403 hotfix)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from werkzeug.exceptions import Forbidden

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / 'instance' / 'predad_verify_ro.db'


def _mock_user(*, role: str, user_id: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.role = role
    user.is_authenticated = True
    user.is_admin = role == 'admin'
    user.is_manager = role == 'manager'
    user.is_director = role == 'director'
    user.is_production = role == 'production'
    user.is_logistics = role == 'logistics'
    return user


class AttachCustomerToOrderTests(unittest.TestCase):
    @patch('views.db.session')
    @patch('views.CustomerOrder')
    @patch('views._ensure_can_manage_order')
    def test_requires_order_manage_permission(self, mock_ensure, mock_order_model, mock_session):
        from views import _attach_customer_to_order

        order = MagicMock(id=1, customer_id=99)
        mock_order_model.query.get.return_value = order
        customer = MagicMock(id=10)

        result = _attach_customer_to_order(1, customer)

        mock_ensure.assert_called_once_with(order)
        self.assertEqual(order.customer_id, 10)
        mock_session.commit.assert_called_once()
        self.assertIs(result, order)

    @patch('views.db.session')
    @patch('views.CustomerOrder')
    @patch('views._ensure_can_manage_order', side_effect=Forbidden())
    def test_denied_when_cannot_manage_order(self, mock_ensure, mock_order_model, mock_session):
        from views import _attach_customer_to_order

        order = MagicMock(id=1, customer_id=99)
        mock_order_model.query.get.return_value = order
        customer = MagicMock(id=10)

        with self.assertRaises(Forbidden):
            _attach_customer_to_order(1, customer)
        mock_session.commit.assert_not_called()


class EnsureCustomerOrderEditAccessTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.req_ctx = self.app.test_request_context()
        self.req_ctx.push()

    def tearDown(self):
        self.req_ctx.pop()
        self.ctx.pop()

    @patch('views.current_user', _mock_user(role='production', user_id=7))
    def test_production_blocked(self):
        from views import _ensure_customer_order_edit_access

        with self.assertRaises(Forbidden):
            _ensure_customer_order_edit_access('', None)

    @patch('views.current_user', _mock_user(role='logistics', user_id=5))
    def test_logistics_blocked(self):
        from views import _ensure_customer_order_edit_access

        with self.assertRaises(Forbidden):
            _ensure_customer_order_edit_access('', None)

    @patch('views.current_user', _mock_user(role='manager', user_id=4))
    @patch('views.CustomerOrder')
    @patch('views._ensure_can_manage_order')
    def test_order_context_checks_manage_permission(self, mock_manage, mock_order_model):
        from views import _ensure_customer_order_edit_access

        order = MagicMock(id=1)
        mock_order_model.query.get_or_404.return_value = order

        result = _ensure_customer_order_edit_access('order_edit', 1)

        mock_manage.assert_called_once_with(order)
        self.assertIs(result, order)

    @patch('views.current_user', _mock_user(role='manager', user_id=2))
    @patch('views.CustomerOrder')
    @patch('views._ensure_can_manage_order', side_effect=Forbidden())
    def test_other_manager_denied_for_foreign_order(self, mock_manage, mock_order_model):
        from views import _ensure_customer_order_edit_access

        order = MagicMock(id=1, assigned_user_id=4)
        mock_order_model.query.get_or_404.return_value = order

        with self.assertRaises(Forbidden):
            _ensure_customer_order_edit_access('order_edit', 1)


@unittest.skipUnless(SNAPSHOT.exists(), 'snapshot DB missing')
class OrderCustomerEditHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls._tmp.close()
        shutil.copy(SNAPSHOT, cls._tmp.name)
        from app import create_app

        cls.app = create_app()
        cls.app.config.update({
            'TESTING': True,
            'WTF_CSRF_ENABLED': False,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{cls._tmp.name}',
        })

    @classmethod
    def tearDownClass(cls):
        Path(cls._tmp.name).unlink(missing_ok=True)

    def _login(self, client, user_id: int):
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _customer_post_data(self, **overrides):
        data = {
            'customer_type': 'PERSON',
            'name': 'Белоногова Алена Андреевна',
            'phone': '87764755555',
            'iin_bin': '890311400050',
            'is_active': 'y',
            'submit': 'Сохранить',
        }
        data.update(overrides)
        return data

    def test_owning_manager_can_get_edit_from_order_context(self):
        client = self.app.test_client()
        self._login(client, 4)
        rv = client.get('/customers/1/edit?return_to=order_edit&order_id=1')
        self.assertEqual(rv.status_code, 200)

    def test_director_can_get_edit_from_order_context(self):
        client = self.app.test_client()
        self._login(client, 6)
        rv = client.get('/customers/1/edit?return_to=order_edit&order_id=1')
        self.assertEqual(rv.status_code, 200)

    def test_other_manager_forbidden_in_order_context(self):
        client = self.app.test_client()
        self._login(client, 2)
        rv = client.get('/customers/1/edit?return_to=order_edit&order_id=1')
        self.assertEqual(rv.status_code, 403)

    def test_production_forbidden(self):
        client = self.app.test_client()
        self._login(client, 7)
        rv = client.get('/customers/1/edit', follow_redirects=False)
        self.assertIn(rv.status_code, (302, 403))

    def test_logistics_forbidden(self):
        client = self.app.test_client()
        self._login(client, 5)
        rv = client.get('/customers/1/edit', follow_redirects=False)
        self.assertIn(rv.status_code, (302, 403))

    def test_owning_manager_post_redirects_to_order_edit(self):
        client = self.app.test_client()
        self._login(client, 4)
        data = self._customer_post_data(return_to='order_edit', order_id='1')
        rv = client.post(
            '/customers/1/edit?return_to=order_edit&order_id=1',
            data=data,
            follow_redirects=False,
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn('/orders/1/edit', rv.location or '')

    def test_standalone_edit_works_for_manager(self):
        client = self.app.test_client()
        self._login(client, 4)
        rv = client.get('/customers/1/edit')
        self.assertEqual(rv.status_code, 200)

    def test_other_manager_cannot_attach_via_post(self):
        client = self.app.test_client()
        self._login(client, 2)
        data = self._customer_post_data(return_to='order_edit', order_id='1')
        rv = client.post('/customers/1/edit', data=data, follow_redirects=False)
        self.assertEqual(rv.status_code, 403)


if __name__ == '__main__':
    unittest.main()
