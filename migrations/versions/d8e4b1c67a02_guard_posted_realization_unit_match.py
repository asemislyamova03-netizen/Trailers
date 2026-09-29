"""Запрет чужой привязки проведённой строки и смены id связанной единицы

Revision ID: d8e4b1c67a02
Revises: c3f8a1d94e27
Create Date: 2026-09-29 18:10:00.000000

Инвариант только для уже проведённой реализации. Черновик, NULL и строка
без документа со статусом posted не переписываются.

* UPDATE produced_unit_id у строки с родителем status='posted' запрещён.
  Так прямой переход на другой существующий выпуск, в том числе на вторую
  единицу того же прицепа, не двигает когорту «продано из выпущенных».
  NULL остаётся NULL: эта миграция ничего не заполняет.
* INSERT строки в уже проведённую реализацию и смена status на posted
  допускают produced_unit_id только если единица есть и у неё тот же
  trailer_id и item_id, что у строки. Иначе это чужой выпуск.
* UPDATE produced_unit.id запрещён, пока на старый id ссылается любая
  строка реализации. Иначе ссылка повисает, а триггер DELETE её уже не видит.
  Черновик тоже ссылка: висячий id не оставляем и у него.

Штатный post пишет ключ, пока статус ещё draft, и только потом ставит
posted. Удаление реализации удаляет строку, а не меняет id выпуска.
Статус vin_assigned, количество 1 и запрет второй единицы на прицепе
по-прежнему проверяет assign_produced_units_on_post. Этот файл их не
подменяет.

PRAGMA foreign_keys не включается. Уже висячий id и уже записанный чужой
id не ищутся и не чистятся. Четыре колонки модели вне Alembic
(item.tent_hight_mm, item.has_jockey_wheel, trailer.otts_id,
otts.full_mass_kg) эта миграция не добавляет.

Будущий Alembic batch rebuild таблиц sales_realization,
sales_realization_line или produced_unit этой миграцией не лечится.
История старых миграций не переписывается. Простой ADD COLUMN таблицу
не пересобирает: триггеры остаются, PRAGMA foreign_keys остаётся 0.
Принудительный batch recreate='always' на копии временной базы падает
на переименовании _alembic_tmp_* и оставляет эти временные таблицы:
триггер другой таблицы ссылается на ещё не возвращённое имя
(main.sales_realization_line, main.produced_unit или main.sales_realization).
Вердикт: пересборка этих трёх таблиц пока небезопасна.
"""

from alembic import op


revision = 'd8e4b1c67a02'
down_revision = 'c3f8a1d94e27'
branch_labels = None
depends_on = None

POSTED_REBIND_MESSAGE = 'проведённая строка не меняет produced_unit_id на другой выпуск'
POSTED_FOREIGN_UNIT_MESSAGE = 'проведённая строка не привязывается к чужому выпуску'
PARENT_ID_MESSAGE = 'нельзя сменить id единицы выпуска: на неё ссылается строка реализации'

TRIGGER_NAMES = (
    'trg_srl_posted_produced_unit_rebind_update',
    'trg_srl_posted_foreign_unit_insert',
    'trg_sales_realization_post_foreign_unit',
    'trg_sales_realization_insert_posted_foreign_unit',
    'trg_produced_unit_id_change_while_linked',
)

_REBIND_UPDATE = f"""
CREATE TRIGGER trg_srl_posted_produced_unit_rebind_update
BEFORE UPDATE OF produced_unit_id ON sales_realization_line
FOR EACH ROW
WHEN NEW.produced_unit_id IS NOT OLD.produced_unit_id
 AND EXISTS (
    SELECT 1 FROM sales_realization
    WHERE id = NEW.realization_id AND status = 'posted'
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_REBIND_MESSAGE}');
END
"""

_FOREIGN_INSERT = f"""
CREATE TRIGGER trg_srl_posted_foreign_unit_insert
BEFORE INSERT ON sales_realization_line
FOR EACH ROW
WHEN NEW.produced_unit_id IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM sales_realization
    WHERE id = NEW.realization_id AND status = 'posted'
 )
BEGIN
    SELECT RAISE(ABORT, '{POSTED_FOREIGN_UNIT_MESSAGE}')
    WHERE NOT EXISTS (
        SELECT 1 FROM produced_unit
        WHERE id = NEW.produced_unit_id
          AND trailer_id IS NOT NULL
          AND trailer_id = NEW.trailer_id
          AND item_id = NEW.item_id
    );
END
"""

_STATUS_UPDATE = f"""
CREATE TRIGGER trg_sales_realization_post_foreign_unit
BEFORE UPDATE OF status ON sales_realization
FOR EACH ROW
WHEN NEW.status = 'posted' AND OLD.status IS NOT 'posted'
BEGIN
    SELECT RAISE(ABORT, '{POSTED_FOREIGN_UNIT_MESSAGE}')
    WHERE EXISTS (
        SELECT 1 FROM sales_realization_line AS line
        WHERE line.realization_id = NEW.id
          AND line.produced_unit_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM produced_unit AS unit
              WHERE unit.id = line.produced_unit_id
                AND unit.trailer_id IS NOT NULL
                AND unit.trailer_id = line.trailer_id
                AND unit.item_id = line.item_id
          )
    );
END
"""

_STATUS_INSERT = f"""
CREATE TRIGGER trg_sales_realization_insert_posted_foreign_unit
BEFORE INSERT ON sales_realization
FOR EACH ROW
WHEN NEW.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, '{POSTED_FOREIGN_UNIT_MESSAGE}')
    WHERE EXISTS (
        SELECT 1 FROM sales_realization_line AS line
        WHERE line.realization_id = NEW.id
          AND line.produced_unit_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM produced_unit AS unit
              WHERE unit.id = line.produced_unit_id
                AND unit.trailer_id IS NOT NULL
                AND unit.trailer_id = line.trailer_id
                AND unit.item_id = line.item_id
          )
    );
END
"""

_PARENT_ID = f"""
CREATE TRIGGER trg_produced_unit_id_change_while_linked
BEFORE UPDATE OF id ON produced_unit
FOR EACH ROW
WHEN NEW.id IS NOT OLD.id
 AND EXISTS (
    SELECT 1 FROM sales_realization_line
    WHERE produced_unit_id = OLD.id
 )
BEGIN
    SELECT RAISE(ABORT, '{PARENT_ID_MESSAGE}');
END
"""


def _require_sqlite() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError(
            'Триггер проведённой связи produced_unit_id рассчитан только на SQLite. '
            'PRAGMA foreign_keys эта миграция не включает.'
        )


def upgrade():
    _require_sqlite()
    # Старые NULL, висячие id и уже чужие id не обновляются.
    op.execute(_REBIND_UPDATE)
    op.execute(_FOREIGN_INSERT)
    op.execute(_STATUS_UPDATE)
    op.execute(_STATUS_INSERT)
    op.execute(_PARENT_ID)


def downgrade():
    _require_sqlite()
    for name in reversed(TRIGGER_NAMES):
        op.execute(f'DROP TRIGGER IF EXISTS {name}')
