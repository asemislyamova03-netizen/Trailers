#!/usr/bin/env python3
"""Локальные тесты массового проведения смены. Только временная sqlite, не live DB."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')


def _qty(warehouse_id: int, item_id: int, storage_area_id=None) -> Decimal:
    from models import InventoryBalance

    query = InventoryBalance.query.filter_by(warehouse_id=warehouse_id, item_id=item_id)
    if storage_area_id is not None:
        query = query.filter_by(storage_area_id=storage_area_id)
    total = Decimal('0')
    for row in query.all():
        total += Decimal(row.quantity or 0)
    return total


class ShiftMassPostTests(unittest.TestCase):
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
        db.init_app = original_init_app
        cls.db = db
        with cls.app.app_context():
            engine_url = str(db.engine.url).replace('\\', '/')
            tmp_url = cls._tmp.name.replace('\\', '/')
            if tmp_url not in engine_url:
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
        from models import (
            InventoryBalance,
            Item,
            ProductCategory,
            ProductionEmployee,
            ProductionRequest,
            ProductionRequestLine,
            ProductionShift,
            ProductionShiftMaterial,
            ProductionShiftOutput,
            ProductionWorkshop,
            SupplyNeed,
            User,
            Warehouse,
            WarehouseStorageArea,
        )

        self.factory = Warehouse(
            name='Кокшетау завод',
            is_active=True,
            is_production=True,
            warehouse_kind='assembly',
            is_sales_point=False,
            can_sell=False,
            can_ship_to_customer=False,
            primary_product_category=None,
            shopfloor_posting_mode='legacy_plus_one',
        )
        self.light_sales = Warehouse(
            name='Реализация легковые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='light_trailer',
        )
        self.cargo_sales = Warehouse(
            name='Реализация грузовые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='cargo_trailer',
        )
        self.db.session.add_all([self.factory, self.light_sales, self.cargo_sales])
        self.db.session.flush()

        self.light_area = WarehouseStorageArea(
            warehouse_id=self.factory.id,
            code='DIR_LIGHT',
            name='Легковые ТМЦ',
            area_type='direction_stock',
            is_active=True,
            sort_order=210,
            product_category='light_trailer',
            shopfloor_posting_mode='legacy_plus_one',
        )
        self.cargo_area = WarehouseStorageArea(
            warehouse_id=self.factory.id,
            code='DIR_CARGO',
            name='Грузовые ТМЦ',
            area_type='direction_stock',
            is_active=True,
            sort_order=220,
            product_category='cargo_trailer',
            shopfloor_posting_mode='legacy_plus_one',
        )
        self.components = WarehouseStorageArea(
            warehouse_id=self.factory.id,
            code='COMPONENTS',
            name='Комплектующие',
            area_type='storage',
            is_active=True,
            sort_order=10,
            product_category=None,
        )
        self.db.session.add_all([self.light_area, self.cargo_area, self.components])
        self.db.session.flush()

        self.light_cat = ProductCategory(code='light_trailer', name='Легковые', kind='goods', sort_order=1)
        self.cargo_cat = ProductCategory(code='cargo_trailer', name='Грузовые', kind='goods', sort_order=2)
        self.db.session.add_all([self.light_cat, self.cargo_cat])
        self.db.session.flush()

        self.sheet = Item(item_type='COMPONENT', article='M', name='Лист 2 мм', unit='шт')
        self.frame = Item(item_type='COMPONENT', article='P', name='Рама 2500', unit='шт')
        self.trailer_model = Item(
            item_type='TRAILER',
            article='T',
            name='Прицеп T',
            unit='шт',
            product_category_id=self.light_cat.id,
        )
        self.cargo_item = Item(
            item_type='TRAILER',
            article='CARGO',
            name='Грузовой',
            unit='шт',
            product_category_id=self.cargo_cat.id,
        )
        self.db.session.add_all([self.sheet, self.frame, self.trailer_model, self.cargo_item])
        self.db.session.flush()

        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id,
            storage_area_id=self.components.id,
            item_id=self.sheet.id,
            quantity=Decimal('8'),
            unit='шт',
        ))
        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id,
            storage_area_id=self.light_area.id,
            item_id=self.sheet.id,
            quantity=Decimal('8'),
            unit='шт',
        ))
        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id,
            storage_area_id=self.light_area.id,
            item_id=self.frame.id,
            quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id,
            storage_area_id=self.cargo_area.id,
            item_id=self.sheet.id,
            quantity=Decimal('10'),
            unit='шт',
        ))

        self.workshop = ProductionWorkshop(code='assembly', name='Сборка', workshop_type='trailer')
        self.db.session.add(self.workshop)
        self.admin = User(username='admin', full_name='Админ', role='admin')
        self.admin.set_password('x')
        self.director = User(username='director', full_name='Директор', role='director')
        self.director.set_password('x')
        self.production = User(username='prod', full_name='Цех', role='production')
        self.production.set_password('x')
        self.db.session.add_all([self.admin, self.director, self.production])
        self.db.session.flush()
        self.employee = ProductionEmployee(
            user_id=self.admin.id, full_name='Админ', employee_code='A1', is_active=True
        )
        self.director_employee = ProductionEmployee(
            user_id=self.director.id, full_name='Директор', employee_code='D1', is_active=True
        )
        self.prod_employee = ProductionEmployee(
            user_id=self.production.id, full_name='Цех', employee_code='P1', is_active=True
        )
        self.db.session.add_all([self.employee, self.director_employee, self.prod_employee])
        self.db.session.flush()

        self.need = SupplyNeed(
            need_type='CUSTOMER_ORDER',
            status='IN_PRODUCTION',
            item_id=self.trailer_model.id,
            warehouse_id=self.light_sales.id,
            quantity=1,
        )
        self.db.session.add(self.need)
        self.db.session.flush()
        self.pr = ProductionRequest(
            request_number='PR-000001',
            status='in_progress',
            target_warehouse_id=self.light_sales.id,
        )
        self.db.session.add(self.pr)
        self.db.session.flush()
        self.pr_line = ProductionRequestLine(
            production_request_id=self.pr.id,
            supply_need_id=self.need.id,
            item_id=self.trailer_model.id,
            production_workshop_id=self.workshop.id,
            assembly_warehouse_id=self.factory.id,
            quantity=1,
            produced_qty=0,
            status='in_production',
        )
        self.db.session.add(self.pr_line)
        self.db.session.flush()

        self.shift = ProductionShift(
            employee_id=self.employee.id,
            user_id=self.admin.id,
            workshop_id=self.workshop.id,
            work_area='assembly',
            status='open',
            direction_warehouse_id=self.factory.id,
            direction_area_id=self.light_area.id,
        )
        self.db.session.add(self.shift)
        self.db.session.flush()
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.shift.id,
            item_id=self.sheet.id,
            qty_fact=Decimal('12'),
            unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.frame.id,
            output_type='component',
            quantity=Decimal('5'),
            defect_quantity=Decimal('1'),
            unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.trailer_model.id,
            production_request_line_id=self.pr_line.id,
            output_type='trailer',
            quantity=Decimal('2'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()

    def _open(self, area):
        from shift_posting import MODE_SHIFT_ONLY, set_direction_shopfloor_mode

        set_direction_shopfloor_mode(area, MODE_SHIFT_ONLY)

    def _post(self, shift=None, senior=True, actor_senior=True, area_id=None):
        from shift_posting import post_shift
        from models import WarehouseStorageArea

        target_area_id = area_id if area_id is not None else self.light_area.id
        area = WarehouseStorageArea.query.get(target_area_id)
        if area and area.is_active:
            self._open(area)
        return post_shift(
            shift=shift or self.shift,
            direction_area_id=target_area_id,
            hours_fact='8',
            senior_confirmed=senior,
            actor_is_senior=actor_senior,
            created_by_user_id=self.admin.id,
        )

    def _add_bom(self, item, component, qty='1'):
        from models import ItemBillOfMaterials, ItemBillOfMaterialsLine

        bom = ItemBillOfMaterials(item_id=item.id, version='default', is_active=True, name=item.article)
        self.db.session.add(bom)
        self.db.session.flush()
        self.db.session.add(ItemBillOfMaterialsLine(
            bom_id=bom.id,
            component_item_id=component.id,
            quantity_per_unit=Decimal(qty),
            unit='шт',
            is_required=True,
            is_active=True,
        ))
        return bom

    def test_shortage_without_senior_keeps_book(self):
        from shift_posting import ShiftPostingError

        with self.assertRaises(ShiftPostingError):
            self._post(senior=False)
        self.db.session.rollback()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('8'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))
        from models import ProductionShift
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')

    def test_director_cannot_confirm_shortage(self):
        from shift_posting import ShiftPostingError

        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=True, actor_senior=False)
        self.assertIn('admin', str(ctx.exception).lower())
        self.db.session.rollback()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('8'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))

    def test_numeric_example_and_retry(self):
        from models import InventoryOperation, ProducedUnit, ProductionRequestLine, ProductionShift, SupplyNeed

        result = self._post()
        self.db.session.commit()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('0'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))
        self.assertEqual(_qty(self.factory.id, self.frame.id, self.light_area.id), Decimal('5'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), Decimal('8'))
        issues = InventoryOperation.query.filter_by(operation_type='production_issue', status='posted').all()
        shortages = InventoryOperation.query.filter_by(operation_type='production_shortage', status='posted').all()
        receipts = InventoryOperation.query.filter_by(operation_type='production_output_receipt', status='posted').all()
        self.assertEqual(len(issues), 1)
        self.assertEqual(sum(Decimal(str(line.quantity)) for line in issues[0].lines.all()), Decimal('8'))
        self.assertEqual(issues[0].source_area_id, self.light_area.id)
        self.assertEqual(len(shortages), 1)
        self.assertEqual(Decimal(str(shortages[0].lines.first().quantity)), Decimal('4'))
        self.assertEqual(shortages[0].source_area_id, self.light_area.id)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(Decimal(str(receipts[0].lines.first().quantity)), Decimal('5'))
        self.assertEqual(receipts[0].target_area_id, self.light_area.id)
        units = ProducedUnit.query.order_by(ProducedUnit.unit_seq.asc()).all()
        self.assertEqual(len(units), 2)
        self.assertTrue(all(u.status == 'produced_no_vin' and u.trailer_id is None for u in units))
        self.assertEqual(self.pr_line.quantity, 1)
        self.assertEqual(ProductionRequestLine.query.get(self.pr_line.id).produced_qty, 1)
        replenish = SupplyNeed.query.filter_by(need_type='STOCK_REPLENISHMENT').all()
        self.assertEqual(len(replenish), 1)
        self.assertEqual(replenish[0].quantity, 1)
        self.assertEqual(replenish[0].warehouse_id, self.light_sales.id)
        self.assertEqual(result['units_created'], 2)
        self.assertEqual(result['replenishment_units'], 1)
        self.assertEqual(result['direction_area_id'], self.light_area.id)

        from shift_posting import ShiftAlreadyPosted
        with self.assertRaises(ShiftAlreadyPosted):
            self._post()
        self.db.session.rollback()
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_issue').count(), 1)
        self.assertEqual(ProducedUnit.query.count(), 2)
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'posted')
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))

    def test_shared_sheet_light_does_not_reduce_cargo(self):
        from models import InventoryOperation

        self._post()
        self.db.session.commit()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('0'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), Decimal('8'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id), Decimal('18'))
        issue = InventoryOperation.query.filter_by(operation_type='production_issue').one()
        self.assertEqual(issue.source_area_id, self.light_area.id)
        self.assertNotEqual(issue.source_area_id, self.cargo_area.id)

    def test_scrap_does_not_receipt(self):
        from models import InventoryOperation, ProducedUnit

        self._post()
        self.db.session.commit()
        defect_receipts = [
            op for op in InventoryOperation.query.filter_by(operation_type='production_output_receipt').all()
            if Decimal(str(op.lines.first().quantity)) == Decimal('1')
        ]
        self.assertEqual(defect_receipts, [])
        self.assertEqual(ProducedUnit.query.count(), 2)
        self.assertEqual(_qty(self.factory.id, self.frame.id, self.light_area.id), Decimal('5'))
        self.assertEqual(_qty(self.factory.id, self.frame.id, self.cargo_area.id), Decimal('0'))

    def test_output_without_order(self):
        from models import Item, ProductionShift, ProductionShiftOutput, ProducedUnit, SupplyNeed

        shift = ProductionShift(
            employee_id=self.employee.id,
            user_id=self.admin.id,
            workshop_id=self.workshop.id,
            work_area='assembly',
            status='open',
            direction_warehouse_id=self.factory.id,
            direction_area_id=self.light_area.id,
        )
        self.db.session.add(shift)
        self.db.session.flush()
        extra = Item.query.filter_by(article='T').first()
        self.db.session.add(ProductionShiftOutput(
            shift_id=shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=extra.id,
            output_type='trailer',
            quantity=Decimal('1'),
            defect_quantity=Decimal('0'),
        ))
        self.db.session.commit()
        self._post(shift=shift, senior=False)
        self.db.session.commit()
        self.assertEqual(ProducedUnit.query.filter_by(shift_output_id=shift.outputs.first().id).count(), 1)
        self.assertEqual(SupplyNeed.query.filter_by(need_type='STOCK_REPLENISHMENT').count(), 1)
        self.assertEqual(
            SupplyNeed.query.filter_by(need_type='STOCK_REPLENISHMENT').first().warehouse_id,
            self.light_sales.id,
        )

    def test_mid_transaction_rollback(self):
        from models import ProductionShift

        with patch('shift_posting.apply_shift_component_receipt', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._post()
            self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('8'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))
        self.assertEqual(_qty(self.factory.id, self.frame.id, self.light_area.id), Decimal('0'))

    def test_parallel_post_one_wins(self):
        from models import InventoryOperation, ProducedUnit, ProductionShift
        from shift_posting import ShiftAlreadyPosted, ShiftPostingError, post_shift

        self._open(self.light_area)
        self.db.session.commit()
        errors = []
        wins = []

        def worker():
            with self.app.app_context():
                shift = ProductionShift.query.get(self.shift.id)
                try:
                    post_shift(
                        shift=shift,
                        direction_area_id=self.light_area.id,
                        hours_fact='8',
                        senior_confirmed=True,
                        actor_is_senior=True,
                        created_by_user_id=self.admin.id,
                    )
                    self.db.session.commit()
                    wins.append(1)
                except (ShiftAlreadyPosted, ShiftPostingError, Exception) as exc:
                    self.db.session.rollback()
                    errors.append(type(exc).__name__)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertGreaterEqual(len(wins), 1)
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_issue').count(), 1)
        self.assertEqual(ProducedUnit.query.count(), 2)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))

    def test_shift_only_blocks_plus_one_other_direction_keeps_plus_one(self):
        from models import ItemBillOfMaterials, ItemBillOfMaterialsLine, ProductionRequest, ProductionRequestLine, ProducedUnit, SupplyNeed
        from shift_posting import MODE_SHIFT_ONLY, plus_one_blocked_reason, set_direction_shopfloor_mode

        set_direction_shopfloor_mode(self.light_area, MODE_SHIFT_ONLY)
        self.db.session.commit()
        self.assertEqual(plus_one_blocked_reason(self.pr_line), 'Выпуск только через смену. Учёт направления открыт.')

        cargo_need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT',
            status='IN_PRODUCTION',
            item_id=self.cargo_item.id,
            warehouse_id=self.cargo_sales.id,
            quantity=1,
        )
        self.db.session.add(cargo_need)
        self.db.session.flush()
        cargo_pr = ProductionRequest(
            request_number='PR-000002',
            status='in_progress',
            target_warehouse_id=self.cargo_sales.id,
        )
        self.db.session.add(cargo_pr)
        self.db.session.flush()
        cargo_line = ProductionRequestLine(
            production_request_id=cargo_pr.id,
            supply_need_id=cargo_need.id,
            item_id=self.cargo_item.id,
            assembly_warehouse_id=self.factory.id,
            quantity=1,
            produced_qty=0,
            status='in_production',
        )
        self.db.session.add(cargo_line)
        bom = ItemBillOfMaterials(item_id=self.cargo_item.id, version='default', is_active=True, name='cargo')
        self.db.session.add(bom)
        self.db.session.flush()
        self.db.session.add(ItemBillOfMaterialsLine(
            bom_id=bom.id,
            component_item_id=self.sheet.id,
            quantity_per_unit=Decimal('1'),
            unit='шт',
            is_required=True,
            is_active=True,
        ))
        self.db.session.commit()
        self.assertIsNone(plus_one_blocked_reason(cargo_line))

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.production.id)
            sess['_fresh'] = True
        before_cargo_zone = _qty(self.factory.id, self.sheet.id, self.cargo_area.id)
        before_light_zone = _qty(self.factory.id, self.sheet.id, self.light_area.id)
        before_general = _qty(self.factory.id, self.sheet.id, self.components.id)
        before_units = ProducedUnit.query.count()
        rv = client.post(f'/production/lines/{self.pr_line.id}/produce-one', follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(ProducedUnit.query.count(), before_units)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), before_light_zone)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), before_cargo_zone)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), before_general)

        rv2 = client.post(f'/production/lines/{cargo_line.id}/produce-one', follow_redirects=False)
        self.assertEqual(rv2.status_code, 302)
        self.assertEqual(ProducedUnit.query.count(), before_units + 1)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), before_cargo_zone)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), before_light_zone)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), before_general - Decimal('1'))

    def test_close_shortage_does_not_issue_again(self):
        from models import InventoryOperation
        from shift_posting import close_shift_shortage

        self._post()
        self.db.session.commit()
        material = self.shift.materials.first()
        issues_before = InventoryOperation.query.filter_by(operation_type='production_issue').count()
        close_shift_shortage(material_id=material.id, user_id=self.admin.id)
        self.db.session.commit()
        self.assertEqual(material.shortage_status, 'closed_by_count')
        self.assertEqual(
            InventoryOperation.query.filter_by(operation_type='production_issue').count(),
            issues_before,
        )
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('0'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))

    def test_non_direction_area_is_not_guessed(self):
        from shift_posting import ShiftPostingError, post_shift

        with self.assertRaises(ShiftPostingError):
            post_shift(
                shift=self.shift,
                direction_area_id=self.components.id,
                senior_confirmed=True,
                actor_is_senior=True,
                created_by_user_id=self.admin.id,
            )
        self.db.session.rollback()
        self.shift.direction_area_id = None
        self.db.session.flush()
        with self.assertRaises(ShiftPostingError):
            post_shift(
                shift=self.shift,
                direction_area_id=None,
                senior_confirmed=True,
                actor_is_senior=True,
                created_by_user_id=self.admin.id,
            )
        self.db.session.rollback()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('8'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), Decimal('10'))

    def test_schema_install_empty_zones_keeps_plus_one_on_general(self):
        from models import InventoryBalance, Item, ProductionRequest, ProductionRequestLine, ProducedUnit, SupplyNeed, Warehouse, WarehouseStorageArea
        from shift_posting import ensure_direction_areas_for_warehouse, plus_one_blocked_reason

        old = Warehouse(
            name='ФабрикаАльфа',
            is_active=True,
            is_production=True,
            warehouse_kind='assembly',
            is_sales_point=False,
            can_sell=False,
            can_ship_to_customer=False,
        )
        old_sales = Warehouse(
            name='Склад ФабрикаАльфа',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='light_trailer',
        )
        self.db.session.add_all([old, old_sales])
        self.db.session.flush()
        general = WarehouseStorageArea(
            warehouse_id=old.id,
            code='COMPONENTS',
            name='Комплектующие',
            area_type='storage',
            is_active=True,
            sort_order=10,
        )
        self.db.session.add(general)
        self.db.session.flush()
        item = Item(item_type='TRAILER', article='OLD', name='Старый', unit='шт', product_category_id=self.light_cat.id)
        self.db.session.add(item)
        self.db.session.flush()
        self.db.session.add(InventoryBalance(
            warehouse_id=old.id, storage_area_id=general.id, item_id=self.sheet.id, quantity=Decimal('8'), unit='шт'
        ))
        self._add_bom(item, self.sheet)
        need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT', status='IN_PRODUCTION', item_id=item.id, warehouse_id=old_sales.id, quantity=2
        )
        self.db.session.add(need)
        self.db.session.flush()
        pr = ProductionRequest(request_number='PR-OLD-001', status='in_progress', target_warehouse_id=old_sales.id)
        self.db.session.add(pr)
        self.db.session.flush()
        line = ProductionRequestLine(
            production_request_id=pr.id,
            supply_need_id=need.id,
            item_id=item.id,
            assembly_warehouse_id=old.id,
            quantity=2,
            produced_qty=0,
            status='in_production',
        )
        self.db.session.add(line)
        self.db.session.commit()
        self.assertIsNone(plus_one_blocked_reason(line))

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.production.id)
            sess['_fresh'] = True
        rv = client.post(f'/production/lines/{line.id}/produce-one', follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(ProducedUnit.query.filter_by(item_id=item.id).count(), 1)
        self.assertEqual(_qty(old.id, self.sheet.id, general.id), Decimal('7'))
        self.assertFalse(
            WarehouseStorageArea.query.filter(
                WarehouseStorageArea.warehouse_id == old.id,
                WarehouseStorageArea.code.in_(('DIR_LIGHT', 'DIR_CARGO')),
            ).count()
        )

        ensure_direction_areas_for_warehouse(old)
        self.db.session.commit()
        self.assertIsNone(plus_one_blocked_reason(line))
        light_dir = WarehouseStorageArea.query.filter_by(warehouse_id=old.id, code='DIR_LIGHT').one()
        cargo_dir = WarehouseStorageArea.query.filter_by(warehouse_id=old.id, code='DIR_CARGO').one()
        self.assertEqual(light_dir.shopfloor_posting_mode, 'legacy_plus_one')
        self.assertEqual(cargo_dir.shopfloor_posting_mode, 'legacy_plus_one')
        rv2 = client.post(f'/production/lines/{line.id}/produce-one', follow_redirects=False)
        self.assertEqual(rv2.status_code, 302)
        self.assertEqual(ProducedUnit.query.filter_by(item_id=item.id).count(), 2)
        self.assertEqual(_qty(old.id, self.sheet.id, general.id), Decimal('6'))
        self.assertEqual(_qty(old.id, self.sheet.id, light_dir.id), Decimal('0'))
        self.assertEqual(_qty(old.id, self.sheet.id, cargo_dir.id), Decimal('0'))

    def test_empty_inactive_zone_blocks_shift_even_admin(self):
        from models import InventoryBalance
        from shift_posting import ShiftPostingError, post_shift

        for row in InventoryBalance.query.filter_by(storage_area_id=self.light_area.id, item_id=self.sheet.id):
            row.quantity = Decimal('0')
        self.light_area.is_active = False
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            post_shift(
                shift=self.shift,
                direction_area_id=self.light_area.id,
                senior_confirmed=True,
                actor_is_senior=True,
                created_by_user_id=self.admin.id,
            )
        self.assertIn('неактивн', str(ctx.exception).lower())
        self.db.session.rollback()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), Decimal('8'))

    def test_unopened_empty_zone_blocks_shift_even_admin(self):
        from models import InventoryBalance
        from shift_posting import ShiftPostingError, post_shift

        for row in InventoryBalance.query.filter_by(storage_area_id=self.light_area.id, item_id=self.sheet.id):
            row.quantity = Decimal('0')
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            post_shift(
                shift=self.shift,
                direction_area_id=self.light_area.id,
                senior_confirmed=True,
                actor_is_senior=True,
                created_by_user_id=self.admin.id,
            )
        message = str(ctx.exception).lower()
        self.assertIn('не открыт', message)
        self.assertIn('инвентаризац', message)
        self.db.session.rollback()
        from models import ProductionShift
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')

    def test_signed_transfer_preserves_total_and_does_not_auto_split(self):
        from models import InventoryOperation
        from shift_posting import ShiftPostingError, transfer_signed_qty_to_direction

        before_total = _qty(self.factory.id, self.sheet.id)
        before_general = _qty(self.factory.id, self.sheet.id, self.components.id)
        before_light = _qty(self.factory.id, self.sheet.id, self.light_area.id)
        before_cargo = _qty(self.factory.id, self.sheet.id, self.cargo_area.id)
        ops_before = InventoryOperation.query.filter_by(operation_type='transfer').count()
        with self.assertRaises(ShiftPostingError):
            transfer_signed_qty_to_direction(
                warehouse_id=self.factory.id,
                direction_area_id=self.light_area.id,
                lines=[],
                created_by_user_id=self.admin.id,
                from_storage_area_id=self.components.id,
            )
        self.db.session.rollback()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), before_general)

        transfer_signed_qty_to_direction(
            warehouse_id=self.factory.id,
            direction_area_id=self.light_area.id,
            lines=[(self.sheet.id, Decimal('5'))],
            created_by_user_id=self.admin.id,
            from_storage_area_id=self.components.id,
        )
        self.db.session.commit()
        self.assertEqual(_qty(self.factory.id, self.sheet.id), before_total)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), before_general - Decimal('5'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), before_light + Decimal('5'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), before_cargo)
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='transfer').count(), ops_before + 1)

    def test_repeat_open_and_repost_creates_no_extra_movements(self):
        from models import InventoryOperation
        from shift_posting import MODE_SHIFT_ONLY, ShiftAlreadyPosted, set_direction_shopfloor_mode

        ops_before = InventoryOperation.query.count()
        set_direction_shopfloor_mode(self.light_area, MODE_SHIFT_ONLY)
        set_direction_shopfloor_mode(self.light_area, MODE_SHIFT_ONLY)
        self.db.session.flush()
        self.assertEqual(InventoryOperation.query.count(), ops_before)
        self._post()
        self.db.session.commit()
        issues = InventoryOperation.query.filter_by(operation_type='production_issue').count()
        shortages = InventoryOperation.query.filter_by(operation_type='production_shortage').count()
        receipts = InventoryOperation.query.filter_by(operation_type='production_output_receipt').count()
        set_direction_shopfloor_mode(self.light_area, MODE_SHIFT_ONLY)
        self.db.session.commit()
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_issue').count(), issues)
        with self.assertRaises(ShiftAlreadyPosted):
            self._post()
        self.db.session.rollback()
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_issue').count(), issues)
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_shortage').count(), shortages)
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_output_receipt').count(), receipts)

    def test_unopened_light_plus_one_uses_general_not_direction_zone(self):
        from models import ProducedUnit
        from shift_posting import plus_one_blocked_reason

        self._add_bom(self.trailer_model, self.sheet)
        self.db.session.commit()
        self.assertIsNone(plus_one_blocked_reason(self.pr_line))
        before_general = _qty(self.factory.id, self.sheet.id, self.components.id)
        before_light = _qty(self.factory.id, self.sheet.id, self.light_area.id)
        before_cargo = _qty(self.factory.id, self.sheet.id, self.cargo_area.id)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.production.id)
            sess['_fresh'] = True
        rv = client.post(f'/production/lines/{self.pr_line.id}/produce-one', follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(ProducedUnit.query.count(), 1)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.components.id), before_general - Decimal('1'))
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), before_light)
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.cargo_area.id), before_cargo)

    def test_director_cannot_switch_shopfloor_mode(self):
        """Finding 1: режим зоны — только admin; director получает отказ, mode не меняется."""
        from models import WarehouseStorageArea
        from shift_posting import MODE_LEGACY, MODE_SHIFT_ONLY, plus_one_blocked_reason

        self.assertEqual(self.light_area.shopfloor_posting_mode, MODE_LEGACY)
        self.assertIsNone(plus_one_blocked_reason(self.pr_line))

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        rv = client.post(
            f'/warehouses/direction-areas/{self.light_area.id}/shopfloor-mode',
            data={'shopfloor_posting_mode': MODE_SHIFT_ONLY},
            follow_redirects=False,
        )
        self.assertEqual(rv.status_code, 302)
        area = WarehouseStorageArea.query.get(self.light_area.id)
        self.assertEqual(area.shopfloor_posting_mode, MODE_LEGACY)
        self.assertIsNone(plus_one_blocked_reason(self.pr_line))

        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin.id)
            sess['_fresh'] = True
        rv_admin = client.post(
            f'/warehouses/direction-areas/{self.light_area.id}/shopfloor-mode',
            data={'shopfloor_posting_mode': MODE_SHIFT_ONLY},
            follow_redirects=False,
        )
        self.assertEqual(rv_admin.status_code, 302)
        area = WarehouseStorageArea.query.get(self.light_area.id)
        self.assertEqual(area.shopfloor_posting_mode, MODE_SHIFT_ONLY)

    def test_sequential_same_item_shortage_requires_senior(self):
        """Finding 2: две строки одного COMPONENT 6+6 при книге 8 → нехватка 4 до любых issue."""
        from models import InventoryOperation, ProductionShift, ProductionShiftMaterial
        from shift_posting import ShiftPostingError

        for row in list(self.shift.materials.all()):
            self.db.session.delete(row)
        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.shift.id, item_id=self.sheet.id, qty_fact=Decimal('6'), unit='шт',
        ))
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.shift.id, item_id=self.sheet.id, qty_fact=Decimal('6'), unit='шт',
        ))
        self.db.session.commit()

        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=False)
        self.assertIn('нехватка', str(ctx.exception).lower())
        self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('8'))
        self.assertEqual(
            InventoryOperation.query.filter_by(operation_type='production_issue', status='posted').count(),
            0,
        )

        self._post(senior=True, actor_senior=True)
        self.db.session.commit()
        self.assertEqual(_qty(self.factory.id, self.sheet.id, self.light_area.id), Decimal('0'))
        issues = InventoryOperation.query.filter_by(operation_type='production_issue', status='posted').all()
        shortages = InventoryOperation.query.filter_by(operation_type='production_shortage', status='posted').all()
        issued_total = sum(
            sum(Decimal(str(line.quantity)) for line in op.lines.all()) for op in issues
        )
        shortage_total = sum(
            sum(Decimal(str(line.quantity)) for line in op.lines.all()) for op in shortages
        )
        self.assertEqual(issued_total, Decimal('8'))
        self.assertEqual(shortage_total, Decimal('4'))
        self.assertEqual(len(issues), 2)
        self.assertEqual(len(shortages), 1)

    def test_output_missing_item_rejected(self):
        """Finding 3: выпуск без номенклатуры — ShiftPostingError, без ProducedUnit."""
        from models import ProducedUnit, ProductionShift, ProductionShiftOutput
        from shift_posting import ShiftPostingError

        for row in list(self.shift.materials.all()):
            self.db.session.delete(row)
        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=None,
            output_type='trailer',
            quantity=Decimal('1'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=False)
        self.assertIn('номенклатур', str(ctx.exception).lower())
        self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)

    def test_cargo_trailer_on_light_direction_rejected(self):
        """Finding 3: грузовой TRAILER на DIR_LIGHT — отклонение."""
        from models import ProducedUnit, ProductionShift, ProductionShiftOutput
        from shift_posting import ShiftPostingError

        for row in list(self.shift.materials.all()):
            self.db.session.delete(row)
        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.cargo_item.id,
            output_type='trailer',
            quantity=Decimal('1'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=False)
        message = str(ctx.exception).lower()
        self.assertIn('не совпадает', message)
        self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)

    def test_request_line_wrong_direction_rejected(self):
        """Finding 3: production_request_line грузового направления на light-смене — отказ."""
        from models import (
            ProducedUnit,
            ProductionRequest,
            ProductionRequestLine,
            ProductionShift,
            ProductionShiftOutput,
            SupplyNeed,
        )
        from shift_posting import ShiftPostingError

        cargo_need = SupplyNeed(
            need_type='CUSTOMER_ORDER',
            status='IN_PRODUCTION',
            item_id=self.cargo_item.id,
            warehouse_id=self.cargo_sales.id,
            quantity=1,
        )
        self.db.session.add(cargo_need)
        self.db.session.flush()
        cargo_pr = ProductionRequest(
            request_number='PR-CARGO-DIR',
            status='in_progress',
            target_warehouse_id=self.cargo_sales.id,
        )
        self.db.session.add(cargo_pr)
        self.db.session.flush()
        cargo_line = ProductionRequestLine(
            production_request_id=cargo_pr.id,
            supply_need_id=cargo_need.id,
            item_id=self.cargo_item.id,
            production_workshop_id=self.workshop.id,
            assembly_warehouse_id=self.factory.id,
            quantity=1,
            produced_qty=0,
            status='in_production',
        )
        self.db.session.add(cargo_line)
        self.db.session.flush()

        for row in list(self.shift.materials.all()):
            self.db.session.delete(row)
        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        # Light trailer output wired to a cargo request line (item_id mismatch path).
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.trailer_model.id,
            production_request_line_id=cargo_line.id,
            output_type='trailer',
            quantity=Decimal('1'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=False)
        message = str(ctx.exception).lower()
        self.assertTrue('не совпадает' in message or 'заявк' in message)
        self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)

    def test_fractional_trailer_qty_rejected(self):
        """Finding 4: TRAILER qty=2.5 → ShiftPostingError без усечения; целое 2 проходит."""
        from models import ProducedUnit, ProductionShift, ProductionShiftOutput
        from shift_posting import ShiftPostingError

        for row in list(self.shift.materials.all()):
            self.db.session.delete(row)
        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.trailer_model.id,
            output_type='trailer',
            quantity=Decimal('2.5'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()
        with self.assertRaises(ShiftPostingError) as ctx:
            self._post(senior=False)
        self.assertIn('цел', str(ctx.exception).lower())
        self.db.session.rollback()
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)

        for row in list(self.shift.outputs.all()):
            self.db.session.delete(row)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=self.trailer_model.id,
            output_type='trailer',
            quantity=Decimal('2'),
            defect_quantity=Decimal('0'),
            unit='шт',
        ))
        self.db.session.commit()
        self._post(senior=False)
        self.db.session.commit()
        self.assertEqual(ProducedUnit.query.count(), 2)
        self.assertEqual(ProductionShift.query.get(self.shift.id).status, 'posted')


if __name__ == '__main__':
    unittest.main()
