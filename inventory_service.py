"""Складской учёт комплектующих и материалов (ТМЦ) через InventoryBalance / InventoryOperation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_

from inventory_units import format_inventory_quantity, normalize_unit, units_match
from models import (
    InventoryBalance,
    InventoryOperation,
    InventoryOperationLine,
    Item,
    ItemBillOfMaterials,
    ItemBillOfMaterialsLine,
    ProductionRequestLine,
    Warehouse,
    WarehouseStorageArea,
    db,
)

# Физические зоны склада (участок), не типы номенклатуры.
DEFAULT_STORAGE_AREAS: list[tuple[str, str, str, int]] = [
    ('MAIN', 'Основная зона', 'storage', 10),
    ('METAL', 'Участок металла', 'workshop', 20),
    ('ASSEMBLY', 'Сборочный участок', 'workshop', 30),
]

TMZ_ITEM_TYPES: tuple[str, ...] = ('COMPONENT',)

TMZ_GROUP_LABELS: dict[str, str] = {
    'component': 'Комплектующие',
    'semi_finished': 'Полуфабрикаты / заготовки',
    'metal': 'Металл и лист',
    'other': 'Прочее ТМЦ',
}


class InsufficientInventoryError(ValueError):
    def __init__(self, shortages: list[dict]):
        self.shortages = shortages
        parts = [
            f'«{row["name"]}»: нужно {row["required"]} {row["unit"]}, на складе {row["available"]}'
            for row in shortages
        ]
        super().__init__('Недостаточно материалов для выпуска. ' + '; '.join(parts))

ReceiptLine = tuple[int, Decimal | float | int, str | None, str | None]


def ensure_storage_areas_for_warehouse(warehouse_id: int) -> list[WarehouseStorageArea]:
    warehouse = Warehouse.query.get(warehouse_id)
    if not warehouse:
        return []
    existing = WarehouseStorageArea.query.filter_by(warehouse_id=warehouse_id, is_active=True).count()
    if existing:
        return (
            WarehouseStorageArea.query.filter_by(warehouse_id=warehouse_id, is_active=True)
            .order_by(WarehouseStorageArea.sort_order.asc(), WarehouseStorageArea.id.asc())
            .all()
        )
    for code, name, area_type, sort_order in DEFAULT_STORAGE_AREAS:
        db.session.add(
            WarehouseStorageArea(
                warehouse_id=warehouse_id,
                code=code,
                name=name,
                area_type=area_type,
                sort_order=sort_order,
                is_active=True,
            )
        )
    db.session.flush()
    return (
        WarehouseStorageArea.query.filter_by(warehouse_id=warehouse_id, is_active=True)
        .order_by(WarehouseStorageArea.sort_order.asc(), WarehouseStorageArea.id.asc())
        .all()
    )


def _existing_balance(
    warehouse_id: int,
    item_id: int,
    storage_area_id: int | None,
) -> InventoryBalance | None:
    return InventoryBalance.query.filter_by(
        warehouse_id=warehouse_id,
        storage_area_id=storage_area_id,
        item_id=item_id,
    ).first()


def get_or_create_balance(
    warehouse_id: int,
    item_id: int,
    storage_area_id: int | None = None,
    *,
    unit: str | None = None,
) -> InventoryBalance:
    balance = _existing_balance(warehouse_id, item_id, storage_area_id)
    if balance:
        return balance
    item = Item.query.get(item_id)
    balance_unit = normalize_unit(unit or (item.unit if item else None))
    balance = InventoryBalance(
        warehouse_id=warehouse_id,
        storage_area_id=storage_area_id,
        item_id=item_id,
        quantity=Decimal('0'),
        reserved_quantity=Decimal('0'),
        unit=balance_unit,
    )
    db.session.add(balance)
    db.session.flush()
    return balance


def apply_inventory_receipt(
    *,
    warehouse_id: int,
    storage_area_id: int | None,
    lines: list[ReceiptLine],
    created_by_user_id: int | None,
    document_ref: str | None = None,
    comment: str | None = None,
) -> InventoryOperation:
    if not lines:
        raise ValueError('Добавьте хотя бы одну позицию прихода.')
    operation = InventoryOperation(
        operation_type='receipt',
        status='posted',
        target_warehouse_id=warehouse_id,
        target_area_id=storage_area_id,
        created_by_user_id=created_by_user_id,
        posted_at=datetime.utcnow(),
        comment=' '.join(part for part in [document_ref, comment] if part) or None,
    )
    db.session.add(operation)
    db.session.flush()
    posted_lines = 0
    for item_id, quantity, line_comment, line_unit in lines:
        qty = Decimal(str(quantity))
        if qty <= 0:
            continue
        item = Item.query.get(item_id)
        if not item:
            raise ValueError(f'Номенклатура #{item_id} не найдена.')
        item_unit = normalize_unit(item.unit)
        unit = normalize_unit(line_unit or item.unit)
        if not units_match(unit, item_unit):
            raise ValueError(
                f'«{item.name}»: в номенклатуре учёт в {item_unit}, в приходе указано {unit}. '
                f'Исправьте единицу в карточке номенклатуры или строке прихода.'
            )
        balance = _existing_balance(warehouse_id, item_id, storage_area_id)
        if balance and Decimal(balance.quantity or 0) > 0 and not units_match(balance.unit, unit):
            raise ValueError(
                f'«{item.name}»: остаток на складе ведётся в {normalize_unit(balance.unit)}, '
                f'приход в {unit} невозможен.'
            )
        if not balance:
            balance = get_or_create_balance(warehouse_id, item_id, storage_area_id, unit=unit)
        elif not units_match(balance.unit, unit):
            balance.unit = unit
        db.session.add(
            InventoryOperationLine(
                operation_id=operation.id,
                item_id=item_id,
                quantity=qty,
                unit=unit,
                direction='in',
                comment=line_comment,
            )
        )
        balance.quantity = Decimal(balance.quantity or 0) + qty
        balance.unit = unit
        balance.updated_at = datetime.utcnow()
        if not units_match(item.unit, unit):
            item.unit = unit
        posted_lines += 1
    if posted_lines == 0:
        raise ValueError('Добавьте хотя бы одну позицию с количеством больше нуля.')
    db.session.flush()
    return operation


def tmz_group_code(item: Item | None) -> str:
    if not item:
        return 'other'
    if item.is_internal_bom_item:
        return 'semi_finished'
    name_lower = (item.name or '').lower()
    article_lower = (item.article or '').lower()
    if any(token in name_lower or token in article_lower for token in ('лист', 'профиль', 'труба', 'металл', 'сталь')):
        return 'metal'
    return 'component'


def tmz_group_label(code: str) -> str:
    return TMZ_GROUP_LABELS.get(code, TMZ_GROUP_LABELS['other'])


def group_inventory_balances(balances: list[InventoryBalance]) -> list[dict]:
    grouped: dict[str, list[InventoryBalance]] = {}
    order = ['metal', 'semi_finished', 'component', 'other']
    for balance in balances:
        code = tmz_group_code(balance.item)
        grouped.setdefault(code, []).append(balance)
    return [
        {'code': code, 'label': tmz_group_label(code), 'rows': grouped[code]}
        for code in order
        if code in grouped
    ]


def inventory_balances_query(
    warehouse_id: int | None = None,
    storage_area_id: int | None = None,
    item_type: str | None = None,
    only_positive: bool = False,
    *,
    tmz_only: bool = True,
):
    query = (
        InventoryBalance.query.join(Item, Item.id == InventoryBalance.item_id)
        .outerjoin(WarehouseStorageArea, WarehouseStorageArea.id == InventoryBalance.storage_area_id)
        .filter(Item.is_active == True)
    )
    if tmz_only:
        query = query.filter(Item.item_type.in_(TMZ_ITEM_TYPES))
    if warehouse_id:
        query = query.filter(InventoryBalance.warehouse_id == warehouse_id)
    if storage_area_id:
        query = query.filter(InventoryBalance.storage_area_id == storage_area_id)
    if item_type:
        query = query.filter(Item.item_type == item_type)
    if only_positive:
        query = query.filter(InventoryBalance.quantity > 0)
    return query.order_by(
        WarehouseStorageArea.sort_order.asc(),
        Item.name.asc(),
    )


def resolve_material_warehouse_id(finished_warehouse_id: int | None) -> int | None:
    """Склад ТМЦ (производство) для филиала, куда отгружается готовый прицеп."""
    production_warehouses = (
        Warehouse.query.filter(
            Warehouse.is_active == True,
            or_(Warehouse.is_production == True, Warehouse.warehouse_kind.in_(('production', 'assembly', 'raw_materials'))),
        )
        .order_by(Warehouse.id.asc())
        .all()
    )
    if not production_warehouses:
        return None
    if finished_warehouse_id:
        finished = Warehouse.query.get(finished_warehouse_id)
        if finished and (finished.is_production or finished.warehouse_kind in ('production', 'assembly', 'raw_materials')):
            return finished.id
        if finished:
            tokens = [part.lower() for part in finished.name.replace('—', ' ').split() if len(part) > 2]
            for candidate in production_warehouses:
                name_lower = candidate.name.lower()
                if any(token in name_lower for token in tokens):
                    return candidate.id
    return production_warehouses[0].id


def get_active_bom(item_id: int) -> ItemBillOfMaterials | None:
    return (
        ItemBillOfMaterials.query.filter_by(item_id=item_id, is_active=True)
        .order_by(ItemBillOfMaterials.id.desc())
        .first()
    )


def _available_quantity(balance: InventoryBalance) -> Decimal:
    return Decimal(balance.quantity or 0) - Decimal(balance.reserved_quantity or 0)


def _available_on_warehouse(warehouse_id: int, item_id: int) -> Decimal:
    total = Decimal('0')
    for balance in InventoryBalance.query.filter_by(warehouse_id=warehouse_id, item_id=item_id).all():
        if Decimal(balance.quantity or 0) <= 0:
            continue
        total += _available_quantity(balance)
    return total


def _allocate_issue_from_balances(
    warehouse_id: int,
    item_id: int,
    qty_needed: Decimal,
) -> list[tuple[InventoryBalance, Decimal]]:
    if qty_needed <= 0:
        return []
    balances = (
        InventoryBalance.query.filter_by(warehouse_id=warehouse_id, item_id=item_id)
        .order_by(InventoryBalance.storage_area_id.asc(), InventoryBalance.id.asc())
        .all()
    )
    allocations: list[tuple[InventoryBalance, Decimal]] = []
    left = qty_needed
    for balance in balances:
        available = _available_quantity(balance)
        if available <= 0:
            continue
        take = min(available, left)
        allocations.append((balance, take))
        left -= take
        if left <= 0:
            break
    if left > 0:
        return []
    return allocations


def check_bom_shortages(
    *,
    finished_item_id: int,
    material_warehouse_id: int,
    units_count: int = 1,
) -> list[dict]:
    bom = get_active_bom(finished_item_id)
    if not bom:
        return []
    shortages: list[dict] = []
    multiplier = Decimal(str(units_count))
    for bom_line in bom.lines.filter_by(is_active=True).order_by(ItemBillOfMaterialsLine.sort_order.asc()).all():
        if not bom_line.is_required:
            continue
        component = bom_line.component_item
        if not component or component.item_type == 'TRAILER':
            continue
        required = Decimal(str(bom_line.quantity_per_unit or 0)) * multiplier
        if required <= 0:
            continue
        unit = normalize_unit(bom_line.unit or component.unit)
        available = _available_on_warehouse(material_warehouse_id, component.id)
        if available < required:
            shortages.append({
                'item_id': component.id,
                'name': component.name,
                'required': format_inventory_quantity(required, unit),
                'available': format_inventory_quantity(max(available, 0), unit),
                'unit': unit,
            })
    return shortages


def apply_production_consumption(
    *,
    line: ProductionRequestLine,
    produced_unit_id: int,
    created_by_user_id: int | None,
    units_count: int = 1,
) -> InventoryOperation | None:
    """Списание ТМЦ по спецификации при выпуске (в пределах производственного склада филиала)."""
    if not line.item_id:
        return None
    bom = get_active_bom(line.item_id)
    if not bom:
        return None

    finished_wh_id = line.production_request.target_warehouse_id if line.production_request else None
    material_wh_id = resolve_material_warehouse_id(finished_wh_id)
    if not material_wh_id:
        raise ValueError('Не найден производственный склад для списания материалов.')

    shortages = check_bom_shortages(
        finished_item_id=line.item_id,
        material_warehouse_id=material_wh_id,
        units_count=units_count,
    )
    if shortages:
        raise InsufficientInventoryError(shortages)

    operation = InventoryOperation(
        operation_type='production_issue',
        status='posted',
        source_warehouse_id=material_wh_id,
        production_request_line_id=line.id,
        produced_unit_id=produced_unit_id,
        workshop_id=line.production_workshop_id,
        created_by_user_id=created_by_user_id,
        posted_at=datetime.utcnow(),
        comment=f'Списание по выпуску, заявка {line.production_request.request_number if line.production_request else line.id}',
    )
    db.session.add(operation)
    db.session.flush()

    multiplier = Decimal(str(units_count))
    for bom_line in bom.lines.filter_by(is_active=True).order_by(ItemBillOfMaterialsLine.sort_order.asc()).all():
        if not bom_line.is_required:
            continue
        component = bom_line.component_item
        if not component or component.item_type == 'TRAILER':
            continue
        qty_needed = Decimal(str(bom_line.quantity_per_unit or 0)) * multiplier
        if qty_needed <= 0:
            continue
        unit = normalize_unit(bom_line.unit or component.unit)
        allocations = _allocate_issue_from_balances(material_wh_id, component.id, qty_needed)
        if not allocations:
            raise InsufficientInventoryError([{
                'name': component.name,
                'required': str(qty_needed),
                'available': '0',
                'unit': unit,
            }])
        for balance, take in allocations:
            db.session.add(
                InventoryOperationLine(
                    operation_id=operation.id,
                    item_id=component.id,
                    quantity=take,
                    unit=unit,
                    direction='out',
                    comment=bom_line.comment,
                )
            )
            balance.quantity = Decimal(balance.quantity or 0) - take
            balance.updated_at = datetime.utcnow()
    db.session.flush()
    return operation


def production_warehouse_ids() -> list[int]:
    return [
        row.id
        for row in Warehouse.query.filter(
            Warehouse.is_active == True,
            or_(Warehouse.is_production == True, Warehouse.warehouse_kind == 'production'),
        ).all()
    ]


def item_has_positive_balance(item_id: int) -> bool:
    return (
        InventoryBalance.query.filter(
            InventoryBalance.item_id == item_id,
            InventoryBalance.quantity > 0,
        ).first()
        is not None
    )
