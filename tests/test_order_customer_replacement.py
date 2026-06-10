#!/usr/bin/env python3
"""Tests for customer replacement in order header (not customer card edit)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / 'instance' / 'predad_verify_ro.db'


class OrderCustomerChangeBlockersTests(unittest.TestCase):
    def _order(self, **kwargs):
        order = MagicMock()
        order.id = kwargs.get('id', 19)
        order.status = kwargs.get('status', 'waiting_payment')
        order.documents_issued = kwargs.get('documents_issued', False)
        order.is_shipped = kwargs.get('is_shipped', False)
        return order

    def _mock_counts(self, mock_contract, mock_realization, *, contract=0, realization=0):
        mock_contract.query.filter_by.return_value.count.return_value = contract
        mock_realization.query.filter_by.return_value.count.return_value = realization

    @patch('views.SalesRealization')
    @patch('views.SalesContract')
    def test_no_blockers_when_clean(self, mock_contract, mock_realization):
        from views import _order_customer_change_blockers

        self._mock_counts(mock_contract, mock_realization)
        self.assertEqual(_order_customer_change_blockers(self._order()), [])

    @patch('views.SalesRealization')
    @patch('views.SalesContract')
    def test_contract_blocks(self, mock_contract, mock_realization):
        from views import _order_customer_change_blockers

        self._mock_counts(mock_contract, mock_realization, contract=1)
        blockers = _order_customer_change_blockers(self._order())
        self.assertTrue(any('договор' in b for b in blockers))

    @patch('views.SalesRealization')
    @patch('views.SalesContract')
    def test_realization_blocks(self, mock_contract, mock_realization):
        from views import _order_customer_change_blockers

        self._mock_counts(mock_contract, mock_realization, realization=1)
        blockers = _order_customer_change_blockers(self._order())
        self.assertTrue(any('реализац' in b for b in blockers))

    @patch('views.SalesRealization')
    @patch('views.SalesContract')
    def test_documents_issued_blocks(self, mock_contract, mock_realization):
        from views import _order_customer_change_blockers

        self._mock_counts(mock_contract, mock_realization)
        blockers = _order_customer_change_blockers(self._order(documents_issued=True))
        self.assertTrue(any('документ' in b for b in blockers))

    @patch('views.SalesRealization')
    @patch('views.SalesContract')
    def test_shipped_blocks(self, mock_contract, mock_realization):
        from views import _order_customer_change_blockers

        self._mock_counts(mock_contract, mock_realization)
        blockers = _order_customer_change_blockers(self._order(is_shipped=True))
        self.assertTrue(any('отгруж' in b for b in blockers))


class ApplyOrderCustomerIdChangeTests(unittest.TestCase):
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

    @patch('views.add_order_event')
    @patch('views.Customer')
    @patch('views._order_customer_change_blockers', return_value=['по заказу уже создан договор'])
    def test_blocked_does_not_change_customer(self, mock_blockers, mock_customer, mock_event):
        from views import _apply_order_customer_id_change

        order = MagicMock(id=19, customer_id=928)
        ok = _apply_order_customer_id_change(order, 951)
        self.assertFalse(ok)
        self.assertEqual(order.customer_id, 928)
        mock_event.assert_not_called()

    @patch('views.add_order_event')
    @patch('views._order_customer_change_blockers', return_value=[])
    @patch('views.Customer')
    def test_allowed_changes_customer(self, mock_customer, mock_blockers, mock_event):
        from views import _apply_order_customer_id_change

        order = MagicMock(id=19, customer_id=928)
        mock_customer.query.get.return_value = MagicMock(id=951)
        ok = _apply_order_customer_id_change(order, 951)
        self.assertTrue(ok)
        self.assertEqual(order.customer_id, 951)
        mock_event.assert_called_once()


@unittest.skipUnless(SNAPSHOT.exists(), 'snapshot DB missing')
class OrderCustomerReplacementHttpTests(unittest.TestCase):
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

    def _header_post_data(self, order_number: str, customer_id: int, assigned_user_id: int = 4):
        return {
            'order_number': order_number,
            'order_date': '2026-01-01',
            'lead_id': '0',
            'customer_search': 'test',
            'customer_id': str(customer_id),
            'warehouse_id': '0',
            'assigned_user_id': str(assigned_user_id),
            'expected_date': '',
            'planned_ship_date': '',
            'prepayment_percent': '30',
            'note': '',
            'manager_comment': '',
            'status': 'waiting_payment',
            'submit': 'Сохранить',
        }

    def test_owner_can_replace_customer_in_header(self):
        client = self.app.test_client()
        self._login(client, 4)
        with self.app.app_context():
            from models import CustomerOrder
            before = CustomerOrder.query.get(80).customer_id
        data = self._header_post_data('ORD-000080', 951)
        rv = client.post('/orders/80/edit', data=data, follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        with self.app.app_context():
            from models import CustomerOrder
            after = CustomerOrder.query.get(80).customer_id
        self.assertEqual(after, 951)
        self.assertNotEqual(after, before)

    def test_admin_can_replace_customer(self):
        client = self.app.test_client()
        self._login(client, 1)
        data = self._header_post_data('ORD-000080', 985)
        rv = client.post('/orders/80/edit', data=data, follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        with self.app.app_context():
            from models import CustomerOrder
            self.assertEqual(CustomerOrder.query.get(80).customer_id, 985)

    def test_non_owner_manager_blocked(self):
        client = self.app.test_client()
        self._login(client, 2)
        rv = client.post(
            '/orders/80/edit',
            data=self._header_post_data('ORD-000080', 951),
            follow_redirects=False,
        )
        self.assertEqual(rv.status_code, 403)

    def test_contract_blocks_replacement(self):
        client = self.app.test_client()
        self._login(client, 8)
        with self.app.app_context():
            from models import CustomerOrder
            before = CustomerOrder.query.get(72).customer_id
        rv = client.post(
            '/orders/72/edit',
            data=self._header_post_data('ORD-000072', 1, assigned_user_id=8),
            follow_redirects=False,
        )
        self.assertEqual(rv.status_code, 200)
        self.assertIn('договор', rv.get_data(as_text=True).lower())
        with self.app.app_context():
            from models import CustomerOrder
            self.assertEqual(CustomerOrder.query.get(72).customer_id, before)

    def test_header_form_shows_replace_hint_not_order_edit_link(self):
        client = self.app.test_client()
        self._login(client, 4)
        rv = client.get('/orders/80/edit')
        html = rv.get_data(as_text=True)
        self.assertEqual(rv.status_code, 200)
        self.assertIn('заменить клиента в заказе', html.lower())
        self.assertNotRegex(html, r'/customers/\d+/edit\?[^"\']*return_to=order_edit')


if __name__ == '__main__':
    unittest.main()
