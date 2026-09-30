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

До любого DDL читаются все четыре колонки и журнал
schema_column_origin_f6b2d8c14e90. Несовместимый тип, NOT NULL, DEFAULT
или PRIMARY KEY останавливают ревизию. Неизвестное имя в журнале тоже.
Разрешённое имя без колонки — явный отказ до DDL: журнал имён сам по себе
не доказывает, что колонку создала эта ревизия, и не разрешает её создавать
или удалять. Совместимая колонка и её данные не меняются. Добавляются
только отсутствующие имена, которых в журнале ещё нет.

Владение транзакцией на фактическом migrations/env.py и create_app().
SQLiteImpl держит transactional_ddl = False, поэтому внешний
begin_transaction в env.py — пустой. Внутри run_migrations Alembic всё же
оборачивает шаг и UPDATE alembic_version в одну транзакцию соединения.
На входе в ревизию SQLAlchemy уже считает транзакцию открытой, а
sqlite3.in_transaction ещё False: голый ALTER при этом фиксируется сразу,
отдельно от версии. Поэтому ревизия сама делает BEGIN на DBAPI-соединении
и не вызывает COMMIT и ROLLBACK. Успешный шаг фиксирует Alembic вместе с
версией. Исключение или закрытие соединения до этого фиксирования
откатывает и DDL, и версию. Если транзакция SQLite уже открыта или BEGIN
её не удержал, DDL не начинается.

Downgrade колонки и журнал не удаляет. Атомарный DROP вместе с версией на
этом стеке возможен, но безопасное происхождение из журнала имён не
доказывается. Опасный DROP чужих данных запрещён. Это не защита от
подделки журнала. После отката колонки и уже записанные значения остаются,
версию сдвигает Alembic. Повторный upgrade совместимые значения не
переписывает.

Простой ADD COLUMN эти таблицы не пересобирает, поэтому триггеры
sales_realization / sales_realization_line / produced_unit здесь не
снимаются.

TRAILERS_F6B2D8C14E90_ABORT_AFTER и TRAILERS_F6B2D8C14E90_CLOSE_AFTER —
только проверка обрыва upgrade. TRAILERS_F6B2D8C14E90_DOWNGRADE_ABORT —
только проверка обрыва downgrade. В обычном запуске переменные пустые.
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
CLOSE_ENV = 'TRAILERS_F6B2D8C14E90_CLOSE_AFTER'
DOWNGRADE_ABORT_ENV = 'TRAILERS_F6B2D8C14E90_DOWNGRADE_ABORT'
ALLOWED = {(table, column) for table, column, _expected in COLUMNS}

REFUSED_BEFORE_DDL = 'ревизия f6b2d8c14e90 остановлена до DDL'
LEDGER_NAME_WITHOUT_COLUMN = (
    'в журнале есть имя, колонки нет. Журнал имён не доказывает '
    'происхождение и не разрешает DDL'
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


def _probe_point(name: str):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return None
    if raw == 'end':
        return 'end'
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} должен быть целым числом или end.') from exc
    if value < 0:
        raise RuntimeError(f'{name} не может быть отрицательным.')
    return value


def _execute_statement(raw, statement) -> None:
    kind = statement[0]
    if kind == 'sql':
        raw.execute(statement[1])
        return
    if kind == 'insert':
        raw.execute(
            f'INSERT INTO {LEDGER_TABLE} (table_name, column_name) VALUES (?, ?)',
            (statement[1], statement[2]),
        )
        return
    raise RuntimeError(f'Неизвестный оператор миграции: {kind}.')


def _run_in_alembic_transaction(raw, statements: list) -> None:
    """BEGIN без своего COMMIT. Фиксирует или откатывает Alembic.

    На create_app()/env.py голый ALTER при sqlite3.in_transaction=False
    попадает в файл раньше UPDATE alembic_version. Явный BEGIN удерживает
    DDL в той же транзакции соединения, которую Alembic закрывает уже после
    записи версии. Свой ROLLBACK здесь не делается: иначе тест не отличает
    откат Alembic от отката самой ревизии.
    """
    if not statements:
        return
    if raw.in_transaction:
        raise RuntimeError(
            f'{REFUSED_BEFORE_DDL}: транзакция SQLite уже открыта. '
            'Второй BEGIN не делается, DDL не начат.'
        )
    try:
        raw.execute('BEGIN')
    except Exception as exc:
        raise RuntimeError(
            f'{REFUSED_BEFORE_DDL}: не удалось открыть транзакцию SQLite ({exc}). '
            'DDL не начат.'
        ) from exc
    if not raw.in_transaction:
        raise RuntimeError(
            f'{REFUSED_BEFORE_DDL}: BEGIN не удержал транзакцию SQLite. DDL не начат.'
        )
    limit = _probe_point(ABORT_ENV)
    close_after = _probe_point(CLOSE_ENV)
    for index, statement in enumerate(statements, start=1):
        if isinstance(limit, int) and index > limit:
            raise RuntimeError(
                'проверка отката f6b2d8c14e90: остановка до записи alembic_version '
                f'после {limit} DDL'
            )
        _execute_statement(raw, statement)
        if isinstance(close_after, int) and index >= close_after:
            raw.close()
            raise RuntimeError(
                'проверка закрытия соединения f6b2d8c14e90: соединение закрыто '
                f'после {close_after} DDL, до записи alembic_version'
            )
    if limit == 'end':
        raise RuntimeError(
            'проверка отката f6b2d8c14e90: остановка после DDL до записи alembic_version'
        )
    if close_after == 'end':
        raw.close()
        raise RuntimeError(
            'проверка закрытия соединения f6b2d8c14e90: соединение закрыто после DDL, '
            'до записи alembic_version'
        )


def _refuse(problems: list[str]) -> None:
    raise RuntimeError(f'{REFUSED_BEFORE_DDL}: ' + '; '.join(problems))


def upgrade() -> None:
    raw = _sqlite_raw()
    problems = []
    missing = []
    present = {}
    for table, column, expected in COLUMNS:
        row = _column_rows(raw, table).get(column)
        present[(table, column)] = row
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
    if ledger is not None:
        for table, column in sorted(ledger & ALLOWED):
            if present[(table, column)] is None:
                problems.append(f'{table}.{column}: {LEDGER_NAME_WITHOUT_COLUMN}')
    if problems:
        _refuse(problems)

    statements = []
    if missing and ledger is None:
        statements.append((
            'sql',
            f'CREATE TABLE {LEDGER_TABLE} ('
            'table_name TEXT NOT NULL, '
            'column_name TEXT NOT NULL, '
            'PRIMARY KEY (table_name, column_name))',
        ))
    for table, column, expected in missing:
        statements.append(('insert', table, column))
        statements.append((
            'sql',
            f'ALTER TABLE {table} ADD COLUMN {column} {expected}',
        ))
    _run_in_alembic_transaction(raw, statements)


def downgrade() -> None:
    """Не удаляет колонки и журнал.

    Список имён в schema_column_origin_f6b2d8c14e90 не доказывает, что
    колонку создала эта ревизия. NULL в колонке тоже ничего не доказывает.
    Подделку журнала эта функция не распознаёт и не обещает распознать.
    DDL нет, поэтому обрыв до записи версии не может стереть чужие данные.
    Версию при обычном возврате сдвигает Alembic. Колонки остаются.
    """
    raw = _sqlite_raw()
    mode = os.environ.get(DOWNGRADE_ABORT_ENV) or ''
    if mode == '':
        return
    if mode == 'raise':
        raise RuntimeError(
            'проверка отката f6b2d8c14e90: исключение до записи alembic_version, DDL нет'
        )
    if mode == 'close':
        raw.close()
        raise RuntimeError(
            'проверка отката f6b2d8c14e90: соединение закрыто до записи alembic_version, DDL нет'
        )
    raise RuntimeError(
        f'{DOWNGRADE_ABORT_ENV} должен быть пустым, raise или close.'
    )
