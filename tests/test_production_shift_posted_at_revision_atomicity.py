#!/usr/bin/env python3
"""Атомарность ревизии b4e8c1a90d27 на новой SQLite.

Схема собирается Alembic до f6b2d8c14e90, затем upgrade/downgrade
самой ревизии даты. Обычное соединение create_app(), PRAGMA foreign_keys=0.
Обрыв после DDL, отказ UPDATE версии и закрытие соединения читаются
новым соединением sqlite3. Рабочая trailers.db не открывается.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
MIGRATION_PATH = (
    ROOT / 'migrations' / 'versions' / 'b4e8c1a90d27_freeze_production_shift_posted_at.py'
)
PREVIOUS_REVISION = 'f6b2d8c14e90'
GUARD_REVISION = 'b4e8c1a90d27'
WITNESS_NOTE = 'atomicity-witness'


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


def _migration():
    spec = importlib.util.spec_from_file_location('b4e8c1a90d27_candidate', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PostedAtRevisionAtomicityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stat = LIVE_DB.stat() if LIVE_DB.exists() else None
        fd, name = tempfile.mkstemp(suffix='-posted-at-atomicity.sqlite')
        os.close(fd)
        cls.work = Path(name)
        fd, base_name = tempfile.mkstemp(suffix='-posted-at-atomicity-base.sqlite')
        os.close(fd)
        cls.base = Path(base_name)
        _refuse_live('sqlite:///' + cls.work.as_posix(), cls.work)
        cls.app, cls.db = _bind_app(cls.work)
        cls.migration = _migration()
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
                raise RuntimeError('live trailers.db changed during posted_at atomicity tests')

    def setUp(self):
        self._clear_probes()
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        shutil.copy(self.base, self.work)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.twelve = self._trigger_sql()
        self.assertEqual(len(self.twelve), 12)
        self.assertNotIn(self.migration.TRIGGER_NAME, self.twelve)

    def tearDown(self):
        self._clear_probes()
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_migration_does_not_commit_or_change_date_rule(self):
        text_source = MIGRATION_PATH.read_text(encoding='utf-8')
        self.assertNotIn('.commit(', text_source)
        self.assertNotIn('.rollback(', text_source)
        self.assertNotIn("execute('COMMIT'", text_source)
        self.assertNotIn('execute("COMMIT"', text_source)
        self.assertNotIn("execute('ROLLBACK'", text_source)
        self.assertNotIn('execute("ROLLBACK"', text_source)
        self.assertNotIn('DROP TRIGGER IF EXISTS', text_source)
        self.assertNotIn('foreign_keys=ON', text_source)
        self.assertIn("OLD.status = 'posted'", self.migration.TRIGGER_SQL)
        self.assertIn('OLD.posted_at IS NOT NULL', self.migration.TRIGGER_SQL)
        self.assertEqual(
            self.migration.FROZEN_POSTED_AT_MESSAGE,
            'проведённая смена не меняет дату фиксации',
        )
        for name in self.twelve:
            self.assertNotIn(name, text_source)

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
        durable = self._durable()
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(self.migration.TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['triggers'], self.twelve)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        self.assertEqual(self._schema_rows(), before)
        os.environ.pop(self.migration.UPGRADE_ABORT_ENV, None)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row()[1].strip(), self.migration.TRIGGER_SQL.strip())
        self.assertEqual(self._trigger_sql_without_guard(), self.twelve)
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
        durable = self._durable()
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(self.migration.TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['triggers'], self.twelve)
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
        durable = self._durable()
        self.assertEqual(durable['version'], PREVIOUS_REVISION)
        self.assertNotIn(self.migration.TRIGGER_NAME, durable['triggers'])
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(durable['foreign_keys'], 0)
        self.assertEqual(self._schema_rows(), before)
        self._drop_version_block()
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row()[1].strip(), self.migration.TRIGGER_SQL.strip())
        self.assertEqual(self._trigger_sql_without_guard(), self.twelve)
        self.assertEqual(self._witness(), witness)

    def test_preexisting_canonical_trigger_advances_version_without_rewrite(self):
        witness = self._insert_witness()
        self.db.session.execute(text(self.migration.TRIGGER_SQL))
        self.db.session.commit()
        rowid, sql = self._canonical_row()
        twelve = self._trigger_sql_without_guard()
        self.assertEqual(twelve, self.twelve)
        self._upgrade()
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._canonical_row(), (rowid, sql))
        self.assertEqual(self._trigger_sql_without_guard(), self.twelve)
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
        durable = self._durable()
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
        CREATE TRIGGER trg_production_shift_posted_at_frozen
        BEFORE UPDATE OF posted_at ON production_shift
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
        self.assertEqual(self._trigger_sql_without_guard(), self.twelve)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_mismatched_table_is_preserved_and_version_stays(self):
        witness = self._insert_witness()
        self.db.session.execute(text(
            'CREATE TABLE posted_at_guard_probe (id INTEGER PRIMARY KEY, posted_at TEXT)'
        ))
        self.db.session.execute(text(
            """
            CREATE TRIGGER trg_production_shift_posted_at_frozen
            BEFORE UPDATE OF posted_at ON posted_at_guard_probe
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'чужая таблица');
            END
            """
        ))
        self.db.session.commit()
        before = self._schema_rows()
        stored = self._trigger_row()
        self.assertEqual(stored[0], 'posted_at_guard_probe')
        with self.assertRaises(Exception) as caught:
            self._upgrade()
        self.assertIn('posted_at_guard_probe', _error_text(caught.exception))
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
        durable = self._durable()
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['triggers'], self.twelve)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), schema_at_head)
        os.environ.pop(self.migration.DOWNGRADE_ABORT_ENV, None)
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._trigger_sql(), self.twelve)
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
        durable = self._durable()
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['triggers'], self.twelve)
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
        durable = self._durable()
        self.assertEqual(durable['version'], GUARD_REVISION)
        self.assertEqual(durable['guard'], identity)
        self.assertEqual(durable['witness'], witness)
        self.assertEqual(self._schema_rows(), before)
        self._drop_version_block()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._trigger_sql(), self.twelve)
        self.assertEqual(self._witness(), witness)

    def test_downgrade_mismatched_trigger_is_preserved(self):
        witness = self._insert_witness()
        self._upgrade()
        self.db.session.execute(text(
            f'DROP TRIGGER {self.migration.TRIGGER_NAME}'
        ))
        self.db.session.execute(text(
            """
            CREATE TRIGGER trg_production_shift_posted_at_frozen
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
        self.assertEqual(self._trigger_sql_without_guard(), self.twelve)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)

    def test_downgrade_absent_trigger_moves_version_only(self):
        witness = self._insert_witness()
        self._upgrade()
        self.db.session.execute(text(f'DROP TRIGGER {self.migration.TRIGGER_NAME}'))
        self.db.session.commit()
        twelve = self._trigger_sql()
        self.assertEqual(twelve, self.twelve)
        before = self._schema_rows()
        self._downgrade()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertIsNone(self._trigger_row())
        self.assertEqual(self._trigger_sql(), self.twelve)
        self.assertEqual(self._witness(), witness)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._schema_rows()[1], before[1])

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
            {'note': WITNESS_NOTE},
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
            {'note': WITNESS_NOTE},
        ).one()
        return tuple(row)

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

    def _version(self):
        return self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()

    def _foreign_keys(self) -> int:
        return int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar() or 0)

    def _trigger_sql(self):
        rows = self.db.session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name != 'trg_probe_block_version' "
            "AND name != :guard ORDER BY name"
        ), {'guard': self.migration.TRIGGER_NAME}).all()
        return {row[0]: row[1] for row in rows}

    def _trigger_sql_without_guard(self):
        return self._trigger_sql()

    def _trigger_row(self):
        row = self.db.session.execute(text(
            "SELECT tbl_name, sql, rowid FROM sqlite_master "
            "WHERE type = 'trigger' AND name = :name"
        ), {'name': self.migration.TRIGGER_NAME}).one_or_none()
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
        version = self._version()
        return (version, [tuple(row) for row in rows])

    def _durable(self):
        _refuse_live('sqlite:///' + self.work.as_posix(), self.work)
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
                    (self.migration.TRIGGER_NAME,),
                )
            }
            guard = connection.execute(
                "SELECT sql, rowid FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (self.migration.TRIGGER_NAME,),
            ).fetchone()
            witness = connection.execute(
                """
                SELECT employee_id, work_area, status, posted_at, started_at,
                       planned_date, note
                FROM production_shift WHERE note = ?
                """,
                (WITNESS_NOTE,),
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


if __name__ == '__main__':
    unittest.main()
