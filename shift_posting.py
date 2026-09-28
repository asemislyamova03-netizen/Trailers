"""Атомарное проведение производственной смены (этап 1). Без commit — его делает вызывающий."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import joinedload

from extensions import db
from inventory_service import (
    apply_inventory_transfer,
    apply_shift_component_receipt,
    apply_shift_material_issue_to_zero,
    record_shift_material_shortage,
    resolve_material_warehouse_id,
)
from inventory_units import normalize_unit
from models import (
    InventoryBalance,
    InventoryOperation,
    Item,
    ProducedUnit,
    ProductionRequest,
    ProductionRequestLine,
    ProductionShift,
    ProductionShiftMaterial,
    ProductionShiftOutput,
    SupplyNeed,
    Warehouse,
    WarehouseStorageArea,
)

DIRECTION_CATEGORIES = ('light_trailer', 'cargo_trailer')
DIRECTION_AREA_SPECS = (
    ('DIR_LIGHT', 'Легковые ТМЦ', 'light_trailer', 210),
    ('DIR_CARGO', 'Грузовые ТМЦ', 'cargo_trailer', 220),
)
MODE_LEGACY = 'legacy_plus_one'
MODE_SHIFT_ONLY = 'shift_only'


class ShiftPostingError(ValueError):
    pass


class ShiftAlreadyPosted(Exception):
    """Повторное проведение уже posted-смены — без новых движений."""


def is_direction_area(area: WarehouseStorageArea | None, *, require_active: bool = True) -> bool:
    if not area:
        return False
    if require_active and not area.is_active:
        return False
    return (area.product_category or '') in DIRECTION_CATEGORIES


def ensure_direction_areas_for_warehouse(warehouse: Warehouse) -> list[WarehouseStorageArea]:
    """Создаёт пустые зоны направления. Не переносит существующий остаток."""
    if not warehouse or not warehouse.is_production:
        return []
    areas = []
    for code, name, category, sort_order in DIRECTION_AREA_SPECS:
        area = WarehouseStorageArea.query.filter_by(warehouse_id=warehouse.id, code=code).first()
        if not area:
            area = WarehouseStorageArea(
                warehouse_id=warehouse.id,
                code=code,
                name=name,
                area_type='direction_stock',
                is_active=True,
                sort_order=sort_order,
                product_category=category,
                shopfloor_posting_mode=MODE_LEGACY,
            )
            db.session.add(area)
            db.session.flush()
        else:
            if not area.product_category:
                area.product_category = category
            if not area.shopfloor_posting_mode:
                area.shopfloor_posting_mode = MODE_LEGACY
        areas.append(area)
    return areas


def list_direction_areas() -> list[WarehouseStorageArea]:
    areas = (
        WarehouseStorageArea.query.join(Warehouse, Warehouse.id == WarehouseStorageArea.warehouse_id)
        .filter(
            WarehouseStorageArea.is_active == True,
            Warehouse.is_active == True,
            Warehouse.is_production == True,
            WarehouseStorageArea.product_category.in_(DIRECTION_CATEGORIES),
        )
        .order_by(Warehouse.name.asc(), WarehouseStorageArea.sort_order.asc())
        .all()
    )
    if areas:
        return areas
    factories = Warehouse.query.filter_by(is_active=True, is_production=True).order_by(Warehouse.id.asc()).all()
    created: list[WarehouseStorageArea] = []
    for factory in factories:
        created.extend(ensure_direction_areas_for_warehouse(factory))
    return created


def require_direction_area(area_id: int | None) -> WarehouseStorageArea:
    if not area_id:
        raise ShiftPostingError(
            'Выберите направление учёта (легковые или грузовые) внутри производственного склада. '
            'Это зона остатка, не отдельный завод.'
        )
    area = WarehouseStorageArea.query.get(area_id)
    if not is_direction_area(area, require_active=False):
        raise ShiftPostingError(
            'Направление должно быть зоной DIR_LIGHT или DIR_CARGO на производственном складе. '
            'Не угадываю склад по имени.'
        )
    warehouse = area.warehouse
    if not warehouse or not warehouse.is_production:
        raise ShiftPostingError('Зона направления должна принадлежать производственному складу.')
    if not area.is_active:
        raise ShiftPostingError(
            'Зона направления неактивна. Провести смену нельзя, в том числе с подтверждением admin.'
        )
    if area_shopfloor_mode(area) != MODE_SHIFT_ONLY:
        raise ShiftPostingError(
            'Учёт направления ещё не открыт. Смена с этой зоны недоступна, в том числе подтверждение '
            'недостачи admin. Сначала инвентаризация, перенос подписанных количеств и открытие учёта. '
            'Подтверждение недостачи не заменяет начальную инвентаризацию.'
        )
    return area


def area_shopfloor_mode(area: WarehouseStorageArea | None) -> str:
    if not area:
        return MODE_LEGACY
    mode = (area.shopfloor_posting_mode or MODE_LEGACY).strip()
    return MODE_SHIFT_ONLY if mode == MODE_SHIFT_ONLY else MODE_LEGACY


def accounting_is_open(area: WarehouseStorageArea | None) -> bool:
    return bool(area) and bool(area.is_active) and area_shopfloor_mode(area) == MODE_SHIFT_ONLY


def line_direction_code(line: ProductionRequestLine) -> str | None:
    item = line.item
    category = getattr(item, 'product_category', None) if item else None
    code = getattr(category, 'code', None) if category else None
    if code in DIRECTION_CATEGORIES:
        return code
    finished = line.production_request.target_warehouse if line.production_request else None
    finished_code = (finished.primary_product_category or '') if finished else ''
    if finished_code in DIRECTION_CATEGORIES:
        return finished_code
    return None


def factory_warehouse_for_line(line: ProductionRequestLine) -> Warehouse | None:
    if line.assembly_warehouse_id:
        assembly = Warehouse.query.get(line.assembly_warehouse_id)
        if assembly and assembly.is_production:
            return assembly
    finished_id = line.production_request.target_warehouse_id if line.production_request else None
    material_id = resolve_material_warehouse_id(finished_id)
    if material_id:
        material = Warehouse.query.get(material_id)
        if material and material.is_production:
            return material
    return Warehouse.query.filter_by(is_active=True, is_production=True).order_by(Warehouse.id.asc()).first()


def resolve_line_direction_area(line: ProductionRequestLine) -> WarehouseStorageArea | None:
    code = line_direction_code(line)
    if not code:
        return None
    factory = factory_warehouse_for_line(line)
    if not factory:
        return None
    return (
        WarehouseStorageArea.query.filter_by(
            warehouse_id=factory.id,
            product_category=code,
            is_active=True,
        )
        .order_by(WarehouseStorageArea.id.asc())
        .first()
    )


def plus_one_blocked_reason(line: ProductionRequestLine) -> str | None:
    area = resolve_line_direction_area(line)
    if area_shopfloor_mode(area) == MODE_SHIFT_ONLY:
        return 'Выпуск только через смену. Учёт направления открыт.'
    posted_output = (
        ProductionShiftOutput.query.join(
            ProductionShift, ProductionShift.id == ProductionShiftOutput.shift_id
        )
        .filter(
            ProductionShiftOutput.production_request_line_id == line.id,
            ProductionShift.status == 'posted',
        )
        .first()
    )
    if posted_output:
        return 'По этой заявке уже есть выпуск через проведённую смену.'
    return None


def has_posted_shifts_for_area(area_id: int) -> bool:
    return (
        ProductionShift.query.filter_by(direction_area_id=area_id, status='posted').first()
        is not None
    )


def set_direction_shopfloor_mode(area: WarehouseStorageArea, mode: str) -> None:
    if mode not in (MODE_LEGACY, MODE_SHIFT_ONLY):
        raise ShiftPostingError('Неизвестный режим проведения цеха.')
    if not is_direction_area(area):
        raise ShiftPostingError(
            'Режим смены задаётся на зоне направления (легковые/грузовые) внутри завода.'
        )
    if area_shopfloor_mode(area) == mode:
        return
    if mode == MODE_LEGACY and area_shopfloor_mode(area) == MODE_SHIFT_ONLY:
        if has_posted_shifts_for_area(area.id):
            raise ShiftPostingError(
                'Нельзя вернуть +1: по этому направлению уже есть проведённые смены.'
            )
    area.shopfloor_posting_mode = mode


def transfer_signed_qty_to_direction(
    *,
    warehouse_id: int,
    direction_area_id: int,
    lines: list[tuple],
    created_by_user_id: int | None,
    from_storage_area_id: int | None = None,
    comment: str | None = None,
):
    """Перенос из общего остатка в зону направления. Только подписанные количества, без авто-разбиения."""
    area = WarehouseStorageArea.query.get(direction_area_id)
    if not is_direction_area(area, require_active=False):
        raise ShiftPostingError(
            'Перенос только в зону DIR_LIGHT или DIR_CARGO. Не угадываю направление.'
        )
    if area.warehouse_id != warehouse_id:
        raise ShiftPostingError('Зона направления не принадлежит этому производственному складу.')
    if from_storage_area_id:
        source = WarehouseStorageArea.query.get(from_storage_area_id)
        if is_direction_area(source, require_active=False):
            raise ShiftPostingError(
                'Перенос в зону направления — только из общего остатка, не из другой зоны направления.'
            )
    signed: list[tuple] = []
    for row in lines or []:
        item_id = row[0]
        qty = Decimal(str(row[1] if len(row) > 1 else 0))
        if qty < 0:
            raise ShiftPostingError('Подписанное количество не может быть отрицательным.')
        if qty == 0:
            continue
        comment_line = row[2] if len(row) > 2 else None
        unit = row[3] if len(row) > 3 else None
        signed.append((item_id, qty, comment_line, unit))
    if not signed:
        raise ShiftPostingError(
            'Нужны подписанные количества для переноса. Автоматически общий остаток не распределяю.'
        )
    item_ids = [row[0] for row in signed]
    before = {
        item_id: warehouse_item_qty(warehouse_id, item_id)
        for item_id in item_ids
    }
    operation = apply_inventory_transfer(
        from_warehouse_id=warehouse_id,
        from_storage_area_id=from_storage_area_id,
        to_warehouse_id=warehouse_id,
        to_storage_area_id=area.id,
        lines=signed,
        created_by_user_id=created_by_user_id,
        comment=comment or 'Перенос подписанных количеств в зону направления',
        general_stock_only=from_storage_area_id is None,
    )
    for item_id in item_ids:
        after = warehouse_item_qty(warehouse_id, item_id)
        if after != before[item_id]:
            raise ShiftPostingError(
                f'Перенос должен сохранять общий остаток номенклатуры #{item_id}: было {before[item_id]}, стало {after}.'
            )
    return operation


def sales_warehouse_id_for_direction(direction_code: str, preferred_id: int | None) -> int | None:
    if preferred_id:
        preferred = Warehouse.query.get(preferred_id)
        if preferred and not preferred.is_production:
            return preferred.id
    matches = (
        Warehouse.query.filter(
            Warehouse.is_active == True,
            Warehouse.is_production == False,
            Warehouse.primary_product_category == direction_code,
        )
        .order_by(Warehouse.id.asc())
        .all()
    )
    if len(matches) == 1:
        return matches[0].id
    return preferred_id


def _as_decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _component_good_qty(output: ProductionShiftOutput) -> Decimal:
    """COMPONENT good qty as Decimal — без int()-усечения."""
    qty = _as_decimal(output.quantity)
    if qty < 0:
        raise ShiftPostingError('Количество выпуска комплектующих не может быть отрицательным.')
    return qty


def _trailer_unit_count(output: ProductionShiftOutput) -> int:
    """TRAILER: только целое число единиц. Дробное (2.5) — ошибка, без усечения."""
    qty = _as_decimal(output.quantity)
    if qty < 0:
        raise ShiftPostingError('Количество прицепов не может быть отрицательным.')
    if qty != qty.to_integral_value():
        raise ShiftPostingError(
            f'Количество прицепов должно быть целым числом, получено {qty}. '
            'Дробное количество не усекается.'
        )
    return int(qty)


def _next_request_number() -> str:
    last = db.session.query(ProductionRequest).order_by(ProductionRequest.id.desc()).first()
    next_id = (last.id + 1) if last else 1
    return f'PR-{next_id:06d}'


def _ensure_replenishment_line(
    *,
    output: ProductionShiftOutput,
    shift: ProductionShift,
    factory: Warehouse,
    sales_warehouse_id: int | None,
    item: Item,
    quantity: int,
) -> ProductionRequestLine:
    if output.replenishment_request_line_id:
        existing = ProductionRequestLine.query.get(output.replenishment_request_line_id)
        if existing:
            return existing
    target_id = sales_warehouse_id or factory.id
    need = SupplyNeed(
        need_type='STOCK_REPLENISHMENT',
        status='IN_PRODUCTION',
        item_id=item.id,
        warehouse_id=target_id,
        production_workshop_id=shift.workshop_id,
        quantity=quantity,
        article_snapshot=item.article,
        product_name_snapshot=item.name,
        note=f'Сверх плана, смена #{shift.id}, строка выпуска #{output.id}',
    )
    db.session.add(need)
    db.session.flush()
    pr = ProductionRequest(
        request_number=_next_request_number(),
        status='in_progress',
        target_warehouse_id=target_id,
        note=f'Пополнение из смены #{shift.id}',
    )
    db.session.add(pr)
    db.session.flush()
    line = ProductionRequestLine(
        production_request_id=pr.id,
        supply_need_id=need.id,
        item_id=item.id,
        production_workshop_id=shift.workshop_id,
        assembly_warehouse_id=factory.id,
        quantity=quantity,
        produced_qty=0,
        status='in_production',
        article_snapshot=item.article,
        product_name_snapshot=item.name,
        note=f'shift_output:{output.id}',
    )
    db.session.add(line)
    db.session.flush()
    output.replenishment_request_line_id = line.id
    return line


def _touch_request_line(line: ProductionRequestLine, add_qty: int) -> None:
    line.produced_qty = (line.produced_qty or 0) + add_qty
    line.produced_at = datetime.utcnow()
    if (line.status or '').lower() in ('planned', 'draft', 'waiting_production', 'planned'):
        line.status = 'in_production'
        line.started_at = line.started_at or datetime.utcnow()
    if line.produced_qty >= (line.quantity or 0):
        line.status = 'ready'
        line.completed_at = datetime.utcnow()
    else:
        line.status = 'partial_ready'
    if line.production_request:
        statuses = {row.status for row in line.production_request.lines.all()}
        if statuses <= {'ready', 'closed'}:
            line.production_request.status = 'ready'
        elif 'partial_ready' in statuses or 'ready' in statuses:
            line.production_request.status = 'partial_ready'
        else:
            line.production_request.status = 'in_progress'
    if line.supply_need:
        line.supply_need.status = 'READY' if line.status == 'ready' else 'IN_PRODUCTION'


def _create_produced_units(
    *,
    output: ProductionShiftOutput,
    shift: ProductionShift,
    warehouse: Warehouse,
    item: Item,
    line: ProductionRequestLine,
    start_seq: int,
    count: int,
) -> int:
    created = 0
    for seq in range(start_seq, start_seq + count):
        existing = ProducedUnit.query.filter_by(shift_output_id=output.id, unit_seq=seq).first()
        if existing:
            continue
        unit = ProducedUnit(
            production_request_line_id=line.id,
            order_line_id=line.order_line_id or (line.supply_need.order_line_id if line.supply_need else None),
            item_id=item.id,
            production_workshop_id=shift.workshop_id or line.production_workshop_id,
            target_warehouse_id=line.production_request.target_warehouse_id if line.production_request else warehouse.id,
            order_id=(line.supply_need.order_id if line.supply_need else None),
            shift_output_id=output.id,
            unit_seq=seq,
            produced_at=datetime.utcnow(),
            status='produced_no_vin',
            note=f'Смена #{shift.id}',
        )
        db.session.add(unit)
        created += 1
    if created:
        db.session.flush()
        _touch_request_line(line, created)
    return created


def _hours_fact(shift: ProductionShift, form_hours) -> Decimal:
    if form_hours not in (None, ''):
        hours = Decimal(str(form_hours))
        if hours < 0:
            raise ShiftPostingError('Часы смены не могут быть отрицательными.')
        return hours
    ended = shift.ended_at or datetime.utcnow()
    seconds = max((ended - shift.started_at).total_seconds(), 0)
    return (Decimal(str(seconds)) / Decimal('3600')).quantize(Decimal('0.01'))


def warehouse_item_qty(warehouse_id: int, item_id: int, storage_area_id: int | None = None) -> Decimal:
    query = InventoryBalance.query.filter_by(warehouse_id=warehouse_id, item_id=item_id)
    if storage_area_id is not None:
        query = query.filter_by(storage_area_id=storage_area_id)
    total = Decimal('0')
    for balance in query.all():
        total += Decimal(balance.quantity or 0)
    return total


def close_shift_shortage(*, material_id: int, user_id: int | None) -> ProductionShiftMaterial:
    material = ProductionShiftMaterial.query.get(material_id)
    if not material:
        raise ShiftPostingError('Строка материала смены не найдена.')
    if _as_decimal(material.qty_shortage) <= 0:
        raise ShiftPostingError('По этой строке нет открытой недостачи.')
    if material.shortage_status == 'closed_by_count':
        return material
    extra_issue = InventoryOperation.query.filter(
        InventoryOperation.shift_material_id == material.id,
        InventoryOperation.operation_type == 'production_issue',
        InventoryOperation.status == 'posted',
        InventoryOperation.comment.ilike('%закрытие недостачи%'),
    ).first()
    if extra_issue:
        raise ShiftPostingError('Закрытие недостачи не должно создавать повторное списание.')
    material.shortage_status = 'closed_by_count'
    material.note = ((material.note or '') + f'\nclosed_by_count user={user_id}').strip()
    db.session.flush()
    return material


def post_shift(
    *,
    shift: ProductionShift,
    direction_area_id: int | None = None,
    direction_warehouse_id: int | None = None,
    hours_fact=None,
    senior_confirmed: bool,
    actor_is_senior: bool,
    created_by_user_id: int | None,
) -> dict:
    """Провести смену одним набором flush. Commit снаружи. Exception → rollback."""
    if shift.status == 'posted':
        raise ShiftAlreadyPosted()
    if shift.status == 'closed':
        raise ShiftPostingError('Архивную закрытую смену нельзя провести этим контуром.')
    if shift.status != 'open':
        raise ShiftPostingError(f'Смену в статусе {shift.status} провести нельзя.')

    area = require_direction_area(direction_area_id or shift.direction_area_id)
    warehouse = area.warehouse
    if direction_warehouse_id and direction_warehouse_id != warehouse.id:
        raise ShiftPostingError('Зона направления не принадлежит выбранному производственному складу.')
    shift.direction_area_id = area.id
    shift.direction_warehouse_id = warehouse.id
    direction_code = area.product_category

    materials = (
        ProductionShiftMaterial.query.options(joinedload(ProductionShiftMaterial.item))
        .filter_by(shift_id=shift.id)
        .order_by(ProductionShiftMaterial.id.asc())
        .all()
    )
    outputs = (
        ProductionShiftOutput.query.options(joinedload(ProductionShiftOutput.item))
        .filter_by(shift_id=shift.id)
        .order_by(ProductionShiftOutput.id.asc())
        .all()
    )
    if not materials and not outputs:
        raise ShiftPostingError('Добавьте выпуск или факт материалов перед проведением смены.')

    # Pre-validate outputs before any issue/receipt (no silent skip).
    for output in outputs:
        item = output.item or (Item.query.get(output.item_id) if output.item_id else None)
        if not item or not output.item_id:
            raise ShiftPostingError(
                f'Строка выпуска #{output.id}: не указана номенклатура. Проведение отклонено.'
            )
        if item.item_type == 'TRAILER':
            category = getattr(item, 'product_category', None)
            item_direction = getattr(category, 'code', None) if category else None
            if item_direction not in DIRECTION_CATEGORIES:
                raise ShiftPostingError(
                    f'Строка выпуска #{output.id}: у прицепа «{item.name}» нет категории направления '
                    f'(light_trailer/cargo_trailer).'
                )
            if item_direction != direction_code:
                raise ShiftPostingError(
                    f'Строка выпуска #{output.id}: прицеп «{item.name}» категории «{item_direction}» '
                    f'не совпадает с направлением смены «{direction_code}».'
                )
            # Whole-number check early so fractional trailer qty never reaches unit creation.
            _trailer_unit_count(output)
        elif item.item_type == 'COMPONENT':
            _component_good_qty(output)
        if output.production_request_line_id:
            line = ProductionRequestLine.query.get(output.production_request_line_id)
            if not line:
                raise ShiftPostingError(
                    f'Строка выпуска #{output.id}: заявка production_request_line_id='
                    f'{output.production_request_line_id} не найдена.'
                )
            if line.item_id != output.item_id:
                raise ShiftPostingError(
                    f'Строка выпуска #{output.id}: номенклатура выпуска не совпадает со строкой заявки '
                    f'(item_id {output.item_id} ≠ {line.item_id}).'
                )
            line_code = line_direction_code(line)
            if line_code != direction_code:
                raise ShiftPostingError(
                    f'Строка выпуска #{output.id}: направление строки заявки «{line_code}» '
                    f'не совпадает с направлением смены «{direction_code}».'
                )

    pending_shortages: list[tuple[ProductionShiftMaterial, Decimal]] = []
    # Sequential remaining-available per (warehouse, area, item) so two lines of the same
    # COMPONENT share one book qty (6+6 against 8 → shortage 4 on the second line).
    remaining_available: dict[tuple[int, int, int], Decimal] = {}
    for material in materials:
        item = material.item or Item.query.get(material.item_id)
        if not item or item.item_type != 'COMPONENT':
            raise ShiftPostingError('Списывать можно только номенклатуру COMPONENT.')
        qty_fact = _as_decimal(material.qty_fact)
        if qty_fact < 0:
            raise ShiftPostingError('Факт расхода не может быть отрицательным.')
        key = (warehouse.id, area.id, item.id)
        if key not in remaining_available:
            remaining_available[key] = warehouse_item_qty(warehouse.id, item.id, area.id)
        available = remaining_available[key]
        shortage = max(qty_fact - available, Decimal('0'))
        remaining_available[key] = max(available - qty_fact, Decimal('0'))
        if shortage > 0:
            pending_shortages.append((material, shortage))

    if pending_shortages and not (senior_confirmed and actor_is_senior):
        names = ', '.join(
            f'«{(row[0].item.name if row[0].item else row[0].item_id)}» нехватка {row[1]}'
            for row in pending_shortages
        )
        raise ShiftPostingError(
            'Факт больше книги. Нужно подтверждение старшего (admin) после открытия учёта зоны. '
            'Это не заменяет начальную инвентаризацию. ' + names
        )

    issued_ops = 0
    shortage_ops = 0
    receipt_ops = 0
    units_created = 0
    replenishment_units = 0

    for material in materials:
        item = material.item
        unit = normalize_unit(material.unit or (item.unit if item else 'шт'))
        qty_fact = _as_decimal(material.qty_fact)
        _op, issued, leftover = apply_shift_material_issue_to_zero(
            warehouse_id=warehouse.id,
            item_id=item.id,
            qty_fact=qty_fact,
            shift_id=shift.id,
            shift_material_id=material.id,
            workshop_id=shift.workshop_id,
            created_by_user_id=created_by_user_id,
            unit=unit,
            storage_area_id=area.id,
        )
        if issued > 0:
            issued_ops += 1
        material.qty_issued = issued
        material.qty_shortage = leftover
        material.status = 'posted'
        if leftover > 0:
            record_shift_material_shortage(
                warehouse_id=warehouse.id,
                item_id=item.id,
                qty_fact=qty_fact,
                qty_issued=issued,
                qty_shortage=leftover,
                shift_id=shift.id,
                shift_material_id=material.id,
                created_by_user_id=created_by_user_id,
                unit=unit,
                storage_area_id=area.id,
            )
            material.shortage_status = 'open'
            shortage_ops += 1
        else:
            material.shortage_status = 'none'
        if warehouse_item_qty(warehouse.id, item.id, area.id) < 0:
            raise ShiftPostingError('Отрицательный остаток запрещён.')

    for output in outputs:
        item = output.item or Item.query.get(output.item_id)
        unit = normalize_unit(output.unit or item.unit)
        if item.item_type == 'COMPONENT':
            good_qty = _component_good_qty(output)
            if good_qty > 0:
                apply_shift_component_receipt(
                    warehouse_id=warehouse.id,
                    item_id=item.id,
                    quantity=good_qty,
                    shift_id=shift.id,
                    shift_output_id=output.id,
                    created_by_user_id=created_by_user_id,
                    unit=unit,
                    storage_area_id=area.id,
                )
                receipt_ops += 1
            output.status = 'posted'
            continue
        if item.item_type != 'TRAILER':
            output.status = 'posted'
            continue
        good_qty = _trailer_unit_count(output)
        remaining_on_line = 0
        customer_line = None
        if output.production_request_line_id:
            customer_line = ProductionRequestLine.query.get(output.production_request_line_id)
            if customer_line:
                remaining_on_line = max((customer_line.quantity or 0) - (customer_line.produced_qty or 0), 0)
        customer_count = min(good_qty, remaining_on_line)
        overplan_count = good_qty - customer_count
        seq = 1
        preferred_sales_id = None
        if customer_line and customer_line.production_request:
            preferred_sales_id = customer_line.production_request.target_warehouse_id
        sales_id = sales_warehouse_id_for_direction(direction_code, preferred_sales_id)
        if customer_count and customer_line:
            units_created += _create_produced_units(
                output=output,
                shift=shift,
                warehouse=warehouse,
                item=item,
                line=customer_line,
                start_seq=seq,
                count=customer_count,
            )
            seq += customer_count
        if overplan_count:
            replenishment_line = _ensure_replenishment_line(
                output=output,
                shift=shift,
                factory=warehouse,
                sales_warehouse_id=sales_id,
                item=item,
                quantity=overplan_count,
            )
            created = _create_produced_units(
                output=output,
                shift=shift,
                warehouse=warehouse,
                item=item,
                line=replenishment_line,
                start_seq=seq,
                count=overplan_count,
            )
            units_created += created
            replenishment_units += created
        output.status = 'posted'

    now = datetime.utcnow()
    shift.ended_at = shift.ended_at or now
    shift.hours_fact = _hours_fact(shift, hours_fact)
    shift.posted_at = now
    shift.posted_by_user_id = created_by_user_id
    shift.senior_shortage_confirmed = bool(senior_confirmed and actor_is_senior and pending_shortages)
    shift.status = 'posted'
    db.session.flush()
    return {
        'issued_ops': issued_ops,
        'shortage_ops': shortage_ops,
        'receipt_ops': receipt_ops,
        'units_created': units_created,
        'replenishment_units': replenishment_units,
        'warehouse_id': warehouse.id,
        'direction_area_id': area.id,
    }
