#!/usr/bin/env python3
"""Статус проведённой смены на новой SQLite после полной цепочки Alembic.

Обычное соединение create_app(), PRAGMA foreign_keys=0.
Схема собирается Alembic, не db.create_all().
Рабочая trailers.db не открывается: файл только вне checkout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

WORKSPACE_LIVE = Path('/workspace/trailers.db').resolve()
LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
MIGRATION_PATH = (
    ROOT / 'migrations' / 'versions' / 'c9d4e2a81f06_freeze_production_shift_posted_status.py'
)
PREVIOUS_REVISION = 'b4e8c1a90d27'
GUARD_REVISION = 'c9d4e2a81f06'
FROZEN_STATUS_MESSAGE = 'проведённая смена не меняет статус'
FROZEN_DATE_MESSAGE = 'проведённая смена не меняет дату фиксации'
TRIGGER_NAME = 'trg_production_shift_posted_status_frozen'
DATE_TRIGGER_NAME = 'trg_production_shift_posted_at_frozen'
JANUARY_POSTED_AT = '2026-01-15 12:00:00.000000'
MARCH_POSTED_AT = '2026-03-10 09:00:00.000000'
CHECKOUTS = (Path('/workspace').resolve(), ROOT.resolve())


def _prove_disposable(path: Path) -> str:
    resolved = path.resolve()
    uri = 'sqlite:///' + resolved.as_posix()
    print(f'SYNTHETIC_SQLITE uri={uri} path={resolved}', flush=True)
    if resolved.name == 'trailers.db' or resolved.as_posix().endswith('/trailers.db'):
        raise RuntimeError(f'Refused to touch live DB: {resolved}')
    if uri.replace('\\', '/').endswith('/trailers.db'):
        raise RuntimeError(f'Refused to touch live DB url: {uri}')
    for checkout in CHECKOUTS:
        if resolved == checkout or checkout in resolved.parents:
            raise RuntimeError(f'SQLite must stay outside checkout: {resolved}')
    if not resolved.exists():
        raise RuntimeError(f'SQLite path is missing before connect: {resolved}')
    for live in (WORKSPACE_LIVE, LIVE_DB):
        if live.exists() and resolved.exists() and resolved.stat().st_ino == live.stat().st_ino:
            raise RuntimeError(f'Refused live inode: {resolved}')
    return uri


def _bind_app(path: Path):
    uri = _prove_disposable(path)
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


def _migration():
    spec = importlib.util.spec_from_file_location('c9d4e2a81f06_candidate', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_stat(path: Path):
    if not path.exists():
        return None
    current = path.stat()
    return (current.st_ino, current.st_size, current.st_mtime_ns)


class _DisposableMixin(unittest.TestCase):
    @classmethod
    def _remember_live(cls):
        cls.live_stats = {
            'workspace': _live_stat(WORKSPACE_LIVE),
            'checkout': _live_stat(LIVE_DB),
        }

    @classmethod
    def _assert_live_unchanged(cls):
        for label, path in (('workspace', WORKSPACE_LIVE), ('checkout', LIVE_DB)):
            current = _live_stat(path)
            if current != cls.live_stats[label]:
                raise RuntimeError(f'live trailers.db changed ({label})')

    def _clear_probes(self):
        for name in (
            self.migration.UPGRADE_ABORT_ENV,
            self.migration.UPGRADE_CLOSE_ENV,
            self.migration.DOWNGRADE_ABORT_ENV,
            self.migration.DOWNGRADE_CLOSE_ENV,
        ):
            os.environ.pop(name, None)

    def _config(self):
        from flask import current_app

        return current_app.extensions['migrate'].migrate.get_config(MIGRATIONS)

    def _upgrade(self):
        from alembic import command

        self.db.session.remove()
        command.upgrade(self._config(), GUARD_REVISION)

    def _downgrade(self):
        from alembic import command

        self.db.session.remove()
        command.downgrade(self._config(), PREVIOUS_REVISION)

    def _reset(self):
        try:
            self.db.session.rollback()
        except Exception:
            pass
        self.db.session.remove()
        self.db.engine.dispose()

    def _version(self):
        return self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()

    def _foreign_keys(self) -> int:
        return int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar() or 0)

    def _previous_trigger_sql(self):
        rows = self.db.session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name != 'trg_probe_block_version' "
            "AND name != :guard ORDER BY name"
        ), {'guard': TRIGGER_NAME}).all()
        return {row[0]: row[1] for row in rows}

    def _trigger_row(self):
        row = self.db.session.execute(text(
            "SELECT tbl_name, sql, rowid FROM sqlite_master "
            "WHERE type = 'trigger' AND name = :name"
        ), {'name': TRIGGER_NAME}).one_or_none()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    def _canonical_row(self):
        row = self._trigger_row()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'production_shift')
        return (row[2], row[1])

    def _schema_rows(self):
        rows = self.db.session.execute(text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )).all()
        return (self._version(), [tuple(row) for row in rows])

    def _install_version_block(self, blocked_version: str):
        if blocked_version not in {PREVIOUS_REVISION, GUARD_REVISION}:
            raise RuntimeError('probe version is not a revision id of this test')
        self.db.session.execute(text(
            f"""
            CREATE TRIGGER trg_probe_block_version
            BEFORE UPDATE ON alembic_version
            WHEN NEW.version_num = '{blocked_version}'
            BEGIN
                SELECT RAISE(ABORT, 'probe version update refused');
            END
            """
        ))
        self.db.session.commit()

    def _drop_version_block(self):
        self.db.session.execute(text('DROP TRIGGER IF EXISTS trg_probe_block_version'))
        self.db.session.commit()

    def _durable(self, witness_note=None):
        _prove_disposable(self.work)
        self.db.session.remove()
        self.db.engine.dispose()
        connection = sqlite3.connect(self.work.as_posix())
        try:
            version = connection.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0]
            triggers = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name != 'trg_probe_block_version' "
                    "AND name != ? ORDER BY name",
                    (TRIGGER_NAME,),
                )
            }
            guard = connection.execute(
                "SELECT sql, rowid FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (TRIGGER_NAME,),
            ).fetchone()
            witness = None
            if witness_note is not None:
                witness = connection.execute(
                    """
                    SELECT employee_id, work_area, status, posted_at, started_at,
                           planned_date, note
                    FROM production_shift WHERE note = ?
                    """,
                    (witness_note,),
                ).fetchone()
            foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
            return {
                'version': version,
                'triggers': triggers,
                'guard': None if guard is None else (guard[1], guard[0]),
                'witness': None if witness is None else tuple(witness),
                'foreign_keys': int(foreign_keys or 0),
            }
        finally:
            connection.close()


class PostedStatusRevisionAtomicityTests(_DisposableMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._remember_live()
        fd, name = tempfile.mkstemp(
            prefix='status-frozen-atomicity-',
            suffix='.sqlite',
            dir='/tmp',
        )
        os.close(fd)
        cls.work = Path(name)
        fd, base_name = tempfile.mkstemp(
            prefix='status-frozen-atomicity-base-',
            suffix='.sqlite',
            dir='/tmp',
        )
        os.close(fd)
        cls.base = Path(base_name)
        cls.app, cls.db = _bind_app(cls.work)
        cls.migration = _migration()
        with cls.app.app_context():
            engine_url = str(cls.db.engine.url).replace('\\', '/')
            _prove_disposable(Path(engine_url.removeprefix('sqlite:///')))
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
        cls._assert_live_unchanged()

    def setUp(self):
        self._clear_probes()
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        shutil.copy(self.base, self.work)
        self.ctx = self.app.app_context()
        self.ctx.push()
        _prove_disposable(self.work)
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.thirteen = self._previous_trigger_sql()
        self.assertEqual(len(self.thirteen), 13)
        self.assertIn(DATE_TRIGGER_NAME, self.thirteen)
        self.assertNotIn(TRIGGER_NAME, self.thirteen)

    def tearDown(self):
        self._clear_probes()
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_invalid_fault_option_is_rejected_before_ddl(self):
        witness = self._insert_witness()
        before = self._schema_rows()
        os.environ[self.migration.UPGRADE_ABORT_ENV] = 'before_ddl'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        message = _error_text(caught.exception)
        self.assertIn('остановлена до DDL', message)
        self.assertIn('after_ddl', message)
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        self.assertEqual(self._schema_rows(), before)
        self.assertIsNone(self._trigger_row())

    def test_invalid_downgrade_fault_option_is_rejected_before_ddl(self):
        witness = self._insert_witness()
        self._upgrade()
        identity = self._canonical_row()
        before = self._schema_rows()
        os.environ[self.migration.DOWNGRADE_ABORT_ENV] = 'not-a-probe'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), before)

    def test_begin_refused_when_sqlite_transaction_already_open(self):
        before = self._schema_rows()
        self.db.session.rollback()
        self.db.session.remove()
        self.db.engine.dispose()
        _prove_disposable(self.work)
        raw = sqlite3.connect(self.work.as_posix())
        try:
            raw.execute('BEGIN')
            self.assertTrue(raw.in_transaction)
            with self.assertRaises(RuntimeError) as caught:
                self.migration._begin_for_alembic(raw)
            self.assertIn('уже открыта', str(caught.exception))
            self.assertIn('DDL не начат', str(caught.exception))
            raw.rollback()
        finally:
            raw.close()
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._schema_rows(), before)
        self.assertEqual(self._foreign_keys(), 0)

    def test_upgrade_abort_after_ddl_keeps_old_version_then_retry(self):
        witness = self._insert_witness()
        before = self._schema_rows()
        os.environ[self.migration.UPGRADE_ABORT_ENV] = 'after_ddl'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('после DDL', _error_text(caught.exception))
        self.assertIn('до записи alembic_version', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        self.assertEqual(self._schema_rows(), before)
        os.environ.pop(self.migration.UPGRADE_ABORT_ENV, None)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row()[1].strip(), self.migration.TRIGGER_SQL.strip())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_upgrade_close_after_ddl_is_absent_on_fresh_connection(self):
        witness = self._insert_witness()
        os.environ[self.migration.UPGRADE_CLOSE_ENV] = 'after_ddl'
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('соединение закрыто после DDL', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        os.environ.pop(self.migration.UPGRADE_CLOSE_ENV, None)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertIsNotNone(self._canonical_row())
        self.assertEqual(self._witness(), witness)

    def test_upgrade_version_update_refusal_rolls_back_then_retry(self):
        witness = self._insert_witness()
        self._install_version_block(GUARD_REVISION)
        before = self._schema_rows()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('probe version update refused', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        self.assertEqual(self._schema_rows(), before)
        self._drop_version_block()
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row()[1].strip(), self.migration.TRIGGER_SQL.strip())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)

    def test_preexisting_canonical_trigger_advances_version_without_rewrite(self):
        witness = self._insert_witness()
        self.db.session.execute(text(self.migration.TRIGGER_SQL))
        self.db.session.commit()
        rowid, sql = self._canonical_row()
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row(), (rowid, sql))
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_preexisting_canonical_survives_version_update_refusal(self):
        witness = self._insert_witness()
        self.db.session.execute(text(self.migration.TRIGGER_SQL))
        self.db.session.commit()
        identity = self._canonical_row()
        self._install_version_block(GUARD_REVISION)
        before = self._schema_rows()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('probe version update refused', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), before)
        self._drop_version_block()
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row(), identity)
        self.assertEqual(self._witness(), witness)

    def test_mismatched_sql_is_preserved_and_version_stays(self):
        witness = self._insert_witness()
        foreign_sql = """
        CREATE TRIGGER trg_production_shift_posted_status_frozen
        BEFORE UPDATE OF status ON production_shift
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'чужой триггер');
        END
        """
        self.db.session.execute(text(foreign_sql))
        self.db.session.commit()
        before = self._schema_rows()
        stored = self._trigger_row()
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        message = _error_text(caught.exception)
        self.assertIn('остановлена до DDL', message)
        self.assertIn('не канонический', message)
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._trigger_row(), stored)
        self.assertEqual(self._schema_rows(), before)
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_mismatched_table_is_preserved_and_version_stays(self):
        witness = self._insert_witness()
        self.db.session.execute(text(
            'CREATE TABLE posted_status_guard_probe (id INTEGER PRIMARY KEY, status TEXT)'
        ))
        self.db.session.execute(text(
            """
            CREATE TRIGGER trg_production_shift_posted_status_frozen
            BEFORE UPDATE OF status ON posted_status_guard_probe
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'чужая таблица');
            END
            """
        ))
        self.db.session.commit()
        before = self._schema_rows()
        stored = self._trigger_row()
        self.assertEqual(stored[0], 'posted_status_guard_probe')
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('posted_status_guard_probe', _error_text(caught.exception))
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._trigger_row(), stored)
        self.assertEqual(self._schema_rows(), before)
        self.assertEqual(self._witness(), witness)

    def test_downgrade_abort_after_drop_restores_trigger_then_retry(self):
        witness = self._insert_witness()
        self._upgrade()
        identity = self._canonical_row()
        schema_at_head = self._schema_rows()
        os.environ[self.migration.DOWNGRADE_ABORT_ENV] = 'after_ddl'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('DROP TRIGGER', _error_text(caught.exception))
        self.assertIn('до записи alembic_version', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), schema_at_head)
        os.environ.pop(self.migration.DOWNGRADE_ABORT_ENV, None)
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_downgrade_close_after_drop_keeps_trigger_on_fresh_connection(self):
        witness = self._insert_witness()
        self._upgrade()
        identity = self._canonical_row()
        os.environ[self.migration.DOWNGRADE_CLOSE_ENV] = 'after_ddl'
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('соединение закрыто после DDL', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['triggers'], self.thirteen)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)

    def test_downgrade_version_update_refusal_restores_trigger_then_retry(self):
        witness = self._insert_witness()
        self._upgrade()
        identity = self._canonical_row()
        self._install_version_block(PREVIOUS_REVISION)
        before = self._schema_rows()
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('probe version update refused', _error_text(caught.exception))
        self._reset()
        self.db.engine.dispose()
        durable = self._durable('atomicity-witness')
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), before)
        self._drop_version_block()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)

    def test_downgrade_mismatched_trigger_is_preserved(self):
        witness = self._insert_witness()
        self._upgrade()
        self.db.session.execute(text(f'DROP TRIGGER {TRIGGER_NAME}'))
        self.db.session.execute(text(
            """
            CREATE TRIGGER trg_production_shift_posted_status_frozen
            BEFORE UPDATE OF note ON production_shift
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'чужой downgrade');
            END
            """
        ))
        self.db.session.commit()
        before = self._schema_rows()
        stored = self._trigger_row()
        with self.assertRaises(Exception) as caught:
            self._downgrade()
        self.assertIn('остановлена до DDL', _error_text(caught.exception))
        self._reset()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._trigger_row(), stored)
        self.assertEqual(self._schema_rows(), before)
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_downgrade_absent_trigger_moves_version_only(self):
        witness = self._insert_witness()
        self._upgrade()
        self.db.session.execute(text(f'DROP TRIGGER {TRIGGER_NAME}'))
        self.db.session.commit()
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        before = self._schema_rows()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._previous_trigger_sql(), self.thirteen)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._schema_rows()[1], before[1])

    def _insert_witness(self):
        self.db.session.execute(
            text(
                """
                INSERT INTO production_shift (
                    employee_id, work_area, status, posted_at, started_at,
                    created_at, updated_at, senior_shortage_confirmed,
                    planned_date, note
                ) VALUES (
                    1, 'assembly', 'posted', '2024-05-01 08:00:00.000000',
                    '2024-05-01 08:00:00.000000',
                    '2024-05-01 08:00:00.000000', '2024-05-01 08:00:00.000000', 0,
                    '2024-05-01', :note
                )
                """
            ),
            {'note': 'atomicity-witness'},
        )
        self.db.session.commit()
        return self._witness()

    def _witness(self):
        row = self.db.session.execute(
            text(
                """
                SELECT employee_id, work_area, status, posted_at, started_at,
                       planned_date, note
                FROM production_shift WHERE note = :note
                """
            ),
            {'note': 'atomicity-witness'},
        ).one()
        return tuple(row)


class PostedStatusGuardBehaviorTests(_DisposableMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._remember_live()
        fd, name = tempfile.mkstemp(
            prefix='status-frozen-behavior-',
            suffix='.sqlite',
            dir='/tmp',
        )
        os.close(fd)
        cls.work = Path(name)
        fd, base_b4 = tempfile.mkstemp(
            prefix='status-frozen-behavior-b4-',
            suffix='.sqlite',
            dir='/tmp',
        )
        os.close(fd)
        cls.base_b4 = Path(base_b4)
        fd, base_c9 = tempfile.mkstemp(
            prefix='status-frozen-behavior-c9-',
            suffix='.sqlite',
            dir='/tmp',
        )
        os.close(fd)
        cls.base_c9 = Path(base_c9)
        cls.app, cls.db = _bind_app(cls.work)
        cls.migration = _migration()
        with cls.app.app_context():
            engine_url = str(cls.db.engine.url).replace('\\', '/')
            _prove_disposable(Path(engine_url.removeprefix('sqlite:///')))
            from flask_migrate import upgrade

            cls.db.session.remove()
            upgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
            cls.db.session.remove()
            cls.db.engine.dispose()
            shutil.copy(cls.work, cls.base_b4)
            upgrade(directory=MIGRATIONS, revision=GUARD_REVISION)
            cls._seed(cls)
            cls.db.session.commit()
            cls.db.session.remove()
            cls.db.engine.dispose()
        shutil.copy(cls.work, cls.base_c9)

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.work.unlink(missing_ok=True)
        cls.base_b4.unlink(missing_ok=True)
        cls.base_c9.unlink(missing_ok=True)
        cls._assert_live_unchanged()

    def setUp(self):
        self._clear_probes()
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        shutil.copy(self.base_c9, self.work)
        self.ctx = self.app.app_context()
        self.ctx.push()
        _prove_disposable(self.work)
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(len(self._previous_trigger_sql()), 13)

    def tearDown(self):
        self._clear_probes()
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_migration_source_does_not_rewrite_previous_triggers(self):
        source = MIGRATION_PATH.read_text(encoding='utf-8')
        self.assertNotIn('.commit(', source)
        self.assertNotIn('.rollback(', source)
        self.assertNotIn("execute('COMMIT'", source)
        self.assertNotIn('execute("COMMIT"', source)
        self.assertNotIn("execute('ROLLBACK'", source)
        self.assertNotIn('execute("ROLLBACK"', source)
        self.assertNotIn('DROP TRIGGER IF EXISTS', source)
        self.assertNotIn('foreign_keys=ON', source)
        self.assertNotIn('create_all', source)
        self.assertEqual(self.migration.revision, GUARD_REVISION)
        self.assertEqual(self.migration.down_revision, PREVIOUS_REVISION)
        self.assertEqual(self.migration.FROZEN_STATUS_MESSAGE, FROZEN_STATUS_MESSAGE)
        self.assertIn("OLD.status = 'posted' AND NEW.status IS NOT OLD.status", self.migration.TRIGGER_SQL)
        self.assertNotIn('admin', self.migration.TRIGGER_SQL.lower())
        for name in self._previous_trigger_sql():
            self.assertNotIn(name, source)
        self.assertIn("if raw.in_transaction:", source)

    def test_direct_status_change_works_before_revision_then_rejected(self):
        self._restore_b4()
        thirteen = self._previous_trigger_sql()
        self.assertEqual(len(thirteen), 13)
        self.assertNotIn(TRIGGER_NAME, thirteen)
        dated = self._insert_shift(
            status='posted',
            posted_at=JANUARY_POSTED_AT,
            note='dated-witness',
        )
        legacy = self._insert_shift(
            status='posted',
            posted_at=None,
            note='legacy-null-witness',
        )
        departed = self._insert_shift(
            status='open',
            posted_at=JANUARY_POSTED_AT,
            note='already-left-posted',
            posted_by_user_id=1,
        )
        mutable = self._insert_shift(
            status='posted',
            posted_at=JANUARY_POSTED_AT,
            note='mutable-before-guard',
        )
        dated_before = self._row(dated)
        legacy_before = self._row(legacy)
        departed_before = self._row(departed)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'open', note = 'дыра' WHERE id = :id"),
            {'id': mutable},
        )
        self.db.session.commit()
        self.assertEqual(self._row(mutable)[0], 'open')
        self.assertEqual(self._row(mutable)[1], JANUARY_POSTED_AT)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'posted', note = 'mutable-before-guard' WHERE id = :id"),
            {'id': mutable},
        )
        self.db.session.commit()
        mutable_before = self._row(mutable)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._previous_trigger_sql(), thirteen)
        self.assertEqual(self._row(dated), dated_before)
        self.assertEqual(self._row(legacy), legacy_before)
        self.assertEqual(self._row(departed), departed_before)
        self.assertEqual(self._row(mutable), mutable_before)
        self.assertEqual(departed_before[0], 'open')
        self._expect_status_reject(
            "UPDATE production_shift SET status = 'open', note = 'после' WHERE id = :id",
            {'id': mutable},
        )
        self.assertEqual(self._row(mutable), mutable_before)
        self.db.session.remove()
        from flask_migrate import downgrade

        downgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._previous_trigger_sql(), thirteen)
        self.assertEqual(self._row(dated), dated_before)
        self.assertEqual(self._row(legacy), legacy_before)
        self.assertEqual(self._row(departed), departed_before)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'open' WHERE id = :id"),
            {'id': mutable},
        )
        self.db.session.commit()
        self.assertEqual(self._row(mutable)[0], 'open')
        self.assertEqual(self._row(mutable)[1], JANUARY_POSTED_AT)

    def test_raw_status_changes_rejected_for_legacy_null_and_dated(self):
        legacy = self._insert_shift(status='posted', posted_at=None, note='legacy-guard')
        dated = self._insert_shift(status='posted', posted_at=JANUARY_POSTED_AT, note='dated-guard')
        self.db.session.execute(
            text(
                """
                INSERT INTO production_shift_material (
                    shift_id, item_id, qty_fact, qty_issued, qty_shortage, unit,
                    shortage_status, status, created_at
                ) VALUES (
                    :shift_id, 1, 1, 1, 0, 'шт', 'none', 'posted', :created_at
                )
                """
            ),
            {'shift_id': dated, 'created_at': JANUARY_POSTED_AT},
        )
        self.db.session.commit()
        before_counts = self._movement_counts()
        for shift_id, posted_at in ((legacy, None), (dated, JANUARY_POSTED_AT)):
            original = self._row(shift_id)
            self.assertEqual(original[1], posted_at)
            for status in ('open', 'closed', 'reopened', None):
                self._expect_status_reject(
                    "UPDATE production_shift SET status = :status, note = 'сдвиг' WHERE id = :id",
                    {'id': shift_id, 'status': status},
                )
                self.assertEqual(self._row(shift_id), original)
            from models import ProductionShift

            shift = self.db.session.get(ProductionShift, shift_id)
            shift.status = 'closed'
            with self.assertRaises(IntegrityError) as caught:
                self.db.session.commit()
            self.db.session.rollback()
            self.assertIn(FROZEN_STATUS_MESSAGE, str(caught.exception))
            self.assertEqual(self._row(shift_id), original)
        self._expect_status_reject(
            "UPDATE production_shift SET status = 'open', posted_at = :posted_at WHERE id = :id",
            {'id': dated, 'posted_at': MARCH_POSTED_AT},
        )
        self.assertEqual(self._row(dated)[0], 'posted')
        self.assertEqual(self._row(dated)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._movement_counts(), before_counts)

    def test_unchanged_status_and_first_open_transitions_pass(self):
        posted = self._insert_shift(status='posted', posted_at=JANUARY_POSTED_AT, note='same-status')
        opening = self._insert_shift(status='open', posted_at=None, note='first-post-raw')
        closing = self._insert_shift(status='open', posted_at=None, note='first-close-raw')
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'posted', note = 'та же строка' WHERE id = :id"),
            {'id': posted},
        )
        self.db.session.commit()
        self.assertEqual(self._row(posted)[0], 'posted')
        self.assertEqual(self._row(posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._row(posted)[2], 'та же строка')
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'posted', posted_at = :posted_at WHERE id = :id"),
            {'id': opening, 'posted_at': JANUARY_POSTED_AT},
        )
        self.db.session.commit()
        self.assertEqual(self._row(opening)[0], 'posted')
        self.assertEqual(self._row(opening)[1], JANUARY_POSTED_AT)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'closed' WHERE id = :id"),
            {'id': closing},
        )
        self.db.session.commit()
        self.assertEqual(self._row(closing)[0], 'closed')
        self.assertIsNone(self._row(closing)[1])

    def test_historical_departure_insert_and_delete_stay_outside_guard(self):
        departed = self._insert_shift(
            status='open',
            posted_at=JANUARY_POSTED_AT,
            note='left-earlier',
            posted_by_user_id=1,
        )
        self.assertEqual(self._row(departed)[0], 'open')
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'closed' WHERE id = :id"),
            {'id': departed},
        )
        self.db.session.commit()
        self.assertEqual(self._row(departed)[0], 'closed')
        self.assertEqual(self._row(departed)[1], JANUARY_POSTED_AT)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'posted' WHERE id = :id"),
            {'id': departed},
        )
        self.db.session.commit()
        self.assertEqual(self._row(departed)[0], 'posted')
        inserted = self._insert_shift(status='posted', posted_at=MARCH_POSTED_AT, note='inserted-posted')
        self.assertEqual(self._row(inserted)[0], 'posted')
        self.db.session.execute(
            text('DELETE FROM production_shift WHERE id = :id'),
            {'id': inserted},
        )
        self.db.session.commit()
        self.assertIsNone(self.db.session.execute(
            text('SELECT id FROM production_shift WHERE id = :id'),
            {'id': inserted},
        ).scalar())

    def test_roles_cannot_close_or_post_already_posted(self):
        before_counts = self._movement_counts()
        report_before = self._report_fingerprint()
        expectations = (
            ('admin', self.admin_id, self.admin_shift_id, None),
            ('director', self.director_id, self.director_shift_id, None),
            ('manager', self.manager_id, self.manager_shift_id, None),
            ('production', self.production_id, self.production_shift_id, None),
        )
        for role, user_id, shift_id, _unused in expectations:
            original = self._row(shift_id)
            self.assertEqual(original[0], 'posted')
            self.assertEqual(original[1], JANUARY_POSTED_AT)
            closed, closed_client = self._request(
                user_id, 'POST', f'/production/shifts/{shift_id}/close',
            )
            self.assertEqual(closed.status_code, 302, self._flash_text(closed_client))
            self.assertIn('Смена уже проведена.', self._flash_text(closed_client))
            self.assertEqual(self._row(shift_id), original)
            posted, posted_client = self._request(
                user_id, 'POST', f'/production/shifts/{shift_id}/post',
                {'direction_area_id': str(self.area_id), 'hours_fact': '8'},
            )
            self.assertEqual(posted.status_code, 302, self._flash_text(posted_client))
            self.assertIn('Смена уже проведена. Повторных движений нет.', self._flash_text(posted_client))
            self.assertEqual(self._row(shift_id), original)
            self.assertEqual(role in {'admin', 'director', 'manager', 'production'}, True)
        self.assertEqual(self._movement_counts(), before_counts)
        self.assertEqual(self._report_fingerprint(), report_before)
        self.assertEqual(self._foreign_keys(), 0)

    def test_route_first_post_replay_keeps_stock_effects_and_report(self):
        shift_id = self._open_shift()
        self._add_material(shift_id, '3')
        self._add_output(shift_id, self.trailer_id, '1')
        before_post_units = self._movement_counts()[3]
        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/post',
            {'direction_area_id': str(self.area_id), 'hours_fact': '8'},
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertIn('проведена', self._flash_text(client))
        posted = self._row(shift_id)
        self.assertEqual(posted[0], 'posted')
        self.assertIsNotNone(posted[1])
        counts = self._movement_counts()
        self.assertEqual(counts[1], 1)
        self.assertEqual(counts[2], 0)
        self.assertEqual(counts[3], before_post_units + 1)
        self.assertGreater(counts[0], 0)
        report = self._report_fingerprint()
        self.assertGreater(report['totals']['good_trailers'], 0)
        replay, replay_client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/post',
            {'direction_area_id': str(self.area_id), 'hours_fact': '8'},
        )
        self.assertEqual(replay.status_code, 302, self._flash_text(replay_client))
        self.assertIn('Повторных движений нет.', self._flash_text(replay_client))
        self.assertEqual(self._row(shift_id), posted)
        self.assertEqual(self._movement_counts(), counts)
        self.assertEqual(self._report_fingerprint(), report)
        from shift_posting import ShiftAlreadyPosted, post_shift
        from models import ProductionShift

        with self.assertRaises(ShiftAlreadyPosted):
            post_shift(
                shift=self.db.session.get(ProductionShift, shift_id),
                direction_area_id=self.area_id,
                hours_fact='8',
                senior_confirmed=False,
                actor_is_senior=False,
                created_by_user_id=self.production_id,
            )
        self.db.session.rollback()
        self.assertEqual(self._row(shift_id), posted)
        self.assertEqual(self._movement_counts(), counts)
        self.assertEqual(self._report_fingerprint(), report)

    def test_service_first_post_and_route_first_close(self):
        from models import ProductionShift
        from shift_posting import ShiftAlreadyPosted, post_shift

        shift_id = self._open_shift()
        self._add_material(shift_id, '2')
        result = post_shift(
            shift=self.db.session.get(ProductionShift, shift_id),
            direction_area_id=self.area_id,
            hours_fact='4',
            senior_confirmed=False,
            actor_is_senior=False,
            created_by_user_id=self.production_id,
        )
        self.db.session.commit()
        self.assertEqual(self._row(shift_id)[0], 'posted')
        self.assertIsNotNone(self._row(shift_id)[1])
        self.assertEqual(result['shortage_ops'], 0)
        self.assertGreater(result['issued_ops'], 0)
        self.assertEqual(result['units_created'], 0)
        posted = self._row(shift_id)
        counts = self._movement_counts()
        with self.assertRaises(ShiftAlreadyPosted):
            post_shift(
                shift=self.db.session.get(ProductionShift, shift_id),
                direction_area_id=self.area_id,
                hours_fact='4',
                senior_confirmed=False,
                actor_is_senior=False,
                created_by_user_id=self.production_id,
            )
        self.db.session.rollback()
        self.assertEqual(self._row(shift_id), posted)
        self.assertEqual(self._movement_counts(), counts)

        close_id = self._open_shift()
        response, client = self._request(
            self.production_id, 'POST', f'/production/shifts/{close_id}/close',
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        self.assertIn('закрыта', self._flash_text(client))
        self.assertEqual(self._row(close_id)[0], 'closed')
        self.assertIsNone(self._row(close_id)[1])
        self.assertEqual(self.db.session.execute(
            text('SELECT COUNT(*) FROM inventory_operation WHERE shift_id = :id'),
            {'id': close_id},
        ).scalar(), 0)
        self.assertEqual(self.db.session.execute(
            text('SELECT COUNT(*) FROM produced_unit'),
        ).scalar(), counts[3])

    def _restore_b4(self):
        self.db.session.remove()
        self.db.engine.dispose()
        shutil.copy(self.base_b4, self.work)
        _prove_disposable(self.work)
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)

    def _insert_shift(self, *, status, posted_at, note, posted_by_user_id=None):
        self.db.session.execute(
            text(
                """
                INSERT INTO production_shift (
                    employee_id, work_area, status, posted_at, posted_by_user_id,
                    started_at, created_at, updated_at, senior_shortage_confirmed,
                    planned_date, note
                ) VALUES (
                    1, 'assembly', :status, :posted_at, :posted_by_user_id,
                    :started_at, :started_at, :started_at, 0,
                    :planned_date, :note
                )
                """
            ),
            {
                'status': status,
                'posted_at': posted_at,
                'posted_by_user_id': posted_by_user_id,
                'started_at': JANUARY_POSTED_AT,
                'planned_date': '2026-01-15',
                'note': note,
            },
        )
        self.db.session.commit()
        return self.db.session.execute(
            text('SELECT id FROM production_shift WHERE note = :note'),
            {'note': note},
        ).scalar()

    def _expect_status_reject(self, sql, params):
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(text(sql), params)
            self.db.session.commit()
        self.db.session.rollback()
        message = str(caught.exception)
        self.assertTrue(
            FROZEN_STATUS_MESSAGE in message or FROZEN_DATE_MESSAGE in message,
            message,
        )
        if params.get('status') != 'ignored' and 'posted_at' not in sql:
            self.assertIn(FROZEN_STATUS_MESSAGE, message)

    def _row(self, shift_id):
        row = self.db.session.execute(
            text(
                """
                SELECT status, posted_at, note, posted_by_user_id
                FROM production_shift WHERE id = :id
                """
            ),
            {'id': shift_id},
        ).one()
        return tuple(row)

    def _movement_counts(self):
        def _count(sql):
            return int(self.db.session.execute(text(sql)).scalar() or 0)

        return (
            _count('SELECT COUNT(*) FROM inventory_operation'),
            _count('SELECT COUNT(*) FROM production_shift_material'),
            _count(
                "SELECT COUNT(*) FROM production_shift_material "
                "WHERE CAST(qty_shortage AS TEXT) NOT IN ('0', '0.0', '0.000')"
            ),
            _count('SELECT COUNT(*) FROM produced_unit'),
            _count(
                "SELECT COUNT(*) FROM inventory_operation "
                "WHERE operation_type = 'production_shortage'"
            ),
        )

    def _report_fingerprint(self):
        from shift_director_report import build_shift_director_report

        report = build_shift_director_report(
            period_start=datetime(2000, 1, 1, 0, 0, 0),
            period_end=datetime(2100, 1, 1, 0, 0, 0),
        )
        return json.loads(json.dumps(report, default=str, sort_keys=True))

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

    def _open_shift(self) -> int:
        from models import ProductionShift

        response, client = self._request(
            self.production_id,
            'POST',
            '/production/shifts/open',
            {
                'workshop_id': str(self.workshop_id),
                'direction_area_id': str(self.area_id),
                'work_area': 'assembly',
            },
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))
        opened = (
            ProductionShift.query.filter_by(status='open', employee_id=self.production_employee_id)
            .order_by(ProductionShift.id.desc())
            .first()
        )
        self.assertIsNotNone(opened, self._flash_text(client))
        return opened.id

    def _add_material(self, shift_id: int, qty: str):
        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/material',
            {'item_id': str(self.sheet_id), 'qty_fact': qty, 'unit': 'шт'},
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))

    def _add_output(self, shift_id: int, item_id: int, quantity: str):
        response, client = self._request(
            self.production_id,
            'POST',
            f'/production/shifts/{shift_id}/output',
            {
                'item_id': str(item_id),
                'output_type': 'trailer',
                'quantity': quantity,
                'defect_quantity': '0',
                'unit': 'шт',
            },
        )
        self.assertEqual(response.status_code, 302, self._flash_text(client))

    @staticmethod
    def _seed(cls):
        from models import (
            InventoryBalance,
            Item,
            ProductCategory,
            ProductionEmployee,
            ProductionShift,
            ProductionWorkshop,
            User,
            Warehouse,
            WarehouseStorageArea,
        )
        from shift_posting import MODE_SHIFT_ONLY

        factory = Warehouse(
            name='Синтетика статус завод',
            is_active=True,
            is_production=True,
            warehouse_kind='assembly',
            is_sales_point=False,
            can_sell=False,
            can_ship_to_customer=False,
            primary_product_category=None,
        )
        sales = Warehouse(
            name='Синтетика статус реализация',
            is_active=True,
            is_production=False,
            warehouse_kind='finished_goods',
            is_sales_point=True,
            can_sell=True,
            can_ship_to_customer=True,
            primary_product_category='light_trailer',
        )
        cls.db.session.add_all([factory, sales])
        cls.db.session.flush()
        area = WarehouseStorageArea(
            warehouse_id=factory.id,
            code='DIR_LIGHT',
            name='Легковые ТМЦ статус',
            area_type='direction_stock',
            is_active=True,
            sort_order=210,
            product_category='light_trailer',
            shopfloor_posting_mode=MODE_SHIFT_ONLY,
        )
        cls.db.session.add(area)
        cls.db.session.flush()
        light_cat = ProductCategory.query.filter_by(code='light_trailer').one()
        sheet = Item(item_type='COMPONENT', article='SHEET-STATUS', name='Лист статус', unit='шт')
        trailer = Item(
            item_type='TRAILER',
            article='LIGHT-STATUS',
            name='Легковой статус',
            unit='шт',
            product_category_id=light_cat.id,
        )
        cls.db.session.add_all([sheet, trailer])
        cls.db.session.flush()
        cls.db.session.add(InventoryBalance(
            warehouse_id=factory.id,
            storage_area_id=area.id,
            item_id=sheet.id,
            quantity=Decimal('8'),
            unit='шт',
        ))
        workshop = ProductionWorkshop(code='assembly', name='Сборка статус', workshop_type='trailer')
        cls.db.session.add(workshop)
        users = {}
        for role, username, full_name in (
            ('admin', 'admin-status', 'Админ статус'),
            ('director', 'director-status', 'Директор статус'),
            ('manager', 'manager-status', 'Менеджер статус'),
            ('production', 'prod-status', 'Цех статус'),
        ):
            user = User(username=username, full_name=full_name, role=role)
            user.set_password('x')
            cls.db.session.add(user)
            users[role] = user
        cls.db.session.flush()
        employees = {}
        for role, user in users.items():
            employee = ProductionEmployee(
                user_id=user.id,
                full_name=user.full_name,
                employee_code=f'STATUS-{role}',
                is_active=True,
            )
            cls.db.session.add(employee)
            employees[role] = employee
        cls.db.session.flush()
        shift_ids = {}
        for role, employee in employees.items():
            shift = ProductionShift(
                employee_id=employee.id,
                user_id=users[role].id,
                workshop_id=workshop.id,
                work_area='assembly',
                planned_date=datetime(2026, 1, 15).date(),
                started_at=datetime(2026, 1, 15, 8, 0, 0),
                status='posted',
                posted_at=datetime(2026, 1, 15, 12, 0, 0),
                posted_by_user_id=users[role].id,
                direction_warehouse_id=factory.id,
                direction_area_id=area.id,
                senior_shortage_confirmed=False,
                note=f'posted-{role}',
            )
            cls.db.session.add(shift)
            cls.db.session.flush()
            shift_ids[role] = shift.id
        cls.factory_id = factory.id
        cls.area_id = area.id
        cls.sheet_id = sheet.id
        cls.trailer_id = trailer.id
        cls.workshop_id = workshop.id
        cls.admin_id = users['admin'].id
        cls.director_id = users['director'].id
        cls.manager_id = users['manager'].id
        cls.production_id = users['production'].id
        cls.production_employee_id = employees['production'].id
        cls.admin_shift_id = shift_ids['admin']
        cls.director_shift_id = shift_ids['director']
        cls.manager_shift_id = shift_ids['manager']
        cls.production_shift_id = shift_ids['production']


if __name__ == '__main__':
    unittest.main()
