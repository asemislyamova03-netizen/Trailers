"""Только чтение: агрегаты директорского отчёта по проведённым сменам.

Не проводит смены, не меняет остатки и не подключается к живому серверу.

«Продано из выпущенных» — снимок когорты. Когорта: ProducedUnit прицепов
проведённых смен, уже отобранных по ProductionShift.posted_at.
Числитель: сколько этих единиц сейчас имеют ровно одну строку
SalesRealizationLine с produced_unit_id, inventory_effect='trailer_unit'
и родительским SalesRealization.status='posted'.
Дата реализации и posted_at продажи не фильтруются.
Удалённая реализация в число не входит: дату продажи и возврата
восстановить нельзя.

Материалы, годные детали и брак деталей возвращаются только строками
одной номенклатуры и одной единицы. Общего количества разных Item или
разных единиц нет: ни в totals, ни по участку, ни по зоне.
Прицепы (число единиц) и часы остаются отдельными показателями.
Старые +1 не входят в выпуск и расход posted-смен.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func

from inventory_units import normalize_unit
from models import (
    InventoryOperation,
    ProducedUnit,
    ProductionShift,
    ProductionShiftMaterial,
    ProductionShiftOutput,
    SalesRealization,
    SalesRealizationLine,
)

SOLD_FROM_PRODUCED_LABEL = 'Из выпущенных за период продано на сейчас'

SOLD_FROM_PRODUCED_LIMITATION = (
    'Удалённая реализация больше не считается. '
    'Историческую дату продажи и возврата восстановить нельзя: '
    'отдельного документа возврата нет, строка удаляется вместе с документом. '
    'Это текущее состояние когорты, а не продажи выбранного периода. '
    'realization_date и posted_at продажи не фильтруются. '
    'Старый +1, продажа склада с пустым ключом, черновик, деталь '
    'и совпадение trailer_id без produced_unit_id в число не входят.'
)


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _sold_from_produced_snapshot(cohort_unit_ids: set[int]) -> dict:
    """Сколько единиц когорты продано сейчас. Дату продажи не смотрит."""
    sold = 0
    if cohort_unit_ids:
        counts = (
            SalesRealizationLine.query
            .join(SalesRealization, SalesRealization.id == SalesRealizationLine.realization_id)
            .filter(
                SalesRealizationLine.produced_unit_id.in_(cohort_unit_ids),
                SalesRealizationLine.inventory_effect == 'trailer_unit',
                SalesRealization.status == 'posted',
            )
            .with_entities(
                SalesRealizationLine.produced_unit_id,
                func.count(SalesRealizationLine.id),
            )
            .group_by(SalesRealizationLine.produced_unit_id)
            .all()
        )
        # Ровно одна строка. Две строки на одну единицу числом не становятся.
        sold = sum(1 for _unit_id, line_count in counts if line_count == 1)
    return {
        'status': 'SNAPSHOT',
        'value': sold,
        'cohort_units': len(cohort_unit_ids),
        'label': SOLD_FROM_PRODUCED_LABEL,
        'limitation': SOLD_FROM_PRODUCED_LIMITATION,
        'sale_date_filter': None,
    }


def _apply_period(query, column, period_start: datetime | None, period_end: datetime | None):
    if period_start is not None:
        query = query.filter(column >= period_start)
    if period_end is not None:
        query = query.filter(column <= period_end)
    return query


def _zone_code(shift: ProductionShift) -> str:
    area = shift.direction_area
    code = (area.code or '').strip() if area else ''
    return code or 'UNKNOWN'


def _empty_area(work_area: str, direction_code: str, direction_name: str) -> dict:
    return {
        'work_area': work_area,
        'direction_code': direction_code,
        'direction_name': direction_name,
        'shift_count': 0,
        'hours': Decimal('0'),
        'defect_trailer_units': {},
        'defect_part_items': {},
        'defect_other_units': {},
        'good_trailers': 0,
        'good_trailer_declared_qty': Decimal('0'),
        'good_part_items': {},
    }


def _measure_unit(unit: str | None, item_unit: str | None = None) -> str:
    return normalize_unit(unit or item_unit)


def _add_unit_qty(store: dict[str, Decimal], unit: str, qty: Decimal) -> None:
    if qty == 0:
        return
    store[unit] = store.get(unit, Decimal('0')) + qty


def _unit_lines(store: dict[str, Decimal]) -> list[dict]:
    return [
        {'unit': unit, 'qty': qty}
        for unit, qty in sorted(store.items())
        if qty != 0
    ]


def _add_item_qty(store: dict[tuple, dict], item, item_id, unit: str, qty: Decimal) -> None:
    """Складывать только одну номенклатуру и одну единицу."""
    if qty == 0:
        return
    key = (item_id or 0, unit)
    group = store.get(key)
    if group is None:
        group = {
            'item_id': item_id,
            'item_article': (item.article or '') if item else '',
            'item_name': (item.name or '') if item else '',
            'unit': unit,
            'qty': Decimal('0'),
        }
        store[key] = group
    group['qty'] += qty


def _item_lines(store: dict[tuple, dict]) -> list[dict]:
    return [
        group
        for group in sorted(
            store.values(),
            key=lambda row: (row['item_article'] or '', row['unit'] or '', row['item_id'] or 0),
        )
        if group['qty'] != 0
    ]


def _finalize_area(row: dict) -> dict:
    row['defect_trailer_lines'] = _unit_lines(row.pop('defect_trailer_units'))
    row['defect_part_lines'] = _item_lines(row.pop('defect_part_items'))
    row['defect_other_lines'] = _unit_lines(row.pop('defect_other_units'))
    row['good_part_lines'] = _item_lines(row.pop('good_part_items'))
    return row


def build_shift_director_report(
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict:
    """Собрать агрегаты. Ничего не пишет в сессию."""
    posted_shifts = _apply_period(
        ProductionShift.query.filter(ProductionShift.status == 'posted'),
        ProductionShift.posted_at,
        period_start,
        period_end,
    ).all()
    posted_ids = {shift.id for shift in posted_shifts}
    outputs = []
    if posted_ids:
        outputs = (
            ProductionShiftOutput.query
            .filter(
                ProductionShiftOutput.shift_id.in_(posted_ids),
                ProductionShiftOutput.status == 'posted',
            )
            .all()
        )
    output_ids = [output.id for output in outputs]
    units = []
    if output_ids:
        units = ProducedUnit.query.filter(ProducedUnit.shift_output_id.in_(output_ids)).all()
    units_by_output: dict[int, list] = {}
    for unit in units:
        units_by_output.setdefault(unit.shift_output_id, []).append(unit)

    shifts_by_id = {shift.id: shift for shift in posted_shifts}
    areas: dict[tuple[str, str], dict] = {}

    def area_bucket(shift: ProductionShift) -> dict:
        code = _zone_code(shift)
        name = shift.direction_area.name if shift.direction_area and shift.direction_area.name else code
        key = (shift.work_area or '', code)
        if key not in areas:
            areas[key] = _empty_area(shift.work_area or '', code, name)
        return areas[key]

    for shift in posted_shifts:
        bucket = area_bucket(shift)
        bucket['shift_count'] += 1
        bucket['hours'] += _d(shift.hours_fact)

    good_trailers = 0
    cohort_unit_ids: set[int] = set()
    good_trailer_declared = Decimal('0')
    good_part_items: dict[tuple, dict] = {}
    defect_trailer_units: dict[str, Decimal] = {}
    defect_part_items: dict[tuple, dict] = {}
    defect_other_units: dict[str, Decimal] = {}
    unexpected_component_units = 0

    for output in outputs:
        shift = shifts_by_id.get(output.shift_id)
        if not shift:
            continue
        bucket = area_bucket(shift)
        defect = _d(output.defect_quantity)
        item = output.item
        item_type = item.item_type if item else None
        measure_unit = _measure_unit(output.unit, item.unit if item else None)
        if item_type == 'TRAILER':
            _add_unit_qty(defect_trailer_units, measure_unit, defect)
            _add_unit_qty(bucket['defect_trailer_units'], measure_unit, defect)
        elif item_type == 'COMPONENT':
            _add_item_qty(defect_part_items, item, output.item_id, measure_unit, defect)
            _add_item_qty(bucket['defect_part_items'], item, output.item_id, measure_unit, defect)
        else:
            _add_unit_qty(defect_other_units, measure_unit, defect)
            _add_unit_qty(bucket['defect_other_units'], measure_unit, defect)
        linked_units = units_by_output.get(output.id, [])
        if item_type == 'TRAILER':
            declared = _d(output.quantity)
            bucket['good_trailer_declared_qty'] += declared
            good_trailer_declared += declared
            bucket['good_trailers'] += len(linked_units)
            good_trailers += len(linked_units)
            for unit in linked_units:
                cohort_unit_ids.add(unit.id)
        elif item_type == 'COMPONENT':
            good = _d(output.quantity)
            _add_item_qty(good_part_items, item, output.item_id, measure_unit, good)
            _add_item_qty(bucket['good_part_items'], item, output.item_id, measure_unit, good)
            unexpected_component_units += len(linked_units)
        else:
            unexpected_component_units += len(linked_units)

    materials = []
    if posted_ids:
        materials = (
            ProductionShiftMaterial.query
            .filter(
                ProductionShiftMaterial.shift_id.in_(posted_ids),
                ProductionShiftMaterial.status == 'posted',
            )
            .all()
        )

    # Расход только по (зона, номенклатура, единица). Суммы зоны нет:
    # 8 кг и 5 шт нельзя сложить в одно число.
    material_groups: dict[tuple[str, int, str], dict] = {}
    for material in materials:
        shift = shifts_by_id.get(material.shift_id)
        if not shift:
            continue
        code = _zone_code(shift)
        fact = _d(material.qty_fact)
        issued = _d(material.qty_issued)
        shortage = _d(material.qty_shortage)
        item = material.item
        unit = _measure_unit(material.unit, item.unit if item else None)
        group_key = (code, material.item_id, unit)
        group = material_groups.get(group_key)
        if not group:
            group = {
                'direction_code': code,
                'item_id': material.item_id,
                'item_article': item.article if item else '',
                'item_name': item.name if item else '',
                'unit': unit,
                'qty_fact': Decimal('0'),
                'qty_issued': Decimal('0'),
                'qty_shortage': Decimal('0'),
                'line_count': 0,
            }
            material_groups[group_key] = group
        group['qty_fact'] += fact
        group['qty_issued'] += issued
        group['qty_shortage'] += shortage
        group['line_count'] += 1

    legacy_unit_moment = func.coalesce(ProducedUnit.produced_at, ProducedUnit.created_at)
    legacy_units = _apply_period(
        ProducedUnit.query.filter(ProducedUnit.shift_output_id.is_(None)),
        legacy_unit_moment,
        period_start,
        period_end,
    ).all()
    overlap_units = 0
    seen_overlap_units: set[int] = set()
    legacy_issue_groups: dict[tuple[int, str], dict] = {}
    plus_one_issues = _apply_period(
        InventoryOperation.query.filter(
            InventoryOperation.operation_type == 'production_issue',
            InventoryOperation.status == 'posted',
            InventoryOperation.shift_material_id.is_(None),
            InventoryOperation.produced_unit_id.isnot(None),
        ),
        func.coalesce(InventoryOperation.posted_at, InventoryOperation.created_at),
        period_start,
        period_end,
    ).all()
    for operation in plus_one_issues:
        out_lines = []
        for line in operation.lines.all():
            if (line.direction or 'out') != 'out':
                continue
            out_lines.append((line, _d(line.quantity)))
        produced = operation.produced_unit
        if produced is not None and produced.shift_output_id:
            if produced.id not in seen_overlap_units:
                seen_overlap_units.add(produced.id)
                overlap_units += 1
            continue
        for line, line_qty in out_lines:
            item = line.item
            unit = _measure_unit(line.unit, item.unit if item else None)
            group_key = (line.item_id, unit)
            group = legacy_issue_groups.get(group_key)
            if not group:
                group = {
                    'item_id': line.item_id,
                    'item_article': item.article if item else '',
                    'item_name': item.name if item else '',
                    'unit': unit,
                    'quantity': Decimal('0'),
                    'line_count': 0,
                }
                legacy_issue_groups[group_key] = group
            group['quantity'] += line_qty
            group['line_count'] += 1

    excluded_closed = _apply_period(
        ProductionShift.query.filter(ProductionShift.status == 'closed'),
        func.coalesce(ProductionShift.ended_at, ProductionShift.started_at),
        period_start,
        period_end,
    ).count()
    excluded_open = _apply_period(
        ProductionShift.query.filter(ProductionShift.status == 'open'),
        ProductionShift.started_at,
        period_start,
        period_end,
    ).count()

    area_rows = [
        _finalize_area(row)
        for row in sorted(
            areas.values(),
            key=lambda row: (row['direction_code'], row['work_area']),
        )
    ]
    material_rows = sorted(
        material_groups.values(),
        key=lambda row: (
            row['direction_code'],
            row['item_article'] or '',
            row['unit'] or '',
            row['item_id'] or 0,
        ),
    )
    legacy_issue_rows = sorted(
        legacy_issue_groups.values(),
        key=lambda row: (row['item_article'] or '', row['unit'] or '', row['item_id'] or 0),
    )
    return {
        'sold_from_produced': _sold_from_produced_snapshot(cohort_unit_ids),
        'totals': {
            'good_trailers': good_trailers,
            'good_trailer_declared_qty': good_trailer_declared,
            'good_trailer_unit_gap': good_trailer_declared - Decimal(good_trailers),
            'good_part_lines': _item_lines(good_part_items),
            'defect_trailer_lines': _unit_lines(defect_trailer_units),
            'defect_part_lines': _item_lines(defect_part_items),
            'defect_other_lines': _unit_lines(defect_other_units),
            'hours': sum((row['hours'] for row in area_rows), Decimal('0')),
            'legacy_plus_one_trailers': len(legacy_units),
            'overlap_units': overlap_units,
            'unexpected_component_units': unexpected_component_units,
            'excluded_closed_shifts': excluded_closed,
            'excluded_open_shifts': excluded_open,
        },
        'area_rows': area_rows,
        'material_rows': material_rows,
        'legacy_issue_rows': legacy_issue_rows,
    }
