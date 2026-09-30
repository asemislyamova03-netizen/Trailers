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
"""

from alembic import op


revision = 'b4e8c1a90d27'
down_revision = 'f6b2d8c14e90'
branch_labels = None
depends_on = None

FROZEN_POSTED_AT_MESSAGE = 'проведённая смена не меняет дату фиксации'
TRIGGER_NAME = 'trg_production_shift_posted_at_frozen'

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


def _require_sqlite() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Триггер даты фиксации смены рассчитан только на SQLite. '
            'PRAGMA foreign_keys эта миграция не включает.'
        )


def upgrade():
    _require_sqlite()
    op.execute(TRIGGER_SQL)


def downgrade():
    _require_sqlite()
    op.execute(f'DROP TRIGGER IF EXISTS {TRIGGER_NAME}')
