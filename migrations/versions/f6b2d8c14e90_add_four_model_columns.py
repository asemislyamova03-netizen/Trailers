"""Четыре колонки модели, которых не было после Alembic head.

Revision ID: f6b2d8c14e90
Revises: e1b7c4d92a58
Create Date: 2026-09-30 12:00:00.000000

Добавляет только:

* item.tent_hight_mm INTEGER NULL
* item.has_jockey_wheel BOOLEAN NULL
* trailer.otts_id INTEGER NULL
* otts.full_mass_kg INTEGER NULL

Без DEFAULT, без backfill, без переименования и без внешнего ключа.
Опечатка tent_hight_mm сохраняется. PRAGMA foreign_keys не включается.
Девять колонок item, которые есть в Alembic и отсутствуют в классе Item,
эта ревизия не трогает. Старые downgrade c1d03f92e3c6 и d1f2a3b4c5d6
остаются отдельным BLOCKED: история миграций не переписывается.

До любого DDL читаются все четыре колонки. Несовместимый тип, NOT NULL,
DEFAULT или PRIMARY KEY останавливают ревизию, и ни одна колонка не
добавляется. Совместимая колонка и её данные не меняются. Добавляются
только отсутствующие.

Происхождение пишется в schema_column_origin_f6b2d8c14e90 в той же
SQLite-транзакции, что и ALTER. NULL в самой колонке происхождение не
доказывает. Downgrade удаляет только строки этого журнала. Если журнала
нет или в нём есть неизвестное имя, откат останавливается и колонки не
трогает.

Простой ADD COLUMN и DROP COLUMN эти таблицы не пересобирают, поэтому
триггеры sales_realization / sales_realization_line / produced_unit
здесь не снимаются. Будущая пересборка таблицы, на которую ссылается
триггер, по-прежнему требует приёма из e1b7c4d92a58. Этот файл его не
переносит в старые ревизии.

TRAILERS_F6B2D8C14E90_ABORT_AFTER — только проверка отката. В обычном
запуске переменная пустая. Если это целое N, транзакция обрывается
после N DDL-операций и откатывается до COMMIT.
"""

from __future__ import annotations

import os

from alembic import op


revision = 'f6b2d8c14e90'
down_revision = 'e1b7c4d92a58'
branch_labels = None
depends_on = None

COLUMNS = (
    ('item', 'tent_hight_mm', 'INTEGER'),
    ('item', 'has_jockey_wheel', 'BOOLEAN'),
    ('trailer', 'otts_id', 'INTEGER'),
    ('otts', 'full_mass_kg', 'INTEGER'),
)
LEDGER_TABLE = 'schema_column_origin_f6b2d8c14e90'
ABORT_ENV = 'TRAILERS_F6B2D8C14E90_ABORT_AFTER'
ALLOWED = {(table, column) for table, column, _expected in COLUMNS}

REFUSED_BEFORE_DDL = 'ревизия f6b2d8c14e90 остановлена до DDL'
NO_LEDGER_MESSAGE = (
    'откат f6b2d8c14e90 остановлен: нет таблицы происхождения '
    f'{LEDGER_TABLE}. Колонки не удаляются, потому что нельзя доказать, '
    'что их создала эта ревизия. NULL в колонке происхождение не доказывает.'
)


def _sqlite_raw():
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Ревизия f6b2d8c14e90 рассчитана только на SQLite. '
            'PRAGMA foreign_keys она не включает.'
        )
    connection = getattr(bind, 'connection', None)
    raw = getattr(connection, 'driver_connection', None) if connection is not None else None
    if raw is None:
        raise RuntimeError('Нет DBAPI-соединения SQLite для атомарного DDL.')
    return raw


def _table_exists(raw, table: str) -> bool:
    return raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _column_rows(raw, table: str) -> dict:
    if table not in {item[0] for item in COLUMNS}:
        raise RuntimeError(f'Неизвестная таблица {table}.')
    if not _table_exists(raw, table):
        raise RuntimeError(f'Нет таблицы {table}. DDL не начат.')
    return {row[1]: row for row in raw.execute(f'PRAGMA table_info({table})').fetchall()}


def _problems(row, expected_type: str) -> list[str]:
    actual = (row[2] or '').strip().upper()
    problems = []
    if actual != expected_type:
        problems.append(f'type {row[2]!r} != {expected_type!r}')
    if row[3] != 0:
        problems.append('NOT NULL')
    if row[4] is not None:
        problems.append(f'default {row[4]!r}')
    if row[5]:
        problems.append('PRIMARY KEY')
    return problems


def _ledger_rows(raw):
    if not _table_exists(raw, LEDGER_TABLE):
        return None
    return {
        (row[0], row[1])
        for row in raw.execute(
            f'SELECT table_name, column_name FROM {LEDGER_TABLE}'
        ).fetchall()
    }


def _unknown_ledger(ledger) -> list[tuple[str, str]]:
    if ledger is None:
        return []
    return sorted(ledger - ALLOWED)


def _abort_after() -> int | None:
    raw = os.environ.get(ABORT_ENV)
    if raw is None or raw == '':
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{ABORT_ENV} должен быть целым числом.') from exc
    if value < 0:
        raise RuntimeError(f'{ABORT_ENV} не может быть отрицательным.')
    return value


def _rollback(raw) -> None:
    try:
        raw.execute('ROLLBACK')
    except Exception:
        raw.rollback()


def _run_atomic(raw, statements: list[str]) -> None:
    if not statements:
        return
    limit = _abort_after()
    try:
        raw.execute('BEGIN')
    except Exception as exc:
        raise RuntimeError(
            f'{REFUSED_BEFORE_DDL}: не удалось открыть транзакцию SQLite ({exc}). '
            'DDL не начат.'
        ) from exc
    try:
        for index, sql in enumerate(statements, start=1):
            if limit is not None and index > limit:
                raise RuntimeError(
                    f'проверка отката f6b2d8c14e90: остановка до COMMIT после {limit} DDL'
                )
            raw.execute(sql)
        raw.execute('COMMIT')
    except Exception:
        _rollback(raw)
        raise


def _refuse(problems: list[str]) -> None:
    raise RuntimeError(f'{REFUSED_BEFORE_DDL}: ' + '; '.join(problems))


def upgrade() -> None:
    raw = _sqlite_raw()
    problems = []
    missing = []
    for table, column, expected in COLUMNS:
        row = _column_rows(raw, table).get(column)
        if row is None:
            missing.append((table, column, expected))
            continue
        column_problems = _problems(row, expected)
        if column_problems:
            problems.append(f"{table}.{column}: {', '.join(column_problems)}")
    ledger = _ledger_rows(raw)
    unknown = _unknown_ledger(ledger)
    if unknown:
        problems.append(
            'таблица происхождения содержит неизвестные строки: '
            + ', '.join(f'{table}.{column}' for table, column in unknown)
        )
    if problems:
        _refuse(problems)

    statements = []
    if ledger is None:
        statements.append(
            f'CREATE TABLE {LEDGER_TABLE} ('
            'table_name TEXT NOT NULL, '
            'column_name TEXT NOT NULL, '
            'PRIMARY KEY (table_name, column_name))'
        )
    for table, column, expected in missing:
        statements.append(
            'INSERT INTO '
            f"{LEDGER_TABLE} (table_name, column_name) VALUES ('{table}', '{column}')"
        )
        statements.append(f'ALTER TABLE {table} ADD COLUMN {column} {expected}')
    _run_atomic(raw, statements)


def downgrade() -> None:
    raw = _sqlite_raw()
    ledger = _ledger_rows(raw)
    if ledger is None:
        raise RuntimeError(NO_LEDGER_MESSAGE)
    unknown = _unknown_ledger(ledger)
    if unknown:
        raise RuntimeError(
            'откат f6b2d8c14e90 остановлен: в таблице происхождения есть '
            'неизвестные строки: '
            + ', '.join(f'{table}.{column}' for table, column in unknown)
            + '. Колонки не удаляются.'
        )
    statements = []
    for table, column, _expected in COLUMNS:
        if (table, column) not in ledger:
            continue
        if column not in _column_rows(raw, table):
            continue
        statements.append(f'ALTER TABLE {table} DROP COLUMN {column}')
    statements.append(f'DROP TABLE {LEDGER_TABLE}')
    _run_atomic(raw, statements)
