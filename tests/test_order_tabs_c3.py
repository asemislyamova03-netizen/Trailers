#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _GetOr404Query:
    def __init__(self, item):
        self._item = item

    def get_or_404(self, _id):
        return self._item


class OrderTabsC3Tests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_normalize_order_detail_tab_defaults_to_overview(self):
        from views import _normalize_order_detail_tab

        self.assertEqual(_normalize_order_detail_tab(None), 'overview')
        self.assertEqual(_normalize_order_detail_tab('invalid'), 'overview')
        self.assertEqual(_normalize_order_detail_tab('documents'), 'documents')

    def test_redirect_order_detail_documents_adds_tab_query(self):
        from views import _redirect_order_detail

        with self.app.test_request_context('/'):
            response = _redirect_order_detail(42, tab='documents', default_tab='overview')
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/orders/42?tab=documents'))

    @patch('views._ensure_can_manage_order', return_value=None)
    @patch('views.CustomerOrder', new=SimpleNamespace(query=_GetOr404Query(SimpleNamespace(id=77, status='cancelled', documents_issued=False, is_shipped=False))))
    def test_order_contract_create_redirects_to_documents_tab_on_blocker(self, *_mocks):
        from views import order_contract_create

        with self.app.test_request_context('/orders/77/contract/new', method='POST', data={'tab': 'documents'}):
            response = order_contract_create.__wrapped__(77)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/orders/77?tab=documents', response.location)

    @patch('views._ensure_can_manage_order', return_value=None)
    @patch('views.CustomerOrder', new=SimpleNamespace(query=_GetOr404Query(SimpleNamespace(id=88, status='in_production', documents_issued=True, is_shipped=False))))
    def test_order_issue_documents_redirects_to_documents_tab_on_blocker(self, *_mocks):
        from views import order_issue_documents

        with self.app.test_request_context('/orders/88/issue-documents', method='POST', data={'tab': 'documents'}):
            response = order_issue_documents.__wrapped__(88)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/orders/88?tab=documents', response.location)

    @patch('views._ensure_can_manage_order', return_value=None)
    def test_mark_order_document_blocker_redirects_to_documents_tab(self, *_mocks):
        from views import _mark_order_document

        order = SimpleNamespace(id=91, documents_issued=True, is_shipped=False, document_status='invoice_sent')
        with self.app.test_request_context('/orders/91/mark-invoice-sent', method='POST', data={'tab': 'documents'}):
            response = _mark_order_document(order, 'invoice_sent', 'invoice_sent', 'ok')
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/orders/91?tab=documents'))


if __name__ == '__main__':
    unittest.main()
