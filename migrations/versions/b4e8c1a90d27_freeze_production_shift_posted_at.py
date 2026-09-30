"""Дата фиксации проведённой смены не переписывается.

Revision ID: b4e8c1a90d27
Revises: f6b2d8c14e90
Create Date: 2026-09-30 16:40:00.000000

Отчёт директора берёт когорту выпуска по ProductionShift.posted_at.
Обычное соединение Flask оставляет PRAGMA foreign_keys=0, поэтому
прямой UPDATE после проведения переносит смену в другой период.

Эта ревизия добавляет один BEFORE UPDATE OF posted_at.
Она не включает foreign_keys, не меняет 12 уже существующих триггеров,
не чинит старые downgrade и не обновляет строки.

Правило узкое:

* у открытой смены posted_at ещё NULL, первая запись даты проходит
  вместе со штатным проведением;
* если статус уже posted или дата уже записана, другое значение и NULL
  отвергаются одним statement, в том числе вместе со сменой status;
* повторная запись даты после posted → open тоже отвергается.

Статус эта ревизия не замораживает. Прямой SQL может увести смену из
posted в open и тем самым вынуть её из когорты, не трогая дату.
Обратный переход в posted дату не двигает. Запрет такого перехода был
бы новым правилом отмены смены: его здесь нет.

Старые NULL остаются NULL. Backfill нет.

Атомарность с уже установленного f6b2d8c14e90. SQLiteImpl держит
transactional_ddl = False, поэтому внешний begin_transaction в env.py
пустой. pysqlite фиксирует голый CREATE TRIGGER и DROP TRIGGER сразу,
отдельно от UPDATE alembic_version. Ревизия сама делает BEGIN на
DBAPI-соединении и не вызывает COMMIT и ROLLBACK. Успешный шаг
фиксирует Alembic вместе с версией. Исключение, закрытие соединения
или отказ записи версии откатывают и DDL, и версию.

Повтор после уже случившегося обрыва upgrade: точный канонический
триггер при старой версии остаётся как есть, версию дописывает Alembic.
Чужой триггер с тем же именем, другой таблицей или другим SQL даёт
отказ до DDL и до смены версии. Чужой триггер не снимается и не
пересоздаётся. Stamp нет.

Downgrade этой ревизии такой же: канонический триггер снимается в
транзакции Alembic, отсутствие триггера только позволяет сдвинуть
версию, чужой триггер сохраняется. Старые downgrade не чинятся.

TRAILERS_B4E8C1A90D27_UPGRADE_ABORT, TRAILERS_B4E8C1A90D27_UPGRADE_CLOSE,
TRAILERS_B4E8C1A90D27_DOWNGRADE_ABORT и
TRAILERS_B4E8C1A90D27_DOWNGRADE_CLOSE — только проверка обрыва.
В обычном запуске переменные пустые. Допустимое значение: after_ddl.
"""

from __future__ import annotations

import os

from alembic import op


revision = 'b4e8c1a90d27'
down_revision = 'f6b2d8c14e90'
branch_labels = None
depends_on = None

FROZEN_POSTED_AT_MESSAGE = 'проведённая смена не меняет дату фиксации'
TRIGGER_NAME = 'trg_production_shift_posted_at_frozen'
TRIGGER_TABLE = 'production_shift'

TRIGGER_SQL = f"""
CREATE TRIGGER {TRIGGER_NAME}
BEFORE UPDATE OF posted_at ON production_shift
FOR EACH ROW
WHEN (
    OLD.status = 'posted'
    OR OLD.posted_at IS NOT NULL
)
 AND NEW.posted_at IS NOT OLD.posted_at
BEGIN
    SELECT RAISE(ABORT, '{FROZEN_POSTED_AT_MESSAGE}');
END
"""

REFUSED_BEFORE_DDL = 'ревизия b4e8c1a90d27 остановлена до DDL'
UPGRADE_ABORT_ENV = 'TRAILERS_B4E8C1A90D27_UPGRADE_ABORT'
UPGRADE_CLOSE_ENV = 'TRAILERS_B4E8C1A90D27_UPGRADE_CLOSE'
DOWNGRADE_ABORT_ENV = 'TRAILERS_B4E8C1A90D27_DOWNGRADE_ABORT'
DOWNGRADE_CLOSE_ENV = 'TRAILERS_B4E8C1A90D27_DOWNGRADE_CLOSE'
_PROBE_ENVS = (
    UPGRADE_ABORT_ENV,
    UPGRADE_CLOSE_ENV,
    DOWNGRADE_ABORT_ENV,
    DOWNGRADE_CLOSE_ENV,
)


def _sqlite_raw():
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Триггер даты фиксации смены рассчитан только на SQLite. '
            'PRAGMA foreign_keys эта миграция не включает.'
        )
    connection = getattr(bind, 'connection', None)
    raw = getattr(connection, 'driver_connection', None) if connection is not None else None
    if raw is None:
        raise RuntimeError('Нет DBAPI-соединения SQLite для атомарного DDL.')
    return raw


def _probe_mode(name: str):
    value = os.environ.get(name)
    if value is None or value == '':
        return None
    if value != 'after_ddl':
        raise RuntimeError(
            f'{REFUSED_BEFORE_DDL}: {name} должен быть пустым или after_ddl.'
        )
    return value


def _validate_probes() -> None:
    for name in _PROBE_ENVS:
        _probe_mode(name)


def _trigger_row(raw):
    return raw.execute(
        "SELECT tbl_name, sql, rowid FROM sqlite_master "
        "WHERE type = 'trigger' AND name = ?",
        (TRIGGER_NAME,),
    ).fetchone()


def _is_canonical(row) -> bool:
    if row is None:
        return False
    table_name, sql, _rowid = row
    return table_name == TRIGGER_TABLE and (sql or '').strip() == TRIGGER_SQL.strip()


def _refuse_foreign(row) -> None:
    table_name = row[0] if row is not None else '?'
    raise RuntimeError(
        f'{REFUSED_BEFORE_DDL}: триггер {TRIGGER_NAME} уже есть '
        f'на таблице {table_name}, но это не канонический триггер '
        f'{TRIGGER_TABLE}. Схема и alembic_version не меняются.'
    )


def _begin_for_alembic(raw) -> None:
    """BEGIN без своего COMMIT. Фиксирует или откатывает Alembic."""
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


def _fault_after_ddl(raw, *, action: str, abort_env: str, close_env: str) -> None:
    if _probe_mode(abort_env) == 'after_ddl':
        raise RuntimeError(
            'проверка отката b4e8c1a90d27: остановка после DDL '
            f'({action}) до записи alembic_version'
        )
    if _probe_mode(close_env) == 'after_ddl':
        raw.close()
        raise RuntimeError(
            'проверка закрытия соединения b4e8c1a90d27: соединение закрыто после DDL '
            f'({action}), до записи alembic_version'
        )


def upgrade():
    _validate_probes()
    raw = _sqlite_raw()
    row = _trigger_row(raw)
    if row is not None:
        if not _is_canonical(row):
            _refuse_foreign(row)
        return
    _begin_for_alembic(raw)
    raw.execute(TRIGGER_SQL)
    _fault_after_ddl(
        raw,
        action='CREATE TRIGGER',
        abort_env=UPGRADE_ABORT_ENV,
        close_env=UPGRADE_CLOSE_ENV,
    )


def downgrade():
    _validate_probes()
    raw = _sqlite_raw()
    row = _trigger_row(raw)
    if row is None:
        return
    if not _is_canonical(row):
        _refuse_foreign(row)
    _begin_for_alembic(raw)
    raw.execute(f'DROP TRIGGER {TRIGGER_NAME}')
    _fault_after_ddl(
        raw,
        action='DROP TRIGGER',
        abort_env=DOWNGRADE_ABORT_ENV,
        close_env=DOWNGRADE_CLOSE_ENV,
    )
