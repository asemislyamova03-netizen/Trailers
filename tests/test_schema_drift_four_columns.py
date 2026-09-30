#!/usr/bin/env python3
"""Четыре колонки модели на новой временной SQLite.

Цепочка только Alembic, без db.create_all и без ручного выравнивания схемы.
Рабочая trailers.db не открывается. PRAGMA foreign_keys остаётся 0.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
PREVIOUS_REVISION = 'e1b7c4d92a58'
SCHEMA_REVISION = 'f6b2d8c14e90'
LEDGER = 'schema_column_origin_f6b2d8c14e90'
ABORT_ENV = 'TRAILERS_F6B2D8C14E90_ABORT_AFTER'
COLUMNS = (
    ('item', 'tent_hight_mm', 'INTEGER'),
    ('item', 'has_jockey_wheel', 'BOOLEAN'),
    ('trailer', 'otts_id', 'INTEGER'),
    ('otts', 'full_mass_kg', 'INTEGER'),
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


def _error_text(exc: BaseException) -> str:
    parts = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return '\n'.join(parts)


class SchemaDriftFourColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stat = LIVE_DB.stat() if LIVE_DB.exists() else None
        fd, name = tempfile.mkstemp(suffix='-schema-drift.sqlite')
        os.close(fd)
        cls.work = Path(name)
        fd, base_name = tempfile.mkstemp(suffix='-schema-drift-base.sqlite')
        os.close(fd)
        cls.base = Path(base_name)
        _refuse_live('sqlite:///' + cls.work.as_posix(), cls.work)
        cls.app, cls.db = _bind_app(cls.work)
        with cls.app.app_context():
            engine_url = str(cls.db.engine.url).replace('\\', '/')
            _refuse_live(engine_url, cls.work)
            if 'foreign_keys' in (ROOT / 'app.py').read_text(encoding='utf-8').lower():
                raise RuntimeError('app.py must not enable PRAGMA foreign_keys')
            from flask_migrate import upgrade

            cls.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
            cls.db.session.remove()
            cls.db.engine.dispose()
        shutil.copy(cls.work, cls.base)

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.work.unlink(missing_ok=True)
        cls.base.unlink(missing_ok=True)
        if cls.live_stat is not None and LIVE_DB.exists():
            current = LIVE_DB.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                cls.live_stat.st_ino,
                cls.live_stat.st_size,
                cls.live_stat.st_mtime_ns,
            ):
                raise RuntimeError('live trailers.db changed during schema drift tests')

    def setUp(self):
        os.environ.pop(ABORT_ENV, None)
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        shutil.copy(self.base, self.work)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])

    def tearDown(self):
        os.environ.pop(ABORT_ENV, None)
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_empty_chain_adds_nullable_columns_and_downgrade_removes_only_them(self):
        from alembic.script import ScriptDirectory
        from models import Item, OTTS, Trailer

        self.assertEqual(ScriptDirectory(MIGRATIONS).get_heads(), [SCHEMA_REVISION])
        self._assert_orm_missing()
        ids = self._insert_rows()
        self._upgrade()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        for table, column, expected in COLUMNS:
            self._assert_shape(table, column, expected)
        self.assertIsNone(self._column('otts', 'full_weight_kg'))
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertIsNone(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids))
        self.assertIsNone(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids))
        self.assertIsNone(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids))
        self.assertIsNone(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids))
        self.assertEqual(Item.query.filter_by(name='Синтетика drift').count(), 1)
        self.assertEqual(Trailer.query.filter_by(vin='VIN-DRIFT-0001').count(), 1)
        self.assertEqual(OTTS.query.filter_by(number='ОТТС-СИНТ').count(), 1)
        referred = {
            row[2]
            for row in self.db.session.execute(text('PRAGMA foreign_key_list(trailer)')).all()
        }
        self.assertNotIn('otts', referred)
        self.db.session.execute(
            text('UPDATE trailer SET otts_id = 919999 WHERE id = :trailer_id'),
            ids,
        )
        self.db.session.commit()
        self.assertEqual(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids), 919999)

        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, _expected in COLUMNS:
            self.assertIsNone(self._column(table, column))
        self.assertIsNone(self._ledger())
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._scalar('SELECT vin FROM trailer WHERE id = :trailer_id', ids), 'VIN-DRIFT-0001')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)
        self._assert_orm_missing()

    def test_repeat_upgrade_does_not_rewrite_values(self):
        ids = self._insert_rows()
        self._upgrade()
        self.db.session.execute(
            text(
                'UPDATE item SET tent_hight_mm = 1818, has_jockey_wheel = 1 '
                'WHERE id = :id'
            ),
            ids,
        )
        self.db.session.execute(
            text('UPDATE otts SET full_mass_kg = 3500 WHERE id = :otts_id'),
            ids,
        )
        self.db.session.commit()
        self._upgrade()
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids), 3500)
        self.db.session.execute(
            text('UPDATE alembic_version SET version_num = :revision'),
            {'revision': PREVIOUS_REVISION},
        )
        self.db.session.commit()
        self._upgrade()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids), 3500)
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])

    def test_partial_compatible_columns_keep_nonzero_and_downgrade_drops_only_added(self):
        self.db.session.execute(text('ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER'))
        self.db.session.execute(text('ALTER TABLE item ADD COLUMN has_jockey_wheel BOOLEAN'))
        self.db.session.commit()
        ids = self._insert_rows(tent_hight_mm=1777, has_jockey_wheel=1)
        self._upgrade()
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1777)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertIsNone(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids))
        self.assertIsNone(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids))
        self._assert_shape('trailer', 'otts_id', 'INTEGER')
        self._assert_shape('otts', 'full_mass_kg', 'INTEGER')
        self.assertEqual(
            self._ledger(),
            {('trailer', 'otts_id'), ('otts', 'full_mass_kg')},
        )
        self._downgrade()
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1777)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertIsNone(self._column('trailer', 'otts_id'))
        self.assertIsNone(self._column('otts', 'full_mass_kg'))
        self.assertIsNone(self._ledger())
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])

    def test_all_compatible_preexisting_columns_survive_downgrade(self):
        from models import Item, OTTS, Trailer

        self.db.session.execute(text('ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER'))
        self.db.session.execute(text('ALTER TABLE item ADD COLUMN has_jockey_wheel BOOLEAN'))
        self.db.session.execute(text('ALTER TABLE trailer ADD COLUMN otts_id INTEGER'))
        self.db.session.execute(text('ALTER TABLE otts ADD COLUMN full_mass_kg INTEGER'))
        self.db.session.commit()
        ids = self._insert_rows(
            tent_hight_mm=1818,
            has_jockey_wheel=1,
            otts_id=4242,
            full_mass_kg=3500,
        )
        self._upgrade()
        self.assertEqual(self._ledger(), set())
        self.assertTrue(self._ledger_exists())
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids), 4242)
        self.assertEqual(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids), 3500)
        self._downgrade()
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids), 4242)
        self.assertEqual(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids), 3500)
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(Item.query.filter_by(name='Синтетика drift').one().tent_hight_mm, 1818)
        self.assertEqual(Trailer.query.filter_by(vin='VIN-DRIFT-0001').one().otts_id, 4242)
        self.assertEqual(OTTS.query.filter_by(number='ОТТС-СИНТ').one().full_mass_kg, 3500)
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)

    def test_incompatible_fourth_column_adds_nothing(self):
        self.db.session.execute(text('ALTER TABLE otts ADD COLUMN full_mass_kg TEXT'))
        self.db.session.commit()
        ids = self._insert_rows(full_mass_kg='не-число')
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('otts.full_mass_kg', _error_text(caught.exception))
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._column('item', 'tent_hight_mm'))
        self.assertIsNone(self._column('item', 'has_jockey_wheel'))
        self.assertIsNone(self._column('trailer', 'otts_id'))
        row = self._column('otts', 'full_mass_kg')
        self.assertEqual(row[2].upper(), 'TEXT')
        self.assertEqual(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids), 'не-число')
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)

    def test_incompatible_type_nullability_and_default_add_nothing(self):
        self.db.session.execute(text('ALTER TABLE item ADD COLUMN tent_hight_mm INT'))
        self.db.session.execute(text(
            'ALTER TABLE item ADD COLUMN has_jockey_wheel BOOLEAN DEFAULT 1'
        ))
        self.db.session.execute(text(
            'ALTER TABLE otts ADD COLUMN full_mass_kg INTEGER NOT NULL DEFAULT 1'
        ))
        self.db.session.commit()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        message = _error_text(caught.exception)
        self.assertIn('item.tent_hight_mm', message)
        self.assertIn('item.has_jockey_wheel', message)
        self.assertIn('otts.full_mass_kg', message)
        self.assertIn('NOT NULL', message)
        self.assertIn('default', message)
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._column('item', 'tent_hight_mm')[2].upper(), 'INT')
        self.assertEqual(self._column('item', 'has_jockey_wheel')[4], '1')
        self.assertEqual(self._column('otts', 'full_mass_kg')[3], 1)
        self.assertIsNone(self._column('trailer', 'otts_id'))
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._temp_tables(), [])

    def test_partial_upgrade_failure_rolls_back_then_clean_upgrade_works(self):
        ids = self._insert_rows()
        os.environ[ABORT_ENV] = '2'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('остановка до COMMIT', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, _expected in COLUMNS:
            self.assertIsNone(self._column(table, column), column)
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        os.environ.pop(ABORT_ENV, None)
        self._upgrade()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        for table, column, expected in COLUMNS:
            self._assert_shape(table, column, expected)
        self.assertIsNone(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids))
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})

    def test_downgrade_without_ledger_keeps_columns(self):
        ids = self._insert_rows()
        self._upgrade()
        self.db.session.execute(
            text('UPDATE item SET tent_hight_mm = 1919 WHERE id = :id'),
            ids,
        )
        self.db.session.execute(text(f'DROP TABLE {LEDGER}'))
        self.db.session.commit()
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('нет таблицы происхождения', _error_text(caught.exception))
        self.assertIn('Колонки не удаляются', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self._assert_shape('trailer', 'otts_id', 'INTEGER')
        self._assert_shape('otts', 'full_mass_kg', 'INTEGER')
        self.assertFalse(self._ledger_exists())

    def test_partial_downgrade_failure_rolls_back(self):
        ids = self._insert_rows()
        self._upgrade()
        self.db.session.execute(
            text('UPDATE item SET tent_hight_mm = 1919, has_jockey_wheel = 1 WHERE id = :id'),
            ids,
        )
        self.db.session.commit()
        os.environ[ABORT_ENV] = '1'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('остановка до COMMIT', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        os.environ.pop(ABORT_ENV, None)
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._column('item', 'tent_hight_mm'))
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertIsNone(self._ledger())
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])

    def test_unknown_ledger_row_blocks_downgrade_and_upgrade(self):
        self.db.session.execute(text(
            f'CREATE TABLE {LEDGER} ('
            'table_name TEXT NOT NULL, column_name TEXT NOT NULL, '
            'PRIMARY KEY (table_name, column_name))'
        ))
        self.db.session.execute(text(
            f"INSERT INTO {LEDGER} (table_name, column_name) VALUES ('item', 'not_ours')"
        ))
        self.db.session.commit()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('not_ours', _error_text(caught.exception))
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, _expected in COLUMNS:
            self.assertIsNone(self._column(table, column))
        self.assertEqual(
            self._scalar(f"SELECT column_name FROM {LEDGER} WHERE table_name = 'item'"),
            'not_ours',
        )

        self.db.session.execute(text(f'DROP TABLE {LEDGER}'))
        self.db.session.commit()
        self._upgrade()
        self.db.session.execute(text(
            f"INSERT INTO {LEDGER} (table_name, column_name) VALUES ('item', 'not_ours')"
        ))
        self.db.session.commit()
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('неизвестные строки', _error_text(caught.exception))
        self.assertIn('Колонки не удаляются', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self._assert_shape('item', 'tent_hight_mm', 'INTEGER')
        self.assertIn(('item', 'not_ours'), self._ledger())

    def _config(self):
        from flask import current_app

        return current_app.extensions['migrate'].migrate.get_config(MIGRATIONS)

    def _upgrade(self):
        from alembic import command

        self._reset()
        command.upgrade(self._config(), SCHEMA_REVISION)

    def _downgrade(self):
        from alembic import command

        self._reset()
        command.downgrade(self._config(), PREVIOUS_REVISION)

    def _reset(self):
        self.db.session.rollback()
        self.db.session.remove()

    def _version(self):
        return self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()

    def _foreign_keys(self) -> int:
        return int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar())

    def _trigger_count(self) -> int:
        return int(self.db.session.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
        )).scalar())

    def _temp_tables(self):
        return [
            row[0]
            for row in self.db.session.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '%alembic_tmp%'"
            )).all()
        ]

    def _column(self, table: str, column: str):
        for row in self.db.session.execute(text(f'PRAGMA table_info({table})')).all():
            if row[1] == column:
                return row
        return None

    def _assert_shape(self, table: str, column: str, expected_type: str):
        row = self._column(table, column)
        self.assertIsNotNone(row, f'{table}.{column}')
        self.assertEqual((row[2] or '').strip().upper(), expected_type)
        self.assertEqual(row[3], 0)
        self.assertIsNone(row[4])
        self.assertEqual(row[5], 0)

    def _ledger_exists(self) -> bool:
        return self.db.session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {'name': LEDGER},
        ).scalar() is not None

    def _ledger(self):
        if not self._ledger_exists():
            return None
        return {
            (row[0], row[1])
            for row in self.db.session.execute(text(
                f'SELECT table_name, column_name FROM {LEDGER}'
            )).all()
        }

    def _scalar(self, sql: str, params=None):
        return self.db.session.execute(text(sql), params or {}).scalar()

    def _assert_orm_missing(self):
        from models import Item, OTTS, Trailer

        for model in (Item, Trailer, OTTS):
            with self.assertRaises(OperationalError) as caught:
                model.query.all()
            self.assertIn('no such column', str(caught.exception).lower())
            self._reset()

    def _insert_rows(self, **columns):
        item_extra = ''
        item_values = ''
        params = {
            'item_type': 'TRAILER',
            'name': 'Синтетика drift',
            'unit': 'шт',
        }
        for name in ('tent_hight_mm', 'has_jockey_wheel'):
            if name in columns:
                item_extra += f', {name}'
                item_values += f', :{name}'
                params[name] = columns[name]
        self.db.session.execute(text(
            'INSERT INTO warehouse (name, is_active, is_production) '
            "VALUES ('Склад drift', 1, 0)"
        ))
        self.db.session.execute(text(
            f'INSERT INTO item (item_type, name, unit, is_active{item_extra}) '
            f"VALUES (:item_type, :name, :unit, 1{item_values})"
        ), params)
        item_id = self.db.session.execute(text('SELECT last_insert_rowid()')).scalar()
        otts_extra = ''
        otts_values = ''
        otts_params = {
            'number': 'ОТТС-СИНТ',
            'modification': '002',
            'otts_name': 'Синтетика ОТТС',
        }
        if 'full_mass_kg' in columns:
            otts_extra = ', full_mass_kg'
            otts_values = ', :full_mass_kg'
            otts_params['full_mass_kg'] = columns['full_mass_kg']
        self.db.session.execute(text(
            'INSERT INTO otts (number, modification, name, axle_count, is_active'
            f'{otts_extra}) VALUES (:number, :modification, :otts_name, 1, 1{otts_values})'
        ), otts_params)
        otts_id = self.db.session.execute(text('SELECT last_insert_rowid()')).scalar()
        warehouse_id = self.db.session.execute(text(
            "SELECT id FROM warehouse WHERE name = 'Склад drift'"
        )).scalar()
        trailer_extra = ''
        trailer_values = ''
        trailer_params = {
            'vin': 'VIN-DRIFT-0001',
            'item_id': item_id,
            'warehouse_id': warehouse_id,
        }
        if 'otts_id' in columns:
            trailer_extra = ', otts_id'
            trailer_values = ', :otts_link'
            trailer_params['otts_link'] = columns['otts_id']
        self.db.session.execute(text(
            'INSERT INTO trailer (vin, item_id, warehouse_id, created_at, status'
            f'{trailer_extra}) VALUES (:vin, :item_id, :warehouse_id, '
            f"'2026-01-01 00:00:00', 'IN_STOCK'{trailer_values})"
        ), trailer_params)
        trailer_id = self.db.session.execute(text('SELECT last_insert_rowid()')).scalar()
        self.db.session.commit()
        return {'id': item_id, 'otts_id': otts_id, 'trailer_id': trailer_id}
