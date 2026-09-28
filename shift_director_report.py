"""Только чтение: агрегаты директорского отчёта по проведённым сменам.

Не проводит смены, не меняет остатки и не подключается к живому серверу.
«Продано из выпущенных» не считается: прямой связи единицы выпуска с реализацией нет.

Расход на экран — только строки номенклатуры со своей единицей.
material_fact_by_zone и legacy_plus_one_issue_qty не показывать:
это суммы разных Item/unit и они дают ложный итог.
Брак прицепов и брак деталей считаются раздельно, по единицам измерения.
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
)

DIRECTION_ZONE_CODES = ('DIR_LIGHT', 'DIR_CARGO')

SOLD_FROM_PRODUCED = {
    'status': 'BLOCKED',
    'value': None,
    'reason': (
        'Связи единицы выпуска с реализацией нет. '
        'У SalesRealizationLine нет produced_unit_id. '
        'У VinRegistry нет produced_unit_id (колонка снята миграцией e5b8c7d9a012). '
        'ProducedUnit.trailer_id пустой после проведения смены и не уникален в БД. '
        'Совпадение trailer_id со строкой реализации — не документ продажи этой единицы.'
    ),
    'paths': [
        'models.py: SalesRealizationLine — trailer_id, order_line_id, vin_registry_id; produced_unit_id нет',
        'models.py: VinRegistry — produced_unit_id нет',
        'models.py: ProducedUnit.trailer_id — nullable, без unique',
        'shift_posting.py: _create_produced_units — trailer_id не заполняется, статус produced_no_vin',
        'views.py: _attach_produced_unit_to_existing_trailer — trailer_id пишется позже, при VIN',
        'views.py: _posted_realization_for_trailer — продажа ищется по trailer_id, не по ProducedUnit',
    ],
    'proposal': (
        'Минимально, отдельным решением: nullable sales_realization_line.produced_unit_id. '
        'Заполнять при проведении реализации только если у этого trailer ровно один ProducedUnit. '
        'Без backfill и без подстановки числа через trailer_id. Пока колонки нет — показатель не считать.'
    ),
}


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


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


def _empty_zone() -> dict:
    return {
        'qty_fact': Decimal('0'),
        'qty_issued': Decimal('0'),
        'qty_shortage': Decimal('0'),
    }


def _empty_area(work_area: str, direction_code: str, direction_name: str) -> dict:
    return {
        'work_area': work_area,
        'direction_code': direction_code,
        'direction_name': direction_name,
        'shift_count': 0,
        'hours': Decimal('0'),
        'defect_qty': Decimal('0'),
        'defect_trailer_units': {},
        'defect_part_units': {},
        'defect_other_units': {},
        'good_trailers': 0,
        'good_trailer_declared_qty': Decimal('0'),
        'good_parts': Decimal('0'),
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


def _finalize_area(row: dict) -> dict:
    row['defect_trailer_lines'] = _unit_lines(row.pop('defect_trailer_units'))
    row['defect_part_lines'] = _unit_lines(row.pop('defect_part_units'))
    row['defect_other_lines'] = _unit_lines(row.pop('defect_other_units'))
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
    good_trailer_declared = Decimal('0')
    good_parts = Decimal('0')
    defect_qty = Decimal('0')
    defect_trailer_units: dict[str, Decimal] = {}
    defect_part_units: dict[str, Decimal] = {}
    defect_other_units: dict[str, Decimal] = {}
    unexpected_component_units = 0

    for output in outputs:
        shift = shifts_by_id.get(output.shift_id)
        if not shift:
            continue
        bucket = area_bucket(shift)
        defect = _d(output.defect_quantity)
        bucket['defect_qty'] += defect
        defect_qty += defect
        item = output.item
        item_type = item.item_type if item else None
        defect_unit = _measure_unit(output.unit, item.unit if item else None)
        if item_type == 'TRAILER':
            defect_store = defect_trailer_units
            area_store = bucket['defect_trailer_units']
        elif item_type == 'COMPONENT':
            defect_store = defect_part_units
            area_store = bucket['defect_part_units']
        else:
            defect_store = defect_other_units
            area_store = bucket['defect_other_units']
        _add_unit_qty(defect_store, defect_unit, defect)
        _add_unit_qty(area_store, defect_unit, defect)
        linked_units = units_by_output.get(output.id, [])
        if item_type == 'TRAILER':
            declared = _d(output.quantity)
            bucket['good_trailer_declared_qty'] += declared
            good_trailer_declared += declared
            bucket['good_trailers'] += len(linked_units)
            good_trailers += len(linked_units)
        elif item_type == 'COMPONENT':
            good = _d(output.quantity)
            bucket['good_parts'] += good
            good_parts += good
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

    # Суммы зоны остаются для проверок одной номенклатуры.
    # На экран их не выводить: разные Item и единицы дают ложный итог.
    material_fact_by_zone = {code: _empty_zone() for code in DIRECTION_ZONE_CODES}
    other_zone_fact = _empty_zone()
    material_groups: dict[tuple[str, int, str], dict] = {}
    for material in materials:
        shift = shifts_by_id.get(material.shift_id)
        if not shift:
            continue
        code = _zone_code(shift)
        target = material_fact_by_zone.get(code)
        if target is None:
            target = other_zone_fact
        fact = _d(material.qty_fact)
        issued = _d(material.qty_issued)
        shortage = _d(material.qty_shortage)
        target['qty_fact'] += fact
        target['qty_issued'] += issued
        target['qty_shortage'] += shortage
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
    legacy_issue_qty = Decimal('0')
    overlap_units = 0
    overlap_issue_qty = Decimal('0')
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
        qty = Decimal('0')
        for line in operation.lines.all():
            if (line.direction or 'out') != 'out':
                continue
            line_qty = _d(line.quantity)
            qty += line_qty
            out_lines.append((line, line_qty))
        produced = operation.produced_unit
        if produced is not None and produced.shift_output_id:
            overlap_issue_qty += qty
            if produced.id not in seen_overlap_units:
                seen_overlap_units.add(produced.id)
                overlap_units += 1
            continue
        legacy_issue_qty += qty
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
        'sold_from_produced': dict(SOLD_FROM_PRODUCED),
        'totals': {
            'good_trailers': good_trailers,
            'good_trailer_declared_qty': good_trailer_declared,
            'good_trailer_unit_gap': good_trailer_declared - Decimal(good_trailers),
            'good_parts': good_parts,
            'defect_qty': defect_qty,
            'defect_trailer_lines': _unit_lines(defect_trailer_units),
            'defect_part_lines': _unit_lines(defect_part_units),
            'defect_other_lines': _unit_lines(defect_other_units),
            'hours': sum((row['hours'] for row in area_rows), Decimal('0')),
            'legacy_plus_one_trailers': len(legacy_units),
            'legacy_plus_one_issue_qty': legacy_issue_qty,
            'overlap_units': overlap_units,
            'overlap_issue_qty': overlap_issue_qty,
            'unexpected_component_units': unexpected_component_units,
            'excluded_closed_shifts': excluded_closed,
            'excluded_open_shifts': excluded_open,
        },
        'area_rows': area_rows,
        'material_fact_by_zone': material_fact_by_zone,
        'other_zone_fact': other_zone_fact,
        'material_rows': material_rows,
        'legacy_issue_rows': legacy_issue_rows,
    }
