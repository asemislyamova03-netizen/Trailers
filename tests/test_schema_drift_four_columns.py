#!/usr/bin/env python3
"""Четыре колонки модели на новой временной SQLite.

Цепочка только Alembic, без db.create_all и без ручного выравнивания схемы.
Рабочая trailers.db не открывается. PRAGMA foreign_keys остаётся 0.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
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
CLOSE_ENV = 'TRAILERS_F6B2D8C14E90_CLOSE_AFTER'
DOWNGRADE_ABORT_ENV = 'TRAILERS_F6B2D8C14E90_DOWNGRADE_ABORT'
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
        os.environ.pop(CLOSE_ENV, None)
        os.environ.pop(DOWNGRADE_ABORT_ENV, None)
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
        os.environ.pop(CLOSE_ENV, None)
        os.environ.pop(DOWNGRADE_ABORT_ENV, None)
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_empty_chain_adds_nullable_columns_and_downgrade_keeps_them(self):
        from alembic.script import ScriptDirectory
        from models import Item, OTTS, Trailer

        directory = ScriptDirectory(MIGRATIONS)
        heads = directory.get_heads()
        self.assertEqual(len(heads), 1)
        revision = directory.get_revision(heads[0])
        line = []
        while revision is not None:
            line.append(revision.revision)
            down = revision.down_revision
            if down is None:
                break
            self.assertIsInstance(down, str)
            revision = directory.get_revision(down)
        # f6b2d8c14e90 больше не head: следом идёт узкий триггер posted_at.
        # Ревизия четырёх колонок остаётся на единственной линейной цепочке.
        self.assertIn(SCHEMA_REVISION, line)
        self.assertLess(line.index(heads[0]), line.index(SCHEMA_REVISION))
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

        self.db.session.execute(
            text('UPDATE item SET tent_hight_mm = 1818 WHERE id = :id'),
            ids,
        )
        self.db.session.commit()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, expected in COLUMNS:
            self._assert_shape(table, column, expected)
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._scalar('SELECT vin FROM trailer WHERE id = :trailer_id', ids), 'VIN-DRIFT-0001')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(Item.query.filter_by(name='Синтетика drift').one().tent_hight_mm, 1818)
        durable_version, durable_tent = self._durable('item', 'tent_hight_mm')
        self.assertEqual(durable_version, PREVIOUS_REVISION)
        self.assertTrue(durable_tent)
        self._upgrade()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)

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

    def test_partial_compatible_columns_keep_nonzero_and_downgrade_keeps_added(self):
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
        self._assert_shape('trailer', 'otts_id', 'INTEGER')
        self._assert_shape('otts', 'full_mass_kg', 'INTEGER')
        self.assertIsNone(self._scalar('SELECT otts_id FROM trailer WHERE id = :trailer_id', ids))
        self.assertIsNone(self._scalar('SELECT full_mass_kg FROM otts WHERE id = :otts_id', ids))
        self.assertEqual(
            self._ledger(),
            {('trailer', 'otts_id'), ('otts', 'full_mass_kg')},
        )
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
        self.assertIsNone(self._ledger())
        self.assertFalse(self._ledger_exists())
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
        self.assertIn('до записи alembic_version', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, _expected in COLUMNS:
            self.assertIsNone(self._column(table, column), column)
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        durable_version, durable_tent = self._durable('item', 'tent_hight_mm')
        self.assertEqual(durable_version, PREVIOUS_REVISION)
        self.assertFalse(durable_tent)
        os.environ.pop(ABORT_ENV, None)
        self._upgrade()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        for table, column, expected in COLUMNS:
            self._assert_shape(table, column, expected)
        self.assertIsNone(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids))
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})

    def test_upgrade_abort_after_all_ddl_keeps_old_version_and_schema(self):
        ids = self._insert_rows()
        os.environ[ABORT_ENV] = 'end'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('после DDL до записи alembic_version', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        version, tent = self._durable('item', 'tent_hight_mm')
        self.assertEqual(version, PREVIOUS_REVISION)
        self.assertFalse(tent)
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)

    def test_upgrade_close_after_ddl_keeps_old_version_and_schema(self):
        ids = self._insert_rows()
        os.environ[CLOSE_ENV] = 'end'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('соединение закрыто после DDL', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        version, tent = self._durable('item', 'tent_hight_mm')
        self.assertEqual(version, PREVIOUS_REVISION)
        self.assertFalse(tent)
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._foreign_keys(), 0)

    def test_downgrade_without_ledger_keeps_columns_and_moves_version(self):
        ids = self._insert_rows()
        self._upgrade()
        self.db.session.execute(
            text('UPDATE item SET tent_hight_mm = 1919 WHERE id = :id'),
            ids,
        )
        self.db.session.execute(text(f'DROP TABLE {LEDGER}'))
        self.db.session.commit()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self._assert_shape('item', 'tent_hight_mm', 'INTEGER')
        self._assert_shape('trailer', 'otts_id', 'INTEGER')
        self._assert_shape('otts', 'full_mass_kg', 'INTEGER')
        self.assertFalse(self._ledger_exists())
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._foreign_keys(), 0)

    def test_downgrade_exception_and_close_keep_version_and_columns(self):
        ids = self._insert_rows()
        self._upgrade()
        self.db.session.execute(
            text('UPDATE item SET tent_hight_mm = 1919, has_jockey_wheel = 1 WHERE id = :id'),
            ids,
        )
        self.db.session.commit()
        os.environ[DOWNGRADE_ABORT_ENV] = 'raise'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('исключение до записи alembic_version', _error_text(caught.exception))
        self.assertIn('DDL нет', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), SCHEMA_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self.assertEqual(self._scalar('SELECT has_jockey_wheel FROM item WHERE id = :id', ids), 1)
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})

        os.environ[DOWNGRADE_ABORT_ENV] = 'close'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('соединение закрыто до записи alembic_version', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        version, tent = self._durable('item', 'tent_hight_mm')
        self.assertEqual(version, SCHEMA_REVISION)
        self.assertTrue(tent)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        os.environ.pop(DOWNGRADE_ABORT_ENV, None)
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1919)
        self._assert_shape('item', 'tent_hight_mm', 'INTEGER')
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._scalar('SELECT name FROM item WHERE id = :id', ids), 'Синтетика drift')

    def test_ledger_name_without_column_refuses_before_ddl(self):
        self._create_ledger()
        self.db.session.execute(text(
            f"INSERT INTO {LEDGER} (table_name, column_name) VALUES ('item', 'tent_hight_mm')"
        ))
        self.db.session.commit()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        message = _error_text(caught.exception)
        self.assertIn('item.tent_hight_mm', message)
        self.assertIn('колонки нет', message)
        self.assertIn('остановлена до DDL', message)
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        for table, column, _expected in COLUMNS:
            self.assertIsNone(self._column(table, column))
        self.assertEqual(
            self._scalar(
                f"SELECT column_name FROM {LEDGER} WHERE table_name = 'item'"
            ),
            'tent_hight_mm',
        )
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._temp_tables(), [])
        self.assertEqual(self._foreign_keys(), 0)

    def test_preexisting_column_listed_in_ledger_is_not_dropped(self):
        self.db.session.execute(text('ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER'))
        self.db.session.commit()
        ids = self._insert_rows(tent_hight_mm=1818)
        self._create_ledger()
        self.db.session.execute(text(
            f"INSERT INTO {LEDGER} (table_name, column_name) VALUES ('item', 'tent_hight_mm')"
        ))
        self.db.session.commit()
        self._upgrade()
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self._assert_shape('item', 'has_jockey_wheel', 'BOOLEAN')
        self._assert_shape('trailer', 'otts_id', 'INTEGER')
        self._assert_shape('otts', 'full_mass_kg', 'INTEGER')
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self._assert_shape('item', 'tent_hight_mm', 'INTEGER')
        self._assert_shape('item', 'has_jockey_wheel', 'BOOLEAN')
        self.assertEqual(self._ledger(), {tuple(item[:2]) for item in COLUMNS})
        self.assertEqual(self._trigger_count(), 12)
        self.assertEqual(self._foreign_keys(), 0)

    def test_open_sqlite_transaction_refuses_before_ddl(self):
        module = self._migration_module()
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE item (id INTEGER)')
        connection.execute('BEGIN')
        self.assertTrue(connection.in_transaction)
        with self.assertRaises(RuntimeError) as caught:
            module._run_in_alembic_transaction(connection, [(
                'sql',
                'ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER',
            )])
        self.assertIn('остановлена до DDL', str(caught.exception))
        self.assertIn('DDL не начат', str(caught.exception))
        columns = {row[1] for row in connection.execute('PRAGMA table_info(item)')}
        self.assertNotIn('tent_hight_mm', columns)
        connection.close()

    def test_unknown_ledger_row_blocks_upgrade_and_downgrade_does_not_drop(self):
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
        ids = self._insert_rows()
        self.db.session.execute(text(
            f"INSERT INTO {LEDGER} (table_name, column_name) VALUES ('item', 'not_ours')"
        ))
        self.db.session.execute(text(
            'UPDATE item SET tent_hight_mm = 1818 WHERE id = :id'
        ), ids)
        self.db.session.commit()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)
        self._assert_shape('item', 'tent_hight_mm', 'INTEGER')
        self.assertIn(('item', 'not_ours'), self._ledger())
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('not_ours', _error_text(caught.exception))
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._scalar('SELECT tent_hight_mm FROM item WHERE id = :id', ids), 1818)

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
        try:
            self.db.session.remove()
        finally:
            self.db.engine.dispose()

    def _durable(self, table: str, column: str):
        _refuse_live('sqlite:///' + self.work.as_posix(), self.work)
        self.db.session.remove()
        self.db.engine.dispose()
        connection = sqlite3.connect(self.work.as_posix())
        try:
            version = connection.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0]
            names = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            return version, column in names
        finally:
            connection.close()

    def _create_ledger(self):
        self.db.session.execute(text(
            f'CREATE TABLE {LEDGER} ('
            'table_name TEXT NOT NULL, column_name TEXT NOT NULL, '
            'PRIMARY KEY (table_name, column_name))'
        ))

    def _migration_module(self):
        path = ROOT / 'migrations' / 'versions' / 'f6b2d8c14e90_add_four_model_columns.py'
        spec = importlib.util.spec_from_file_location('f6b2d8c14e90_candidate', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
