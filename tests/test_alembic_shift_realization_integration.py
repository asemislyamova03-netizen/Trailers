#!/usr/bin/env python3
"""Один сквозной сценарий на новой SQLite после полной цепочки Alembic.

Схема только upgrade до f6b2d8c14e90. Нет db.create_all, нет ручного ALTER
и нет выравнивания колонок. Действия идут через create_app и штатные
маршруты. Синтетические справочники и заказ — фикстуры.

Рабочая trailers.db не открывается. PRAGMA foreign_keys остаётся 0.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
SCHEMA_REVISION = 'f6b2d8c14e90'
VIN_FULL = 'XINT000000000001'
CARD_RE = re.compile(
    r'<div class="small text-muted">(?P<title>[^<]*)</div>\s*'
    r'<div class="fw-bold">(?P<value>[^<]*)</div>\s*'
    r'<div class="small text-muted">(?P<caption>[^<]*)</div>',
)


def _refuse_live(url: str, path: Path) -> None:
    resolved = path.resolve()
    normalized = url.replace('\\', '/')
    if resolved == LIVE_DB or resolved.name == 'trailers.db':
        raise RuntimeError(f'Refused to touch live DB file: {resolved}')
    if normalized.endswith('/trailers.db') or normalized.endswith('trailers.db'):
        raise RuntimeError(f'Refused to touch live DB url: {normalized}')


def _bind_app(path: Path):
    uri = 'sqlite:///' + path.as_posix()
    _refuse_live(uri, path)
    from extensions import db

    original_init_app = db.init_app

    def _init_app(app):
        app.config['SQLALCHEMY_DATABASE_URI'] = uri
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {'check_same_thread': False, 'timeout': 30},
        }
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        return original_init_app(app)

    db.init_app = _init_app
    from app import create_app

    app = create_app()
    app.config['WTF_CSRF_ENABLED'] = False
    db.init_app = original_init_app
    return app, db


def _qty(warehouse_id: int, item_id: int, storage_area_id=None) -> Decimal:
    from models import InventoryBalance

    query = InventoryBalance.query.filter_by(warehouse_id=warehouse_id, item_id=item_id)
    if storage_area_id is not None:
        query = query.filter_by(storage_area_id=storage_area_id)
    total = Decimal('0')
    for row in query.all():
        total += Decimal(row.quantity or 0)
    return total


class AlembicShiftRealizationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stat = LIVE_DB.stat() if LIVE_DB.exists() else None
        fd, name = tempfile.mkstemp(suffix='-alembic-shift-integration.sqlite')
        os.close(fd)
        cls.work = Path(name)
        _refuse_live('sqlite:///' + cls.work.as_posix(), cls.work)
        cls.app, cls.db = _bind_app(cls.work)
        with cls.app.app_context():
            engine_url = str(cls.db.engine.url).replace('\\', '/')
            _refuse_live(engine_url, cls.work)
            if 'foreign_keys' in (ROOT / 'app.py').read_text(encoding='utf-8').lower():
                raise RuntimeError('app.py must not enable PRAGMA foreign_keys')
            from flask_migrate import upgrade

            cls.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=SCHEMA_REVISION)
            cls.db.session.remove()
            cls.db.engine.dispose()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.work.unlink(missing_ok=True)
        if cls.live_stat is not None and LIVE_DB.exists():
            current = LIVE_DB.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                cls.live_stat.st_ino,
                cls.live_stat.st_size,
                cls.live_stat.st_mtime_ns,
            ):
                raise RuntimeError('live trailers.db changed during alembic integration test')

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.trigger_sql = self._trigger_sql()
        self.assertEqual(len(self.trigger_sql), 12)
        self.assertEqual(self._temp_tables(), [])

    def tearDown(self):
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_shift_vin_realization_and_late_sale_cohort_on_alembic_head(self):
        from models import (
            InventoryOperation,
            Item,
            OTTS,
            ProducedUnit,
            ProductionShift,
            SalesRealization,
            SalesRealizationLine,
            Trailer,
            WarehouseStorageArea,
        )
        from shift_director_report import SOLD_FROM_PRODUCED_LABEL, build_shift_director_report
        from shift_posting import MODE_LEGACY, MODE_SHIFT_ONLY

        self._seed()
        self.assertIsNone(Item.query.get(self.sheet_id).tent_hight_mm)
        self.assertIsNone(Item.query.get(self.sheet_id).has_jockey_wheel)
        self.assertIsNone(OTTS.query.get(self.otts_id).full_mass_kg)
        self.assertEqual(Trailer.query.count(), 0)

        self._assert_mode_boundary()
        light = WarehouseStorageArea.query.get(self.light_area_id)
        cargo = WarehouseStorageArea.query.get(self.cargo_area_id)
        self.assertEqual(light.shopfloor_posting_mode, MODE_SHIFT_ONLY)
        self.assertEqual(cargo.shopfloor_posting_mode, MODE_LEGACY)
        self.assertEqual(InventoryOperation.query.count(), 0)
        self.assertEqual(self._sheet(self.light_area_id), Decimal('8'))
        self.assertEqual(self._sheet(self.cargo_area_id), Decimal('10'))
        self.assertEqual(self._sheet(self.components_id), Decimal('8'))

        self._reject_unopened_cargo_shift()
        self._reject_wrong_direction_output()
        self.assertEqual(InventoryOperation.query.count(), 0)
        self.assertEqual(ProducedUnit.query.count(), 0)
        self.assertEqual(self._sheet(self.light_area_id), Decimal('8'))
        self.assertEqual(ProductionShift.query.filter_by(status='open').count(), 0)

        shift_id = self._post_light_shift()
        self._repeat_post_does_not_double(shift_id)
        self.assertEqual(self._sheet(self.light_area_id), Decimal('5'))
        self.assertEqual(self._frame(self.light_area_id), Decimal('4'))
        self.assertEqual(self._sheet(self.cargo_area_id), Decimal('10'))
        self.assertEqual(self._sheet(self.components_id), Decimal('8'))
        shift_units = (
            ProducedUnit.query
            .filter(ProducedUnit.shift_output_id.isnot(None))
            .order_by(ProducedUnit.id.asc())
            .all()
        )
        self.assertEqual(len(shift_units), 2)
        self.assertTrue(all(unit.status == 'produced_no_vin' and unit.trailer_id is None for unit in shift_units))
        sold_unit_id = shift_units[0].id
        unsold_unit_id = shift_units[1].id

        self._assign_vin(sold_unit_id)
        trailer = Trailer.query.filter_by(vin=VIN_FULL).one()
        self.assertIsNone(trailer.otts_id)
        self.assertEqual(Item.query.get(trailer.item_id).article, 'LIGHT-INT')
        realization = self._realize(sold_unit_id, trailer.id)
        self.assertEqual(realization.status, 'posted')
        self.assertEqual(realization.realization_date, date.today())
        self.assertGreater(realization.realization_date, date(2026, 1, 31))
        line = SalesRealizationLine.query.filter_by(realization_id=realization.id).one()
        self.assertEqual(line.produced_unit_id, sold_unit_id)
        self.assertEqual(line.inventory_effect, 'trailer_unit')
        self.assertIsNone(
            SalesRealizationLine.query.filter_by(produced_unit_id=unsold_unit_id).first()
        )
        self.assertEqual(Trailer.query.get(trailer.id).status, 'SOLD')

        # Дата проведения смены — только чтобы когорта выпуска была январём,
        # а продажа маршрута осталась сегодняшней, вне этого периода.
        posted_shift = ProductionShift.query.get(shift_id)
        posted_shift.posted_at = datetime(2026, 1, 15, 12, 0, 0)
        self.db.session.commit()

        january_start = datetime(2026, 1, 1, 0, 0, 0)
        january_end = datetime(2026, 1, 31, 23, 59, 59)
        january = build_shift_director_report(period_start=january_start, period_end=january_end)
        sold = january['sold_from_produced']
        self.assertEqual(sold['status'], 'SNAPSHOT')
        self.assertEqual(sold['label'], SOLD_FROM_PRODUCED_LABEL)
        self.assertEqual(sold['value'], 1)
        self.assertEqual(sold['cohort_units'], 2)
        self.assertIsNone(sold['sale_date_filter'])
        self.assertEqual(january['totals']['good_trailers'], 2)
        self.assertEqual(january['totals']['legacy_plus_one_trailers'], 0)
        self._assert_report_page('2026-01-01', '2026-01-31', '1')

        sale_outside = realization.realization_date
        outside_start = datetime(sale_outside.year, sale_outside.month, 1, 0, 0, 0)
        outside_end = datetime(sale_outside.year, sale_outside.month, sale_outside.day, 23, 59, 59)
        outside = build_shift_director_report(period_start=outside_start, period_end=outside_end)
        self.assertEqual(outside['sold_from_produced']['value'], 0)
        self.assertEqual(outside['sold_from_produced']['cohort_units'], 0)
        self.assertEqual(outside['totals']['good_trailers'], 0)

        self._legacy_plus_one_other_direction()
        january_after = build_shift_director_report(
            period_start=january_start,
            period_end=january_end,
        )
        self.assertEqual(january_after['sold_from_produced']['value'], 1)
        self.assertEqual(january_after['sold_from_produced']['cohort_units'], 2)
        self.assertEqual(january_after['totals']['legacy_plus_one_trailers'], 0)
        outside_after = build_shift_director_report(
            period_start=outside_start,
            period_end=outside_end,
        )
        self.assertEqual(outside_after['sold_from_produced']['value'], 0)
        self.assertEqual(outside_after['sold_from_produced']['cohort_units'], 0)
        self.assertEqual(outside_after['totals']['legacy_plus_one_trailers'], 1)
        plus_one = ProducedUnit.query.filter(ProducedUnit.shift_output_id.is_(None)).one()
        self.assertNotEqual(plus_one.id, sold_unit_id)
        self.assertIsNone(SalesRealization.query.filter_by(id=realization.id).one().lines.filter_by(
            produced_unit_id=plus_one.id
        ).first())

        self.assertEqual(self._trigger_sql(), self.trigger_sql)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertIsNone(Item.query.get(self.sheet_id).tent_hight_mm)
        self.assertIsNone(Item.query.get(self.light_item_id).has_jockey_wheel)
        self.assertIsNone(Trailer.query.get(trailer.id).otts_id)
        self.assertIsNone(OTTS.query.get(self.otts_id).full_mass_kg)

    def _seed(self):
        from models import (
            Customer,
            CustomerOrder,
            CustomerOrderLine,
            InventoryBalance,
            Item,
            ItemBillOfMaterials,
            ItemBillOfMaterialsLine,
            OTTS,
            ProductCategory,
            ProductionRequest,
            ProductionRequestLine,
            ProductionWorkshop,
            SupplyNeed,
            User,
            VinRegistry,
            Warehouse,
            WarehouseStorageArea,
        )

        factory = Warehouse(
            name='Синтетика завод',
            is_active=True,
            is_production=True,
            warehouse_kind='assembly',
            is_sales_point=False,
            can_sell=False,
            can_ship_to_customer=False,
            primary_product_category=None,
        )
        light_sales = Warehouse(
            name='Синтетика реализация легковые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='light_trailer',
        )
        cargo_sales = Warehouse(
            name='Синтетика реализация грузовые',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='cargo_trailer',
        )
        self.db.session.add_all([factory, light_sales, cargo_sales])
        self.db.session.flush()

        light_area = WarehouseStorageArea(
            warehouse_id=factory.id,
            code='DIR_LIGHT',
            name='Легковые ТМЦ',
            area_type='direction_stock',
            is_active=True,
            sort_order=210,
            product_category='light_trailer',
            shopfloor_posting_mode='legacy_plus_one',
        )
        cargo_area = WarehouseStorageArea(
            warehouse_id=factory.id,
            code='DIR_CARGO',
            name='Грузовые ТМЦ',
            area_type='direction_stock',
            is_active=True,
            sort_order=220,
            product_category='cargo_trailer',
            shopfloor_posting_mode='legacy_plus_one',
        )
        components = WarehouseStorageArea(
            warehouse_id=factory.id,
            code='COMPONENTS',
            name='Комплектующие',
            area_type='storage',
            is_active=True,
            sort_order=10,
            product_category=None,
        )
        self.db.session.add_all([light_area, cargo_area, components])
        self.db.session.flush()

        light_cat = ProductCategory.query.filter_by(code='light_trailer').one()
        cargo_cat = ProductCategory.query.filter_by(code='cargo_trailer').one()

        sheet = Item(item_type='COMPONENT', article='SHEET-INT', name='Лист синтетика', unit='шт')
        frame = Item(item_type='COMPONENT', article='FRAME-INT', name='Рама синтетика', unit='шт')
        light_item = Item(
            item_type='TRAILER',
            article='LIGHT-INT',
            name='Легковой синтетика',
            unit='шт',
            product_category_id=light_cat.id,
        )
        cargo_item = Item(
            item_type='TRAILER',
            article='CARGO-INT',
            name='Грузовой синтетика',
            unit='шт',
            product_category_id=cargo_cat.id,
        )
        self.db.session.add_all([sheet, frame, light_item, cargo_item])
        self.db.session.flush()

        self.db.session.add_all([
            InventoryBalance(
                warehouse_id=factory.id, storage_area_id=light_area.id,
                item_id=sheet.id, quantity=Decimal('8'), unit='шт',
            ),
            InventoryBalance(
                warehouse_id=factory.id, storage_area_id=cargo_area.id,
                item_id=sheet.id, quantity=Decimal('10'), unit='шт',
            ),
            InventoryBalance(
                warehouse_id=factory.id, storage_area_id=components.id,
                item_id=sheet.id, quantity=Decimal('8'), unit='шт',
            ),
        ])

        bom = ItemBillOfMaterials(item_id=cargo_item.id, version='default', is_active=True, name='CARGO-INT')
        self.db.session.add(bom)
        self.db.session.flush()
        self.db.session.add(ItemBillOfMaterialsLine(
            bom_id=bom.id,
            component_item_id=sheet.id,
            quantity_per_unit=Decimal('1'),
            unit='шт',
            is_required=True,
            is_active=True,
        ))

        workshop = ProductionWorkshop(code='assembly', name='Сборка', workshop_type='trailer')
        self.db.session.add(workshop)
        admin = User(username='admin-int', full_name='Админ синтетика', role='admin')
        admin.set_password('x')
        director = User(username='director-int', full_name='Директор синтетика', role='director')
        director.set_password('x')
        production = User(username='prod-int', full_name='Цех синтетика', role='production')
        production.set_password('x')
        self.db.session.add_all([workshop, admin, director, production])
        self.db.session.flush()

        light_need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT',
            status='IN_PRODUCTION',
            item_id=light_item.id,
            warehouse_id=light_sales.id,
            quantity=2,
        )
        cargo_need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT',
            status='IN_PRODUCTION',
            item_id=cargo_item.id,
            warehouse_id=cargo_sales.id,
            quantity=1,
        )
        self.db.session.add_all([light_need, cargo_need])
        self.db.session.flush()
        light_request = ProductionRequest(
            request_number='PR-INT-LIGHT',
            status='in_progress',
            target_warehouse_id=light_sales.id,
        )
        cargo_request = ProductionRequest(
            request_number='PR-INT-CARGO',
            status='in_progress',
            target_warehouse_id=cargo_sales.id,
        )
        self.db.session.add_all([light_request, cargo_request])
        self.db.session.flush()
        light_line = ProductionRequestLine(
            production_request_id=light_request.id,
            supply_need_id=light_need.id,
            item_id=light_item.id,
            production_workshop_id=workshop.id,
            assembly_warehouse_id=factory.id,
            quantity=2,
            produced_qty=0,
            status='in_production',
        )
        cargo_line = ProductionRequestLine(
            production_request_id=cargo_request.id,
            supply_need_id=cargo_need.id,
            item_id=cargo_item.id,
            production_workshop_id=workshop.id,
            assembly_warehouse_id=factory.id,
            quantity=1,
            produced_qty=0,
            status='in_production',
        )
        self.db.session.add_all([light_line, cargo_line])
        self.db.session.flush()

        customer = Customer(customer_type='PERSON', name='Покупатель синтетика', is_active=True)
        otts = OTTS(
            number='ОТТС-ИНТ',
            modification='002',
            name='Синтетика ОТТС',
            axle_count=1,
            is_active=True,
        )
        self.db.session.add_all([customer, otts])
        self.db.session.flush()
        order = CustomerOrder(
            order_number='ORD-INT-0001',
            customer_id=customer.id,
            item_id=light_item.id,
            warehouse_id=light_sales.id,
            assigned_user_id=director.id,
            quantity=1,
            price=Decimal('100'),
            status='confirmed',
            documents_issued=False,
            fulfillment_source='production',
            article_snapshot=light_item.article,
            product_name_snapshot=light_item.name,
        )
        self.db.session.add(order)
        self.db.session.flush()
        order_line = CustomerOrderLine(
            order_id=order.id,
            line_no=1,
            line_type='TRAILER',
            fulfillment_source='production',
            item_id=light_item.id,
            quantity=1,
            unit_price=Decimal('100'),
            total_price=Decimal('100'),
            article_snapshot=light_item.article,
            product_name_snapshot=light_item.name,
            include_in_realization=True,
        )
        self.db.session.add(order_line)
        self.db.session.flush()
        self.db.session.add(VinRegistry(
            vin_full=VIN_FULL,
            serial7='9000001',
            status='free',
        ))
        self.db.session.commit()

        self.factory_id = factory.id
        self.light_sales_id = light_sales.id
        self.light_area_id = light_area.id
        self.cargo_area_id = cargo_area.id
        self.components_id = components.id
        self.sheet_id = sheet.id
        self.frame_id = frame.id
        self.light_item_id = light_item.id
        self.cargo_item_id = cargo_item.id
        self.workshop_id = workshop.id
        self.admin_id = admin.id
        self.director_id = director.id
        self.production_id = production.id
        self.light_line_id = light_line.id
        self.cargo_line_id = cargo_line.id
        self.customer_id = customer.id
        self.otts_id = otts.id
        self.order_id = order.id
        self.order_line_id = order_line.id

    def _assert_mode_boundary(self):
        from models import WarehouseStorageArea
        from shift_posting import MODE_LEGACY, MODE_SHIFT_ONLY

        denied, denied_client = self._request(
            self.director_id,
            'POST',
            f'/warehouses/direction-areas/{self.light_area_id}/shopfloor-mode',
            {'shopfloor_posting_mode': MODE_SHIFT_ONLY},
        )
        self.assertEqual(denied.status_code, 302, self._flash_text(denied_client))
        self.assertEqual(denied.headers.get('Location'), '/')
        self.assertEqual(
            WarehouseStorageArea.query.get(self.light_area_id).shopfloor_posting_mode,
            MODE_LEGACY,
        )
        self.assertEqual(
            WarehouseStorageArea.query.get(self.cargo_area_id).shopfloor_posting_mode,
            MODE_LEGACY,
        )

        opened, opened_client = self._request(
            self.admin_id,
            'POST',
            f'/warehouses/direction-areas/{self.light_area_id}/shopfloor-mode',
            {'shopfloor_posting_mode': MODE_SHIFT_ONLY},
        )
        self.assertEqual(opened.status_code, 302, self._flash_text(opened_client))
        self.assertEqual(opened.headers.get('Location'), '/warehouses')
        self.assertEqual(
            WarehouseStorageArea.query.get(self.light_area_id).shopfloor_posting_mode,
            MODE_SHIFT_ONLY,
        )
        self.assertEqual(
            WarehouseStorageArea.query.get(self.cargo_area_id).shopfloor_posting_mode,
            MODE_LEGACY,
        )

    def _reject_unopened_cargo_shift(self):
        from models import ProducedUnit, ProductionShift

        shift_id = self._open_shift(self.cargo_area_id)
        self._add_output(shift_id, self.cargo_item_id, 'trailer', '1', self.cargo_line_id)
        response, client = self._post_shift(shift_id, self.cargo_area_id)
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertIn('не открыт', self._flash_text(client).lower())
        self.assertEqual(ProductionShift.query.get(shift_id).status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)
        self._close_shift(shift_id)
        self.assertEqual(ProductionShift.query.get(shift_id).status, 'closed')

    def _reject_wrong_direction_output(self):
        from models import ProducedUnit, ProductionShift, ProductionShiftMaterial

        shift_id = self._open_shift(self.light_area_id)
        self._add_material(shift_id, self.sheet_id, '1')
        self._add_output(shift_id, self.cargo_item_id, 'trailer', '1', None)
        response, client = self._post_shift(shift_id, self.light_area_id)
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertIn('не совпадает', self._flash_text(client).lower())
        stored = ProductionShift.query.get(shift_id)
        self.assertEqual(stored.status, 'open')
        self.assertEqual(ProducedUnit.query.count(), 0)
        self.assertEqual(self._sheet(self.light_area_id), Decimal('8'))
        material = ProductionShiftMaterial.query.filter_by(shift_id=shift_id).one()
        self.assertNotEqual(material.status, 'posted')
        self._close_shift(shift_id)
        self.assertEqual(ProductionShift.query.get(shift_id).status, 'closed')

    def _post_light_shift(self) -> int:
        from models import InventoryOperation, ProductionShift

        shift_id = self._open_shift(self.light_area_id)
        self._add_material(shift_id, self.sheet_id, '3')
        self._add_output(shift_id, self.frame_id, 'component', '4', None)
        self._add_output(shift_id, self.light_item_id, 'trailer', '2', self.light_line_id)
        response, client = self._post_shift(shift_id, self.light_area_id)
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertNotIn('отклонено', self._flash_text(client).lower())
        stored = ProductionShift.query.get(shift_id)
        self.assertEqual(stored.status, 'posted', self._flash_text(client))
        self.assertIsNotNone(stored.posted_at)
        self.assertEqual(
            InventoryOperation.query.filter_by(operation_type='production_issue', status='posted').count(),
            1,
        )
        self.assertEqual(
            InventoryOperation.query.filter_by(
                operation_type='production_output_receipt', status='posted',
            ).count(),
            1,
        )
        self.assertEqual(
            InventoryOperation.query.filter_by(operation_type='production_shortage', status='posted').count(),
            0,
        )
        return shift_id

    def _repeat_post_does_not_double(self, shift_id: int):
        from models import InventoryOperation, ProducedUnit, ProductionShift

        issues = InventoryOperation.query.filter_by(operation_type='production_issue').count()
        receipts = InventoryOperation.query.filter_by(operation_type='production_output_receipt').count()
        units = ProducedUnit.query.count()
        response, client = self._post_shift(shift_id, self.light_area_id)
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertIn('уже проведена', self._flash_text(client).lower())
        self.assertEqual(ProductionShift.query.get(shift_id).status, 'posted')
        self.assertEqual(InventoryOperation.query.filter_by(operation_type='production_issue').count(), issues)
        self.assertEqual(
            InventoryOperation.query.filter_by(operation_type='production_output_receipt').count(),
            receipts,
        )
        self.assertEqual(ProducedUnit.query.count(), units)

    def _assign_vin(self, unit_id: int):
        from models import ProducedUnit, Trailer, VinRegistry

        attached, attach_client = self._request(
            self.director_id,
            'POST',
            f'/orders/{self.order_id}/lines/{self.order_line_id}/attach-produced-unit/{unit_id}',
        )
        self.assertEqual(attached.status_code, 302, self._flash_text(attach_client))
        unit = ProducedUnit.query.get(unit_id)
        self.assertEqual(unit.order_id, self.order_id)
        self.assertEqual(unit.order_line_id, self.order_line_id)

        vin = VinRegistry.query.filter_by(vin_full=VIN_FULL).one()
        assigned, assign_client = self._request(
            self.director_id,
            'POST',
            f'/logistics/produced-units/{unit_id}/assign-vin',
            {
                'vin_registry_id': str(vin.id),
                'manufacture_date': date.today().isoformat(),
            },
        )
        self.assertEqual(assigned.status_code, 302, self._flash_text(assign_client))
        self.assertNotIn('ошибка', self._flash_text(assign_client).lower())
        unit = ProducedUnit.query.get(unit_id)
        self.assertEqual(unit.status, 'vin_assigned', self._flash_text(assign_client))
        self.assertIsNotNone(unit.trailer_id)
        trailer = Trailer.query.get(unit.trailer_id)
        self.assertEqual(trailer.vin, VIN_FULL)
        self.assertEqual(trailer.item_id, self.light_item_id)
        vin = VinRegistry.query.get(vin.id)
        self.assertEqual(vin.status, 'confirmed')
        self.assertEqual(vin.trailer_id, trailer.id)
        self.assertEqual(vin.order_line_id, self.order_line_id)

    def _realize(self, unit_id: int, trailer_id: int):
        from models import CustomerOrder, OrderPayment, SalesContract, SalesRealization, SalesRealizationLine

        order = CustomerOrder.query.get(self.order_id)
        order.documents_issued = True
        order.trailer_id = trailer_id
        self.db.session.add(SalesContract(
            contract_number='SC-INT-0001',
            customer_id=self.customer_id,
            trailer_id=trailer_id,
            order_id=order.id,
            price=Decimal('100'),
        ))
        self.db.session.add(OrderPayment(
            order_id=order.id,
            amount=Decimal('100'),
            status='CONFIRMED',
        ))
        self.db.session.commit()

        created, create_client = self._request(
            self.director_id,
            'POST',
            f'/orders/{self.order_id}/realizations/new',
        )
        self.assertEqual(created.status_code, 302, self._flash_text(create_client))
        realization = SalesRealization.query.filter_by(order_id=self.order_id).one()
        self.assertEqual(realization.status, 'draft', self._flash_text(create_client))
        draft_line = SalesRealizationLine.query.filter_by(realization_id=realization.id).one()
        self.assertEqual(draft_line.trailer_id, trailer_id)
        self.assertIsNone(draft_line.produced_unit_id)

        posted, post_client = self._request(
            self.director_id,
            'POST',
            f'/realizations/{realization.id}/post',
        )
        self.assertEqual(posted.status_code, 302, self._flash_text(post_client))
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'posted', self._flash_text(post_client))
        self.assertEqual(
            SalesRealizationLine.query.filter_by(realization_id=stored.id).one().produced_unit_id,
            unit_id,
        )
        return stored

    def _legacy_plus_one_other_direction(self):
        from models import ProducedUnit, WarehouseStorageArea
        from shift_posting import MODE_LEGACY

        self.assertEqual(
            WarehouseStorageArea.query.get(self.cargo_area_id).shopfloor_posting_mode,
            MODE_LEGACY,
        )
        before_light = self._sheet(self.light_area_id)
        before_cargo = self._sheet(self.cargo_area_id)
        before_general = self._sheet(self.components_id)
        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/lines/{self.cargo_line_id}/produce-one',
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        plus_one = ProducedUnit.query.filter(ProducedUnit.shift_output_id.is_(None)).one()
        self.assertEqual(plus_one.item_id, self.cargo_item_id)
        self.assertEqual(plus_one.status, 'produced_no_vin', self._flash_text(client))
        self.assertEqual(self._sheet(self.light_area_id), before_light)
        self.assertEqual(self._sheet(self.cargo_area_id), before_cargo)
        self.assertEqual(self._sheet(self.components_id), before_general - Decimal('1'))

    def _assert_report_page(self, date_from: str, date_to: str, expected_value: str):
        response, client = self._request(
            self.director_id,
            'GET',
            f'/director/reports/shifts?date_from={date_from}&date_to={date_to}',
        )
        self.assertEqual(response.status_code, 200, self._flash_text(client) or response.get_data(as_text=True)[:500])
        body = response.get_data(as_text=True)
        self.assertIn('Из выпущенных за период продано на сейчас', body)
        self.assertIn('восстановить нельзя', body)
        cards = {
            match.group('title').strip(): match
            for match in CARD_RE.finditer(body)
        }
        self.assertEqual(
            cards['Из выпущенных за период продано на сейчас'].group('value').strip(),
            expected_value,
        )

    def _open_shift(self, area_id: int) -> int:
        from models import ProductionShift

        response, client = self._request(
            self.production_id,
            'POST',
            '/production/shifts/open',
            {
                'workshop_id': str(self.workshop_id),
                'direction_area_id': str(area_id),
                'work_area': 'assembly',
            },
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        opened = ProductionShift.query.filter_by(status='open', direction_area_id=area_id).one()
        return opened.id

    def _close_shift(self, shift_id: int):
        from models import ProductionShift

        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/close',
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertEqual(ProductionShift.query.get(shift_id).status, 'closed', self._flash_text(client))

    def _add_material(self, shift_id: int, item_id: int, qty: str):
        from models import ProductionShiftMaterial

        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/material',
            {'item_id': str(item_id), 'qty_fact': qty, 'unit': 'шт'},
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertGreater(
            ProductionShiftMaterial.query.filter_by(shift_id=shift_id, item_id=item_id).count(),
            0,
            self._flash_text(client),
        )

    def _add_output(self, shift_id: int, item_id: int, output_type: str, quantity: str, line_id):
        from models import ProductionShiftOutput

        data = {
            'item_id': str(item_id),
            'output_type': output_type,
            'quantity': quantity,
            'defect_quantity': '0',
            'unit': 'шт',
        }
        if line_id is not None:
            data['production_request_line_id'] = str(line_id)
        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/output',
            data,
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertGreater(
            ProductionShiftOutput.query.filter_by(shift_id=shift_id, item_id=item_id).count(),
            0,
            self._flash_text(client),
        )

    def _post_shift(self, shift_id: int, area_id: int):
        return self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/post',
            {'direction_area_id': str(area_id), 'hours_fact': '8'},
        )

    def _request(self, user_id: int, method: str, url: str, data=None):
        from flask import g

        g.pop('_login_user', None)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        if method == 'POST':
            response = client.post(url, data=data or {}, follow_redirects=False)
        else:
            response = client.get(url, follow_redirects=False)
        return response, client

    def _flash_text(self, client) -> str:
        with client.session_transaction() as sess:
            flashes = sess.get('_flashes') or []
        return ' '.join(str(item[1]) for item in flashes)

    def _sheet(self, area_id: int) -> Decimal:
        return _qty(self.factory_id, self.sheet_id, area_id)

    def _frame(self, area_id: int) -> Decimal:
        return _qty(self.factory_id, self.frame_id, area_id)

    def _version(self):
        return self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()

    def _foreign_keys(self) -> int:
        return int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar() or 0)

    def _trigger_sql(self):
        rows = self.db.session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )).all()
        return [(row[0], row[1]) for row in rows]

    def _temp_tables(self):
        rows = self.db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%alembic_tmp%'"
        )).all()
        return [row[0] for row in rows]


if __name__ == '__main__':
    unittest.main()
