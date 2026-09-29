"""SQLite-триггеры существования produced_unit_id, без глобального pragma

Revision ID: c3f8a1d94e27
Revises: b7e2c4a9d815
Create Date: 2026-09-29 16:50:00.000000

Сравнение вариантов на SHA 665d021:

* Проверки в assign_produced_units_on_post и POST /realizations/<id>/post
  не видят прямой INSERT/UPDATE и DELETE produced_unit. Их оставляем:
  существование родителя не доказывает верную продажу.
* Слушатель SQLAlchemy тоже не видит сырой SQL.
* CHECK не умеет подзапрос к produced_unit.
* PRAGMA foreign_keys=ON включил бы все внешние ключи схемы, не только
  этот столбец. Эта миграция pragma не ставит.

Выбран узкий триггер SQLite. Он срабатывает при foreign_keys=0.
Строки не обновляются и не заполняются: NULL остаётся NULL.
Уже записанный чужой id эта миграция не ищет и не чистит.
"""

from alembic import op


revision = 'c3f8a1d94e27'
down_revision = 'b7e2c4a9d815'
branch_labels = None
depends_on = None

MISSING_UNIT_MESSAGE = 'produced_unit_id ссылается на отсутствующую единицу выпуска'
DELETE_PARENT_MESSAGE = 'нельзя удалить единицу выпуска: на неё ссылается строка реализации'

TRIGGER_NAMES = (
    'trg_srl_produced_unit_id_insert',
    'trg_srl_produced_unit_id_update',
    'trg_produced_unit_delete_linked_line',
)

_INSERT_TRIGGER = f"""
CREATE TRIGGER trg_srl_produced_unit_id_insert
BEFORE INSERT ON sales_realization_line
FOR EACH ROW
WHEN NEW.produced_unit_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '{MISSING_UNIT_MESSAGE}')
    WHERE NOT EXISTS (
        SELECT 1 FROM produced_unit WHERE id = NEW.produced_unit_id
    );
END
"""

_UPDATE_TRIGGER = f"""
CREATE TRIGGER trg_srl_produced_unit_id_update
BEFORE UPDATE OF produced_unit_id ON sales_realization_line
FOR EACH ROW
WHEN NEW.produced_unit_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '{MISSING_UNIT_MESSAGE}')
    WHERE NOT EXISTS (
        SELECT 1 FROM produced_unit WHERE id = NEW.produced_unit_id
    );
END
"""

_DELETE_TRIGGER = f"""
CREATE TRIGGER trg_produced_unit_delete_linked_line
BEFORE DELETE ON produced_unit
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{DELETE_PARENT_MESSAGE}')
    WHERE EXISTS (
        SELECT 1 FROM sales_realization_line
        WHERE produced_unit_id = OLD.id
    );
END
"""


def _require_sqlite() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Триггер связи produced_unit_id рассчитан только на SQLite. '
            'PRAGMA foreign_keys эта миграция не включает.'
        )


def upgrade():
    _require_sqlite()
    # Существующие NULL и уже висячие id не трогаем.
    op.execute(_INSERT_TRIGGER)
    op.execute(_UPDATE_TRIGGER)
    op.execute(_DELETE_TRIGGER)


def downgrade():
    _require_sqlite()
    for name in reversed(TRIGGER_NAMES):
        op.execute(f'DROP TRIGGER IF EXISTS {name}')
