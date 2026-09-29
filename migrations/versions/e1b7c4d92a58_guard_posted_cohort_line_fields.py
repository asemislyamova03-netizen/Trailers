"""Заморозка полей проведённой связи, которые двигают когорту

Revision ID: e1b7c4d92a58
Revises: d8e4b1c67a02
Create Date: 2026-09-29 21:20:00.000000

Инвариант прежний: проведённая строка с produced_unit_id смотрит на
существующую единицу с тем же trailer_id и item_id, и эта связь не
переезжает на другой выпуск. d8e4b1c67a02 закрывает смену самого ключа.
Эта миграция закрывает прямой SQL, который ключ не трогает, но либо
вынимает строку из числителя «продано из выпущенных», либо переносит
единицу в другую когорту, либо расходится с прицепом и номенклатурой.

Пока родительский документ не posted, поля пишутся как раньше.
Привязка VIN до проведения ставит produced_unit.status и trailer_id:
проведённой строки ещё нет, триггер её не держит.

После posted прямой UPDATE запрещён, если у строки есть produced_unit_id:

* sales_realization_line.inventory_effect
* sales_realization_line.realization_id, если старый или новый документ posted
* sales_realization_line.trailer_id и item_id
* produced_unit.trailer_id, item_id и shift_output_id, пока на единицу
  ссылается проведённая строка

produced_unit.status намеренно не заморожен. Смена статуса не входит
в снимок когорты. Законная привязка VIN до проведения должна писать
status. Отдельный запрет «навсегда оставить vin_assigned» — бизнес-решение:
в коде уже есть другие финальные статусы, а число продажи от них не меняется.

Черновик может держать чужой id. Цена, комментарий и количество этой
миграцией не блокируются. Старые NULL и уже испорченные значения не
ищутся и не чинятся. PRAGMA foreign_keys не включается. Четыре колонки
модели вне Alembic эта миграция не добавляет.

Будущая пересборка Alembic batch для sales_realization,
sales_realization_line или produced_unit по-прежнему падает, если
триггеры ещё висят: SQLite не переименовывает _alembic_tmp_*, пока
другой триггер ссылается на старое имя. История старых миграций здесь
не переписывается. Безопасный приём только для новой миграции, которая
сама пересобирает одну из этих таблиц:

1. Прочитать из sqlite_master name и sql всех trigger, чей SQL упоминает
   пересобираемую таблицу.
2. DROP TRIGGER IF EXISTS для каждого такого имени.
3. Выполнить batch_alter_table.
4. Заново выполнить сохранённый CREATE TRIGGER.
5. PRAGMA foreign_keys не включать.

Простой ADD COLUMN таблицу не пересобирает, триггеры при этом остаются.
"""

from alembic import op


revision = 'e1b7c4d92a58'
down_revision = 'd8e4b1c67a02'
branch_labels = None
depends_on = None

POSTED_EFFECT_MESSAGE = 'проведённая строка не меняет inventory_effect'
POSTED_DOCUMENT_MESSAGE = 'проведённая строка не меняет документ реализации'
POSTED_LINE_MATCH_MESSAGE = 'проведённая строка не меняет прицеп или номенклатуру связанного выпуска'
POSTED_UNIT_COHORT_MESSAGE = (
    'нельзя менять прицеп, номенклатуру или смену проданной единицы выпуска'
)

GUARDED_TABLES = (
    'sales_realization',
    'sales_realization_line',
    'produced_unit',
)

TRIGGER_NAMES = (
    'trg_srl_posted_inventory_effect_update',
    'trg_srl_posted_realization_id_update',
    'trg_srl_posted_trailer_item_update',
    'trg_produced_unit_posted_cohort_update',
)

_EFFECT_UPDATE = f"""
CREATE TRIGGER trg_srl_posted_inventory_effect_update
BEFORE UPDATE OF inventory_effect ON sales_realization_line
FOR EACH ROW
WHEN NEW.inventory_effect IS NOT OLD.inventory_effect
 AND (
    NEW.produced_unit_id IS NOT NULL
    OR OLD.produced_unit_id IS NOT NULL
 )
 AND (
    EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = NEW.realization_id AND status = 'posted'
    )
    OR EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = OLD.realization_id AND status = 'posted'
    )
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_EFFECT_MESSAGE}');
END
"""

_DOCUMENT_UPDATE = f"""
CREATE TRIGGER trg_srl_posted_realization_id_update
BEFORE UPDATE OF realization_id ON sales_realization_line
FOR EACH ROW
WHEN NEW.realization_id IS NOT OLD.realization_id
 AND (
    NEW.produced_unit_id IS NOT NULL
    OR OLD.produced_unit_id IS NOT NULL
 )
 AND (
    EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = OLD.realization_id AND status = 'posted'
    )
    OR EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = NEW.realization_id AND status = 'posted'
    )
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_DOCUMENT_MESSAGE}');
END
"""

_LINE_MATCH_UPDATE = f"""
CREATE TRIGGER trg_srl_posted_trailer_item_update
BEFORE UPDATE OF trailer_id, item_id ON sales_realization_line
FOR EACH ROW
WHEN (
    NEW.trailer_id IS NOT OLD.trailer_id
    OR NEW.item_id IS NOT OLD.item_id
 )
 AND (
    NEW.produced_unit_id IS NOT NULL
    OR OLD.produced_unit_id IS NOT NULL
 )
 AND (
    EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = NEW.realization_id AND status = 'posted'
    )
    OR EXISTS (
        SELECT 1 FROM sales_realization
        WHERE id = OLD.realization_id AND status = 'posted'
    )
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_LINE_MATCH_MESSAGE}');
END
"""

_UNIT_COHORT_UPDATE = f"""
CREATE TRIGGER trg_produced_unit_posted_cohort_update
BEFORE UPDATE OF trailer_id, item_id, shift_output_id ON produced_unit
FOR EACH ROW
WHEN (
    NEW.trailer_id IS NOT OLD.trailer_id
    OR NEW.item_id IS NOT OLD.item_id
    OR NEW.shift_output_id IS NOT OLD.shift_output_id
 )
 AND EXISTS (
    SELECT 1
    FROM sales_realization_line AS line
    JOIN sales_realization AS doc ON doc.id = line.realization_id
    WHERE line.produced_unit_id = OLD.id
      AND doc.status = 'posted'
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_UNIT_COHORT_MESSAGE}');
END
"""

TRIGGER_SQL = (
    _EFFECT_UPDATE,
    _DOCUMENT_UPDATE,
    _LINE_MATCH_UPDATE,
    _UNIT_COHORT_UPDATE,
)


def _require_sqlite() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Триггер полей проведённой когорты рассчитан только на SQLite. '
            'PRAGMA foreign_keys эта миграция не включает.'
        )


def upgrade():
    _require_sqlite()
    # Уже записанные чужие ключи, NULL и висячие id не обновляются.
    for statement in TRIGGER_SQL:
        op.execute(statement)


def downgrade():
    _require_sqlite()
    for name in reversed(TRIGGER_NAMES):
        op.execute(f'DROP TRIGGER IF EXISTS {name}')
