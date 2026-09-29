#!/usr/bin/env python3
"""Отчёт директора по сменам. Только временная sqlite, не live DB."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

# Скаляры, которые складывали разные Item или разные единицы.
_FORBIDDEN_QUANTITY_KEYS = frozenset({
    'defect_qty',
    'good_parts',
    'legacy_plus_one_issue_qty',
    'overlap_issue_qty',
    'material_fact_by_zone',
    'other_zone_fact',
})


class ShiftDirectorReportTests(unittest.TestCase):
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
        )
        self.light_sales = Warehouse(
            name='Реализация легковые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            primary_product_category='light_trailer',
        )
        self.cargo_sales = Warehouse(
            name='Реализация грузовые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
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
            product_category='light_trailer',
            shopfloor_posting_mode='shift_only',
        )
        self.cargo_area = WarehouseStorageArea(
            warehouse_id=self.factory.id,
            code='DIR_CARGO',
            name='Грузовые ТМЦ',
            area_type='direction_stock',
            is_active=True,
            product_category='cargo_trailer',
            shopfloor_posting_mode='shift_only',
        )
        self.db.session.add_all([self.light_area, self.cargo_area])
        self.db.session.flush()

        self.light_cat = ProductCategory(code='light_trailer', name='Легковые', kind='goods', sort_order=1)
        self.cargo_cat = ProductCategory(code='cargo_trailer', name='Грузовые', kind='goods', sort_order=2)
        self.db.session.add_all([self.light_cat, self.cargo_cat])
        self.db.session.flush()

        self.sheet = Item(item_type='COMPONENT', article='M', name='Лист 2 мм', unit='шт')
        self.frame = Item(item_type='COMPONENT', article='P', name='Рама 2500', unit='шт')
        self.light_item = Item(
            item_type='TRAILER', article='T', name='Прицеп T', unit='шт',
            product_category_id=self.light_cat.id,
        )
        self.cargo_item = Item(
            item_type='TRAILER', article='CARGO', name='Грузовой', unit='шт',
            product_category_id=self.cargo_cat.id,
        )
        self.db.session.add_all([self.sheet, self.frame, self.light_item, self.cargo_item])
        self.db.session.flush()

        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id, storage_area_id=self.light_area.id,
            item_id=self.sheet.id, quantity=Decimal('8'), unit='шт',
        ))
        self.db.session.add(InventoryBalance(
            warehouse_id=self.factory.id, storage_area_id=self.cargo_area.id,
            item_id=self.sheet.id, quantity=Decimal('10'), unit='шт',
        ))

        self.workshop = ProductionWorkshop(code='assembly', name='Сборка', workshop_type='trailer')
        self.db.session.add(self.workshop)
        self.admin = User(username='admin', full_name='Админ', role='admin')
        self.admin.set_password('x')
        self.director = User(username='director', full_name='Директор', role='director')
        self.director.set_password('x')
        self.db.session.add_all([self.admin, self.director])
        self.db.session.flush()
        self.employee = ProductionEmployee(
            user_id=self.admin.id, full_name='Админ', employee_code='A1', is_active=True,
        )
        self.db.session.add(self.employee)
        self.db.session.flush()

        need = SupplyNeed(
            need_type='CUSTOMER_ORDER', status='IN_PRODUCTION',
            item_id=self.light_item.id, warehouse_id=self.light_sales.id, quantity=1,
        )
        self.db.session.add(need)
        self.db.session.flush()
        pr = ProductionRequest(
            request_number='PR-000001', status='in_progress',
            target_warehouse_id=self.light_sales.id,
        )
        self.db.session.add(pr)
        self.db.session.flush()
        self.pr_line = ProductionRequestLine(
            production_request_id=pr.id, supply_need_id=need.id, item_id=self.light_item.id,
            production_workshop_id=self.workshop.id, assembly_warehouse_id=self.factory.id,
            quantity=1, produced_qty=0, status='in_production',
        )
        self.db.session.add(self.pr_line)
        self.db.session.flush()

        self.light_shift = self._draft_shift('assembly', self.light_area.id)
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=self.sheet.id, qty_fact=Decimal('12'), unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.light_shift.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=self.frame.id, output_type='component', quantity=Decimal('5'),
            defect_quantity=Decimal('1'), unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.light_shift.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=self.light_item.id, production_request_line_id=self.pr_line.id,
            output_type='trailer', quantity=Decimal('2'), defect_quantity=Decimal('0'), unit='шт',
        ))

        self.cargo_shift = self._draft_shift('welding', self.cargo_area.id)
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.cargo_shift.id, item_id=self.sheet.id, qty_fact=Decimal('3'), unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.cargo_shift.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=self.cargo_item.id, output_type='trailer', quantity=Decimal('1'),
            defect_quantity=Decimal('2'), unit='шт',
        ))

        closed = ProductionShift(
            employee_id=self.employee.id, user_id=self.admin.id, workshop_id=self.workshop.id,
            work_area='assembly', status='closed', hours_fact=Decimal('99'),
            direction_warehouse_id=self.factory.id, direction_area_id=self.light_area.id,
            started_at=datetime.utcnow(), ended_at=datetime.utcnow(),
        )
        self.db.session.add(closed)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=closed.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=self.light_item.id, output_type='trailer', quantity=Decimal('9'),
            defect_quantity=Decimal('7'), unit='шт', status='posted',
        ))

        opened = self._draft_shift('paint', self.light_area.id)
        self.db.session.add(ProductionShiftMaterial(
            shift_id=opened.id, item_id=self.sheet.id, qty_fact=Decimal('50'), unit='шт', status='draft',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=opened.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=self.frame.id, output_type='component', quantity=Decimal('40'),
            defect_quantity=Decimal('6'), unit='шт', status='accepted',
        ))
        self.db.session.commit()

    def _draft_shift(self, work_area: str, area_id: int):
        from models import ProductionShift

        shift = ProductionShift(
            employee_id=self.employee.id, user_id=self.admin.id, workshop_id=self.workshop.id,
            work_area=work_area, status='open',
            direction_warehouse_id=self.factory.id, direction_area_id=area_id,
        )
        self.db.session.add(shift)
        self.db.session.flush()
        return shift

    def _post(self, shift, area_id, hours):
        from shift_posting import post_shift

        return post_shift(
            shift=shift,
            direction_area_id=area_id,
            hours_fact=hours,
            senior_confirmed=True,
            actor_is_senior=True,
            created_by_user_id=self.admin.id,
        )

    def _add_legacy_plus_one(self):
        from models import InventoryOperation, InventoryOperationLine, ProducedUnit

        unit = ProducedUnit(
            production_request_line_id=self.pr_line.id,
            item_id=self.light_item.id,
            shift_output_id=None,
            status='produced_no_vin',
            produced_at=datetime.utcnow(),
            note='старый +1',
        )
        self.db.session.add(unit)
        self.db.session.flush()
        operation = InventoryOperation(
            operation_type='production_issue',
            status='posted',
            source_warehouse_id=self.factory.id,
            source_area_id=self.light_area.id,
            produced_unit_id=unit.id,
            shift_material_id=None,
            posted_at=datetime.utcnow(),
            comment='Списание по выпуску +1',
        )
        self.db.session.add(operation)
        self.db.session.flush()
        self.db.session.add(InventoryOperationLine(
            operation_id=operation.id, item_id=self.sheet.id,
            quantity=Decimal('7'), unit='шт', direction='out',
        ))
        self.db.session.commit()
        return unit

    def _report(self):
        from shift_director_report import build_shift_director_report

        return build_shift_director_report()

    def _assert_no_mixed_item_quantity(self, report):
        """В ответе нет общего количества разных номенклатур или единиц."""

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key, _FORBIDDEN_QUANTITY_KEYS, path)
                    if key in ('qty_fact', 'qty_issued', 'qty_shortage', 'quantity'):
                        self.assertIn('item_id', node, path)
                        self.assertIsNotNone(node['item_id'], path)
                        self.assertTrue(node.get('unit'), path)
                    walk(value, f'{path}.{key}')
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f'{path}[{index}]')

        walk(report, 'report')
        for row in report['area_rows']:
            self.assertNotIn('defect_qty', row)
        self.assertEqual(report['sold_from_produced']['status'], 'BLOCKED')
        self.assertIsNone(report['sold_from_produced']['value'])

    def test_directions_keep_shift_output_apart_from_legacy_and_closed(self):
        self._post(self.light_shift, self.light_area.id, '8')
        self._post(self.cargo_shift, self.cargo_area.id, '4')
        self.db.session.commit()
        self._add_legacy_plus_one()

        report = self._report()
        totals = report['totals']
        self.assertEqual(totals['good_trailers'], 3)
        self.assertEqual(totals['good_trailer_declared_qty'], Decimal('3'))
        self.assertEqual(totals['good_trailer_unit_gap'], Decimal('0'))
        self.assertNotIn('good_parts', totals)
        self.assertEqual(
            {(line['item_article'], line['unit']): line['qty'] for line in totals['good_part_lines']},
            {('P', 'шт'): Decimal('5')},
        )
        self.assertEqual(totals['good_part_lines'][0]['item_name'], 'Рама 2500')
        self.assertNotIn('defect_qty', totals)
        self.assertEqual(totals['defect_trailer_lines'], [{'unit': 'шт', 'qty': Decimal('2')}])
        self.assertEqual(len(totals['defect_part_lines']), 1)
        defect_part = totals['defect_part_lines'][0]
        self.assertEqual(defect_part['item_article'], 'P')
        self.assertEqual(defect_part['item_name'], 'Рама 2500')
        self.assertEqual(defect_part['unit'], 'шт')
        self.assertEqual(defect_part['qty'], Decimal('1'))
        self.assertEqual(totals['defect_other_lines'], [])
        self.assertEqual(totals['hours'], Decimal('12'))
        self.assertEqual(totals['legacy_plus_one_trailers'], 1)
        self.assertNotIn('legacy_plus_one_issue_qty', totals)
        self.assertNotIn('overlap_issue_qty', totals)
        self.assertEqual(len(report['legacy_issue_rows']), 1)
        self.assertEqual(report['legacy_issue_rows'][0]['quantity'], Decimal('7'))
        self.assertEqual(report['legacy_issue_rows'][0]['unit'], 'шт')
        self.assertEqual(totals['overlap_units'], 0)
        self.assertEqual(totals['excluded_closed_shifts'], 1)
        self.assertEqual(totals['excluded_open_shifts'], 1)
        self.assertEqual(report['sold_from_produced']['status'], 'BLOCKED')
        self.assertIsNone(report['sold_from_produced']['value'])

        by_key = {(row['work_area'], row['direction_code']): row for row in report['area_rows']}
        light = by_key[('assembly', 'DIR_LIGHT')]
        cargo = by_key[('welding', 'DIR_CARGO')]
        self.assertEqual(light['good_trailers'], 2)
        self.assertEqual(
            {(line['item_article'], line['unit']): line['qty'] for line in light['good_part_lines']},
            {('P', 'шт'): Decimal('5')},
        )
        self.assertNotIn('defect_qty', light)
        self.assertEqual(
            {(line['item_article'], line['unit']): line['qty'] for line in light['defect_part_lines']},
            {('P', 'шт'): Decimal('1')},
        )
        self.assertEqual(light['defect_trailer_lines'], [])
        self.assertEqual(light['hours'], Decimal('8'))
        self.assertEqual(cargo['good_trailers'], 1)
        self.assertEqual(cargo['good_part_lines'], [])
        self.assertNotIn('defect_qty', cargo)
        self.assertEqual(cargo['defect_part_lines'], [])
        self.assertEqual(cargo['defect_trailer_lines'], [{'unit': 'шт', 'qty': Decimal('2')}])
        self.assertEqual(cargo['hours'], Decimal('4'))
        self.assertNotIn(('assembly', 'DIR_LIGHT_CLOSED'), by_key)
        self.assertNotIn(('paint', 'DIR_LIGHT'), by_key)

        self.assertNotIn('material_fact_by_zone', report)
        self.assertNotIn('other_zone_fact', report)
        materials = {
            (row['direction_code'], row['item_article'], row['unit']): row
            for row in report['material_rows']
        }
        light_sheet = materials[('DIR_LIGHT', 'M', 'шт')]
        self.assertEqual(light_sheet['item_id'], self.sheet.id)
        self.assertEqual(light_sheet['qty_fact'], Decimal('12'))
        self.assertEqual(light_sheet['qty_issued'], Decimal('8'))
        self.assertEqual(light_sheet['qty_shortage'], Decimal('4'))
        cargo_sheet = materials[('DIR_CARGO', 'M', 'шт')]
        self.assertEqual(cargo_sheet['qty_fact'], Decimal('3'))
        self.assertEqual(cargo_sheet['qty_issued'], Decimal('3'))
        self.assertEqual(cargo_sheet['qty_shortage'], Decimal('0'))
        self.assertEqual(len(report['material_rows']), 2)
        self._assert_no_mixed_item_quantity(report)

    def test_repeat_post_does_not_duplicate_report(self):
        from shift_posting import ShiftAlreadyPosted

        self._post(self.light_shift, self.light_area.id, '8')
        self._post(self.cargo_shift, self.cargo_area.id, '4')
        self.db.session.commit()
        before = self._report()
        with self.assertRaises(ShiftAlreadyPosted):
            self._post(self.light_shift, self.light_area.id, '8')
        self.db.session.rollback()
        after = self._report()
        self.assertEqual(before['totals'], after['totals'])
        self.assertNotIn('material_fact_by_zone', before)
        self.assertNotIn('material_fact_by_zone', after)
        self.assertEqual(before['area_rows'], after['area_rows'])
        self.assertEqual(before['material_rows'], after['material_rows'])
        self.assertEqual(after['totals']['good_trailers'], 3)
        light = [row for row in after['material_rows'] if row['direction_code'] == 'DIR_LIGHT']
        self.assertEqual(len(light), 1)
        self.assertEqual(light[0]['item_id'], self.sheet.id)
        self.assertEqual(light[0]['qty_fact'], Decimal('12'))
        self._assert_no_mixed_item_quantity(after)

    def test_draft_material_on_posted_shift_is_not_fact(self):
        from models import ProductionShiftMaterial

        self._post(self.light_shift, self.light_area.id, '8')
        self.db.session.commit()
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=self.sheet.id,
            qty_fact=Decimal('100'), unit='шт', status='draft',
        ))
        self.db.session.commit()
        report = self._report()
        light = [row for row in report['material_rows'] if row['direction_code'] == 'DIR_LIGHT']
        self.assertEqual(len(light), 1)
        self.assertEqual(light[0]['item_id'], self.sheet.id)
        self.assertEqual(light[0]['unit'], 'шт')
        self.assertEqual(light[0]['qty_fact'], Decimal('12'))
        self.assertNotIn('material_fact_by_zone', report)
        self.assertEqual(report['totals']['good_trailers'], 2)
        self.assertEqual(
            {(line['item_article'], line['unit']): line['qty'] for line in report['totals']['good_part_lines']},
            {('P', 'шт'): Decimal('5')},
        )

    def test_sold_from_produced_stays_blocked_when_trailer_matches_realization(self):
        from models import Customer, ProducedUnit, SalesRealization, SalesRealizationLine, Trailer

        self._post(self.light_shift, self.light_area.id, '8')
        self.db.session.commit()
        unit = ProducedUnit.query.filter(ProducedUnit.shift_output_id.isnot(None)).first()
        self.assertIsNone(unit.trailer_id)
        customer = Customer(customer_type='PERSON', name='Покупатель')
        self.db.session.add(customer)
        self.db.session.flush()
        trailer = Trailer(
            vin='VINBLOCKED0000001', item_id=self.light_item.id,
            warehouse_id=self.light_sales.id, status='SOLD',
        )
        self.db.session.add(trailer)
        self.db.session.flush()
        unit.trailer_id = trailer.id
        realization = SalesRealization(
            customer_id=customer.id, status='posted', realization_date=date.today(),
            total_amount=Decimal('0'),
        )
        self.db.session.add(realization)
        self.db.session.flush()
        self.db.session.add(SalesRealizationLine(
            realization_id=realization.id, line_no=1, line_type='TRAILER',
            trailer_id=trailer.id, item_id=self.light_item.id,
            quantity=Decimal('1'), inventory_effect='trailer_unit',
        ))
        self.db.session.commit()

        report = self._report()
        sold = report['sold_from_produced']
        self.assertEqual(sold['status'], 'BLOCKED')
        self.assertIsNone(sold['value'])
        self.assertIn('produced_unit_id', sold['reason'])
        self.assertTrue(sold['paths'])
        self.assertNotIn('sold_count', report)
        self.assertEqual(report['totals']['good_trailers'], 2)

    def test_shifts_page_shows_blocked_metric(self):
        self._post(self.light_shift, self.light_area.id, '8')
        self._post(self.cargo_shift, self.cargo_area.id, '4')
        self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        response = client.get('/director/reports/shifts')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('BLOCKED', body)
        self.assertIn('DIR_LIGHT', body)
        self.assertIn('DIR_CARGO', body)
        self.assertIn('Продано из выпущенных', body)
        self.assertIn('produced_unit_id', body)
        self.assertIn('Брак прицепов', body)
        self.assertIn('Брак деталей', body)
        self.assertNotIn('Факт DIR_LIGHT', body)
        self.assertNotIn('Факт DIR_CARGO', body)
        self.assertNotRegex(body, r'<th>Брак</th>')

    def _seed_mixed_unit_consumption(self):
        from models import Item, ProductionShiftMaterial, ProductionShiftOutput

        plate = Item(item_type='COMPONENT', article='SHEET', name='Лист кг', unit='кг')
        bolt = Item(item_type='COMPONENT', article='BOLT', name='Крепеж', unit='шт')
        self.db.session.add_all([plate, bolt])
        self.db.session.flush()
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=plate.id,
            qty_fact=Decimal('8'), unit='кг',
        ))
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=bolt.id,
            qty_fact=Decimal('5'), unit='шт',
        ))
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.light_shift.id, employee_id=self.employee.id, workshop_id=self.workshop.id,
            item_id=plate.id, output_type='component', quantity=Decimal('0'),
            defect_quantity=Decimal('4'), unit='кг',
        ))
        self.db.session.flush()
        self._post(self.light_shift, self.light_area.id, '8')
        self._post(self.cargo_shift, self.cargo_area.id, '4')
        self.db.session.commit()
        self._add_legacy_material_lines([
            (plate, Decimal('9'), 'кг'),
            (bolt, Decimal('6'), 'шт'),
        ])
        return plate, bolt

    def _add_legacy_material_lines(self, lines):
        from models import InventoryOperation, InventoryOperationLine, ProducedUnit

        unit = ProducedUnit(
            production_request_line_id=self.pr_line.id,
            item_id=self.light_item.id,
            shift_output_id=None,
            status='produced_no_vin',
            produced_at=datetime.utcnow(),
            note='старый +1, разные единицы',
        )
        self.db.session.add(unit)
        self.db.session.flush()
        operation = InventoryOperation(
            operation_type='production_issue',
            status='posted',
            source_warehouse_id=self.factory.id,
            source_area_id=self.light_area.id,
            produced_unit_id=unit.id,
            shift_material_id=None,
            posted_at=datetime.utcnow(),
            comment='Списание по выпуску +1, разные единицы',
        )
        self.db.session.add(operation)
        self.db.session.flush()
        for item, qty, unit_code in lines:
            self.db.session.add(InventoryOperationLine(
                operation_id=operation.id, item_id=item.id,
                quantity=qty, unit=unit_code, direction='out',
            ))
        self.db.session.commit()

    def test_two_materials_with_different_units_stay_separate(self):
        self._seed_mixed_unit_consumption()
        report = self._report()
        light_rows = {
            (row['item_article'], row['unit']): row
            for row in report['material_rows']
            if row['direction_code'] == 'DIR_LIGHT'
        }
        plate = light_rows[('SHEET', 'кг')]
        bolt = light_rows[('BOLT', 'шт')]
        self.assertEqual(plate['qty_fact'], Decimal('8'))
        self.assertEqual(plate['qty_issued'], Decimal('0'))
        self.assertEqual(plate['qty_shortage'], Decimal('8'))
        self.assertEqual(bolt['qty_fact'], Decimal('5'))
        self.assertEqual(bolt['qty_issued'], Decimal('0'))
        self.assertEqual(bolt['qty_shortage'], Decimal('5'))
        for row in report['material_rows']:
            self.assertTrue(row['unit'])
            self.assertIsNotNone(row['item_id'])
            self.assertNotEqual(row['qty_fact'], Decimal('13'))
            self.assertNotEqual(row['qty_fact'], Decimal('25'))
        self.assertNotIn('material_fact_by_zone', report)
        self.assertNotIn('other_zone_fact', report)
        self.assertNotIn('defect_qty', report['totals'])
        self.assertNotIn('legacy_plus_one_issue_qty', report['totals'])
        self._assert_no_mixed_item_quantity(report)

        legacy = {(row['item_article'], row['unit']): row for row in report['legacy_issue_rows']}
        self.assertEqual(legacy[('SHEET', 'кг')]['quantity'], Decimal('9'))
        self.assertEqual(legacy[('BOLT', 'шт')]['quantity'], Decimal('6'))
        for row in report['legacy_issue_rows']:
            self.assertNotEqual(row['quantity'], Decimal('15'))

        self.assertEqual(
            report['totals']['defect_trailer_lines'],
            [{'unit': 'шт', 'qty': Decimal('2')}],
        )
        part_lines = {
            (line['item_article'], line['unit']): line
            for line in report['totals']['defect_part_lines']
        }
        self.assertEqual(part_lines[('SHEET', 'кг')]['qty'], Decimal('4'))
        self.assertEqual(part_lines[('SHEET', 'кг')]['item_name'], 'Лист кг')
        self.assertEqual(part_lines[('P', 'шт')]['qty'], Decimal('1'))
        self.assertEqual(part_lines[('P', 'шт')]['item_name'], 'Рама 2500')
        self.assertEqual(len(part_lines), 2)
        by_key = {(row['work_area'], row['direction_code']): row for row in report['area_rows']}
        light_parts = {
            (line['item_article'], line['unit']): line['qty']
            for line in by_key[('assembly', 'DIR_LIGHT')]['defect_part_lines']
        }
        self.assertEqual(light_parts, {('SHEET', 'кг'): Decimal('4'), ('P', 'шт'): Decimal('1')})
        self.assertEqual(by_key[('assembly', 'DIR_LIGHT')]['defect_trailer_lines'], [])
        self.assertEqual(
            by_key[('welding', 'DIR_CARGO')]['defect_trailer_lines'],
            [{'unit': 'шт', 'qty': Decimal('2')}],
        )

    def test_shifts_page_does_not_show_false_quantity_sum(self):
        self._seed_mixed_unit_consumption()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        response = client.get('/director/reports/shifts')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('8 кг', body)
        self.assertIn('5 шт', body)
        self.assertIn('9 кг', body)
        self.assertIn('6 шт', body)
        self.assertIn('4 кг', body)
        self.assertIn('1 шт', body)
        self.assertIn('Лист кг', body)
        self.assertIn('Крепеж', body)
        for forbidden in ('13 кг', '13 шт', '15 кг', '15 шт', '25 кг', '25 шт', '7 кг', '7 шт'):
            self.assertNotIn(forbidden, body)
        self.assertNotRegex(body, r'списание\s+\d')
        self.assertNotIn('Факт DIR_LIGHT', body)
        self.assertNotIn('Факт DIR_CARGO', body)
        self.assertNotRegex(body, r'<th>Брак</th>')
        cards = {
            match.group('title').strip(): match
            for match in re.finditer(
                r'<div class="small text-muted">(?P<title>[^<]*)</div>\s*'
                r'<div class="fw-bold">(?P<value>[^<]*)</div>\s*'
                r'<div class="small text-muted">(?P<caption>[^<]*)</div>',
                body,
            )
        }
        self.assertEqual(cards['Расход материалов'].group('value').strip(), 'по номенклатуре')
        self.assertNotRegex(cards['Старые +1'].group('caption'), r'\d')
        self.assertEqual(cards['Брак прицепов'].group('value').strip(), '2 шт')
        self.assertEqual(
            cards['Брак деталей'].group('value').strip(),
            'P Рама 2500 1 шт; SHEET Лист кг 4 кг',
        )
        self.assertEqual(cards['Годные детали'].group('value').strip(), 'P Рама 2500 5 шт')
        self.assertNotIn('Брак', cards)
        for card in cards.values():
            value = card.group('value')
            caption = card.group('caption')
            for forbidden in ('13', '15', '25'):
                self.assertNotRegex(value, rf'(?<!\d){forbidden}(?!\d)')
                self.assertNotRegex(caption, rf'(?<!\d){forbidden}(?!\d)')

    def _post_both(self):
        self._post(self.light_shift, self.light_area.id, '8')
        self._post(self.cargo_shift, self.cargo_area.id, '4')
        self.db.session.commit()

    def _add_component_output(self, article, name, unit, good_qty, defect_qty):
        from models import Item, ProductionShiftOutput

        item = Item(item_type='COMPONENT', article=article, name=name, unit=unit)
        self.db.session.add(item)
        self.db.session.flush()
        self.db.session.add(ProductionShiftOutput(
            shift_id=self.light_shift.id,
            employee_id=self.employee.id,
            workshop_id=self.workshop.id,
            item_id=item.id,
            output_type='component',
            quantity=good_qty,
            defect_quantity=defect_qty,
            unit=unit,
        ))
        self.db.session.flush()
        return item

    def _lines_by_article_unit(self, lines):
        return {(line['item_article'], line['unit']): line for line in lines}

    def test_two_components_with_different_units_are_not_summed(self):
        plate = self._add_component_output('SHEET', 'Лист кг', 'кг', Decimal('4'), Decimal('2'))
        bolt = self._add_component_output('BOLT', 'Крепеж', 'шт', Decimal('3'), Decimal('1'))
        self._post_both()

        report = self._report()
        totals = report['totals']
        self.assertEqual(totals['good_trailers'], 3)
        self.assertEqual(totals['hours'], Decimal('12'))
        self.assertEqual(totals['defect_trailer_lines'], [{'unit': 'шт', 'qty': Decimal('2')}])
        self.assertNotIn('good_parts', totals)

        good = self._lines_by_article_unit(totals['good_part_lines'])
        self.assertEqual(good[('SHEET', 'кг')]['qty'], Decimal('4'))
        self.assertEqual(good[('SHEET', 'кг')]['item_id'], plate.id)
        self.assertEqual(good[('SHEET', 'кг')]['item_name'], 'Лист кг')
        self.assertEqual(good[('BOLT', 'шт')]['qty'], Decimal('3'))
        self.assertEqual(good[('BOLT', 'шт')]['item_id'], bolt.id)
        self.assertEqual(good[('P', 'шт')]['qty'], Decimal('5'))
        self.assertEqual(len(good), 3)
        for line in totals['good_part_lines']:
            self.assertNotIn(line['qty'], (Decimal('7'), Decimal('8'), Decimal('9'), Decimal('12')))

        defects = self._lines_by_article_unit(totals['defect_part_lines'])
        self.assertEqual(defects[('SHEET', 'кг')]['qty'], Decimal('2'))
        self.assertEqual(defects[('BOLT', 'шт')]['qty'], Decimal('1'))
        self.assertEqual(defects[('P', 'шт')]['qty'], Decimal('1'))
        self.assertEqual(len(defects), 3)
        for line in totals['defect_part_lines']:
            self.assertNotEqual(line['qty'], Decimal('7'))

        by_key = {(row['work_area'], row['direction_code']): row for row in report['area_rows']}
        light_good = self._lines_by_article_unit(by_key[('assembly', 'DIR_LIGHT')]['good_part_lines'])
        self.assertEqual(light_good[('SHEET', 'кг')]['qty'], Decimal('4'))
        self.assertEqual(light_good[('BOLT', 'шт')]['qty'], Decimal('3'))
        self.assertEqual(by_key[('welding', 'DIR_CARGO')]['good_part_lines'], [])
        self.assertNotIn('defect_qty', report['totals'])
        self.assertNotIn('defect_qty', by_key[('assembly', 'DIR_LIGHT')])
        self._assert_no_mixed_item_quantity(report)

    def test_two_components_with_same_unit_are_not_summed(self):
        from models import ProductionShiftMaterial

        hinge = self._add_component_output('HINGE', 'Петля', 'шт', Decimal('4'), Decimal('6'))
        latch = self._add_component_output('LATCH', 'Защёлка', 'шт', Decimal('3'), Decimal('2'))
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=hinge.id, qty_fact=Decimal('4'), unit='шт',
        ))
        self.db.session.add(ProductionShiftMaterial(
            shift_id=self.light_shift.id, item_id=latch.id, qty_fact=Decimal('3'), unit='шт',
        ))
        self.db.session.flush()
        self._post_both()

        report = self._report()
        good = self._lines_by_article_unit(report['totals']['good_part_lines'])
        self.assertEqual(good[('HINGE', 'шт')]['qty'], Decimal('4'))
        self.assertEqual(good[('HINGE', 'шт')]['item_name'], 'Петля')
        self.assertEqual(good[('HINGE', 'шт')]['item_id'], hinge.id)
        self.assertEqual(good[('LATCH', 'шт')]['qty'], Decimal('3'))
        self.assertEqual(good[('LATCH', 'шт')]['item_name'], 'Защёлка')
        self.assertEqual(good[('LATCH', 'шт')]['item_id'], latch.id)
        self.assertEqual(good[('P', 'шт')]['qty'], Decimal('5'))
        same_unit = [line for line in report['totals']['good_part_lines'] if line['unit'] == 'шт']
        self.assertEqual(len(same_unit), 3)
        for line in same_unit:
            self.assertNotEqual(line['qty'], Decimal('7'))
            self.assertNotEqual(line['qty'], Decimal('12'))

        defects = self._lines_by_article_unit(report['totals']['defect_part_lines'])
        self.assertEqual(defects[('HINGE', 'шт')]['item_name'], 'Петля')
        self.assertEqual(defects[('HINGE', 'шт')]['qty'], Decimal('6'))
        self.assertEqual(defects[('LATCH', 'шт')]['item_name'], 'Защёлка')
        self.assertEqual(defects[('LATCH', 'шт')]['qty'], Decimal('2'))
        self.assertEqual(defects[('P', 'шт')]['qty'], Decimal('1'))
        defect_pieces = [line for line in report['totals']['defect_part_lines'] if line['unit'] == 'шт']
        self.assertEqual(len(defect_pieces), 3)
        for line in defect_pieces:
            self.assertNotIn(line['qty'], (Decimal('7'), Decimal('8'), Decimal('9')))

        self.assertEqual(report['totals']['good_trailers'], 3)
        self.assertEqual(report['totals']['hours'], Decimal('12'))
        self.assertEqual(report['totals']['defect_trailer_lines'], [{'unit': 'шт', 'qty': Decimal('2')}])
        self.assertNotIn('defect_qty', report['totals'])
        for row in report['area_rows']:
            self.assertNotIn('defect_qty', row)

        light_materials = {
            (row['item_article'], row['unit']): row
            for row in report['material_rows']
            if row['direction_code'] == 'DIR_LIGHT'
        }
        self.assertEqual(light_materials[('HINGE', 'шт')]['qty_fact'], Decimal('4'))
        self.assertEqual(light_materials[('HINGE', 'шт')]['item_id'], hinge.id)
        self.assertEqual(light_materials[('LATCH', 'шт')]['qty_fact'], Decimal('3'))
        self.assertEqual(light_materials[('LATCH', 'шт')]['item_id'], latch.id)
        same_unit_facts = [
            row['qty_fact'] for row in light_materials.values() if row['unit'] == 'шт'
        ]
        self.assertEqual(len(same_unit_facts), 3)
        for qty in same_unit_facts:
            self.assertNotEqual(qty, Decimal('7'))
            self.assertNotEqual(qty, Decimal('19'))
        self.assertNotIn('material_fact_by_zone', report)
        self._assert_no_mixed_item_quantity(report)

    def test_shifts_page_does_not_sum_good_parts_across_items(self):
        self._add_component_output('SHEET', 'Лист кг', 'кг', Decimal('4'), Decimal('2'))
        self._add_component_output('HINGE', 'Петля', 'шт', Decimal('3'), Decimal('6'))
        self._post_both()

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        response = client.get('/director/reports/shifts')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Лист кг', body)
        self.assertIn('Петля', body)
        self.assertIn('Рама 2500', body)
        self.assertIn('4 кг', body)
        self.assertIn('3 шт', body)
        self.assertIn('5 шт', body)
        self.assertIn('2 кг', body)
        self.assertIn('6 шт', body)
        self.assertNotIn('7 шт', body)
        self.assertNotRegex(body, r'<td>\s*12(\.0+)?\s*</td>')
        self.assertNotRegex(body, r'<td>\s*7(\.0+)?\s*</td>')

        cards = {
            match.group('title').strip(): match
            for match in re.finditer(
                r'<div class="small text-muted">(?P<title>[^<]*)</div>\s*'
                r'<div class="fw-bold">(?P<value>[^<]*)</div>\s*'
                r'<div class="small text-muted">(?P<caption>[^<]*)</div>',
                body,
            )
        }
        self.assertEqual(
            cards['Годные детали'].group('value').strip(),
            'HINGE Петля 3 шт; P Рама 2500 5 шт; SHEET Лист кг 4 кг',
        )
        self.assertEqual(
            cards['Брак деталей'].group('value').strip(),
            'HINGE Петля 6 шт; P Рама 2500 1 шт; SHEET Лист кг 2 кг',
        )
        self.assertNotIn('7', cards['Годные детали'].group('value'))
        self.assertNotIn('8', cards['Годные детали'].group('value'))
        self.assertNotIn('9', cards['Годные детали'].group('value'))
        self.assertNotIn('12', cards['Годные детали'].group('value'))
        self.assertNotIn('7 шт', cards['Брак деталей'].group('value'))
        self.assertNotIn('8 шт', cards['Брак деталей'].group('value'))
        self.assertNotIn('9 шт', cards['Брак деталей'].group('value'))
        self.assertEqual(cards['Брак прицепов'].group('value').strip(), '2 шт')
        self.assertEqual(cards['Годные прицепы'].group('value').strip(), '3')
        self.assertRegex(cards['Часы'].group('value'), r'^12(\.0+)?$')
