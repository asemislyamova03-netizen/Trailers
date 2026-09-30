#!/usr/bin/env python3
"""Дата фиксации смены на новой SQLite после полной цепочки Alembic.

Сначала на f6b2d8c14e90 прямой UPDATE переносит когорту.
После b4e8c1a90d27 та же запись отвергается. Старые строки не
переписываются. PRAGMA foreign_keys остаётся 0.
Рабочая trailers.db не открывается.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')

LIVE_DB = (ROOT / 'trailers.db').resolve()
MIGRATIONS = str(ROOT / 'migrations')
PREVIOUS_REVISION = 'f6b2d8c14e90'
GUARD_REVISION = 'b4e8c1a90d27'
FROZEN_MESSAGE = 'проведённая смена не меняет дату фиксации'
TRIGGER_NAME = 'trg_production_shift_posted_at_frozen'
JANUARY_START = datetime(2026, 1, 1, 0, 0, 0)
JANUARY_END = datetime(2026, 1, 31, 23, 59, 59)
MARCH_START = datetime(2026, 3, 1, 0, 0, 0)
MARCH_END = datetime(2026, 3, 31, 23, 59, 59)
JANUARY_POSTED_AT = '2026-01-15 12:00:00.000000'
MARCH_POSTED_AT = '2026-03-10 09:00:00.000000'


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


class ProductionShiftPostedAtGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_stat = LIVE_DB.stat() if LIVE_DB.exists() else None
        fd, name = tempfile.mkstemp(suffix='-shift-posted-at-guard.sqlite')
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
            upgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
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
                raise RuntimeError('live trailers.db changed during posted_at guard test')

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)

    def tearDown(self):
        self.db.session.rollback()
        self.db.session.remove()
        self.ctx.pop()

    def test_posted_at_freeze_upgrade_and_downgrade(self):
        from flask_migrate import downgrade, upgrade
        from models import ProductionShift

        migration_text = (
            ROOT / 'migrations' / 'versions' / 'b4e8c1a90d27_freeze_production_shift_posted_at.py'
        ).read_text(encoding='utf-8')
        self.assertNotIn('PRAGMA foreign_keys', migration_text)
        self.assertNotIn('foreign_keys=ON', migration_text)
        for name in self._trigger_sql():
            self.assertNotIn(name, migration_text)

        before_sql = self._trigger_sql()
        self.assertEqual(len(before_sql), 12)
        self.assertNotIn(TRIGGER_NAME, before_sql)

        witness_posted = self._insert_shift(
            status='posted',
            posted_at='2024-05-01 08:00:00.000000',
            planned_date='2024-05-01',
            started_at='2024-05-01 08:00:00.000000',
            note='witness-posted',
        )
        witness_open = self._insert_shift(
            status='open',
            posted_at=None,
            planned_date='2025-01-01',
            started_at='2025-01-01 08:00:00.000000',
            note='witness-open',
        )
        mutable_posted = self._insert_shift(
            status='posted',
            posted_at=JANUARY_POSTED_AT,
            planned_date='2026-01-15',
            started_at='2026-01-15 08:00:00.000000',
            note='mutable-posted',
        )
        null_posted = self._insert_shift(
            status='posted',
            posted_at=None,
            planned_date='2020-01-01',
            started_at='2020-01-01 08:00:00.000000',
            note='null-posted',
        )
        mutable_open = self._insert_shift(
            status='open',
            posted_at=None,
            planned_date='2026-02-02',
            started_at='2026-02-02 08:00:00.000000',
            note='mutable-open',
        )
        witness_posted_before = self._raw(witness_posted)
        witness_open_before = self._raw(witness_open)
        null_posted_before = self._raw(null_posted)

        self.assertEqual(self._shift_count(JANUARY_START, JANUARY_END), 1)
        self.assertEqual(self._shift_count(MARCH_START, MARCH_END), 0)
        self.db.session.execute(
            text("UPDATE production_shift SET posted_at = :posted_at WHERE id = :id"),
            {'id': mutable_posted, 'posted_at': MARCH_POSTED_AT},
        )
        self.db.session.commit()
        self.assertEqual(self._shift_count(JANUARY_START, JANUARY_END), 0)
        self.assertEqual(self._shift_count(MARCH_START, MARCH_END), 1)
        self.db.session.execute(
            text(
                "UPDATE production_shift SET posted_at = :posted_at, status = 'posted' "
                "WHERE id = :id"
            ),
            {'id': mutable_posted, 'posted_at': JANUARY_POSTED_AT},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)

        self.db.session.remove()
        upgrade(directory=MIGRATIONS, revision=GUARD_REVISION)
        self.assertEqual(self._version(), GUARD_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._temp_tables(), [])
        after_sql = self._trigger_sql()
        self.assertEqual(set(before_sql), set(after_sql) - {TRIGGER_NAME})
        for name, sql in before_sql.items():
            self.assertEqual(after_sql[name], sql)
        self.assertIn('BEFORE UPDATE OF posted_at ON production_shift', after_sql[TRIGGER_NAME])
        self.assertEqual(len(after_sql), 13)
        self.assertEqual(self._raw(witness_posted), witness_posted_before)
        self.assertEqual(self._raw(witness_open), witness_open_before)
        self.assertEqual(self._raw(null_posted), null_posted_before)
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertIsNone(self._raw(null_posted)[1])

        self._expect_reject(
            "UPDATE production_shift SET posted_at = :posted_at WHERE id = :id",
            {'id': mutable_posted, 'posted_at': MARCH_POSTED_AT},
        )
        self._expect_reject(
            "UPDATE production_shift SET posted_at = NULL WHERE id = :id",
            {'id': mutable_posted},
        )
        self._expect_reject(
            "UPDATE production_shift SET status = 'open', posted_at = :posted_at WHERE id = :id",
            {'id': mutable_posted, 'posted_at': MARCH_POSTED_AT},
        )
        self.assertEqual(self._raw(mutable_posted)[0], 'posted')
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._shift_count(JANUARY_START, JANUARY_END), 1)
        self.assertEqual(self._shift_count(MARCH_START, MARCH_END), 0)

        shift = self.db.session.get(ProductionShift, mutable_posted)
        shift.posted_at = datetime(2026, 5, 1, 1, 0, 0)
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.commit()
        self.db.session.rollback()
        self.assertIn(FROZEN_MESSAGE, str(caught.exception))
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)

        shift = self.db.session.get(ProductionShift, mutable_posted)
        shift.note = 'заметка не дата фиксации'
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_posted)[0], 'posted')
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(
            self.db.session.get(ProductionShift, mutable_posted).note,
            'заметка не дата фиксации',
        )

        self._expect_reject(
            "UPDATE production_shift SET posted_at = :posted_at WHERE id = :id",
            {'id': null_posted, 'posted_at': JANUARY_POSTED_AT},
        )
        self.assertIsNone(self._raw(null_posted)[1])
        self.assertEqual(self._raw(null_posted)[0], 'posted')

        self.db.session.execute(
            text(
                "UPDATE production_shift SET planned_date = '2026-02-03', "
                "started_at = '2026-02-03 07:00:00.000000' WHERE id = :id"
            ),
            {'id': mutable_open},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_open)[0], 'open')
        self.assertIsNone(self._raw(mutable_open)[1])
        self.assertEqual(self._raw(mutable_open)[2], '2026-02-03')
        self.db.session.execute(
            text(
                "UPDATE production_shift SET status = 'posted', posted_at = :posted_at "
                "WHERE id = :id"
            ),
            {'id': mutable_open, 'posted_at': '2026-02-02 18:00:00.000000'},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_open)[0], 'posted')
        self.assertEqual(self._raw(mutable_open)[1], '2026-02-02 18:00:00.000000')
        self._expect_reject(
            "UPDATE production_shift SET posted_at = :posted_at WHERE id = :id",
            {'id': mutable_open, 'posted_at': MARCH_POSTED_AT},
        )
        self.assertEqual(self._raw(mutable_open)[1], '2026-02-02 18:00:00.000000')

        # Статус триггер не держит: это не процедура отмены.
        # Дата при этом остаётся прежней, в март когорта не переезжает.
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'open' WHERE id = :id"),
            {'id': mutable_posted},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_posted)[0], 'open')
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self._expect_reject(
            "UPDATE production_shift SET posted_at = :posted_at WHERE id = :id",
            {'id': mutable_posted, 'posted_at': MARCH_POSTED_AT},
        )
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._shift_count(JANUARY_START, JANUARY_END), 0)
        self.assertEqual(self._shift_count(MARCH_START, MARCH_END), 0)
        self.db.session.execute(
            text("UPDATE production_shift SET status = 'posted' WHERE id = :id"),
            {'id': mutable_posted},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._shift_count(JANUARY_START, JANUARY_END), 1)

        self.assertEqual(self._raw(witness_posted), witness_posted_before)
        self.assertEqual(self._raw(witness_open), witness_open_before)

        self.db.session.remove()
        downgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
        self.assertEqual(self._version(), PREVIOUS_REVISION)
        self.assertEqual(self._foreign_keys(), 0)
        self.assertEqual(self._temp_tables(), [])
        restored_sql = self._trigger_sql()
        self.assertEqual(restored_sql, before_sql)
        self.assertNotIn(TRIGGER_NAME, restored_sql)
        self.assertEqual(self._raw(witness_posted), witness_posted_before)
        self.assertEqual(self._raw(witness_open), witness_open_before)
        self.assertIsNone(self._raw(null_posted)[1])
        self.assertEqual(self._raw(mutable_posted)[1], JANUARY_POSTED_AT)
        self.assertEqual(self._raw(mutable_open)[1], '2026-02-02 18:00:00.000000')

        self.db.session.execute(
            text("UPDATE production_shift SET posted_at = :posted_at WHERE id = :id"),
            {'id': null_posted, 'posted_at': '2020-06-01 00:00:00.000000'},
        )
        self.db.session.commit()
        self.assertEqual(self._raw(null_posted)[1], '2020-06-01 00:00:00.000000')
        self.assertEqual(self._raw(witness_posted), witness_posted_before)

    def _insert_shift(self, *, status, posted_at, planned_date, started_at, note):
        self.db.session.execute(
            text(
                """
                INSERT INTO production_shift (
                    employee_id, work_area, status, posted_at, started_at,
                    created_at, updated_at, senior_shortage_confirmed,
                    planned_date, note
                ) VALUES (
                    1, 'assembly', :status, :posted_at, :started_at,
                    :started_at, :started_at, 0,
                    :planned_date, :note
                )
                """
            ),
            {
                'status': status,
                'posted_at': posted_at,
                'started_at': started_at,
                'planned_date': planned_date,
                'note': note,
            },
        )
        self.db.session.commit()
        return self.db.session.execute(
            text('SELECT id FROM production_shift WHERE note = :note'),
            {'note': note},
        ).scalar()

    def _expect_reject(self, sql, params):
        with self.assertRaises(IntegrityError) as caught:
            self.db.session.execute(text(sql), params)
            self.db.session.commit()
        self.db.session.rollback()
        self.assertIn(FROZEN_MESSAGE, str(caught.exception))

    def _raw(self, shift_id):
        row = self.db.session.execute(
            text(
                """
                SELECT status, posted_at, planned_date, started_at
                FROM production_shift WHERE id = :id
                """
            ),
            {'id': shift_id},
        ).one()
        return (row[0], row[1], row[2], row[3])

    def _shift_count(self, period_start, period_end) -> int:
        from shift_director_report import build_shift_director_report

        report = build_shift_director_report(period_start=period_start, period_end=period_end)
        return sum(row['shift_count'] for row in report['area_rows'])

    def _version(self):
        return self.db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()

    def _foreign_keys(self) -> int:
        return int(self.db.session.execute(text('PRAGMA foreign_keys')).scalar() or 0)

    def _trigger_sql(self):
        rows = self.db.session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )).all()
        return {row[0]: row[1] for row in rows}

    def _temp_tables(self):
        rows = self.db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%alembic_tmp%'"
        )).all()
        return [row[0] for row in rows]


if __name__ == '__main__':
    unittest.main()
