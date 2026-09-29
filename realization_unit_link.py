"""Связь строки реализации с единицей выпуска. Только момент проведения.

Черновик этот модуль не вызывает. Исторические строки не заполняются.
Отчёт «продано из выпущенных» здесь не считается: дата и срез периода ещё открыты.

Повтор одной и той же единицы на двух строках запрещает уникальный индекс
uq_sales_realization_line_produced_unit. NULL у разных строк разрешён:
так продажа склада и комплектующие не занимают ключ.

Обычное соединение приложения оставляет PRAGMA foreign_keys=0.
Существование produced_unit_id на SQLite держит триггер миграции
c3f8a1d94e27. Миграция d8e4b1c67a02 не даёт проведённой строке сменить
этот ключ на другой выпуск и не даёт провести чужой прицеп или
номенклатуру. Ключ здесь пишется раньше смены статуса на posted.
Смена id связанной единицы выпуска тем же триггером запрещена.
Миграция e1b7c4d92a58 после posted закрывает прямой SQL по
inventory_effect, документу строки, прицепу и номенклатуре строки,
а также по прицепу, номенклатуре и shift_output_id связанной единицы.
status единицы она не трогает: привязка VIN до проведения пишет статус
и trailer_id, пока проведённой строки ещё нет.
Эта функция всё равно сверяет прицеп, номенклатуру,
статус vin_assigned, количество 1 и повтор. Одна существующая единица
сама по себе продажу не доказывает. Проведение при несовпадении
откатывается целиком и не берёт «первую» единицу.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from extensions import db
from models import ProducedUnit, SalesRealization, SalesRealizationLine, Trailer


class ProducedUnitLinkError(Exception):
    """Проведение нельзя закрыть: связь единицы неоднозначна или не сходится."""


def _is_one_piece(quantity) -> bool:
    try:
        return Decimal(str(quantity)) == Decimal('1')
    except (InvalidOperation, TypeError, ValueError):
        return False


def _posted_line_already_links(unit_id: int, line_id: int | None):
    query = (
        SalesRealizationLine.query
        .join(SalesRealization, SalesRealization.id == SalesRealizationLine.realization_id)
        .filter(
            SalesRealization.status == 'posted',
            SalesRealizationLine.produced_unit_id == unit_id,
        )
    )
    if line_id:
        query = query.filter(SalesRealizationLine.id != line_id)
    return query.first()


def assign_produced_units_on_post(realization: SalesRealization) -> None:
    """Проставить produced_unit_id на строках черновика перед status='posted'.

    Не делает commit. При ошибке вызывающий код обязан откатить сессию:
    уже выставленные в памяти id тогда не сохраняются.
    """
    if realization.status != 'draft':
        raise ProducedUnitLinkError('Связь единицы выпуска пишется только при проведении черновика.')

    lines = list(realization.lines)
    # Сначала снять любой заранее подставленный id, затем записать заново.
    # Иначе уникальный индекс споткнётся о старое значение в той же транзакции.
    for line in lines:
        line.produced_unit_id = None
    db.session.flush()
    pending_unit_ids: set[int] = set()
    for line in lines:
        if (line.inventory_effect or '') != 'trailer_unit':
            continue
        _assign_trailer_line(line, pending_unit_ids)
    db.session.flush()


def _assign_trailer_line(line: SalesRealizationLine, pending_unit_ids: set[int]) -> None:
    if not _is_one_piece(line.quantity):
        raise ProducedUnitLinkError(
            f'Строка реализации #{line.line_no}: для единицы выпуска количество должно быть 1. '
            'Проведение отменено.'
        )
    if not line.trailer_id:
        raise ProducedUnitLinkError(
            f'Строка реализации #{line.line_no}: у строки прицепа нет прицепа. Проведение отменено.'
        )

    units = (
        ProducedUnit.query
        .filter(ProducedUnit.trailer_id == line.trailer_id)
        .order_by(ProducedUnit.id.asc())
        .all()
    )
    if not units:
        line.produced_unit_id = None
        return
    if len(units) >= 2:
        raise ProducedUnitLinkError(
            f'Строка реализации #{line.line_no}: на прицепе {len(units)} единиц выпуска. '
            'Первая не выбирается, проведение отменено.'
        )

    unit = units[0]
    trailer = line.trailer or Trailer.query.get(line.trailer_id)
    if (
        unit.status != 'vin_assigned'
        or trailer is None
        or line.item_id is None
        or unit.item_id != line.item_id
        or unit.item_id != trailer.item_id
        or unit.trailer_id != line.trailer_id
    ):
        raise ProducedUnitLinkError(
            f'Строка реализации #{line.line_no}: единица выпуска не сходится '
            'с прицепом, номенклатурой или статусом vin_assigned. Проведение отменено.'
        )
    if unit.id in pending_unit_ids or _posted_line_already_links(unit.id, line.id):
        raise ProducedUnitLinkError(
            f'Строка реализации #{line.line_no}: эта единица выпуска уже связана '
            'с другой строкой реализации. Проведение отменено.'
        )
    line.produced_unit_id = unit.id
    pending_unit_ids.add(unit.id)
    db.session.flush()
