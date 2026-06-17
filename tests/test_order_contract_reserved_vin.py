#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _LinesQuery:
    def __init__(self, lines):
        self._lines = lines

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._lines


class _GetOr404Query:
    def __init__(self, item):
        self._item = item

    def get_or_404(self, _id):
        return self._item


class _ContractQuery:
    def __init__(self, existing=None):
        self._existing = existing

    def filter_by(self, **kwargs):
        return SimpleNamespace(first=lambda: self._existing)


class _VinRow(SimpleNamespace):
    pass


class OrderContractReservedVinTests(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    @staticmethod
    def _production_line(**kwargs):
        return SimpleNamespace(
            id=kwargs.get('id', 501),
            line_no=kwargs.get('line_no', 1),
            line_type='TRAILER',
            fulfillment_source='production',
            quantity=1,
            item_id=kwargs.get('item_id', 531),
            article_snapshot=kwargs.get('article_snapshot', '002-2513E50Q14-OK60'),
            product_name_snapshot=kwargs.get('product_name_snapshot', 'Прицеп тест'),
            item=None,
            reservations=[],
            contract_lines=[],
            include_in_vehicle_contract=True,
            unit_price=100000,
            total_price=100000,
            otss_number=None,
            otss_type=None,
            otss_modification=None,
            vin_modification_code='002',
        )

    @staticmethod
    def _order(**kwargs):
        line = kwargs.get('line') or OrderContractReservedVinTests._production_line()
        return SimpleNamespace(
            id=kwargs.get('id', 500),
            status=kwargs.get('status', 'in_production'),
            documents_issued=kwargs.get('documents_issued', False),
            is_shipped=kwargs.get('is_shipped', False),
            trailer_id=kwargs.get('trailer_id'),
            trailer=kwargs.get('trailer'),
            customer_id=kwargs.get('customer_id', 10),
            price=kwargs.get('price', 100000),
            remaining_amount=kwargs.get('remaining_amount', 0),
            order_number=kwargs.get('order_number', 'ORD-000500'),
            lines=_LinesQuery([line]),
        )

    def test_contract_create_blockers_allow_reserved_vin_without_trailer(self):
        from views import _order_contract_create_blockers, _order_lines_document_blockers

        order = self._order(trailer_id=None, trailer=None)
        vin_row = _VinRow(id=901, vin_full='MX4000002T0000001', status='reserved', trailer=None)

        with patch('views.get_order_effective_vin', return_value='MX4000002T0000001'), \
             patch('views.SalesContract', new=SimpleNamespace(query=_ContractQuery(None))), \
             patch('views._active_vin_rows_for_order_line', return_value=[vin_row]), \
             patch('views._trailers_for_order_line', return_value=[]):
            contract_blockers = _order_contract_create_blockers(order)
            document_blockers = _order_lines_document_blockers(order)

        self.assertEqual(contract_blockers, [])
        self.assertTrue(any('физический прицеп не привязан' in row for row in document_blockers))

    def test_missing_catalog_blocks_contract_create(self):
        from views import _order_contract_create_blockers

        line = self._production_line(item_id=None, article_snapshot='', product_name_snapshot='')
        order = self._order(line=line)

        with patch('views.get_order_effective_vin', return_value='MX4000002T0000001'), \
             patch('views.SalesContract', new=SimpleNamespace(query=_ContractQuery(None))):
            blockers = _order_contract_create_blockers(order)

        self.assertTrue(any('номенклатура' in row for row in blockers))

    def test_missing_vin_blocks_contract_create(self):
        from views import _order_contract_create_blockers

        order = self._order()

        with patch('views.get_order_effective_vin', return_value=''), \
             patch('views.SalesContract', new=SimpleNamespace(query=_ContractQuery(None))):
            blockers = _order_contract_create_blockers(order)

        self.assertTrue(any('VIN' in row for row in blockers))

    def test_order_contract_create_succeeds_with_reserved_vin_no_trailer_id(self):
        from views import order_contract_create

        order = self._order(trailer_id=None, trailer=None)
        contract = SimpleNamespace(id=77, contract_number='SC-77')
        created_lines = []

        def _capture_lines(sales_contract, source_order):
            created_lines.append((sales_contract, source_order))

        with self.app.test_request_context('/orders/500/contract/new', method='POST', data={'tab': 'documents'}):
            sales_contract_cls = MagicMock()
            sales_contract_cls.query = _ContractQuery(None)
            sales_contract_cls.return_value = contract
            with patch('views.current_user', new=SimpleNamespace(id=1, is_admin=True)), \
                 patch('views.CustomerOrder', new=SimpleNamespace(query=_GetOr404Query(order))), \
                 patch('views._ensure_can_manage_order', return_value=None), \
                 patch('views.get_order_effective_vin', return_value='MX4000002T0000001'), \
                 patch('views.SalesContract', sales_contract_cls), \
                 patch('views._reserve_idempotency_key', return_value=('k1', False)), \
                 patch('views.get_next_contract_number', return_value='SC-77'), \
                 patch('views._create_contract_lines_from_order', side_effect=_capture_lines), \
                 patch('views._finish_idempotency', return_value=None), \
                 patch('views.add_order_event', return_value=None), \
                 patch('views.db.session.add'), \
                 patch('views.db.session.flush'), \
                 patch('views.db.session.commit', return_value=None):
                response = order_contract_create.__wrapped__(500)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/orders/500?tab=documents', response.location)
        self.assertEqual(len(created_lines), 1)
        self.assertIs(created_lines[0][1], order)

    def test_create_contract_lines_from_order_includes_reserved_vin_registry(self):
        from models import SalesContractLine
        from views import _create_contract_lines_from_order

        order = self._order()
        vin_row = _VinRow(
            id=901,
            vin_full='MX4000002T0000001',
            status='reserved',
            trailer=None,
            sales_contract_id=None,
        )
        contract = SimpleNamespace(id=77)
        added_lines = []

        vin_registry_cls = MagicMock()
        vin_registry_cls.query.filter.return_value.order_by.return_value.first.return_value = vin_row

        class _ContractLineQuery:
            def filter_by(self, **kwargs):
                return SimpleNamespace(first=lambda: None)

        with patch.object(SalesContractLine, 'query', _ContractLineQuery()), \
             patch('views.VinRegistry', vin_registry_cls), \
             patch('views.db.session.add', side_effect=lambda row: added_lines.append(row)):
            _create_contract_lines_from_order(contract, order)

        self.assertEqual(len(added_lines), 1)
        contract_line = added_lines[0]
        self.assertEqual(contract_line.vin_registry_id, 901)
        self.assertEqual(contract_line.vin_full, 'MX4000002T0000001')
        self.assertIsNone(contract_line.trailer_id)
        self.assertEqual(vin_row.sales_contract_id, 77)

    def test_issue_documents_still_blocks_without_physical_trailer(self):
        from views import order_issue_documents

        order = self._order(documents_issued=False)
        with self.app.test_request_context('/orders/500/issue-documents', method='POST', data={'tab': 'documents'}):
            with patch('views.current_user', new=SimpleNamespace(id=1, is_admin=True)), \
                 patch('views.CustomerOrder', new=SimpleNamespace(query=_GetOr404Query(order))), \
                 patch('views._ensure_can_manage_order', return_value=None), \
                 patch('views._order_lines_document_blockers', return_value=['по позиции #1 физический прицеп не привязан — завершите выпуск производства или привяжите прицеп из наличия.']):
                response = order_issue_documents.__wrapped__(500)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/orders/500?tab=documents', response.location)

    def test_realization_post_still_blocks_without_trailer(self):
        from views import _realization_post_blockers

        order = self._order(documents_issued=True)
        vin_row = _VinRow(id=901, vin_full='MX4000002T0000001', status='reserved', trailer=None)

        with patch('views.SalesContract', new=SimpleNamespace(query=_ContractQuery(SimpleNamespace(id=1)))), \
             patch('views._order_unrealized_lines_source', return_value=True), \
             patch('views._active_vin_rows_for_order_line', return_value=[vin_row]), \
             patch('views._trailers_for_order_line', return_value=[]):
            blockers = _realization_post_blockers(order)

        self.assertTrue(any('физический прицеп не привязан' in row for row in blockers))

    def test_no_vin_confirm_step_in_contract_blockers(self):
        from views import _order_contract_create_blockers

        order = self._order()
        with patch('views.get_order_effective_vin', return_value='MX4000002T0000001'), \
             patch('views.SalesContract', new=SimpleNamespace(query=_ContractQuery(None))):
            blockers = _order_contract_create_blockers(order)

        joined = ' '.join(blockers).lower()
        self.assertNotIn('подтвердить vin', joined)
        self.assertNotIn('подтверждение vin', joined)


if __name__ == '__main__':
    unittest.main()
