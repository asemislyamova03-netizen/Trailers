#!/usr/bin/env python3
"""Триггеры связи выпуска и реализации на полной Alembic-цепочке.

Только временная sqlite. Обычное соединение create_app() остаётся
с PRAGMA foreign_keys=0. Рабочая trailers.db не открывается.
Четыре колонки модели добавляет ревизия f6b2d8c14e90. Ручной ALTER
в этом тесте больше не нужен.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
HEAD_BEFORE_GUARD = 'b7e2c4a9d815'
GUARD_REVISION = 'c3f8a1d94e27'
MATCH_REVISION = 'd8e4b1c67a02'
FIELD_REVISION = 'e1b7c4d92a58'
SCHEMA_REVISION = 'f6b2d8c14e90'
# Эти nullable-колонки до f6b2d8c14e90 отсутствуют. Новая ревизия добавляет
# их сама. Тест больше не делает ручной ALTER.
MODEL_DRIFT_COLUMNS = (
    ('item', 'tent_hight_mm', 'INTEGER'),
    ('item', 'has_jockey_wheel', 'BOOLEAN'),
    ('trailer', 'otts_id', 'INTEGER'),
    ('otts', 'full_mass_kg', 'INTEGER'),
)


def _load_migration(filename: str, module_name: str):
    path = ROOT / 'migrations' / 'versions' / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard_module():
    return _load_migration(
        'c3f8a1d94e27_guard_realization_produced_unit_link.py',
        'produced_unit_link_sqlite_guard_migration',
    )


def _match_module():
    return _load_migration(
        'd8e4b1c67a02_guard_posted_realization_unit_match.py',
        'posted_realization_unit_match_migration',
    )


def _field_module():
    return _load_migration(
        'e1b7c4d92a58_guard_posted_cohort_line_fields.py',
        'posted_cohort_line_fields_migration',
    )


def _refuse_live(url: str, path: Path) -> None:
    resolved = path.resolve()
    normalized = url.replace('\\', '/')
    if resolved == LIVE_DB or resolved.name == 'trailers.db':
        raise RuntimeError(f'Refused to touch live DB file: {resolved}')
    if normalized.endswith('/trailers.db') or normalized.endswith('trailers.db'):
        raise RuntimeError(f'Refused to touch live DB url: {normalized}')


class ProducedUnitLinkSqliteGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._live_stat = LIVE_DB.stat() if LIVE_DB.exists() else None
        cls._tmp = tempfile.NamedTemporaryFile(suffix='-guard.db', delete=False)
        cls._tmp.close()
        cls.db_path = Path(cls._tmp.name)
        uri = 'sqlite:///' + cls._tmp.name.replace('\\', '/')
        _refuse_live(uri, cls.db_path)

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

        cls.app = create_app()
        cls.app.config['WTF_CSRF_ENABLED'] = False
        db.init_app = original_init_app
        cls.db = db

        with cls.app.app_context():
            engine_url = str(db.engine.url).replace('\\', '/')
            _refuse_live(engine_url, cls.db_path)
            if 'foreign_keys' in (ROOT / 'app.py').read_text(encoding='utf-8').lower():
                raise RuntimeError('app.py must not enable PRAGMA foreign_keys')
            from flask_migrate import upgrade

            db.session.remove()
            upgrade(directory=MIGRATIONS, revision=HEAD_BEFORE_GUARD)
            cls._insert_preexisting_rows()
            db.session.remove()
            upgrade(directory=MIGRATIONS, revision=FIELD_REVISION)
            cls.drift_missing_at_field_revision = cls._missing_drift_columns()
            db.session.remove()
            upgrade(directory=MIGRATIONS, revision=SCHEMA_REVISION)
            if cls._missing_drift_columns():
                raise RuntimeError(
                    'f6b2d8c14e90 не добавила четыре колонки модели: '
                    + ', '.join(cls._missing_drift_columns())
                )
            pragma = db.session.execute(text('PRAGMA foreign_keys')).scalar()
            if int(pragma) != 0:
                raise RuntimeError(f'create_app connection must keep foreign_keys=0, got {pragma}')

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.db_path.unlink(missing_ok=True)
        if cls._live_stat is not None and LIVE_DB.exists():
            current = LIVE_DB.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                cls._live_stat.st_ino,
                cls._live_stat.st_size,
                cls._live_stat.st_mtime_ns,
            ):
                raise RuntimeError('live trailers.db changed during sqlite guard tests')

    @classmethod
    def _insert_preexisting_rows(cls):
        """Строки до триггера: NULL и висячий id. Миграция их не переписывает."""
        cls.db.session.execute(text(
            '''
            INSERT INTO sales_realization_line (
                realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id, comment
            ) VALUES
                (910001, 1, 'trailer', 1, 'шт', 'trailer_unit', NULL, 'guard-old-null'),
                (910002, 1, 'trailer', 1, 'шт', 'trailer_unit', 919999, 'guard-old-orphan')
            '''
        ))
        cls.db.session.commit()

    @classmethod
    def _missing_drift_columns(cls):
        missing = []
        for table, column, _coltype in MODEL_DRIFT_COLUMNS:
            present = {
                row[1]
                for row in cls.db.session.execute(text(f'PRAGMA table_info({table})')).all()
            }
            if column not in present:
                missing.append(f'{table}.{column}')
        return tuple(missing)

    def setUp(self):
        os.environ.pop('TRAILERS_F6B2D8C14E90_ABORT_AFTER', None)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)

    def tearDown(self):
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_upgrade_keeps_old_null_and_orphan_then_downgrade_restores_raw_insert(self):
        from flask_migrate import downgrade, upgrade

        self.assertEqual(
            self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar(),
            SCHEMA_REVISION,
        )
        old_null = self._comment_unit('guard-old-null')
        old_orphan = self._comment_unit('guard-old-orphan')
        self.assertIsNone(old_null)
        self.assertEqual(old_orphan, 919999)
        self.assertEqual(self._trigger_names(), set(self._expected_triggers()))
        violations = self.db.session.execute(text(
            'PRAGMA foreign_key_check(sales_realization_line)'
        )).all()
        orphan_rowid = self.db.session.execute(text(
            "SELECT id FROM sales_realization_line WHERE comment = 'guard-old-orphan'"
        )).scalar()
        dangling_units = {row[1] for row in violations if row[2] == 'produced_unit'}
        self.assertEqual(dangling_units, {orphan_rowid})

        self._assert_missing_rejected(
            '''
            INSERT INTO sales_realization_line (
                realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id, comment
            ) VALUES (910003, 1, 'trailer', 1, 'шт', 'trailer_unit', 918888, 'guard-rejected')
            ''',
        )
        self.assertIsNone(self._comment_unit('guard-rejected'))

        self.db.session.remove()
        downgrade(directory=MIGRATIONS, revision=HEAD_BEFORE_GUARD)
        self.assertEqual(
            self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar(),
            HEAD_BEFORE_GUARD,
        )
        self.assertEqual(self._trigger_names(), set())
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
        self.db.session.execute(text(
            '''
            INSERT INTO sales_realization_line (
                realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id, comment
            ) VALUES (910004, 1, 'trailer', 1, 'шт', 'trailer_unit', 918888, 'guard-while-downgraded')
            '''
        ))
        self.db.session.commit()
        self.assertEqual(self._comment_unit('guard-while-downgraded'), 918888)
        self.db.session.execute(text(
            "DELETE FROM sales_realization_line WHERE comment = 'guard-while-downgraded'"
        ))
        self.db.session.commit()

        self.db.session.remove()
        upgrade(directory=MIGRATIONS, revision=SCHEMA_REVISION)
        self.assertEqual(self._trigger_names(), set(self._expected_triggers()))
        self.assertIsNone(self._comment_unit('guard-old-null'))
        self.assertEqual(self._comment_unit('guard-old-orphan'), 919999)
        self.assertIsNone(self._comment_unit('guard-while-downgraded'))
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)

    def test_raw_insert_and_update_reject_missing_parent(self):
        unit = self._unit()
        self.db.session.commit()
        self.db.session.execute(text(
            '''
            INSERT INTO sales_realization_line (
                id, realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id
            ) VALUES (920001, 920001, 1, 'trailer', 1, 'шт', 'trailer_unit', NULL)
            '''
        ))
        self.db.session.commit()
        self.assertIsNone(self._line_unit(920001))

        self.db.session.execute(text(
            '''
            UPDATE sales_realization_line
            SET comment = 'заметка без смены ключа'
            WHERE id = 920001
            '''
        ))
        self.db.session.commit()

        self._assert_missing_rejected(
            '''
            UPDATE sales_realization_line
            SET produced_unit_id = 929999
            WHERE id = 920001
            ''',
        )
        self.assertIsNone(self._line_unit(920001))

        self.db.session.execute(text(
            'UPDATE sales_realization_line SET produced_unit_id = :unit_id WHERE id = 920001'
        ), {'unit_id': unit.id})
        self.db.session.commit()
        self.assertEqual(self._line_unit(920001), unit.id)

        self._assert_missing_rejected(
            '''
            INSERT INTO sales_realization_line (
                id, realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id
            ) VALUES (920002, 920002, 1, 'trailer', 1, 'шт', 'trailer_unit', 929998)
            ''',
        )
        self.assertIsNone(self._line_unit(920002))

    def test_delete_linked_parent_keeps_both_rows(self):
        unit = self._unit()
        self.db.session.commit()
        self.db.session.execute(text(
            '''
            INSERT INTO sales_realization_line (
                id, realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id
            ) VALUES (930001, 930001, 1, 'trailer', 1, 'шт', 'trailer_unit', :unit_id)
            '''
        ), {'unit_id': unit.id})
        self.db.session.commit()

        guard = _guard_module()

        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(
                text('DELETE FROM produced_unit WHERE id = :unit_id'),
                {'unit_id': unit.id},
            )
            self.db.session.commit()
        self.assertIn(guard.DELETE_PARENT_MESSAGE, str(caught.exception))
        self.db.session.rollback()

        self.assertIsNotNone(self.db.session.execute(
            text('SELECT id FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': unit.id},
        ).scalar())
        linked_id = int(unit.id)
        self.assertEqual(self._line_unit(930001), linked_id)

        self._seed_master()
        free_id = self.db.session.execute(text(
            '''
            INSERT INTO produced_unit (
                production_request_line_id, item_id, status, created_at
            ) VALUES (:line_id, :item_id, 'produced_no_vin', CURRENT_TIMESTAMP)
            RETURNING id
            '''
        ), {
            'line_id': self.request_line.id,
            'item_id': self.item.id,
        }).scalar()
        self.db.session.commit()
        self.db.session.execute(
            text('DELETE FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': free_id},
        )
        self.db.session.commit()
        self.assertIsNone(self.db.session.execute(
            text('SELECT id FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': free_id},
        ).scalar())
        self.assertEqual(self._line_unit(930001), linked_id)

    def test_unique_index_still_rejects_repeat(self):
        unit = self._unit()
        self.db.session.commit()
        self.db.session.execute(text(
            '''
            INSERT INTO sales_realization_line (
                id, realization_id, line_no, line_type, quantity, unit,
                inventory_effect, produced_unit_id
            ) VALUES (940001, 940001, 1, 'trailer', 1, 'шт', 'trailer_unit', :unit_id)
            '''
        ), {'unit_id': unit.id})
        self.db.session.commit()
        with self.assertRaises(IntegrityError):
            self.db.session.execute(text(
                '''
                INSERT INTO sales_realization_line (
                    id, realization_id, line_no, line_type, quantity, unit,
                    inventory_effect, produced_unit_id
                ) VALUES (940002, 940002, 1, 'trailer', 1, 'шт', 'trailer_unit', :unit_id)
                '''
            ), {'unit_id': unit.id})
            self.db.session.commit()
        self.db.session.rollback()
        self.assertEqual(self._count_unit_links(unit.id), 1)

    def test_existing_parent_is_not_a_correct_sale(self):
        from realization_unit_link import ProducedUnitLinkError, assign_produced_units_on_post

        trailer = self._trailer('VIN-GUARD-OTHER-0001')
        other = self._trailer('VIN-GUARD-OTHER-0002')
        own_unit = self._unit(trailer=trailer)
        foreign_unit = self._unit(trailer=other, item=self._other_item())
        self.db.session.commit()

        realization = self._realization(trailer, [{'trailer_id': trailer.id}])
        assign_produced_units_on_post(realization)
        realization.status = 'posted'
        self.db.session.commit()
        self.assertEqual(self._orm_line(realization).produced_unit_id, own_unit.id)

        second = self._unit(trailer=trailer)
        self.db.session.commit()
        ambiguous = self._realization(trailer, [{'trailer_id': trailer.id}])
        with self.assertRaises(ProducedUnitLinkError) as caught:
            assign_produced_units_on_post(ambiguous)
        self.assertIn('Первая не выбирается', str(caught.exception))
        self.db.session.rollback()

        mismatch = self._realization(other, [{
            'trailer_id': other.id,
            'item_id': self._other_item().id,
        }])
        self._orm_line(mismatch).item_id = trailer.item_id
        with self.assertRaises(ProducedUnitLinkError):
            assign_produced_units_on_post(mismatch)
        self.db.session.rollback()

        line_id = self._orm_line(realization).id
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(text(
                '''
                UPDATE sales_realization_line
                SET produced_unit_id = :foreign_id
                WHERE id = :line_id
                '''
            ), {'foreign_id': foreign_unit.id, 'line_id': line_id})
            self.db.session.commit()
        self.assertIn(_match_module().POSTED_REBIND_MESSAGE, str(caught.exception))
        self.db.session.rollback()
        self.assertEqual(self._orm_line(realization).produced_unit_id, own_unit.id)
        self.assertNotEqual(foreign_unit.trailer_id, self._orm_line(realization).trailer_id)

    def test_route_post_delete_and_repost_still_work(self):
        from models import SalesRealization, SalesRealizationLine, Trailer

        trailer = self._trailer('VIN-GUARD-ROUTE-0001')
        unit = self._unit(trailer=trailer)
        self.db.session.commit()
        order = self._sale_order(trailer)
        realization = self._draft_realization(order, trailer)
        client = self._client()
        response = client.post(f'/realizations/{realization.id}/post', follow_redirects=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'posted', response.get_data(as_text=True))
        line = self._orm_line(stored)
        self.assertEqual(line.produced_unit_id, unit.id)
        self.assertEqual(Trailer.query.get(trailer.id).status, 'SOLD')
        old_line_id = line.id

        response = client.post(f'/realizations/{realization.id}/delete', follow_redirects=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIsNone(SalesRealization.query.get(realization.id))
        self.assertIsNone(SalesRealizationLine.query.get(old_line_id))
        self.assertIsNotNone(self.db.session.get(type(unit), unit.id))

        order = self.db.session.get(type(order), order.id)
        repeat = self._draft_realization(order, trailer)
        response = client.post(f'/realizations/{repeat.id}/post', follow_redirects=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        repeat = SalesRealization.query.get(repeat.id)
        self.assertEqual(repeat.status, 'posted', response.get_data(as_text=True))
        self.assertEqual(self._orm_line(repeat).produced_unit_id, unit.id)
        self.assertEqual(self._count_unit_links(unit.id), 1)

    def test_cohort_snapshot_does_not_change_meaning(self):
        from shift_director_report import SOLD_FROM_PRODUCED_LABEL, build_shift_director_report

        trailer = self._trailer('VIN-GUARD-COHORT-0001')
        unit = self._cohort_unit(trailer, posted_at=datetime(2026, 1, 15, 12, 0, 0))
        self._posted_cohort_sale(unit, realization_date=date(2026, 8, 20))
        january = build_shift_director_report(
            period_start=datetime(2026, 1, 1, 0, 0, 0),
            period_end=datetime(2026, 1, 31, 23, 59, 59),
        )
        sold = january['sold_from_produced']
        self.assertEqual(sold['status'], 'SNAPSHOT')
        self.assertEqual(sold['label'], SOLD_FROM_PRODUCED_LABEL)
        self.assertEqual(sold['label'], 'Из выпущенных за период продано на сейчас')
        self.assertIsNone(sold['sale_date_filter'])
        self.assertEqual(sold['value'], 1)
        self.assertEqual(sold['cohort_units'], 1)
        self.assertEqual(january['totals']['good_trailers'], sold['cohort_units'])
        self.assertIn('не продажи выбранного периода', sold['limitation'])

        august = build_shift_director_report(
            period_start=datetime(2026, 8, 1, 0, 0, 0),
            period_end=datetime(2026, 8, 31, 23, 59, 59),
        )
        self.assertEqual(august['sold_from_produced']['value'], 0)
        self.assertEqual(august['sold_from_produced']['cohort_units'], 0)

        client = self._client()
        page = client.get('/director/reports/shifts')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('Из выпущенных за период продано на сейчас', body)
        self.assertIn('восстановить нельзя', body)

    def test_four_model_columns_come_from_schema_revision(self):
        expected = tuple(f'{table}.{column}' for table, column, _coltype in MODEL_DRIFT_COLUMNS)
        self.assertEqual(self.drift_missing_at_field_revision, expected)
        self.assertEqual(self._missing_drift_columns(), ())
        old_names = (
            'd8e4b1c67a02_guard_posted_realization_unit_match.py',
            'e1b7c4d92a58_guard_posted_cohort_line_fields.py',
        )
        for name in old_names:
            migration_text = (ROOT / 'migrations' / 'versions' / name).read_text(encoding='utf-8')
            for _table, column, _coltype in MODEL_DRIFT_COLUMNS:
                self.assertNotIn(f"'{column}'", migration_text)
                self.assertNotIn(f'ADD COLUMN {column}', migration_text)
            self.assertNotIn('foreign_keys=ON', migration_text)
            self.assertNotIn('PRAGMA foreign_keys=ON', migration_text)
        schema_text = (
            ROOT / 'migrations' / 'versions' / 'f6b2d8c14e90_add_four_model_columns.py'
        ).read_text(encoding='utf-8')
        self.assertIn('ADD COLUMN {column}', schema_text)
        for _table, column, _coltype in MODEL_DRIFT_COLUMNS:
            self.assertIn(f"'{column}'", schema_text)
        self.assertNotIn('foreign_keys=ON', schema_text)
        self.assertNotIn('PRAGMA foreign_keys=ON', schema_text)
        self.assertNotIn('create_foreign_key', schema_text)
        referred = {
            row[2]
            for row in self.db.session.execute(text('PRAGMA foreign_key_list(trailer)')).all()
        }
        self.assertNotIn('otts', referred)

    def test_foreign_rebind_and_parent_id_do_not_move_two_directions(self):
        from shift_director_report import build_shift_director_report

        march_start = datetime(2031, 3, 1, 0, 0, 0)
        march_end = datetime(2031, 3, 31, 23, 59, 59)
        october_start = datetime(2031, 10, 1, 0, 0, 0)
        october_end = datetime(2031, 10, 31, 23, 59, 59)
        trailer = self._trailer('VIN-GUARD-DIR-A-0001')
        other = self._trailer('VIN-GUARD-DIR-B-0001', item=self._other_item())
        own = self._cohort_unit(
            trailer,
            posted_at=datetime(2031, 3, 15, 12, 0, 0),
            direction_code='DIR_COHORT_A',
            direction_name='Направление A',
        )
        foreign = self._cohort_unit(
            other,
            posted_at=datetime(2031, 10, 15, 12, 0, 0),
            direction_code='DIR_COHORT_B',
            direction_name='Направление B',
            item=self._other_item(),
        )
        sibling = self._cohort_unit(
            trailer,
            posted_at=datetime(2031, 10, 16, 12, 0, 0),
            direction_code='DIR_COHORT_C',
            direction_name='Направление C',
        )
        sale = self._posted_cohort_sale(own, realization_date=date(2031, 10, 20))
        line_id = self._orm_line(sale).id
        before = self._period_sold(march_start, march_end, october_start, october_end)
        self.assertEqual(before['march'], (1, 1))
        self.assertEqual(before['october'], (0, 2))
        march_areas = {
            row['direction_code']: row['good_trailers']
            for row in build_shift_director_report(
                period_start=march_start,
                period_end=march_end,
            )['area_rows']
        }
        self.assertEqual(march_areas.get('DIR_COHORT_A'), 1)

        self._assert_rebind_rejected(line_id, foreign.id)
        self.assertEqual(self._line_unit(line_id), own.id)
        self.assertEqual(self._period_sold(march_start, march_end, october_start, october_end), before)

        self._assert_rebind_rejected(line_id, sibling.id)
        self.assertEqual(self._line_unit(line_id), own.id)
        self.assertEqual(self._period_sold(march_start, march_end, october_start, october_end), before)

        self._assert_parent_id_rejected(own.id, 830001)
        self.assertEqual(self._line_unit(line_id), own.id)
        self.assertIsNotNone(self.db.session.execute(
            text('SELECT id FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': own.id},
        ).scalar())
        with self.assertRaises(IntegrityError) as deleted:
            self.db.session.execute(
                text('DELETE FROM produced_unit WHERE id = :unit_id'),
                {'unit_id': own.id},
            )
            self.db.session.commit()
        self.assertIn(_guard_module().DELETE_PARENT_MESSAGE, str(deleted.exception))
        self.db.session.rollback()
        self.assertEqual(self._period_sold(march_start, march_end, october_start, october_end), before)

        self.db.session.execute(
            text("UPDATE produced_unit SET note = 'статус выпуска не ключ' WHERE id = :unit_id"),
            {'unit_id': own.id},
        )
        self.db.session.commit()
        self.assertEqual(self._line_unit(line_id), own.id)

        self._assert_rebind_rejected(line_id, 839999)
        self.assertEqual(self._line_unit(line_id), own.id)

        draft = self._realization(trailer, [{'trailer_id': trailer.id}])
        draft_line_id = self._orm_line(draft).id
        self.db.session.execute(
            text('UPDATE sales_realization_line SET produced_unit_id = :unit_id WHERE id = :line_id'),
            {'unit_id': foreign.id, 'line_id': draft_line_id},
        )
        self.db.session.commit()
        self.assertEqual(self._line_unit(draft_line_id), foreign.id)
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(self._period_sold(march_start, march_end, october_start, october_end), before)

        with self.assertRaises(IntegrityError) as posted:
            self.db.session.execute(
                text("UPDATE sales_realization SET status = 'posted' WHERE id = :realization_id"),
                {'realization_id': draft.id},
            )
            self.db.session.commit()
        self.assertIn(_match_module().POSTED_FOREIGN_UNIT_MESSAGE, str(posted.exception))
        self.db.session.rollback()
        self.assertEqual(self.db.session.execute(
            text('SELECT status FROM sales_realization WHERE id = :realization_id'),
            {'realization_id': draft.id},
        ).scalar(), 'draft')
        self.assertEqual(self._period_sold(march_start, march_end, october_start, october_end), before)

        free = self._unit()
        self.db.session.commit()
        free_id = int(free.id)
        renamed = 830002
        self.db.session.execute(
            text('UPDATE produced_unit SET id = :new_id WHERE id = :unit_id'),
            {'new_id': renamed, 'unit_id': free_id},
        )
        self.db.session.commit()
        self.assertIsNone(self.db.session.execute(
            text('SELECT id FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': free_id},
        ).scalar())
        self.db.session.execute(
            text('DELETE FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': renamed},
        )
        self.db.session.commit()

    def test_route_post_replaces_draft_foreign_id(self):
        from models import SalesRealization

        trailer = self._trailer('VIN-GUARD-ROUTE-0002')
        other = self._trailer('VIN-GUARD-ROUTE-0003', item=self._other_item())
        unit = self._unit(trailer=trailer)
        foreign = self._unit(trailer=other, item=self._other_item())
        self.db.session.commit()
        order = self._sale_order(trailer)
        realization = self._draft_realization(order, trailer)
        line_id = self._orm_line(realization).id
        self.db.session.execute(
            text('UPDATE sales_realization_line SET produced_unit_id = :unit_id WHERE id = :line_id'),
            {'unit_id': foreign.id, 'line_id': line_id},
        )
        self.db.session.commit()
        self.assertEqual(self._line_unit(line_id), foreign.id)

        client = self._client()
        response = client.post(f'/realizations/{realization.id}/post', follow_redirects=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        stored = SalesRealization.query.get(realization.id)
        self.assertEqual(stored.status, 'posted', response.get_data(as_text=True))
        self.assertEqual(self._orm_line(stored).produced_unit_id, unit.id)
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)

    def test_downgrade_reopens_foreign_rebind_upgrade_keeps_old_rows(self):
        from flask_migrate import downgrade, upgrade

        trailer = self._trailer('VIN-GUARD-OLD-0001')
        other = self._trailer('VIN-GUARD-OLD-0002', item=self._other_item())
        own = self._unit(trailer=trailer)
        foreign = self._unit(trailer=other, item=self._other_item())
        self.db.session.commit()
        posted = self._posted_cohort_sale(own, realization_date=date(2032, 1, 10))
        line_id = self._orm_line(posted).id
        null_row = self._realization(trailer, [{'trailer_id': trailer.id}])
        null_row.status = 'posted'
        self.db.session.commit()
        null_line_id = self._orm_line(null_row).id
        own_id = int(own.id)
        foreign_id = int(foreign.id)
        self.assertIsNone(self._line_unit(null_line_id))

        try:
            self.db.session.remove()
            downgrade(directory=MIGRATIONS, revision=GUARD_REVISION)
            self.assertEqual(
                self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar(),
                GUARD_REVISION,
            )
            self.assertEqual(self._trigger_names(), set(_guard_module().TRIGGER_NAMES))
            self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
            self.db.session.execute(
                text('UPDATE sales_realization_line SET produced_unit_id = :unit_id WHERE id = :line_id'),
                {'unit_id': foreign_id, 'line_id': line_id},
            )
            self.db.session.commit()
            self.assertEqual(self._line_unit(line_id), foreign_id)
            self.assertIsNone(self._line_unit(null_line_id))
            self.assertIsNone(self._comment_unit('guard-old-null'))
            self.assertEqual(self._comment_unit('guard-old-orphan'), 919999)

            self.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=MATCH_REVISION)
            self.assertEqual(self._line_unit(line_id), foreign_id)
            self.assertIsNone(self._line_unit(null_line_id))
            self.assertIsNone(self._comment_unit('guard-old-null'))
            self.assertEqual(self._comment_unit('guard-old-orphan'), 919999)
            self.assertEqual(self._trigger_names(), set(self._match_trigger_names()))
            self._assert_rebind_rejected(line_id, own_id)
            self.assertEqual(self._line_unit(line_id), foreign_id)
            self._assert_parent_id_rejected(foreign_id, 830003)
            self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
            self.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=FIELD_REVISION)
            self.assertEqual(self._line_unit(line_id), foreign_id)
            self.assertIsNone(self._comment_unit('guard-old-null'))
            self.assertEqual(self._comment_unit('guard-old-orphan'), 919999)
            self.assertEqual(self._trigger_names(), set(self._expected_triggers()))
        finally:
            self.db.session.rollback()
            self.db.session.remove()
            current = self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()
            if current != SCHEMA_REVISION:
                upgrade(directory=MIGRATIONS, revision=SCHEMA_REVISION)

    def test_future_alembic_batch_rebuild_is_not_safe(self):
        import sqlite3

        import sqlalchemy as sa
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext

        copy = self._backup_sqlite()
        engine = sa.create_engine('sqlite:///' + str(copy).replace('\\', '/'))
        batch_errors = {}
        try:
            for table in ('sales_realization_line', 'produced_unit', 'sales_realization'):
                try:
                    with engine.begin() as conn:
                        operation = Operations(MigrationContext.configure(conn))
                        with operation.batch_alter_table(table, recreate='always') as batch:
                            batch.add_column(sa.Column('batch_probe', sa.Integer(), nullable=True))
                    batch_errors[table] = ''
                except Exception as exc:
                    batch_errors[table] = f'{type(exc).__name__}: {exc}'
            with engine.connect() as conn:
                trigger_names = {
                    row[0]
                    for row in conn.execute(sa.text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )).all()
                }
                temp_tables = [
                    row[0]
                    for row in conn.execute(sa.text(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name LIKE '%alembic_tmp%'"
                    )).all()
                ]
                pragma = int(conn.execute(sa.text('PRAGMA foreign_keys')).scalar())
        finally:
            engine.dispose()
            copy.unlink(missing_ok=True)

        plain = self._backup_sqlite()
        plain_conn = sqlite3.connect(plain)
        try:
            plain_conn.execute('ALTER TABLE sales_realization_line ADD COLUMN plain_probe INTEGER')
            plain_conn.commit()
            plain_triggers = {
                row[0]
                for row in plain_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            plain_pragma = int(plain_conn.execute('PRAGMA foreign_keys').fetchone()[0])
            probe = {
                row[1]
                for row in plain_conn.execute('PRAGMA table_info(sales_realization_line)')
            }
        finally:
            plain_conn.close()
            plain.unlink(missing_ok=True)

        self.assertEqual(plain_triggers, set(self._expected_triggers()))
        self.assertEqual(plain_pragma, 0)
        self.assertIn('plain_probe', probe)
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
        lost = set(self._expected_triggers()) - trigger_names
        report = {
            'errors': batch_errors,
            'lost_triggers': sorted(lost),
            'remaining_triggers': sorted(trigger_names),
            'temp_tables': temp_tables,
            'pragma': pragma,
        }
        unsafe = bool(lost or temp_tables or any(batch_errors.values()))
        self.assertTrue(
            unsafe,
            'batch rebuild неожиданно сохранил триггеры: ' + repr(report),
        )
        self.batch_rebuild_report = report

    def test_future_batch_rebuild_keeps_guards_when_triggers_are_dropped_first(self):
        """Будущая миграция: снять триггеры, пересобрать таблицу, создать их снова.

        Историю старых ревизий этот тест не меняет. Копия временной базы
        не является рабочей trailers.db.
        """
        import sqlalchemy as sa
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext

        field = _field_module()
        for table in field.GUARDED_TABLES:
            copy = self._backup_sqlite()
            engine = sa.create_engine('sqlite:///' + str(copy).replace('\\', '/'))
            try:
                with engine.begin() as conn:
                    saved = [
                        (name, sql)
                        for name, sql in conn.execute(sa.text(
                            "SELECT name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND sql IS NOT NULL"
                        )).all()
                        if table in sql
                    ]
                    self.assertTrue(saved, table)
                    for name, _sql in saved:
                        conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS {name}')
                    operation = Operations(MigrationContext.configure(conn))
                    with operation.batch_alter_table(table, recreate='always') as batch:
                        batch.add_column(sa.Column('batch_probe', sa.Integer(), nullable=True))
                    for _name, sql in saved:
                        conn.exec_driver_sql(sql)
                with engine.connect() as conn:
                    names = {
                        row[0]
                        for row in conn.execute(sa.text(
                            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                        )).all()
                    }
                    temp_tables = [
                        row[0]
                        for row in conn.execute(sa.text(
                            "SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name LIKE '%alembic_tmp%'"
                        )).all()
                    ]
                    columns = {
                        row[1]
                        for row in conn.execute(sa.text(f'PRAGMA table_info({table})')).all()
                    }
                    pragma = int(conn.execute(sa.text('PRAGMA foreign_keys')).scalar())
                    with self.assertRaises(sa.exc.IntegrityError) as missing:
                        conn.execute(sa.text(
                            '''
                            INSERT INTO sales_realization_line (
                                realization_id, line_no, line_type, quantity, unit,
                                inventory_effect, produced_unit_id, comment
                            ) VALUES (
                                910005, 1, 'trailer', 1, 'шт', 'trailer_unit',
                                918777, 'guard-batch-missing'
                            )
                            '''
                        ))
                        conn.commit()
            finally:
                engine.dispose()
                copy.unlink(missing_ok=True)

            self.assertIn(_guard_module().MISSING_UNIT_MESSAGE, str(missing.exception))
            self.assertEqual(names, set(self._expected_triggers()))
            self.assertEqual(temp_tables, [])
            self.assertIn('batch_probe', columns)
            self.assertEqual(pragma, 0)
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
        self.assertEqual(
            self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar(),
            SCHEMA_REVISION,
        )

    def test_posted_field_sql_cannot_move_cohort(self):
        from flask_migrate import downgrade, upgrade

        march_start = datetime(2034, 3, 1, 0, 0, 0)
        march_end = datetime(2034, 3, 31, 23, 59, 59)
        october_start = datetime(2034, 10, 1, 0, 0, 0)
        october_end = datetime(2034, 10, 31, 23, 59, 59)
        trailer = self._trailer('VIN-GUARD-FIELD-A-0001')
        other = self._trailer('VIN-GUARD-FIELD-B-0001', item=self._other_item())
        own = self._cohort_unit(
            trailer,
            posted_at=datetime(2034, 3, 15, 12, 0, 0),
            direction_code='DIR_FIELD_A',
            direction_name='Поле A',
        )
        foreign = self._cohort_unit(
            other,
            posted_at=datetime(2034, 10, 15, 12, 0, 0),
            direction_code='DIR_FIELD_B',
            direction_name='Поле B',
            item=self._other_item(),
        )
        sale = self._posted_cohort_sale(own, realization_date=date(2034, 10, 20))
        line = self._orm_line(sale)
        draft = self._realization(other, [{
            'trailer_id': other.id,
            'item_id': other.item_id,
        }])
        draft_line = self._orm_line(draft)
        self.db.session.execute(
            text(
                'UPDATE sales_realization_line SET produced_unit_id = :unit_id '
                'WHERE id = :line_id'
            ),
            {'unit_id': foreign.id, 'line_id': draft_line.id},
        )
        self.db.session.commit()
        other_draft = self._realization(trailer, [{'trailer_id': trailer.id}])
        bare = self._unit(status='produced_no_vin')
        self.db.session.commit()
        before = self._period_sold(march_start, march_end, october_start, october_end)
        self.assertEqual(before['march'], (1, 1))
        self.assertEqual(before['october'], (0, 1))
        line_id = int(line.id)
        draft_line_id = int(draft_line.id)
        other_draft_id = int(other_draft.id)
        sale_id = int(sale.id)
        draft_id = int(draft.id)
        own_id = int(own.id)
        foreign_id = int(foreign.id)
        other_trailer_id = int(other.id)
        other_item_id = int(other.item_id)
        own_trailer_id = int(trailer.id)
        own_item_id = int(trailer.item_id)
        bare_id = int(bare.id)

        field = _field_module()
        try:
            self._assert_posted_field_guards(
                field,
                line_id=line_id,
                draft_line_id=draft_line_id,
                other_draft_id=other_draft_id,
                own_id=own_id,
                foreign_id=foreign_id,
                other_trailer_id=other_trailer_id,
                other_item_id=other_item_id,
                bare_id=bare_id,
                own_trailer_id=own_trailer_id,
                march_start=march_start,
                march_end=march_end,
                october_start=october_start,
                october_end=october_end,
                before=before,
            )

            self.db.session.remove()
            downgrade(directory=MIGRATIONS, revision=MATCH_REVISION)
            self.assertEqual(self._trigger_names(), set(self._match_trigger_names()))
            self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)
            self.db.session.execute(
                text(
                    "UPDATE sales_realization_line SET inventory_effect = 'none' "
                    'WHERE id = :line_id'
                ),
                {'line_id': line_id},
            )
            self.db.session.commit()
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end)['march'],
                (0, 1),
            )
            self.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=FIELD_REVISION)
            self.assertEqual(
                self.db.session.execute(
                    text('SELECT inventory_effect FROM sales_realization_line WHERE id = :line_id'),
                    {'line_id': line_id},
                ).scalar(),
                'none',
            )
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end)['march'],
                (0, 1),
            )
            self._assert_sql_rejected(
                "UPDATE sales_realization_line SET inventory_effect = 'trailer_unit' "
                'WHERE id = :line_id',
                {'line_id': line_id},
                field.POSTED_EFFECT_MESSAGE,
            )
            self.db.session.remove()
            downgrade(directory=MIGRATIONS, revision=MATCH_REVISION)
            self.db.session.execute(
                text(
                    "UPDATE sales_realization_line SET inventory_effect = 'trailer_unit' "
                    'WHERE id = :line_id'
                ),
                {'line_id': line_id},
            )
            self.db.session.commit()
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end),
                before,
            )

            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET realization_id = :draft_id '
                    'WHERE id = :line_id'
                ),
                {'draft_id': draft_id, 'line_id': line_id},
            )
            self.db.session.commit()
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end)['march'],
                (0, 1),
            )
            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET realization_id = :sale_id '
                    'WHERE id = :line_id'
                ),
                {'sale_id': sale_id, 'line_id': line_id},
            )
            self.db.session.commit()

            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET realization_id = :sale_id '
                    'WHERE id = :line_id'
                ),
                {'sale_id': sale_id, 'line_id': draft_line_id},
            )
            self.db.session.commit()
            moved = self._period_sold(march_start, march_end, october_start, october_end)
            self.assertEqual(moved['march'], (1, 1))
            self.assertEqual(moved['october'], (1, 1))
            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET realization_id = :draft_id '
                    'WHERE id = :line_id'
                ),
                {'draft_id': draft_id, 'line_id': draft_line_id},
            )
            self.db.session.commit()

            october_output_id = self.db.session.execute(
                text('SELECT shift_output_id FROM produced_unit WHERE id = :unit_id'),
                {'unit_id': foreign_id},
            ).scalar()
            self.db.session.execute(
                text(
                    'UPDATE produced_unit SET shift_output_id = :output_id '
                    'WHERE id = :unit_id'
                ),
                {'output_id': october_output_id, 'unit_id': own_id},
            )
            self.db.session.commit()
            shifted = self._period_sold(march_start, march_end, october_start, october_end)
            self.assertEqual(shifted['march'], (0, 0))
            self.assertEqual(shifted['october'], (1, 2))
            march_output_id = self.db.session.execute(
                text(
                    '''
                    SELECT output.id
                    FROM production_shift_output AS output
                    JOIN production_shift AS shift ON shift.id = output.shift_id
                    JOIN warehouse_storage_area AS area ON area.id = shift.direction_area_id
                    WHERE area.code = 'DIR_FIELD_A'
                    '''
                )
            ).scalar()
            self.db.session.execute(
                text(
                    'UPDATE produced_unit SET shift_output_id = :output_id '
                    'WHERE id = :unit_id'
                ),
                {'output_id': march_output_id, 'unit_id': own_id},
            )
            self.db.session.commit()
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end),
                before,
            )

            self.db.session.execute(
                text('UPDATE sales_realization_line SET trailer_id = :trailer_id WHERE id = :line_id'),
                {'trailer_id': other_trailer_id, 'line_id': line_id},
            )
            self.db.session.execute(
                text('UPDATE produced_unit SET item_id = :item_id WHERE id = :unit_id'),
                {'item_id': other_item_id, 'unit_id': own_id},
            )
            self.db.session.execute(
                text("UPDATE produced_unit SET status = 'produced_no_vin' WHERE id = :unit_id"),
                {'unit_id': own_id},
            )
            self.db.session.commit()
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end),
                before,
            )
            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET trailer_id = :trailer_id WHERE id = :line_id'
                ),
                {'trailer_id': own_trailer_id, 'line_id': line_id},
            )
            self.db.session.execute(
                text(
                    'UPDATE produced_unit SET item_id = :item_id, status = :status '
                    'WHERE id = :unit_id'
                ),
                {'item_id': own_item_id, 'status': 'vin_assigned', 'unit_id': own_id},
            )
            self.db.session.commit()

            self.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=FIELD_REVISION)
            self.assertEqual(self._trigger_names(), set(self._expected_triggers()))
            self.assertEqual(
                self._period_sold(march_start, march_end, october_start, october_end),
                before,
            )
            self._assert_posted_field_guards(
                field,
                line_id=line_id,
                draft_line_id=draft_line_id,
                other_draft_id=other_draft_id,
                own_id=own_id,
                foreign_id=foreign_id,
                other_trailer_id=other_trailer_id,
                other_item_id=other_item_id,
                bare_id=bare_id,
                own_trailer_id=own_trailer_id,
                march_start=march_start,
                march_end=march_end,
                october_start=october_start,
                october_end=october_end,
                before=before,
            )
        finally:
            self.db.session.rollback()
            self.db.session.remove()
            current = self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()
            if current != SCHEMA_REVISION:
                upgrade(directory=MIGRATIONS, revision=SCHEMA_REVISION)

    def _assert_posted_field_guards(
        self,
        field,
        *,
        line_id,
        draft_line_id,
        other_draft_id,
        own_id,
        foreign_id,
        other_trailer_id,
        other_item_id,
        bare_id,
        own_trailer_id,
        march_start,
        march_end,
        october_start,
        october_end,
        before,
    ):
        self._assert_sql_rejected(
            "UPDATE sales_realization_line SET inventory_effect = 'none' WHERE id = :line_id",
            {'line_id': line_id},
            field.POSTED_EFFECT_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE sales_realization_line SET realization_id = :draft_id WHERE id = :line_id',
            {'draft_id': other_draft_id, 'line_id': line_id},
            field.POSTED_DOCUMENT_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE sales_realization_line SET realization_id = :sale_id WHERE id = :line_id',
            {
                'sale_id': self.db.session.execute(
                    text('SELECT realization_id FROM sales_realization_line WHERE id = :line_id'),
                    {'line_id': line_id},
                ).scalar(),
                'line_id': draft_line_id,
            },
            field.POSTED_DOCUMENT_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE sales_realization_line SET trailer_id = :trailer_id WHERE id = :line_id',
            {'trailer_id': other_trailer_id, 'line_id': line_id},
            field.POSTED_LINE_MATCH_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE sales_realization_line SET item_id = :item_id WHERE id = :line_id',
            {'item_id': other_item_id, 'line_id': line_id},
            field.POSTED_LINE_MATCH_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE produced_unit SET shift_output_id = NULL WHERE id = :unit_id',
            {'unit_id': own_id},
            field.POSTED_UNIT_COHORT_MESSAGE,
        )
        foreign_output_id = self.db.session.execute(
            text('SELECT shift_output_id FROM produced_unit WHERE id = :unit_id'),
            {'unit_id': foreign_id},
        ).scalar()
        self._assert_sql_rejected(
            'UPDATE produced_unit SET shift_output_id = :output_id WHERE id = :unit_id',
            {'output_id': foreign_output_id, 'unit_id': own_id},
            field.POSTED_UNIT_COHORT_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE produced_unit SET trailer_id = :trailer_id WHERE id = :unit_id',
            {'trailer_id': other_trailer_id, 'unit_id': own_id},
            field.POSTED_UNIT_COHORT_MESSAGE,
        )
        self._assert_sql_rejected(
            'UPDATE produced_unit SET item_id = :item_id WHERE id = :unit_id',
            {'item_id': other_item_id, 'unit_id': own_id},
            field.POSTED_UNIT_COHORT_MESSAGE,
        )
        self.assertEqual(
            self._period_sold(march_start, march_end, october_start, october_end),
            before,
        )

        self.db.session.execute(
            text(
                "UPDATE produced_unit SET status = 'released' WHERE id = :unit_id"
            ),
            {'unit_id': own_id},
        )
        self.db.session.commit()
        self.assertEqual(
            self._period_sold(march_start, march_end, october_start, october_end),
            before,
        )
        self.db.session.execute(
            text("UPDATE produced_unit SET status = 'vin_assigned' WHERE id = :unit_id"),
            {'unit_id': own_id},
        )
        self.db.session.execute(
            text(
                'UPDATE sales_realization_line SET unit_price = :price, comment = :comment, '
                'quantity = :quantity WHERE id = :line_id'
            ),
            {'price': '125.00', 'comment': 'цена черновика не ключ', 'quantity': 1, 'line_id': line_id},
        )
        self.db.session.commit()

        self.db.session.execute(
            text(
                "UPDATE produced_unit SET status = 'vin_assigned', trailer_id = :trailer_id "
                'WHERE id = :unit_id'
            ),
            {'trailer_id': own_trailer_id, 'unit_id': bare_id},
        )
        self.db.session.commit()
        self.assertEqual(
            self.db.session.execute(
                text('SELECT status FROM produced_unit WHERE id = :unit_id'),
                {'unit_id': bare_id},
            ).scalar(),
            'vin_assigned',
        )
        original_draft_id = self.db.session.execute(
            text('SELECT realization_id FROM sales_realization_line WHERE id = :line_id'),
            {'line_id': draft_line_id},
        ).scalar()
        self.db.session.execute(
            text(
                'UPDATE sales_realization_line SET trailer_id = :trailer_id, '
                "inventory_effect = 'ship_from_stock' WHERE id = :line_id"
            ),
            {'trailer_id': other_trailer_id, 'line_id': draft_line_id},
        )
        self.db.session.commit()
        self.db.session.execute(
            text(
                'UPDATE sales_realization_line SET realization_id = :draft_id, '
                "inventory_effect = 'trailer_unit', trailer_id = :trailer_id "
                'WHERE id = :line_id'
            ),
            {
                'draft_id': other_draft_id,
                'trailer_id': self.db.session.execute(
                    text('SELECT trailer_id FROM produced_unit WHERE id = :unit_id'),
                    {'unit_id': foreign_id},
                ).scalar(),
                'line_id': draft_line_id,
            },
        )
        self.db.session.commit()
        self.db.session.execute(
            text('UPDATE sales_realization_line SET realization_id = :draft_id WHERE id = :line_id'),
            {'draft_id': original_draft_id, 'line_id': draft_line_id},
        )
        self.db.session.commit()
        self.db.session.execute(
            text(
                'UPDATE produced_unit SET item_id = :item_id WHERE id = :unit_id'
            ),
            {'item_id': other_item_id, 'unit_id': bare_id},
        )
        self.db.session.commit()
        self.assertEqual(
            self._period_sold(march_start, march_end, october_start, october_end),
            before,
        )
        self.assertEqual(int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar()), 0)

    def _assert_sql_rejected(self, sql: str, params: dict, message: str):
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(text(sql), params)
            self.db.session.commit()
        self.assertIn(message, str(caught.exception))
        self.db.session.rollback()

    def _backup_sqlite(self) -> Path:
        import sqlite3

        handle = tempfile.NamedTemporaryFile(suffix='-batch-copy.db', delete=False)
        handle.close()
        copy = Path(handle.name)
        _refuse_live('sqlite:///' + str(copy), copy)
        self.db.session.commit()
        source = sqlite3.connect(self.db_path)
        dest = sqlite3.connect(copy)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        return copy

    def _restore_model_columns_below_schema_revision(self):
        """Модель читает четыре колонки и ниже f6b2d8c14e90 их снова нет.

        На самой ревизии колонки уже созданы миграцией, ALTER не делается.
        После отката они нужны только этому тесту, чтобы отчёт мог прочитать Item.
        """
        version = self.db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        if version == SCHEMA_REVISION:
            return
        changed = False
        for table, column, coltype in MODEL_DRIFT_COLUMNS:
            present = {
                row[1]
                for row in self.db.session.execute(text(f'PRAGMA table_info({table})')).all()
            }
            if column not in present:
                self.db.session.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {column} {coltype}'
                ))
                changed = True
        if changed:
            self.db.session.commit()

    def _period_sold(self, march_start, march_end, october_start, october_end):
        from shift_director_report import build_shift_director_report

        self._restore_model_columns_below_schema_revision()
        march = build_shift_director_report(period_start=march_start, period_end=march_end)
        october = build_shift_director_report(period_start=october_start, period_end=october_end)
        return {
            'march': (
                march['sold_from_produced']['value'],
                march['sold_from_produced']['cohort_units'],
            ),
            'october': (
                october['sold_from_produced']['value'],
                october['sold_from_produced']['cohort_units'],
            ),
        }

    def _assert_rebind_rejected(self, line_id: int, unit_id: int):
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(
                text(
                    'UPDATE sales_realization_line SET produced_unit_id = :unit_id '
                    'WHERE id = :line_id'
                ),
                {'unit_id': unit_id, 'line_id': line_id},
            )
            self.db.session.commit()
        self.assertIn(_match_module().POSTED_REBIND_MESSAGE, str(caught.exception))
        self.db.session.rollback()

    def _assert_parent_id_rejected(self, unit_id: int, new_id: int):
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(
                text('UPDATE produced_unit SET id = :new_id WHERE id = :unit_id'),
                {'new_id': new_id, 'unit_id': unit_id},
            )
            self.db.session.commit()
        self.assertIn(_match_module().PARENT_ID_MESSAGE, str(caught.exception))
        self.db.session.rollback()

    def _match_trigger_names(self):
        return tuple(_guard_module().TRIGGER_NAMES) + tuple(_match_module().TRIGGER_NAMES)

    def _expected_triggers(self):
        return self._match_trigger_names() + tuple(_field_module().TRIGGER_NAMES)

    def _trigger_names(self):
        rows = self.db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )).all()
        return {row[0] for row in rows}

    def _comment_unit(self, comment: str):
        return self.db.session.execute(text(
            'SELECT produced_unit_id FROM sales_realization_line WHERE comment = :comment'
        ), {'comment': comment}).scalar()

    def _line_unit(self, line_id: int):
        return self.db.session.execute(text(
            'SELECT produced_unit_id FROM sales_realization_line WHERE id = :line_id'
        ), {'line_id': line_id}).scalar()

    def _count_unit_links(self, unit_id: int) -> int:
        return self.db.session.execute(text(
            'SELECT COUNT(*) FROM sales_realization_line WHERE produced_unit_id = :unit_id'
        ), {'unit_id': unit_id}).scalar()

    def _assert_missing_rejected(self, sql: str):
        guard = _guard_module()

        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(text(sql))
            self.db.session.commit()
        self.assertIn(guard.MISSING_UNIT_MESSAGE, str(caught.exception))
        self.db.session.rollback()

    def _seed_master(self):
        from models import (
            Customer,
            Item,
            ProductionRequest,
            ProductionRequestLine,
            User,
            Warehouse,
        )

        self.director = User.query.filter_by(username='guard-director').first()
        if self.director is not None:
            self.warehouse = Warehouse.query.filter_by(name='Склад guard').one()
            self.item = Item.query.filter_by(article='GUARD-T').one()
            self.other_item = Item.query.filter_by(article='GUARD-T2').one()
            self.customer = Customer.query.filter_by(name='Покупатель guard').one()
            self.request_line = ProductionRequestLine.query.filter_by(
                production_request_id=ProductionRequest.query.filter_by(
                    request_number='PR-GUARD-1',
                ).one().id,
            ).one()
            return
        self.warehouse = Warehouse(name='Склад guard', is_active=True)
        self.db.session.add(self.warehouse)
        self.db.session.flush()
        self.item = Item(item_type='TRAILER', article='GUARD-T', name='Прицеп guard', unit='шт')
        self.other_item = Item(item_type='TRAILER', article='GUARD-T2', name='Другой guard', unit='шт')
        self.db.session.add_all([self.item, self.other_item])
        self.db.session.flush()
        self.customer = Customer(customer_type='PERSON', name='Покупатель guard')
        self.director = User(username='guard-director', full_name='Директор guard', role='director')
        self.director.set_password('x')
        self.db.session.add_all([self.customer, self.director])
        self.db.session.flush()
        request = ProductionRequest(request_number='PR-GUARD-1', status='in_progress')
        self.db.session.add(request)
        self.db.session.flush()
        self.request_line = ProductionRequestLine(
            production_request_id=request.id,
            item_id=self.item.id,
            quantity=1,
        )
        self.db.session.add(self.request_line)
        self.db.session.commit()

    def _other_item(self):
        self._seed_master()
        return self.other_item

    def _trailer(self, vin: str, item=None):
        from models import Trailer

        self._seed_master()
        trailer = Trailer(
            vin=vin,
            item_id=(item or self.item).id,
            warehouse_id=self.warehouse.id,
            status='IN_STOCK',
        )
        self.db.session.add(trailer)
        self.db.session.flush()
        return trailer

    def _unit(self, *, trailer=None, item=None, shift_output_id=None, status='vin_assigned'):
        from models import ProducedUnit

        self._seed_master()
        unit = ProducedUnit(
            production_request_line_id=self.request_line.id,
            item_id=(item or self.item).id,
            trailer_id=trailer.id if trailer else None,
            shift_output_id=shift_output_id,
            status=status,
        )
        self.db.session.add(unit)
        self.db.session.flush()
        return unit

    def _realization(self, trailer, lines):
        from models import SalesRealization, SalesRealizationLine

        self._seed_master()
        realization = SalesRealization(
            realization_date=date.today(),
            customer_id=self.customer.id,
            warehouse_id=self.warehouse.id,
            status='draft',
            total_amount=Decimal('100'),
        )
        self.db.session.add(realization)
        self.db.session.flush()
        for index, spec in enumerate(lines, start=1):
            self.db.session.add(SalesRealizationLine(
                realization_id=realization.id,
                line_no=index,
                line_type='trailer',
                trailer_id=spec.get('trailer_id', trailer.id),
                item_id=spec.get('item_id', self.item.id),
                quantity=Decimal('1'),
                unit='шт',
                inventory_effect='trailer_unit',
            ))
        self.db.session.commit()
        return realization

    def _orm_line(self, realization, line_no=1):
        from models import SalesRealizationLine

        return SalesRealizationLine.query.filter_by(
            realization_id=realization.id,
            line_no=line_no,
        ).one()

    def _client(self):
        self._seed_master()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.director.id)
            sess['_fresh'] = True
        return client

    def _sale_order(self, trailer):
        from models import CustomerOrder, CustomerOrderLine, OrderPayment, SalesContract, VinRegistry

        self._seed_master()
        order = CustomerOrder(
            order_number=f'ORD-GUARD-{trailer.id:06d}',
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
        serial = f'{int(trailer.id):07d}'
        self.db.session.add(VinRegistry(
            vin_full=trailer.vin,
            serial7=serial,
            status='confirmed',
            customer_order_id=order.id,
            order_line_id=line.id,
            trailer_id=trailer.id,
        ))
        self.db.session.add(SalesContract(
            contract_number=f'SC-GUARD-{trailer.id:06d}',
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

    def _draft_realization(self, order, trailer):
        from models import CustomerOrderLine, SalesRealization, SalesRealizationLine

        line = CustomerOrderLine.query.filter_by(order_id=order.id).one()
        realization = SalesRealization(
            realization_date=date.today(),
            order_id=order.id,
            customer_id=self.customer.id,
            warehouse_id=self.warehouse.id,
            status='draft',
            total_amount=Decimal('100'),
        )
        self.db.session.add(realization)
        self.db.session.flush()
        self.db.session.add(SalesRealizationLine(
            realization_id=realization.id,
            line_no=1,
            line_type='trailer',
            order_line_id=line.id,
            trailer_id=trailer.id,
            item_id=self.item.id,
            quantity=Decimal('1'),
            unit='шт',
            inventory_effect='trailer_unit',
            vin_full=trailer.vin,
        ))
        self.db.session.commit()
        return realization

    def _cohort_unit(
        self,
        trailer,
        posted_at: datetime,
        *,
        direction_code: str = 'DIR_LIGHT',
        direction_name: str = 'Легковое',
        item=None,
    ):
        from models import (
            ProductionEmployee,
            ProductionShift,
            ProductionShiftOutput,
            WarehouseStorageArea,
        )

        self._seed_master()
        item = item or self.item
        area = WarehouseStorageArea(
            warehouse_id=self.warehouse.id,
            code=direction_code,
            name=direction_name,
            area_type='finished',
        )
        employee = ProductionEmployee(full_name='Сборщик guard')
        self.db.session.add_all([area, employee])
        self.db.session.flush()
        shift = ProductionShift(
            employee_id=employee.id,
            work_area='production',
            direction_warehouse_id=self.warehouse.id,
            direction_area_id=area.id,
            status='posted',
            posted_at=posted_at,
            hours_fact=Decimal('8'),
        )
        self.db.session.add(shift)
        self.db.session.flush()
        output = ProductionShiftOutput(
            shift_id=shift.id,
            employee_id=employee.id,
            item_id=item.id,
            output_type='trailer',
            quantity=Decimal('1'),
            defect_quantity=Decimal('0'),
            unit='шт',
            status='posted',
        )
        self.db.session.add(output)
        self.db.session.flush()
        return self._unit(trailer=trailer, item=item, shift_output_id=output.id)

    def _posted_cohort_sale(self, unit, realization_date: date):
        from models import SalesRealization, SalesRealizationLine

        self._seed_master()
        realization = SalesRealization(
            realization_date=realization_date,
            customer_id=self.customer.id,
            warehouse_id=self.warehouse.id,
            status='posted',
            total_amount=Decimal('100'),
            posted_at=datetime(2026, 8, 20, 9, 0, 0),
        )
        self.db.session.add(realization)
        self.db.session.flush()
        self.db.session.add(SalesRealizationLine(
            realization_id=realization.id,
            line_no=1,
            line_type='trailer',
            trailer_id=unit.trailer_id,
            item_id=unit.item_id,
            quantity=Decimal('1'),
            unit='шт',
            inventory_effect='trailer_unit',
            produced_unit_id=unit.id,
        ))
        self.db.session.commit()
        return realization


if __name__ == '__main__':
    unittest.main()
