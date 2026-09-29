#!/usr/bin/env python3
"""Связь SalesRealizationLine.produced_unit_id. Только временная sqlite."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')


class RealizationProducedUnitLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls._tmp.close()
        uri = 'sqlite:///' + cls._tmp.name.replace('\\', '/')
        from extensions import db

        original_init_app = db.init_app

        def _init_app(app):
            app.config['SQLALCHEMY_DATABASE_URI'] = uri
            app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
                'connect_args': {'check_same_thread': False, 'timeout': 15},
            }
            app.config['TESTING'] = True
            app.config['WTF_CSRF_ENABLED'] = False
            return original_init_app(app)

        db.init_app = _init_app
        from app import create_app

        cls.app = create_app()
        cls.app.config['WTF_CSRF_ENABLED'] = False
        db.init_app = original_init_app
        cls.db = db

        with cls.app.app_context():
            engine_url = str(db.engine.url).replace('\\', '/')
            tmp_url = cls._tmp.name.replace('\\', '/')
            if tmp_url not in engine_url or engine_url.endswith('trailers.db'):
                raise RuntimeError(f'Refused to touch live DB: {engine_url}')
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        try:
            Path(cls._tmp.name).unlink(missing_ok=True)
        except PermissionError:
            pass

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.drop_all()
        self.db.create_all()
        self._seed()

    def tearDown(self):
        self.db.session.remove()
        self.ctx.pop()

    def _seed(self):
        from models import Customer, Item, ProductionRequest, ProductionRequestLine, User, Warehouse

        self.warehouse = Warehouse(name='Склад продаж', is_active=True)
        self.db.session.add(self.warehouse)
        self.db.session.flush()
        self.item = Item(item_type='TRAILER', article='T-1', name='Прицеп T', unit='шт')
        self.other_item = Item(item_type='TRAILER', article='T-2', name='Другой прицеп', unit='шт')
        self.part = Item(item_type='COMPONENT', article='BOLT', name='Крепеж', unit='шт')
        self.db.session.add_all([self.item, self.other_item, self.part])
        self.db.session.flush()
        self.customer = Customer(customer_type='PERSON', name='Покупатель')
        self.db.session.add(self.customer)
        self.director = User(username='director', full_name='Директор', role='director')
        self.director.set_password('x')
        self.db.session.add(self.director)
        self.db.session.flush()
        self.request = ProductionRequest(request_number='PR-000001', status='in_progress')
        self.db.session.add(self.request)
        self.db.session.flush()
        self.request_line = ProductionRequestLine(
            production_request_id=self.request.id,
            item_id=self.item.id,
            quantity=1,
        )
        self.db.session.add(self.request_line)
        self.db.session.commit()
        self._vin_serial = 1

    def _trailer(self, vin, item=None):
        from models import Trailer

        trailer = Trailer(
            vin=vin,
            item_id=(item or self.item).id,
            warehouse_id=self.warehouse.id,
            status='IN_STOCK',
        )
        self.db.session.add(trailer)
        self.db.session.flush()
        return trailer

    def _unit(self, *, trailer=None, status='vin_assigned', item=None, shift_output_id=None, note=None):
        from models import ProducedUnit

        unit = ProducedUnit(
            production_request_line_id=self.request_line.id,
            item_id=(item or self.item).id,
            trailer_id=trailer.id if trailer else None,
            shift_output_id=shift_output_id,
            status=status,
            note=note,
        )
        self.db.session.add(unit)
        self.db.session.flush()
        return unit

    def _realization(self, lines, *, status='draft', order=None, commit=True):
        from models import SalesRealization, SalesRealizationLine

        realization = SalesRealization(
            number=None,
            realization_date=date.today(),
            order_id=order.id if order else None,
            customer_id=self.customer.id,
            warehouse_id=self.warehouse.id,
            status=status,
            total_amount=Decimal('100'),
        )
        self.db.session.add(realization)
        self.db.session.flush()
        for index, spec in enumerate(lines, start=1):
            self.db.session.add(SalesRealizationLine(
                realization_id=realization.id,
                line_no=index,
                line_type=spec.get('line_type', 'trailer'),
                order_line_id=spec.get('order_line_id'),
                trailer_id=spec.get('trailer_id'),
                item_id=spec.get('item_id', self.item.id),
                quantity=spec.get('quantity', Decimal('1')),
                unit='шт',
                inventory_effect=spec.get('inventory_effect', 'trailer_unit'),
                vin_full=spec.get('vin_full'),
                produced_unit_id=spec.get('produced_unit_id'),
            ))
        if commit:
            self.db.session.commit()
        return realization

    def _assign(self, realization):
        from realization_unit_link import assign_produced_units_on_post

        assign_produced_units_on_post(realization)

    def _line(self, realization, line_no=1):
        from models import SalesRealizationLine

        return SalesRealizationLine.query.filter_by(
            realization_id=realization.id,
            line_no=line_no,
        ).one()

    def test_draft_keeps_produced_unit_empty(self):
        trailer = self._trailer('VIN0000000000001')
        unit = self._unit(trailer=trailer)
        realization = self._realization([{'trailer_id': trailer.id, 'vin_full': trailer.vin}])
        self.assertEqual(realization.status, 'draft')
        self.assertIsNone(self._line(realization).produced_unit_id)
        self.assertEqual(unit.status, 'vin_assigned')

    def test_post_links_exactly_one_vin_assigned_unit(self):
        trailer = self._trailer('VIN0000000000002')
        unit = self._unit(trailer=trailer)
        realization = self._realization([{'trailer_id': trailer.id, 'item_id': self.item.id}])
        self._assign(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertEqual(self._line(realization).produced_unit_id, unit.id)
        self.assertEqual(realization.status, 'posted')

    def test_stock_sale_with_zero_units_stays_null(self):
        trailer = self._trailer('VIN0000000000003')
        loose = self._unit(trailer=None, status='produced_no_vin', note='смена без VIN')
        realization = self._realization([{'trailer_id': trailer.id}])
        self._assign(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertIsNone(self._line(realization).produced_unit_id)
        from models import SalesRealizationLine

        self.assertIsNone(
            SalesRealizationLine.query.filter_by(produced_unit_id=loose.id).first()
        )

    def test_two_units_refuse_and_do_not_pick_the_first(self):
        trailer = self._trailer('VIN0000000000004')
        first = self._unit(trailer=trailer)
        second = self._unit(trailer=trailer)
        realization = self._realization([{'trailer_id': trailer.id}])
        self._line(realization).produced_unit_id = first.id
        from realization_unit_link import ProducedUnitLinkError

        with self.assertRaises(ProducedUnitLinkError) as caught:
            self._assign(realization)
        self.assertIn('Первая не выбирается', str(caught.exception))
        self.db.session.rollback()
        stored = self._line(realization)
        self.assertEqual(stored.realization.status, 'draft')
        self.assertIsNone(stored.produced_unit_id)

    def test_status_or_item_mismatch_refuses(self):
        from realization_unit_link import ProducedUnitLinkError

        trailer = self._trailer('VIN0000000000005')
        self._unit(trailer=trailer, status='produced_no_vin')
        realization = self._realization([{'trailer_id': trailer.id}])
        with self.assertRaises(ProducedUnitLinkError):
            self._assign(realization)
        self.db.session.rollback()
        self.assertIsNone(self._line(realization).produced_unit_id)
        self.assertEqual(self._line(realization).realization.status, 'draft')

        trailer_item = self._trailer('VIN0000000000006')
        self._unit(trailer=trailer_item, item=self.other_item)
        mismatch = self._realization([{'trailer_id': trailer_item.id, 'item_id': self.item.id}])
        with self.assertRaises(ProducedUnitLinkError):
            self._assign(mismatch)
        self.db.session.rollback()
        self.assertIsNone(self._line(mismatch).produced_unit_id)

    def test_quantity_not_one_refuses(self):
        from realization_unit_link import ProducedUnitLinkError

        trailer = self._trailer('VIN0000000000007')
        self._unit(trailer=trailer)
        realization = self._realization([{'trailer_id': trailer.id, 'quantity': Decimal('2')}])
        with self.assertRaises(ProducedUnitLinkError):
            self._assign(realization)
        self.db.session.rollback()
        self.assertEqual(self._line(realization).realization.status, 'draft')
        self.assertIsNone(self._line(realization).produced_unit_id)

    def test_component_line_does_not_take_a_unit(self):
        trailer = self._trailer('VIN0000000000008')
        unit = self._unit(trailer=trailer)
        realization = self._realization([
            {'trailer_id': trailer.id},
            {
                'line_type': 'component',
                'inventory_effect': 'ship_from_stock',
                'trailer_id': None,
                'item_id': self.part.id,
                'quantity': Decimal('4'),
                'produced_unit_id': unit.id,
            },
        ])
        self._assign(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertEqual(self._line(realization, 1).produced_unit_id, unit.id)
        self.assertIsNone(self._line(realization, 2).produced_unit_id)

    def test_delete_then_repeat_post_is_a_new_line(self):
        from models import SalesRealization, SalesRealizationLine

        trailer = self._trailer('VIN0000000000009')
        unit = self._unit(trailer=trailer)
        order = self._sale_order(trailer)
        realization = self._post_via_route(order, trailer, unit)
        old_line_id = self._line(realization).id
        self.assertEqual(self._line(realization).produced_unit_id, unit.id)

        client = self._client()
        response = client.post(f'/realizations/{realization.id}/delete', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(SalesRealization.query.get(realization.id))
        self.assertIsNone(SalesRealizationLine.query.get(old_line_id))
        self.assertEqual(SalesRealization.query.filter(SalesRealization.cancelled_at.isnot(None)).count(), 0)

        order = self._reload_order(order.id)
        repeat = self._draft_realization(order, trailer)
        response = client.post(f'/realizations/{repeat.id}/post', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        repeat = SalesRealization.query.get(repeat.id)
        self.assertEqual(repeat.status, 'posted')
        self.assertEqual(self._line(repeat).produced_unit_id, unit.id)
        self.assertEqual(SalesRealizationLine.query.filter_by(produced_unit_id=unit.id).count(), 1)

    def test_legacy_plus_one_link_does_not_unlock_report(self):
        from shift_director_report import build_shift_director_report

        trailer = self._trailer('VIN0000000000010')
        plus_one = self._unit(trailer=trailer, shift_output_id=None, note='старый +1')
        self.assertIsNone(plus_one.shift_output_id)
        realization = self._realization([{'trailer_id': trailer.id}])
        self._assign(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertEqual(self._line(realization).produced_unit_id, plus_one.id)
        report = build_shift_director_report()
        self.assertEqual(report['sold_from_produced']['status'], 'BLOCKED')
        self.assertIsNone(report['sold_from_produced']['value'])
        self.assertNotIn('sold_count', report)

    def test_shift_without_vin_is_not_sold(self):
        from models import SalesRealizationLine

        trailer = self._trailer('VIN0000000000011')
        shift_unit = self._unit(trailer=None, status='produced_no_vin', note='смена без VIN')
        realization = self._realization([{'trailer_id': trailer.id}])
        self._assign(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertIsNone(self._line(realization).produced_unit_id)
        self.assertEqual(shift_unit.status, 'produced_no_vin')
        self.assertIsNone(shift_unit.trailer_id)
        self.assertIsNone(SalesRealizationLine.query.filter_by(produced_unit_id=shift_unit.id).first())

    def test_bypass_duplicate_produced_unit_rejected_by_database(self):
        from models import SalesRealizationLine

        trailer = self._trailer('VIN0000000000012')
        unit = self._unit(trailer=trailer)
        self._realization([
            {'trailer_id': trailer.id, 'produced_unit_id': unit.id},
            {
                'line_type': 'component',
                'inventory_effect': 'ship_from_stock',
                'item_id': self.part.id,
                'produced_unit_id': unit.id,
            },
        ], commit=False)
        with self.assertRaises(IntegrityError):
            self.db.session.commit()
        self.db.session.rollback()
        self.assertEqual(SalesRealizationLine.query.filter_by(produced_unit_id=unit.id).count(), 0)

    def test_route_post_rolls_back_when_two_units(self):
        from models import SalesRealization

        trailer = self._trailer('VIN0000000000013')
        first = self._unit(trailer=trailer)
        self._unit(trailer=trailer)
        order = self._sale_order(trailer)
        realization = self._draft_realization(order, trailer, produced_unit_id=first.id)
        response = self._client().post(f'/realizations/{realization.id}/post', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Первая не выбирается', response.get_data(as_text=True))
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'draft')
        self.assertIsNone(stored.posted_at)
        self.assertIsNone(self._line(stored).produced_unit_id)
        from models import Trailer
        self.assertNotEqual(Trailer.query.get(trailer.id).status, 'SOLD')

    def test_route_post_writes_link_and_report_stays_blocked(self):
        from models import SalesRealization
        from shift_director_report import build_shift_director_report

        trailer = self._trailer('VIN0000000000014')
        unit = self._unit(trailer=trailer, note='смена с VIN')
        order = self._sale_order(trailer)
        realization = self._post_via_route(order, trailer, unit)
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'posted')
        self.assertIsNotNone(stored.posted_at)
        self.assertEqual(self._line(stored).produced_unit_id, unit.id)
        from models import Trailer
        self.assertEqual(Trailer.query.get(trailer.id).status, 'SOLD')
        report = build_shift_director_report()
        self.assertEqual(report['sold_from_produced']['status'], 'BLOCKED')
        self.assertIsNone(report['sold_from_produced']['value'])
        client = self._client()
        page = client.get('/director/reports/shifts')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('Продано из выпущенных', body)
        self.assertIn('BLOCKED', body)
        self.assertIn('produced_unit_id', body)

    def test_second_posted_line_for_same_unit_is_refused(self):
        from realization_unit_link import ProducedUnitLinkError

        trailer = self._trailer('VIN0000000000015')
        unit = self._unit(trailer=trailer)
        first = self._realization([{'trailer_id': trailer.id}])
        self._assign(first)
        first.status = 'posted'
        self.db.session.commit()
        second = self._realization([{'trailer_id': trailer.id}])
        with self.assertRaises(ProducedUnitLinkError):
            self._assign(second)
        self.db.session.rollback()
        self.assertEqual(self._line(second).realization.status, 'draft')
        self.assertIsNone(self._line(second).produced_unit_id)

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        return client

    def _sale_order(self, trailer):
        from models import (
            CustomerOrder,
            CustomerOrderLine,
            OrderPayment,
            SalesContract,
            VinRegistry,
        )

        order = CustomerOrder(
            order_number=f'ORD-{trailer.id:06d}',
            customer_id=self.customer.id,
            item_id=self.item.id,
            trailer_id=trailer.id,
            warehouse_id=self.warehouse.id,
            assigned_user_id=self.director.id,
            quantity=1,
            price=Decimal('100'),
            status='confirmed',
            documents_issued=True,
            fulfillment_source='stock',
        )
        self.db.session.add(order)
        self.db.session.flush()
        line = CustomerOrderLine(
            order_id=order.id,
            line_no=1,
            line_type='TRAILER',
            fulfillment_source='stock',
            item_id=self.item.id,
            trailer_id=trailer.id,
            quantity=1,
            unit_price=Decimal('100'),
            total_price=Decimal('100'),
            article_snapshot=self.item.article,
            product_name_snapshot=self.item.name,
            include_in_realization=True,
        )
        self.db.session.add(line)
        self.db.session.flush()
        serial = f'{self._vin_serial:07d}'
        self._vin_serial += 1
        self.db.session.add(VinRegistry(
            vin_full=trailer.vin,
            serial7=serial,
            status='confirmed',
            customer_order_id=order.id,
            order_line_id=line.id,
            trailer_id=trailer.id,
        ))
        self.db.session.add(SalesContract(
            contract_number=f'SC-{trailer.id:06d}',
            customer_id=self.customer.id,
            trailer_id=trailer.id,
            order_id=order.id,
            price=Decimal('100'),
        ))
        self.db.session.add(OrderPayment(
            order_id=order.id,
            amount=Decimal('100'),
            status='CONFIRMED',
        ))
        self.db.session.commit()
        return order

    def _reload_order(self, order_id):
        from models import CustomerOrder

        return CustomerOrder.query.get(order_id)

    def _draft_realization(self, order, trailer, produced_unit_id=None):
        from models import CustomerOrderLine

        line = CustomerOrderLine.query.filter_by(order_id=order.id).one()
        return self._realization(
            [{
                'trailer_id': trailer.id,
                'item_id': self.item.id,
                'order_line_id': line.id,
                'vin_full': trailer.vin,
                'produced_unit_id': produced_unit_id,
            }],
            order=order,
        )

    def _post_via_route(self, order, trailer, unit):
        from models import SalesRealization

        realization = self._draft_realization(order, trailer)
        response = self._client().post(
            f'/realizations/{realization.id}/post',
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'posted', response.get_data(as_text=True))
        self.assertEqual(self._line(stored).produced_unit_id, unit.id)
        return stored


class ProducedUnitLinkMigrationTests(unittest.TestCase):
    def test_revision_chain_and_no_backfill(self):
        import importlib.util

        path = ROOT / 'migrations' / 'versions' / 'b7e2c4a9d815_sales_realization_line_produced_unit.py'
        spec = importlib.util.spec_from_file_location('produced_unit_link_migration', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, 'b7e2c4a9d815')
        self.assertEqual(module.down_revision, 'a7c8d9e0f1a2')
        source = path.read_text(encoding='utf-8')
        self.assertNotIn('op.execute', source)
        self.assertNotIn('UPDATE sales_realization_line', source)
        self.assertNotIn('backfill(', source.lower())

    def test_upgrade_adds_nullable_unique_fk_without_filling_old_rows(self):
        from sqlalchemy import create_engine
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        import importlib.util

        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        url = 'sqlite:///' + tmp.name.replace('\\', '/')
        self.assertNotIn('trailers.db', url)
        engine = create_engine(url)

        @event.listens_for(engine, 'connect')
        def _fk_on(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE produced_unit (id INTEGER PRIMARY KEY)'
            ))
            connection.execute(text(
                '''
                CREATE TABLE sales_realization_line (
                    id INTEGER PRIMARY KEY,
                    realization_id INTEGER NOT NULL,
                    line_no INTEGER NOT NULL,
                    line_type VARCHAR(30) NOT NULL,
                    quantity NUMERIC NOT NULL,
                    unit VARCHAR(20) NOT NULL,
                    inventory_effect VARCHAR(30) NOT NULL
                )
                '''
            ))
            connection.execute(text(
                '''
                INSERT INTO sales_realization_line
                    (id, realization_id, line_no, line_type, quantity, unit, inventory_effect)
                VALUES (1, 10, 1, 'trailer', 1, 'шт', 'trailer_unit')
                '''
            ))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                path = ROOT / 'migrations' / 'versions' / 'b7e2c4a9d815_sales_realization_line_produced_unit.py'
                spec = importlib.util.spec_from_file_location('produced_unit_link_migration_run', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()

        with engine.begin() as connection:
            columns = {
                row[1]: row
                for row in connection.execute(text('PRAGMA table_info(sales_realization_line)'))
            }
            self.assertIn('produced_unit_id', columns)
            self.assertEqual(columns['produced_unit_id'][3], 0)
            old_value = connection.execute(text(
                'SELECT produced_unit_id FROM sales_realization_line WHERE id = 1'
            )).scalar()
            self.assertIsNone(old_value)
            connection.execute(text('INSERT INTO produced_unit (id) VALUES (1)'))
            connection.execute(text(
                '''
                INSERT INTO sales_realization_line
                    (id, realization_id, line_no, line_type, quantity, unit, inventory_effect, produced_unit_id)
                VALUES (2, 11, 1, 'trailer', 1, 'шт', 'trailer_unit', NULL)
                '''
            ))
            connection.execute(text(
                '''
                INSERT INTO sales_realization_line
                    (id, realization_id, line_no, line_type, quantity, unit, inventory_effect, produced_unit_id)
                VALUES (3, 12, 1, 'trailer', 1, 'шт', 'trailer_unit', 1)
                '''
            ))
        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    '''
                    INSERT INTO sales_realization_line
                        (id, realization_id, line_no, line_type, quantity, unit, inventory_effect, produced_unit_id)
                    VALUES (4, 13, 1, 'trailer', 1, 'шт', 'trailer_unit', 1)
                    '''
                ))
        with self.assertRaises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    '''
                    INSERT INTO sales_realization_line
                        (id, realization_id, line_no, line_type, quantity, unit, inventory_effect, produced_unit_id)
                    VALUES (5, 14, 1, 'trailer', 1, 'шт', 'trailer_unit', 999)
                    '''
                ))

        engine.dispose()
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
