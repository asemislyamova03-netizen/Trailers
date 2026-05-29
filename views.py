# views.py
from functools import wraps
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from uuid import uuid4

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, abort, make_response, jsonify, current_app, make_response,
)
from flask_login import (
    login_required, current_user,
    logout_user, login_user
)

from extensions import db
from models import Trailer, Item, ProductCategory, Warehouse, Customer, SalesContract, SalesContractLine, SalesRealization, SalesRealizationLine, ContractTemplate, User, OTTS, Lead, LeadMessage, CustomerOrder, CustomerOrderLine, OrderPayment, OrderEvent, Reservation, SupplyNeed, ProductionRequest, ProductionRequestLine, ProducedUnit, StockMovement, IdempotencyKey, VinRegistry, VinRegistryEvent
from models import (
    TrailerAllowedOption, TrailerBoardHeight, TrailerBoardPriceMatrix, TrailerBodyExecution, TrailerBodySize,
    TrailerDimensionMatrix, TrailerHubOption, TrailerHubPriceMatrix, TrailerOtssModificationMatrix,
    TrailerPlatformPriceMatrix, TrailerProductGroup, TrailerSpecialOption, TrailerSupportWheelOption,
    TrailerSpecialOptionPriceMatrix, TrailerSupportWheelPriceMatrix, TrailerTentOption, TrailerTentPriceMatrix, TrailerWheelOption,
    TrailerWheelPriceMatrix,
)
from forms import (
    TrailerCreateForm, WarehouseForm, ItemForm,
    CustomerForm, SalesContractForm, LoginForm, UserForm, OTTSForm, LeadForm, CustomerOrderForm, OrderPaymentForm,
    SupplyNeedForm, ProductionRequestForm, ProductionRequestLineForm, StockMovementForm, StockMovementBatchForm,
    AssignVinForm, SendTrailerForm, StockReplenishmentForm, TrailerItemChangeForm, ContractTemplateForm,
    KaspiOrderImportForm, KaspiOrderListImportForm
)
from collections import defaultdict
import sqlalchemy as sa
from sqlalchemy import or_
import json
import base64
from wtforms import StringField, SelectField, DateField, BooleanField, DecimalField, IntegerField, TextAreaField, SubmitField

from flask_wtf import FlaskForm
from wtforms.validators import DataRequired, Optional, Length, NumberRange
try:
    from weasyprint import HTML  # опционально, может не загрузиться на Windows
except Exception as e:
    HTML = None
    print("WeasyPrint не доступен:", e)

from sqlalchemy.exc import IntegrityError

from sigex_client import sigex_post_json, sigex_get_json, sigex_post_octet, sigex_delete_json
from kaspi_client import KaspiClientError, KaspiShopClient
from pdf_utils import build_contract_pdf_bytes
from app import csrf
main_bp = Blueprint('main', __name__)


# ========= ДЕКОРАТОРЫ =========

def admin_required(f):
    """
    Доступ только для админа.
    Сначала логин (через login_required), потом проверка роли.
    """
    @wraps(f)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            flash('Доступ запрещён', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.is_admin or current_user.role in roles:
                return f(*args, **kwargs)
            flash('Доступ запрещён для вашей роли', 'danger')
            return redirect(url_for('main.role_home'))
        return wrapped
    return decorator


@main_bp.app_template_global('navigation_section')
def navigation_section(endpoint: str | None = None, path: str | None = None) -> str:
    endpoint = endpoint or (request.endpoint or '')
    path = path or (request.path or '')
    if endpoint in ('main.manager_workspace',) or path in ('/workspace', '/manager/workspace'):
        return 'crm'
    if path.startswith(('/conversations', '/leads', '/integrations/kaspi')):
        return 'crm'
    if path.startswith('/orders') or endpoint in ('main.orders_list', 'main.order_detail'):
        return 'deals'
    if path.startswith('/manager/trailer-picker'):
        return 'availability'
    if path.startswith('/trailers'):
        return 'availability'
    if path.startswith('/stock-replenishment'):
        return 'availability'
    if path.startswith('/stock-movements'):
        return 'movements'
    if path.startswith('/realizations'):
        return 'realizations'
    if path.startswith('/logistics/vin-registry'):
        return 'vin'
    if path.startswith('/logistics'):
        return 'vin'
    if path.startswith('/production'):
        return 'production'
    if path.startswith('/reports') or path.startswith('/director/reports'):
        return 'reports'
    if path.startswith('/customers'):
        return 'clients'
    if path.startswith('/director'):
        return 'director'
    if path.startswith(('/items', '/warehouses', '/catalog', '/otts', '/contracts', '/users')):
        return 'settings'
    return ''


def _production_warehouses():
    return (
        Warehouse.query
        .filter_by(is_active=True, is_production=True)
        .order_by(Warehouse.name)
        .all()
    )


def _sales_warehouses():
    query = Warehouse.query.filter(Warehouse.is_active == True)
    if hasattr(Warehouse, 'can_sell'):
        query = query.filter(Warehouse.can_sell == True)
    elif hasattr(Warehouse, 'is_sales_point'):
        query = query.filter(Warehouse.is_sales_point == True)
    return query.order_by(Warehouse.name).all()


def _trailer_source_warehouses():
    query = Warehouse.query.filter(Warehouse.is_active == True)
    if hasattr(Warehouse, 'can_sell'):
        query = query.filter(or_(Warehouse.can_sell == True, Warehouse.is_production == True))
    elif hasattr(Warehouse, 'is_sales_point'):
        query = query.filter(or_(Warehouse.is_sales_point == True, Warehouse.is_production == True))
    return query.order_by(Warehouse.name).all()


def _default_product_category_for_item_type(item_type: str | None):
    code = 'component' if (item_type or '').upper() == 'COMPONENT' else 'light_trailer'
    return ProductCategory.query.filter_by(code=code).first()


def _category_code(obj) -> str:
    item = getattr(obj, 'item', obj)
    category = getattr(item, 'product_category', None)
    return category.code if category else ''


def _default_production_warehouse():
    if current_user.is_authenticated and current_user.warehouse_id:
        warehouse = Warehouse.query.get(current_user.warehouse_id)
        if warehouse and warehouse.is_active and warehouse.is_production:
            return warehouse

    warehouses = _production_warehouses()
    if not warehouses:
        return None
    return warehouses[0]


def can_receive_movement(movement: StockMovement) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager:
        return bool(
            (movement.order and movement.order.assigned_user_id == current_user.id)
            or not movement.order_id
            or current_user.warehouse_id == movement.to_warehouse_id
        )
    return False


def can_ship_order(order: CustomerOrder) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    return current_user.is_manager and (
        order.assigned_user_id == current_user.id
        or getattr(order, 'created_by_user_id', None) == current_user.id
    )


def can_access_order(order: CustomerOrder) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager:
        return (
            order.assigned_user_id == current_user.id
            or getattr(order, 'created_by_user_id', None) == current_user.id
        )
    if current_user.is_viewer:
        return True
    return False


def can_manage_order(order: CustomerOrder) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    return current_user.is_manager and (
        order.assigned_user_id == current_user.id
        or getattr(order, 'created_by_user_id', None) == current_user.id
    )


def _block_production_commercial_access():
    if current_user.is_production or current_user.is_logistics:
        abort(403)


def _ensure_can_access_order(order: CustomerOrder) -> None:
    _block_production_commercial_access()
    if not can_access_order(order):
        abort(403)


def _ensure_can_manage_order(order: CustomerOrder) -> None:
    _block_production_commercial_access()
    if not can_manage_order(order):
        abort(403)


def can_access_contract(contract: SalesContract) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager:
        if contract.order:
            return can_access_order(contract.order)
        return bool(
            current_user.warehouse_id
            and (
                contract.warehouse_id == current_user.warehouse_id
                or (contract.trailer and contract.trailer.warehouse_id == current_user.warehouse_id)
            )
        )
    return False


def can_manage_contract(contract: SalesContract) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager and contract.order:
        return can_manage_order(contract.order) and not contract.order.documents_issued and not contract.order.is_shipped
    return False


@main_bp.app_template_global('can_manage_contract')
def can_manage_contract_template(contract: SalesContract) -> bool:
    return can_manage_contract(contract)


def _ensure_can_access_contract(contract: SalesContract) -> None:
    _block_production_commercial_access()
    if not can_access_contract(contract):
        abort(403)


def _ensure_can_manage_contract(contract: SalesContract) -> None:
    _block_production_commercial_access()
    if not can_manage_contract(contract):
        abort(403)


def can_access_conversation(lead: Lead) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager:
        if lead.assigned_user_id == current_user.id:
            return True
        if lead.assigned_user_id is None and lead.conversation_status in ('new', 'manager_needed', 'ai_handling'):
            return True
        if current_user.warehouse_id and lead.warehouse_id == current_user.warehouse_id:
            return True
    return False


def can_manage_conversation(lead: Lead) -> bool:
    if current_user.is_admin:
        return True
    if current_user.is_manager:
        return can_access_conversation(lead)
    return False


def _conversation_visible_query():
    query = Lead.query
    if current_user.is_admin or current_user.is_director:
        return query
    if current_user.is_manager:
        conditions = [
            Lead.assigned_user_id == current_user.id,
            sa.and_(
                Lead.assigned_user_id.is_(None),
                Lead.conversation_status.in_(['new', 'manager_needed', 'ai_handling']),
            ),
        ]
        if current_user.warehouse_id:
            conditions.append(Lead.warehouse_id == current_user.warehouse_id)
        return query.filter(or_(*conditions))
    return query.filter(sa.false())


def _ensure_can_access_conversation(lead: Lead) -> None:
    if current_user.is_production or current_user.is_logistics:
        abort(403)
    if not can_access_conversation(lead):
        abort(403)


def _ensure_can_manage_conversation(lead: Lead) -> None:
    if current_user.is_production or current_user.is_logistics:
        abort(403)
    if not can_manage_conversation(lead):
        abort(403)


def _add_lead_system_message(lead: Lead, text: str) -> None:
    db.session.add(LeadMessage(
        lead_id=lead.id,
        direction='OUT',
        sender_type='system',
        channel=lead.channel or lead.source_channel or 'manual',
        text=text,
        is_read=True,
        user_id=current_user.id if current_user and current_user.is_authenticated else None,
    ))
    lead.last_message_at = datetime.utcnow()
    lead.last_message_text = text


def _need_type_key(need: SupplyNeed | None) -> str:
    value = (need.need_type if need else '') or ''
    value = value.upper()
    if value in ('STOCK_REPLENISHMENT', 'WAREHOUSE_STOCK'):
        return 'STOCK_REPLENISHMENT'
    return 'CUSTOMER_ORDER'


def _is_stock_replenishment_need(need: SupplyNeed | None) -> bool:
    return _need_type_key(need) == 'STOCK_REPLENISHMENT'


def _vin_registry_for_order_or_need(order: CustomerOrder | None = None, need: SupplyNeed | None = None) -> VinRegistry | None:
    order_id = getattr(order, 'id', None)
    need_id = getattr(need, 'id', None)
    if not order_id and not need_id:
        return None
    query = VinRegistry.query.filter(VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']))
    if order_id and need_id:
        query = query.filter(or_(VinRegistry.customer_order_id == order_id, VinRegistry.supply_need_id == need_id))
    elif order_id:
        query = query.filter(VinRegistry.customer_order_id == order_id)
    else:
        query = query.filter(VinRegistry.supply_need_id == need_id)
    return query.order_by(
        VinRegistry.docs_issued_at.desc().nullslast(),
        VinRegistry.assigned_at.desc().nullslast(),
        VinRegistry.reserved_at.desc().nullslast(),
        VinRegistry.id.desc(),
    ).first()


def _config_snapshot_summary(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    parts = []
    mapping = [
        ('body_size_code', 'Размер кузова'),
        ('board_height_code', 'Высота борта'),
        ('wheel_code', 'Колёса'),
        ('hub_code', 'Ступица'),
        ('support_wheel_code', 'Опорное колесо'),
        ('tent_code', 'Тент'),
    ]
    for key, label in mapping:
        value = data.get(key) or data.get(key.replace('_code', '_id'))
        if value:
            parts.append(f'{label}: {value}')
    specials = data.get('special_options') or data.get('special_options_json') or data.get('special_option_codes')
    if isinstance(specials, str):
        try:
            parsed = json.loads(specials)
            specials = parsed
        except (TypeError, ValueError):
            specials = [s.strip() for s in specials.split(',') if s.strip()]
    if specials:
        if isinstance(specials, dict):
            specials = [str(k) for k, enabled in specials.items() if enabled]
        if isinstance(specials, (list, tuple, set)):
            parts.append('Спецпараметры: ' + ', '.join(str(item) for item in specials if item))
    return parts


def _produced_unit_context(unit: ProducedUnit | None):
    line = unit.production_request_line if unit else None
    need = line.supply_need if line else None
    order = unit.order if unit and unit.order_id else None
    if not order:
        order = need.order if need and need.order_id else None
    vin_row = _vin_registry_for_order_or_need(order, need)
    if vin_row and vin_row.trailer_id and (not unit or unit.trailer_id != vin_row.trailer_id):
        vin_row = None
    vin_to_apply = vin_row.vin_full if vin_row else None
    vin_conflict_trailer = None
    if vin_to_apply:
        existing_trailer = Trailer.query.filter_by(vin=vin_to_apply).first()
        if existing_trailer and (not unit or existing_trailer.id != unit.trailer_id):
            vin_conflict_trailer = existing_trailer
            vin_to_apply = None
    return {
        'unit': unit,
        'line': line,
        'need': need,
        'order': order,
        'need_type': _need_type_key(need),
        'target_warehouse': unit.target_warehouse if unit else None,
        'vin_registry': vin_row,
        'vin_to_apply': vin_to_apply,
        'vin_conflict_trailer': vin_conflict_trailer,
        'vin_conflict_vin': vin_row.vin_full if vin_conflict_trailer and vin_row else None,
        'vin_docs_issued': bool(vin_row and vin_row.docs_issued_at),
    }


def _trailer_production_context(trailer: Trailer):
    unit = getattr(trailer, 'produced_unit', None)
    if unit is not None and not isinstance(unit, ProducedUnit):
        unit = unit[0] if len(unit) else None
    context = _produced_unit_context(unit)
    if not context['order']:
        active_reservation = _active_reservation_for_trailer(trailer.id)
        if active_reservation:
            context['order'] = active_reservation.order
            context['need_type'] = 'CUSTOMER_ORDER'
    return context


def _trailer_is_customer_shipped(trailer: Trailer | None) -> bool:
    return bool(trailer and (trailer.lifecycle_status or '').lower() == 'customer_shipped')


def _order_is_shipped(order: CustomerOrder | None) -> bool:
    return bool(order and (
        getattr(order, 'is_shipped', False)
        or (order.status or '').lower() in ('shipped', 'done', 'customer_shipped')
    ))


def _trailer_available_for_sale(trailer: Trailer, exclude_order_id: int | None = None) -> bool:
    if not trailer or trailer.status != 'IN_STOCK' or _trailer_is_customer_shipped(trailer):
        return False
    if trailer.warehouse and not getattr(trailer.warehouse, 'can_sell', True):
        return False
    return _active_reservation_for_trailer(trailer.id, exclude_order_id=exclude_order_id) is None


def _active_movement_for_trailer(trailer_id: int, exclude_movement_id: int | None = None):
    q = StockMovement.query.filter(
        StockMovement.trailer_id == trailer_id,
        StockMovement.status.in_(['draft', 'DRAFT', 'sent', 'in_transit']),
    )
    if exclude_movement_id:
        q = q.filter(StockMovement.id != exclude_movement_id)
    return q.first()


def _trailer_available_for_movement(trailer: Trailer, from_warehouse_id: int | None = None, exclude_movement_id: int | None = None) -> bool:
    if not trailer or _trailer_is_customer_shipped(trailer):
        return False
    if trailer.status not in ('IN_STOCK', 'RESERVED', 'SOLD'):
        return False
    if from_warehouse_id and trailer.warehouse_id != from_warehouse_id:
        return False
    return _active_movement_for_trailer(trailer.id, exclude_movement_id=exclude_movement_id) is None


def _order_is_open_for_attachment(order: CustomerOrder) -> bool:
    return bool(order and not order.is_shipped and order.status not in ('cancelled', 'canceled', 'shipped', 'done'))


def _active_order_for_produced_unit(unit: ProducedUnit, exclude_order_id: int | None = None):
    order = unit.order if unit and unit.order_id else None
    if not order and unit and unit.production_request_line and unit.production_request_line.supply_need:
        order = unit.production_request_line.supply_need.order
    if order and _order_is_open_for_attachment(order) and order.id != exclude_order_id:
        return order
    return None


def _active_order_for_trailer(trailer_id: int, exclude_order_id: int | None = None):
    q = CustomerOrder.query.filter(
        CustomerOrder.trailer_id == trailer_id,
        CustomerOrder.status.notin_(['cancelled', 'canceled', 'shipped', 'done']),
        CustomerOrder.is_shipped == False,
    )
    if exclude_order_id:
        q = q.filter(CustomerOrder.id != exclude_order_id)
    return q.order_by(CustomerOrder.created_at.desc()).first()


def _order_attachment_status_for_trailer(trailer: Trailer, order: CustomerOrder):
    if order.trailer_id == trailer.id:
        return 'current'
    if _active_order_for_trailer(trailer.id, exclude_order_id=order.id):
        return 'other'
    if _active_reservation_for_trailer(trailer.id, exclude_order_id=order.id):
        return 'other'
    return 'free'


def _ensure_item_matches_order(item_id: int | None, order: CustomerOrder) -> bool:
    return bool(item_id and order.item_id and item_id == order.item_id)


def _ensure_target_matches_order_warehouse(target_warehouse_id: int | None, order: CustomerOrder) -> bool:
    if not target_warehouse_id or not order.warehouse_id:
        return True
    return target_warehouse_id == order.warehouse_id


def _set_trailer_status_for_order(trailer: Trailer, order: CustomerOrder) -> None:
    if order.documents_issued or order.status == 'sold_not_shipped':
        trailer.status = 'SOLD'
    else:
        trailer.status = 'RESERVED'


def _ensure_order_reservation(order: CustomerOrder, trailer: Trailer, source_type: str) -> None:
    if trailer.status == 'SOLD':
        return
    active = Reservation.query.filter_by(order_id=order.id, trailer_id=trailer.id, status='ACTIVE').first()
    if not active:
        db.session.add(Reservation(
            order_id=order.id,
            trailer_id=trailer.id,
            item_id=order.item_id,
            status='ACTIVE',
            source_type=source_type,
            priority=10,
        ))


def _contract_for_order(order: CustomerOrder) -> SalesContract | None:
    if not order or not order.id:
        return None
    return SalesContract.query.filter_by(order_id=order.id).first()


def _vin_registry_for_trailer_or_vin(trailer: Trailer | None) -> VinRegistry | None:
    if not trailer:
        return None
    query = VinRegistry.query.filter(VinRegistry.status != 'void')
    if trailer.id and trailer.vin:
        query = query.filter(or_(VinRegistry.trailer_id == trailer.id, VinRegistry.vin_full == trailer.vin))
    elif trailer.id:
        query = query.filter(VinRegistry.trailer_id == trailer.id)
    elif trailer.vin:
        query = query.filter(VinRegistry.vin_full == trailer.vin)
    else:
        return None
    return query.order_by(VinRegistry.id.desc()).first()


def _ensure_vin_registry_for_trailer(trailer: Trailer, source: str = 'order_trailer_change') -> VinRegistry | None:
    row = _vin_registry_for_trailer_or_vin(trailer)
    if row:
        return row
    if not trailer or not trailer.vin:
        return None
    parsed, error = _parse_vin_full(trailer.vin)
    if error:
        return None
    row = VinRegistry(status='assigned', trailer_id=trailer.id, source=source, **parsed)
    db.session.add(row)
    db.session.flush()
    _add_vin_event(row, 'created', None, row.status, comment='VIN-реестр создан при замене прицепа в заказе')
    return row


def _trailer_has_other_commercial_links(trailer_id: int, order_id: int | None = None, contract_id: int | None = None) -> bool:
    order_query = CustomerOrder.query.filter(
        CustomerOrder.trailer_id == trailer_id,
        CustomerOrder.status.notin_(['cancelled', 'canceled']),
    )
    if order_id:
        order_query = order_query.filter(CustomerOrder.id != order_id)
    if order_query.first():
        return True

    contract_query = SalesContract.query.filter(SalesContract.trailer_id == trailer_id)
    if contract_id:
        contract_query = contract_query.filter(SalesContract.id != contract_id)
    return contract_query.first() is not None


def _sync_order_trailer_links(order: CustomerOrder, old_trailer: Trailer | None, new_trailer: Trailer | None, source_type: str = 'STOCK') -> tuple[bool, str]:
    contract = _contract_for_order(order)
    if contract and old_trailer and new_trailer and old_trailer.id != new_trailer.id:
        if contract.sigex_last_status == 'done':
            return False, 'Договор уже подписан через SIGEX/eGov QR. Сначала нужна отдельная корректировка договора.'
        other_contract = (
            SalesContract.query
            .filter(SalesContract.trailer_id == new_trailer.id, SalesContract.id != contract.id)
            .first()
        )
        if other_contract:
            return False, 'На новый прицеп уже существует другой договор.'

    if old_trailer and (not new_trailer or old_trailer.id != new_trailer.id):
        _cancel_order_active_reservations(order, old_trailer.id, 'Прицеп заменён в заказе')
        for row in VinRegistry.query.filter(VinRegistry.trailer_id == old_trailer.id, VinRegistry.status != 'void').all():
            if row.customer_order_id == order.id:
                row.customer_order_id = None
            if row.order_line_id and row.order_line and row.order_line.order_id == order.id:
                row.order_line_id = None
            if row.docs_issued_order_id == order.id:
                row.docs_issued_order_id = None
            if contract and row.sales_contract_id == contract.id:
                row.sales_contract_id = None
            if order.reserved_vin_registry_id == row.id:
                order.reserved_vin_registry_id = None
        if not _trailer_has_other_commercial_links(old_trailer.id, order_id=order.id, contract_id=contract.id if contract else None):
            old_trailer.status = 'IN_STOCK'
            old_trailer.lifecycle_status = 'in_stock'

    if new_trailer:
        existing_vin_row = _vin_registry_for_trailer_or_vin(new_trailer)
        if existing_vin_row:
            if existing_vin_row.trailer_id and existing_vin_row.trailer_id != new_trailer.id:
                return False, 'VIN-реестр уже связан с другим физическим прицепом.'
            if existing_vin_row.customer_order_id and existing_vin_row.customer_order_id != order.id:
                return False, 'VIN-реестр нового прицепа уже связан с другим заказом.'
            if contract and existing_vin_row.sales_contract_id and existing_vin_row.sales_contract_id != contract.id:
                return False, 'VIN-реестр нового прицепа уже связан с другим договором.'

        if contract:
            contract.trailer_id = new_trailer.id
            contract.customer_id = order.customer_id
            contract.price = order.price
            contract.is_shipped = bool(order.is_shipped)

        order.trailer_id = new_trailer.id
        if not order.warehouse_id:
            order.warehouse_id = new_trailer.warehouse_id
        order.source_warehouse_id = new_trailer.warehouse_id
        order.fulfillment_source = order.fulfillment_source or ('stock' if source_type == 'STOCK' else source_type.lower())

        if order.is_shipped:
            new_trailer.status = 'SOLD'
            new_trailer.lifecycle_status = 'customer_shipped'
            StockMovement.query.filter(
                StockMovement.order_id == order.id,
                StockMovement.trailer_id == (old_trailer.id if old_trailer else new_trailer.id),
                StockMovement.movement_type == 'customer_shipment',
            ).update({StockMovement.trailer_id: new_trailer.id}, synchronize_session=False)
        elif order.documents_issued or order.status == 'sold_not_shipped':
            new_trailer.status = 'SOLD'
            new_trailer.lifecycle_status = 'sold'
        else:
            new_trailer.status = 'RESERVED'
            new_trailer.lifecycle_status = 'reserved'
            _ensure_order_reservation(order, new_trailer, source_type)

        row = _ensure_vin_registry_for_trailer(new_trailer)
        if row:
            order_line = _primary_order_line(order)
            row.trailer_id = new_trailer.id
            row.customer_order_id = order.id
            row.order_line_id = order_line.id if order_line else row.order_line_id
            row.sales_contract_id = contract.id if contract else row.sales_contract_id
            if order.documents_issued:
                row.docs_issued_order_id = order.id
                row.docs_issued_at = row.docs_issued_at or order.documents_issued_at or datetime.utcnow()
            if order.is_shipped or order.documents_issued or new_trailer.status == 'SOLD':
                row.status = 'confirmed'
            elif row.status == 'free':
                row.status = 'assigned'

    else:
        order.trailer_id = None
        order.source_warehouse_id = None
        if contract:
            contract.trailer_id = None

    return True, ''


def _find_order_for_sold_trailer(trailer: Trailer):
    if not trailer:
        return None
    return (
        CustomerOrder.query
        .filter(
            CustomerOrder.trailer_id == trailer.id,
            CustomerOrder.is_shipped == False,
            CustomerOrder.status.notin_(['cancelled', 'canceled', 'shipped', 'done']),
        )
        .order_by(CustomerOrder.documents_issued_at.desc().nullslast(), CustomerOrder.created_at.desc())
        .first()
    )


def add_order_event(order, event_type, old_value=None, new_value=None, comment=None):
    user_id = None
    if current_user and current_user.is_authenticated:
        user_id = current_user.id
    db.session.add(OrderEvent(
        order_id=order.id,
        user_id=user_id,
        event_type=event_type,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        comment=comment,
    ))


@main_bp.app_template_global()
def form_token():
    return uuid4().hex


def _idempotency_endpoint_key() -> str:
    return f"{request.endpoint or 'unknown'}:{request.path}"[:255]


def _reserve_idempotency_key():
    token = (request.form.get('form_token') or '').strip()
    if not token or not current_user.is_authenticated:
        return None, False
    key = IdempotencyKey(
        user_id=current_user.id,
        endpoint=_idempotency_endpoint_key(),
        form_token=token[:64],
    )
    db.session.add(key)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = IdempotencyKey.query.filter_by(
            user_id=current_user.id,
            endpoint=_idempotency_endpoint_key(),
            form_token=token[:64],
        ).first()
        return existing, True
    return key, False


def _finish_idempotency(key, object_type: str, object_id: int | None = None):
    if key:
        key.object_type = object_type
        key.object_id = object_id


def _duplicate_redirect(key, fallback_url=None):
    flash('Повторный запрос не выполнен: это действие уже было обработано.', 'warning')
    if key and key.object_type == 'CustomerOrder' and key.object_id:
        return redirect(url_for('main.order_detail', order_id=key.object_id))
    if key and key.object_type == 'Lead' and key.object_id:
        return redirect(url_for('main.lead_detail', lead_id=key.object_id))
    if key and key.object_type == 'SalesContract' and key.object_id:
        contract = SalesContract.query.get(key.object_id)
        if contract and contract.order_id:
            return redirect(url_for('main.order_detail', order_id=contract.order_id))
        return redirect(url_for('main.contracts_list'))
    if key and key.object_type == 'Customer' and key.object_id:
        customer = Customer.query.get(key.object_id)
        if customer:
            return _customer_return_redirect(
                customer,
                (request.args.get('return_to') or '').strip(),
                request.args.get('order_id', type=int),
            )
        return redirect(url_for('main.customers_list'))
    if key and key.object_type == 'ProductionRequest' and key.object_id:
        return redirect(url_for('main.production_request_detail', request_id=key.object_id))
    if key and key.object_type in ('StockMovement', 'StockMovementBatch'):
        return redirect(url_for('main.stock_movements_list'))
    if key and key.object_type in ('ProducedUnit', 'Trailer'):
        if current_user.is_logistics:
            return redirect(url_for('main.vin_registry_list'))
        return redirect(url_for('main.logistics_workspace'))
    return redirect(fallback_url or request.referrer or url_for('main.role_home'))


@main_bp.app_template_filter('status_label')
def status_label(value):
    labels = {'draft': 'Черновик', 'new': 'Новая', 'waiting_payment': 'Ждём оплату', 'prepaid': 'Предоплата', 'confirmed': 'Подтверждён', 'waiting_production': 'Ожидает производства', 'in_production': 'В производстве', 'produced_waiting_vin': 'Выпущен, ждёт VIN', 'waiting_transfer': 'Ждёт отправки', 'in_transit': 'В пути', 'arrived': 'Прибыл', 'ready_to_ship': 'Готов к выдаче', 'sold_not_shipped': 'Продан, не отгружен', 'customer_shipped': 'Физически отгружен клиенту', 'shipped': 'Отгружен', 'done': 'Завершён', 'cancelled': 'Отменён', 'canceled': 'Отменён', 'produced_no_vin': 'Выпущен без VIN', 'vin_assigned': 'VIN присвоен', 'planned': 'Запланирована', 'partial_ready': 'Частично выпущена', 'ready': 'Готово', 'closed': 'Закрыта', 'sent': 'Отправлено', 'in_progress': 'В работе', 'approved': 'Утверждена', 'ready_production_warehouse': 'Готов на складе выпуска', 'stock': 'Из наличия', 'other_warehouse': 'С другого склада', 'production': 'Под производство', 'transit': 'В пути', 'free': 'Свободен', 'reserved': 'Зарезервирован', 'assigned': 'Назначен', 'void': 'Аннулирован', 'not_started': 'Не начаты', 'partial': 'Частично', 'realized': 'Реализовано', 'posted': 'Проведено', 'exported': 'Экспортировано', 'invoice_sent': 'Счёт отправлен', 'contract_ready': 'Договор готов', 'documents_ready': 'Документы готовы', 'documents_issued': 'Документы выданы', 'unpaid': 'Не оплачено', 'paid': 'Оплачено', 'order_created': 'Заказ создан', 'order_line_added': 'Позиция добавлена', 'order_status_changed': 'Статус заказа изменён', 'payment_added': 'Оплата добавлена', 'payment_cancelled': 'Оплата отменена', 'trailer_reserved': 'Прицеп зарезервирован', 'trailer_assigned': 'Прицеп назначен', 'production_need_created': 'Создана потребность', 'production_started': 'Производство начато', 'produced_without_vin': 'Выпущено без VIN', 'transfer_requested': 'Запрошено перемещение', 'transfer_started': 'Перемещение начато', 'trailer_received': 'Прицеп принят', 'reservation_cancelled': 'Резерв отменён', 'realization_created': 'Реализация создана', 'realization_posted': 'Реализация проведена', 'uploaded': 'Загружен', 'assigned': 'Назначен', 'voided': 'Аннулирован', 'comment_added': 'Комментарий добавлен'}
    labels.update({
        'ai_handling': 'ИИ ведёт диалог',
        'manager_needed': 'Нужен менеджер',
        'manager_handling': 'В работе у менеджера',
        'waiting_client': 'Ждём клиента',
        'spam': 'Спам',
        'manual': 'Вручную',
        'website': 'Сайт',
        'whatsapp': 'WhatsApp',
        'instagram': 'Instagram',
        'telegram': 'Telegram',
        'phone': 'Телефон',
        'other': 'Другое',
        'customer_order': 'Заказ клиента',
        'stock_replenishment': 'Пополнение склада',
        'warehouse_stock': 'Пополнение склада',
        'warehouse_transfer': 'Между складами',
        'production_arrival': 'Поступление с производства',
        'customer_shipment': 'Отгрузка клиенту',
        'contract_signed': 'Договор подписан',
        'sigex_signed': 'Подписан через SIGEX',
        'ready_production_warehouse': 'Готов на складе выпуска',
        'in_stock': 'В наличии',
        'reserved': 'В резерве',
        'sold': 'Продан',
        'decommissioned': 'Списан',
        'document_ready': 'Документ SIGEX подготовлен',
        'qr_started': 'QR ожидает подписи',
        'fail': 'Ошибка SIGEX',
        'expired': 'QR истёк',
        'org_signed': 'Подписан организацией',
        'canceled': 'QR отменён',
    })
    normalized = str(value or '').lower()
    return labels.get(normalized, value or '')


@main_bp.app_template_filter('status_badge_class')
def status_badge_class(value):
    value = (value or '').lower()
    if value in ('draft', 'new', 'planned', 'manual', 'website', 'phone', 'other', 'customer_order'):
        return 'secondary'
    if value in ('in_progress', 'in_production', 'sent', 'in_transit', 'manager_handling', 'telegram', 'qr_started'):
        return 'primary'
    if value in ('waiting_payment', 'waiting_production', 'waiting_transfer', 'produced_waiting_vin', 'produced_no_vin', 'invoice_sent', 'partial', 'not_started', 'manager_needed', 'waiting_client'):
        return 'warning'
    if value in ('prepaid', 'partial_ready', 'ai_handling', 'whatsapp', 'instagram', 'stock_replenishment', 'warehouse_stock'):
        return 'info'
    if value in ('ready', 'arrived', 'ready_to_ship', 'sold_not_shipped', 'customer_shipped', 'done', 'vin_assigned', 'confirmed', 'paid', 'documents_ready', 'documents_issued', 'contract_ready', 'contract_signed', 'sigex_signed', 'order_created', 'in_stock', 'sold', 'document_ready', 'org_signed'):
        return 'success'
    if value in ('cancelled', 'canceled', 'closed', 'spam', 'decommissioned', 'fail', 'expired'):
        return 'dark'
    if value == 'reserved':
        return 'warning'
    return 'secondary'


@main_bp.app_template_filter('money')
def money(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,.0f}".replace(",", " ") + " ₸"


@main_bp.app_template_filter('date_format')
def date_format(value):
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%d.%m.%Y')
    return value


@main_bp.route('/home')
@login_required
def role_home():
    if current_user.is_admin:
        return redirect(url_for('main.orders_list'))
    if current_user.is_production:
        return redirect(url_for('main.production_workspace'))
    if current_user.is_logistics:
        return redirect(url_for('main.vin_registry_list'))
    if current_user.is_director:
        return redirect(url_for('main.director_dashboard'))
    if current_user.is_manager:
        return redirect(url_for('main.manager_workspace'))
    return redirect(url_for('main.trailers_list'))


# ========= АУТЕНТИФИКАЦИЯ =========

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    # если уже вошли
    if current_user.is_authenticated:
        return redirect(url_for('main.role_home'))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Вы успешно вошли', 'success')

            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)

            return redirect(url_for('main.role_home'))
        else:
            flash('Неверный логин или пароль', 'danger')

    return render_template('login.html', form=form)



@main_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('main.login'))

def _extract_modification_from_vin(vin: str | None) -> str | None:
    """
    Извлекаем код модификации из VIN:
    - берем 4–9 символы (индексы 3:9),
    - из них пытаемся получить последние 3 цифры (00001 -> 001, 00002 -> 002),
    - если не получилось, возвращаем как есть.
    """
    if not vin:
        return None

    vin = vin.strip()
    if len(vin) < 9:
        return None

    raw = vin[3:9]  # 4..9 знак
    raw = raw.strip()
    if not raw:
        return None

    # пробуем вытащить последние 3 цифры
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 3:
        return digits[-3:]  # '00001' -> '001', '00002' -> '002'

    # запасной вариант: привести к int и форматнуть
    try:
        return f"{int(raw):03d}"
    except ValueError:
        return raw

def _fill_trailer_form_choices(form: TrailerCreateForm) -> None:
    """Заполняем choices для склада и характеристик из таблицы Item."""
    # --- Склады ---
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(w.id, w.name) for w in warehouses]

    # --- Характеристики из номенклатуры (Item) ---
    items = Item.query.filter_by(item_type='TRAILER', is_active=True).all()

    size_body_values = sorted({i.size_body for i in items if i.size_body})
    axle_values = sorted({i.axle_count for i in items if i.axle_count is not None})
    radius_values = sorted({i.wheel_radius for i in items if i.wheel_radius})
    board_values = sorted({i.board_height_mm for i in items if i.board_height_mm is not None})

    form.size_body.choices = [(v, v) for v in size_body_values]
    form.axle_count.choices = [(v, str(v)) for v in axle_values]
    form.wheel_radius.choices = [(v, v) for v in radius_values]

    # для высоты борта добавим первую опцию "не выбрано"
    form.board_height_mm.choices = [('', '— не выбрано —')] + [
        (str(v), str(v)) for v in board_values
    ]

def _find_item_for_form(form: TrailerCreateForm):
    """
    Подбирает модель прицепа (Item) по значениям из формы.
    Возвращает (item, None) или (None, 'текст ошибки').
    """
    import sqlalchemy as sa

    q = Item.query.filter_by(item_type='TRAILER', is_active=True)

    if form.size_body.data:
        q = q.filter(Item.size_body == form.size_body.data)

    if form.axle_count.data:
        q = q.filter(Item.axle_count == form.axle_count.data)

    if form.wheel_radius.data:
        q = q.filter(Item.wheel_radius == form.wheel_radius.data)

    if form.board_height_mm.data:
        try:
            bh = int(form.board_height_mm.data)
            q = q.filter(Item.board_height_mm == bh)
        except ValueError:
            return None, 'Некорректное значение высоты борта.'

    # --- Подкатное колесо (обязательный выбор) ---
    # form.has_jockey_wheel.data -> 1 (есть) или 0 (нет)
    has_jw = form.has_jockey_wheel.data
    q = q.filter(Item.has_jockey_wheel == (has_jw == 1))

    # --- Высота тента по tent_height_mm, как мы уже делали ---
    tent_h = form.tent_height_mm.data  # int

    if tent_h == 0:
        q = q.filter(
            sa.or_(
                Item.has_tent == False,
                Item.tent_hight_mm.is_(None)
            )
        )
    else:
        q = q.filter(
            sa.and_(
                Item.has_tent == True,
                Item.tent_hight_mm == tent_h
            )
        )

    item = q.first()
    if not item:
        return None, 'Не удалось подобрать модель по указанным характеристикам. Проверьте матрицу.'

    return item, None


def _fill_trailer_item_change_form_choices(form: TrailerItemChangeForm) -> None:
    items = Item.query.filter_by(item_type='TRAILER', is_active=True).all()
    size_body_values = sorted({i.size_body for i in items if i.size_body})
    axle_values = sorted({i.axle_count for i in items if i.axle_count is not None})
    wheel_values = sorted({i.wheel_radius for i in items if i.wheel_radius})
    board_values = sorted({i.board_height_mm for i in items if i.board_height_mm is not None})
    tent_heights = sorted({i.tent_hight_mm for i in items if i.tent_hight_mm is not None})

    form.size_body.choices = [(v, v) for v in size_body_values]
    form.axle_count.choices = [(v, str(v)) for v in axle_values]
    form.wheel_radius.choices = [(v, v) for v in wheel_values]
    form.board_height_mm.choices = [(str(v), str(v)) for v in board_values]
    form.tent_height_mm.choices = [(0, 'Нет тента')] + [(int(v), f'{v} мм') for v in tent_heights]


def _prefill_trailer_item_change_form(form: TrailerItemChangeForm, item: Item | None) -> None:
    if not item:
        return
    form.size_body.data = item.size_body
    form.axle_count.data = item.axle_count
    form.wheel_radius.data = item.wheel_radius
    form.board_height_mm.data = str(item.board_height_mm) if item.board_height_mm is not None else ''
    form.tent_height_mm.data = item.tent_hight_mm if item.has_tent else 0
    form.has_jockey_wheel.data = 1 if item.has_jockey_wheel else 0


def _item_base_matches_locked_item(item: Item, locked_item: Item | None) -> bool:
    if not locked_item:
        return True
    item_config = _config_from_item(item)
    locked_config = _config_from_item(locked_item)
    return (
        (item_config.get('group_code') or '') == (locked_config.get('group_code') or '')
        and (item_config.get('body_size_code') or '') == (locked_config.get('body_size_code') or '')
        and item.axle_count == locked_item.axle_count
    )


def _default_trailer_config_values() -> dict:
    return {
        'group_code': '002',
        'body_execution_code': 'BOARD',
        'body_size_code': '2515',
        'board_height_code': 'E50',
        'wheel_code': 'Q13',
        'hub_code': '',
        'support_wheel_code': 'OK',
        'tent_code': '90',
        'special_options': [],
    }


def _config_with_defaults(config: dict | None = None) -> dict:
    base = _default_trailer_config_values()
    optional_blank_keys = {'wheel_code', 'hub_code', 'support_wheel_code', 'tent_code'}
    for key, value in (config or {}).items():
        if key == 'special_options':
            base[key] = value or []
        elif value is not None and (value != '' or key in optional_blank_keys):
            base[key] = value
    return base


def _code_by_article_part(model, text: str) -> tuple[str, str]:
    for row in model.query.filter_by(is_active=True).order_by(model.sort_order).all():
        part = row.article_part or row.code
        if part and text.startswith(part):
            return row.code, text[len(part):]
    return '', text


def _infer_body_execution_code_for_item(config: dict, article: str) -> str:
    if not article:
        return config.get('body_execution_code') or 'BOARD'

    from trailer_configurator import build_trailer_configuration_result

    for execution in TrailerBodyExecution.query.filter_by(is_active=True).order_by(TrailerBodyExecution.sort_order, TrailerBodyExecution.code).all():
        trial_config = dict(config)
        trial_config['body_execution_code'] = execution.code
        result = build_trailer_configuration_result(trial_config)
        if not result.get('errors') and (result.get('article') or '') == article:
            return execution.code
    return config.get('body_execution_code') or 'BOARD'


def _config_from_item(item: Item | None) -> dict:
    config = _default_trailer_config_values()
    config['support_wheel_code'] = ''
    config['tent_code'] = ''
    article = (item.article if item else '') or ''
    parts = article.split('-')
    if len(parts) >= 2:
        config['group_code'] = parts[0] or config['group_code']
        left = parts[1]
        code, left = _code_by_article_part(TrailerBodySize, left)
        if code:
            config['body_size_code'] = code
        code, left = _code_by_article_part(TrailerBoardHeight, left)
        if code:
            config['board_height_code'] = code
            config['body_execution_code'] = 'PLATFORM' if code == 'E0' else 'BOARD'
        code, rest = _code_by_article_part(TrailerWheelOption, left)
        if code:
            config['wheel_code'] = code
            config['hub_code'] = ''
        else:
            code, rest = _code_by_article_part(TrailerHubOption, left)
            if code:
                config['hub_code'] = code
                config['wheel_code'] = ''
    if len(parts) >= 3:
        right = ''.join(parts[2:])
        code, right = _code_by_article_part(TrailerSupportWheelOption, right)
        if code:
            config['support_wheel_code'] = code
        code, right = _code_by_article_part(TrailerTentOption, right)
        if code:
            config['tent_code'] = code
        specials = []
        while right:
            code, remainder = _code_by_article_part(TrailerSpecialOption, right)
            if not code or remainder == right:
                break
            specials.append(code)
            right = remainder
        config['special_options'] = specials
    if item:
        if item.has_tent is False:
            config['tent_code'] = ''
        if item.has_jockey_wheel is False:
            config['support_wheel_code'] = ''
    config['body_execution_code'] = _infer_body_execution_code_for_item(config, article)
    return config


def _build_config_result(config: dict) -> dict:
    from trailer_configurator import build_trailer_configuration_result

    result = build_trailer_configuration_result(config)
    result['config'] = config
    return result


def _locked_config_base_matches(config: dict, locked_config: dict | None) -> bool:
    if not locked_config:
        return True
    for key in ('group_code', 'body_execution_code', 'body_size_code'):
        if (config.get(key) or '') != (locked_config.get(key) or ''):
            return False
    return True


def _can_change_trailer_item(trailer: Trailer) -> tuple[bool, str]:
    if _trailer_is_customer_shipped(trailer):
        return False, 'Комплектацию нельзя менять: прицеп уже отгружен клиенту.'
    if _active_movement_for_trailer(trailer.id):
        return False, 'Комплектацию нельзя менять: по прицепу есть активное перемещение.'
    orders = CustomerOrder.query.filter_by(trailer_id=trailer.id).all()
    if any(order.documents_issued or _order_is_shipped(order) for order in orders):
        return False, 'Комплектацию нельзя менять: по связанному заказу уже выданы документы или выполнена отгрузка.'
    if SalesContract.query.filter_by(trailer_id=trailer.id).first():
        return False, 'Комплектацию нельзя менять: по прицепу уже есть договор.'
    if VinRegistry.query.filter(VinRegistry.trailer_id == trailer.id, VinRegistry.docs_issued_at.isnot(None)).first():
        return False, 'Комплектацию нельзя менять: по VIN уже выданы документы.'
    return True, ''


def _can_change_produced_unit_item(unit: ProducedUnit) -> tuple[bool, str]:
    if unit.status != 'produced_no_vin' or unit.trailer_id:
        return False, 'Комплектацию можно менять только у выпущенной единицы без VIN.'
    order = unit.order if unit.order_id else None
    if not order and unit.production_request_line and unit.production_request_line.supply_need:
        order = unit.production_request_line.supply_need.order
    if order and (order.documents_issued or _order_is_shipped(order)):
        return False, 'Комплектацию нельзя менять: по заказу уже выданы документы или выполнена отгрузка.'
    return True, ''


@main_bp.route('/workspace')
@main_bp.route('/manager/workspace')
@login_required
def manager_workspace():
    if not (current_user.is_manager or current_user.is_admin or current_user.is_director):
        return redirect(url_for('main.role_home'))

    warehouses = _sales_warehouses()
    warehouse_id = current_user.warehouse_id
    if current_user.can_view_all:
        warehouse_id = request.args.get('warehouse_id', type=int) or warehouse_id
        if not warehouse_id and warehouses:
            warehouse_id = warehouses[0].id

    if not warehouse_id and warehouses:
        warehouse_id = warehouses[0].id

    active_tab = (request.args.get('tab') or 'orders').strip() or 'orders'
    crm_tabs = {'orders', 'funnel', 'leads', 'conversations', 'assistant'}
    if active_tab not in crm_tabs:
        active_tab = 'orders'

    new_leads = Lead.query.filter(
        or_(Lead.assigned_user_id == current_user.id, Lead.assigned_user_id.is_(None)),
        Lead.status.in_(['NEW', 'IN_PROGRESS'])
    ).order_by(Lead.created_at.desc()).limit(20).all()
    conversation_query = (
        _conversation_visible_query()
        .filter(or_(Lead.assigned_user_id == current_user.id, Lead.assigned_user_id.is_(None)))
        .filter(~Lead.conversation_status.in_(['closed', 'spam']))
    )
    recent_conversations = (
        conversation_query
        .order_by(Lead.unread_count.desc(), Lead.last_message_at.desc().nullslast(), Lead.updated_at.desc())
        .limit(20)
        .all()
    )
    new_conversations_count = conversation_query.filter(Lead.conversation_status == 'new').count()
    unread_messages_count = conversation_query.with_entities(sa.func.coalesce(sa.func.sum(Lead.unread_count), 0)).scalar() or 0
    manager_needed_count = conversation_query.filter(Lead.conversation_status == 'manager_needed').count()
    order_scope = CustomerOrder.query
    if current_user.is_manager:
        order_scope = order_scope.filter(CustomerOrder.assigned_user_id == current_user.id)
    elif warehouse_id:
        order_scope = order_scope.filter(CustomerOrder.warehouse_id == warehouse_id)
    active_orders = order_scope.filter(CustomerOrder.status.notin_(['done', 'cancelled', 'shipped'])).order_by(CustomerOrder.created_at.desc()).limit(30).all()
    active_order_rows = [{'order': order, 'state': _order_list_state(order)} for order in active_orders]
    order_filter_index = {code: idx for idx, (code, _label) in enumerate(ORDER_LIST_FILTERS)}
    funnel_map = {}
    for row in active_order_rows:
        state = row['state']
        order = row['order']
        code = state.get('code') or 'problem'
        if code not in funnel_map:
            funnel_map[code] = {
                'code': code,
                'label': state.get('label') or dict(ORDER_LIST_FILTERS).get(code, code),
                'count': 0,
                'amount': 0,
                'blockers': 0,
                'sort': order_filter_index.get(code, 999),
            }
        funnel_map[code]['count'] += 1
        funnel_map[code]['amount'] += float(order.price or 0)
        if state.get('blocker'):
            funnel_map[code]['blockers'] += 1
    funnel_rows = sorted(funnel_map.values(), key=lambda row: (row['sort'], row['label']))
    waiting_realization_rows = [row for row in active_order_rows if row['state'].get('code') == 'waiting_realization']
    waiting_movement_rows = [row for row in active_order_rows if row['state'].get('code') == 'waiting_movement']
    blocked_order_rows = [row for row in active_order_rows if row['state'].get('blocker')]
    active_order_ids = [order.id for order in active_orders]
    active_order_line_ids = [
        line_id for (line_id,) in (
            CustomerOrderLine.query
            .with_entities(CustomerOrderLine.id)
            .filter(CustomerOrderLine.order_id.in_(active_order_ids))
            .all()
        )
    ] if active_order_ids else []
    assigned_vins_to_confirm = (
        VinRegistry.query
        .filter(
            VinRegistry.status == 'assigned',
            or_(
                VinRegistry.customer_order_id.in_(active_order_ids),
                VinRegistry.order_line_id.in_(active_order_line_ids) if active_order_line_ids else sa.false(),
            ),
        )
        .order_by(VinRegistry.assigned_at.desc().nullslast(), VinRegistry.id.desc())
        .all()
        if active_order_ids else []
    )

    return render_template(
        'manager_workspace.html',
        warehouse_id=warehouse_id,
        warehouses=warehouses,
        new_leads=new_leads,
        recent_conversations=recent_conversations,
        new_conversations_count=new_conversations_count,
        unread_messages_count=unread_messages_count,
        manager_needed_count=manager_needed_count,
        active_orders=active_orders,
        active_order_rows=active_order_rows,
        funnel_rows=funnel_rows,
        waiting_realization_rows=waiting_realization_rows,
        waiting_movement_rows=waiting_movement_rows,
        blocked_order_rows=blocked_order_rows,
        assigned_vins_to_confirm=assigned_vins_to_confirm,
        active_tab=active_tab,
    )


# ========= ПОЛЬЗОВАТЕЛИ (только админ) =========

@main_bp.route('/users')
@admin_required
def users_list():
    users = User.query.order_by(User.username).all()
    return render_template('users_list.html', users=users)


@main_bp.route('/users/new', methods=['GET', 'POST'])
@admin_required
def user_create():
    form = UserForm()
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не привязан —')] + [
        (w.id, w.name) for w in warehouses
    ]

    if form.validate_on_submit():
        user = User(
            username=form.username.data.strip(),
            full_name=form.full_name.data.strip(),
            role=form.role.data,
            warehouse_id=form.warehouse_id.data or None,
        )

        if form.password.data:
            user.set_password(form.password.data)
        else:
            flash('Пароль обязателен при создании пользователя', 'danger')
            return render_template('user_form.html', form=form, title='Новый пользователь')

        db.session.add(user)
        db.session.commit()
        flash('Пользователь создан', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('user_form.html', form=form, title='Новый пользователь')


@main_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    form = UserForm()

    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не привязан —')] + [
        (w.id, w.name) for w in warehouses
    ]

    if request.method == 'GET':
        form.username.data = user.username
        form.full_name.data = user.full_name
        form.role.data = user.role
        form.warehouse_id.data = user.warehouse_id or 0

    if form.validate_on_submit():
        user.username = form.username.data.strip()
        user.full_name = form.full_name.data.strip()
        user.role = form.role.data
        user.warehouse_id = form.warehouse_id.data or None

        if form.password.data:
            user.set_password(form.password.data)

        db.session.commit()
        flash('Пользователь обновлён', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('user_form.html', form=form, title='Редактирование пользователя')


@main_bp.route('/users/<int:user_id>/delete')
@admin_required
def user_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.username == 'admin':
        flash('Нельзя удалить главного администратора', 'danger')
        return redirect(url_for('main.users_list'))

    db.session.delete(user)
    db.session.commit()
    flash('Пользователь удалён', 'success')
    return redirect(url_for('main.users_list'))


# ========= ПРИЦЕПЫ =========
def _fill_trailer_form_choices(form: TrailerCreateForm):
    """Заполнить choices для склада и характеристик из номенклатуры Item."""
    # --- Склады ---
    form.warehouse_id.choices = [
        (w.id, w.name) for w in Warehouse.query.order_by(Warehouse.name).all()
    ]

    # --- Все активные модели прицепов ---
    items = Item.query.filter_by(item_type='TRAILER', is_active=True).all()

    size_body_values = sorted({i.size_body for i in items if i.size_body})
    axle_values      = sorted({i.axle_count for i in items if i.axle_count is not None})
    wheel_values     = sorted({i.wheel_radius for i in items if i.wheel_radius})
    board_values     = sorted({i.board_height_mm for i in items if i.board_height_mm is not None})
    tent_heights     = sorted({i.tent_hight_mm for i in items if i.tent_hight_mm is not None})

    # --- Размер кузова ---
    form.size_body.choices = [(v, v) for v in size_body_values]

    # --- Кол-во осей ---
    form.axle_count.choices = [(v, str(v)) for v in axle_values]

    # --- Размер колеса ---
    form.wheel_radius.choices = [(v, v) for v in wheel_values]

    # --- Высота борта ---
    form.board_height_mm.choices = [(str(v), str(v)) for v in board_values]

    # --- Высота тента ---
    # 0 = нет тента, остальные из матрицы (30, 60...)
    form.tent_height_mm.choices = [(0, 'Нет тента')] + [
        (int(v), f'{v} см') for v in tent_heights
    ]

    # По умолчанию пусть будет "нет тента"
    if form.tent_height_mm.data is None:
        form.tent_height_mm.data = 0

    # Подкатное колесо: по умолчанию "не важно"
    if form.has_jockey_wheel.data is None:
        form.has_jockey_wheel.data = 1   # по умолчанию "да"


@main_bp.route('/trailers')
@login_required
def trailers_list():
    """Список всех прицепов с фильтрами."""
    vin_filter = request.args.get('vin', '').strip()
    article_filter = request.args.get('article', '').strip()
    status_arg = request.args.get('status')
    status_filter = status_arg if status_arg is not None else ('IN_STOCK' if getattr(current_user, 'is_manager', False) else 'all')
    warehouse_id = request.args.get('warehouse_id', type=int)
    category_filter = (request.args.get('category') or ('light_trailer' if getattr(current_user, 'is_manager', False) else 'all')).strip()

    query = Trailer.query.join(Item).outerjoin(ProductCategory, ProductCategory.id == Item.product_category_id).join(Warehouse)

    if vin_filter:
        query = query.filter(Trailer.vin.ilike(f'%{vin_filter}%'))

    if article_filter:
        query = query.filter(Item.article.ilike(f'%{article_filter}%'))

    if status_filter and status_filter != 'all':
        query = query.filter(Trailer.status == status_filter)

    if warehouse_id:
        query = query.filter(Trailer.warehouse_id == warehouse_id)
    elif getattr(current_user, 'is_manager', False):
        source_warehouse_ids = [warehouse.id for warehouse in _trailer_source_warehouses()]
        query = query.filter(Trailer.warehouse_id.in_(source_warehouse_ids) if source_warehouse_ids else sa.false())

    if category_filter and category_filter != 'all':
        query = query.filter(ProductCategory.code == category_filter)

    trailers = query.order_by(Trailer.id.desc()).all()
    warehouses = _trailer_source_warehouses() if getattr(current_user, 'is_manager', False) else Warehouse.query.order_by(Warehouse.name).all()
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order, ProductCategory.name).all()

    return render_template(
        'trailers_list.html',
        trailers=trailers,
        warehouses=warehouses,
        vin_filter=vin_filter,
        article_filter=article_filter,
        status_filter=status_filter,
        warehouse_filter=warehouse_id,
        category_filter=category_filter,
        categories=categories,
    )



@main_bp.route('/trailers/new', methods=['GET', 'POST'])
@login_required
def trailer_create():
    if not current_user.is_admin:
        abort(403)
    form = TrailerCreateForm()
    _fill_trailer_form_choices(form)
    config = _config_with_defaults(_config_from_request_values(request.values))
    result = _build_config_result(config)

    if request.method == 'POST':
        if result.get('errors'):
            for error in result.get('errors') or []:
                flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Новый прицеп', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=None)
        item = get_or_create_configured_item(result)
        vin = (request.form.get('vin') or '').strip().upper()
        warehouse_id = request.form.get('warehouse_id', type=int)
        if not vin or not warehouse_id:
            flash('Укажите VIN и склад.', 'danger')
            return render_template('trailer_form.html', form=form, title='Новый прицеп', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=None)

        trailer = Trailer(
            vin=vin,
            item_id=item.id,
            warehouse_id=warehouse_id,
            manufacture_date=form.manufacture_date.data,
            status=request.form.get('status') or 'IN_STOCK',
        )
        db.session.add(trailer)
        db.session.commit()
        flash('Прицеп создан', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Новый прицеп', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=None)


@main_bp.route('/trailers/<int:trailer_id>/edit', methods=['GET', 'POST'])
@login_required
def trailer_edit(trailer_id):
    if not current_user.is_admin:
        abort(403)
    trailer = Trailer.query.get_or_404(trailer_id)

    form = TrailerCreateForm(
        vin=trailer.vin,
        warehouse_id=trailer.warehouse_id,
        manufacture_date=trailer.manufacture_date,
        status=trailer.status,
    )
    _fill_trailer_form_choices(form)
    base_config = _config_from_item(trailer.item)
    config = _config_with_defaults(_config_from_request_values(request.values) if request.method == 'POST' else base_config)
    result = _build_config_result(config)

    # при GET заполняем характеристики из текущего Item
    if request.method == 'GET' and trailer.item:
        form.size_body.data = trailer.item.size_body
        form.axle_count.data = trailer.item.axle_count
        form.wheel_radius.data = trailer.item.wheel_radius
        form.board_height_mm.data = (
            str(trailer.item.board_height_mm)
            if trailer.item.board_height_mm is not None else ''
        )

        # Высота тента
        if trailer.item.has_tent:
            form.tent_height_mm.data = trailer.item.tent_hight_mm or 0
        else:
            form.tent_height_mm.data = 0

        # Подкатное колесо: строго да / нет
        if trailer.item.has_jockey_wheel is True:
            form.has_jockey_wheel.data = 1
        elif trailer.item.has_jockey_wheel is False:
            form.has_jockey_wheel.data = 0
        else:
            # если в номенклатуре None — реши, как удобнее:
            form.has_jockey_wheel.data = 1  # например, по умолчанию "Да"



    if request.method == 'POST':
        if not _locked_config_base_matches(config, base_config):
            flash('Нельзя менять группу, тип кузова и размер кузова через эту форму.', 'danger')
            return render_template('trailer_form.html', form=form, title='Редактирование прицепа', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=base_config)
        if result.get('errors'):
            for error in result.get('errors') or []:
                flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Редактирование прицепа', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=base_config)
        item = get_or_create_configured_item(result)
        if not _item_base_matches_locked_item(item, trailer.item):
            flash('Нельзя менять размер рамы / кузова и количество осей.', 'danger')
            return render_template('trailer_form.html', form=form, title='Редактирование прицепа', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=base_config)

        trailer.vin = (request.form.get('vin') or '').strip().upper()
        trailer.warehouse_id = request.form.get('warehouse_id', type=int)
        trailer.manufacture_date = form.manufacture_date.data
        trailer.status = request.form.get('status') or trailer.status
        trailer.item_id = item.id

        db.session.commit()
        flash('Прицеп обновлён', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Редактирование прицепа', config_options=_trailer_config_form_context(), config=config, result=result, locked_config=base_config)


@main_bp.route('/trailers/<int:trailer_id>/change-item', methods=['GET', 'POST'])
@role_required('manager', 'director')
def trailer_change_item(trailer_id):
    trailer = Trailer.query.get_or_404(trailer_id)
    back_url = url_for('main.trailers_list', status='IN_STOCK', vin=trailer.vin or '') if current_user.is_manager else url_for('main.trailers_list', vin=trailer.vin or '')
    if current_user.is_manager and trailer.warehouse_id != current_user.warehouse_id:
        abort(403)
    ok, message = _can_change_trailer_item(trailer)
    if not ok:
        flash(message, 'danger')
        return redirect(back_url)

    form = FlaskForm()
    locked_config = _config_from_item(trailer.item)
    config = _config_with_defaults(_config_from_request_values(request.values) if request.method == 'POST' else locked_config)
    result = _build_config_result(config)

    if request.method == 'POST':
        if not _locked_config_base_matches(config, locked_config):
            flash('Нельзя менять группу, тип кузова и размер кузова. Измените только комплектацию.', 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию прицепа',
                current_item=trailer.item,
                target_label=f'Trailer #{trailer.id} {trailer.vin or ""}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        if result.get('errors'):
            for error in result.get('errors') or []:
                flash(error, 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию прицепа',
                current_item=trailer.item,
                target_label=f'Trailer #{trailer.id} {trailer.vin or ""}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        item = get_or_create_configured_item(result)
        if not _item_base_matches_locked_item(item, trailer.item):
            flash('Нельзя менять размер рамы / кузова и количество осей. Выберите комплектацию с тем же размером и осями.', 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию прицепа',
                current_item=trailer.item,
                target_label=f'Trailer #{trailer.id} {trailer.vin or ""}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        old_item = trailer.item
        old_label = old_item.article if old_item else trailer.item_id
        trailer.item_id = item.id
        for unit in ProducedUnit.query.filter_by(trailer_id=trailer.id).all():
            unit.item_id = item.id
        for reservation in Reservation.query.filter_by(trailer_id=trailer.id, status='ACTIVE').all():
            reservation.item_id = item.id
        for order in CustomerOrder.query.filter_by(trailer_id=trailer.id).all():
            if not order.documents_issued and not _order_is_shipped(order):
                order.item_id = item.id
                add_order_event(
                    order,
                    'comment_added',
                    old_value=str(old_label or ''),
                    new_value=item.article or str(item.id),
                    comment='Изменена комплектация закреплённого прицепа',
                )
        db.session.commit()
        flash('Комплектация прицепа обновлена.', 'success')
        return redirect(back_url)

    return render_template(
        'trailer_item_form.html',
        form=form,
        title='Изменить комплектацию прицепа',
        current_item=trailer.item,
        target_label=f'Trailer #{trailer.id} {trailer.vin or ""}',
        back_url=back_url,
        locked_base=True,
        locked_config=locked_config,
        config=config,
        config_options=_trailer_config_form_context(),
        result=result,
    )


@main_bp.route('/trailers/<int:trailer_id>/delete')
@login_required
def trailer_delete(trailer_id):
    if not current_user.is_admin:
        abort(403)
    trailer = Trailer.query.get_or_404(trailer_id)

    if getattr(current_user, 'is_manager', False) and trailer.warehouse_id != current_user.warehouse_id:
        flash('Нет доступа к этому прицепу', 'danger')
        return redirect(url_for('main.trailers_list'))

    # если есть договоры — не даём удалить
    if trailer.sales_contracts:
        flash('Нельзя удалить прицеп, по которому есть договоры', 'danger')
    else:
        db.session.delete(trailer)
        db.session.commit()
        flash('Прицеп удалён', 'success')

    return redirect(url_for('main.trailers_list'))


# ========= СКЛАДЫ =========

@main_bp.route('/warehouses', methods=['GET', 'POST'])
@login_required
def warehouses_list():
    """Список складов + форма добавления нового."""
    if not (current_user.is_admin or current_user.is_director):
        abort(403)

    form = WarehouseForm()
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order, ProductCategory.name).all()
    form.primary_product_category.choices = [('', '— не задано —')] + [(c.code, c.name) for c in categories]

    if request.method == 'POST' and not current_user.is_admin:
        abort(403)

    if form.validate_on_submit():
        name = form.name.data.strip()

        existing = Warehouse.query.filter_by(name=name).first()
        if existing:
            flash('Склад с таким названием уже существует', 'danger')
        else:
            wh = Warehouse(
                name=name,
                is_active=form.is_active.data,
                is_production=form.is_production.data,
                warehouse_kind=form.warehouse_kind.data or 'finished_goods',
                is_sales_point=bool(form.can_sell.data),
                can_sell=bool(form.can_sell.data),
                can_ship_to_customer=bool(form.can_ship_to_customer.data),
                primary_product_category=form.primary_product_category.data or None,
                product_category_scope=form.primary_product_category.data or None,
            )
            db.session.add(wh)
            db.session.commit()
            flash('Склад успешно добавлен', 'success')

        return redirect(url_for('main.warehouses_list'))

    warehouses = (
        Warehouse.query
        .order_by(Warehouse.is_active.desc(), Warehouse.name)
        .all()
    )

    return render_template('warehouses.html', form=form, warehouses=warehouses)


@main_bp.route('/warehouses/<int:warehouse_id>/production-flag', methods=['POST'])
@admin_required
def warehouse_set_production_flag(warehouse_id):
    warehouse = Warehouse.query.get_or_404(warehouse_id)
    warehouse.is_production = request.form.get('is_production') == '1'
    db.session.commit()
    flash('Признак производственного склада обновлён', 'success')
    return redirect(url_for('main.warehouses_list'))

# ========= ОТТС =========

@main_bp.route('/otts')
@login_required
def otts_list():
    otts_list = (
        OTTS.query
        .order_by(OTTS.modification, OTTS.axle_count)
        .all()
    )
    return render_template(
        'otts_list.html',
        otts_list=otts_list,
        title='Справочник ОТТС'
    )


@main_bp.route('/otts/new', methods=['GET', 'POST'])
@login_required
def otts_create():
    form = OTTSForm()

    if form.validate_on_submit():
        otts = OTTS(
            number=form.number.data.strip(),
            date=form.date.data,
            modification=form.modification.data.strip(),
            name=form.name.data.strip(),
            axle_count=form.axle_count.data,
            full_mass_kg=form.full_mass_kg.data,
            is_active=form.is_active.data,
        )
        db.session.add(otts)
        db.session.commit()
        flash('Запись ОТТС создана', 'success')
        return redirect(url_for('main.otts_list'))

    return render_template(
        'otts_form.html',
        form=form,
        title='Новое ОТТС'
    )


@main_bp.route('/otts/<int:otts_id>/edit', methods=['GET', 'POST'])
@login_required
def otts_edit(otts_id):
    otts = OTTS.query.get_or_404(otts_id)
    form = OTTSForm(obj=otts)

    if form.validate_on_submit():
        # Заполняем объект из формы
        form.populate_obj(otts)

        # Чуть подчистим строки
        otts.number = (otts.number or '').strip()
        otts.modification = (otts.modification or '').strip()
        otts.name = (otts.name or '').strip()

        db.session.commit()
        flash('Запись ОТТС обновлена', 'success')
        return redirect(url_for('main.otts_list'))

    return render_template(
        'otts_form.html',
        form=form,
        title='Редактирование ОТТС'
    )


@main_bp.route('/otts/<int:otts_id>/delete')
@admin_required
def otts_delete(otts_id):
    otts = OTTS.query.get_or_404(otts_id)
    db.session.delete(otts)
    db.session.commit()
    flash('Запись ОТТС удалена', 'success')
    return redirect(url_for('main.otts_list'))

def find_trailer_item_by_features(
    axle_count: int | None = None,
    board_height_mm: int | None = None,
    wheel_radius: str | None = None,
    has_tent: bool | None = None,
    tent_hight_mm: int | None = None,   # имя как в модели
    has_jockey_wheel: bool | None = None,
):
    """
    Ищет модель прицепа (Item) по набору характеристик.
    Артикул сам по себе не вводим — он берётся из найденного Item.
    """
    q = Item.query.filter_by(item_type='TRAILER', is_active=True)

    if axle_count is not None:
        q = q.filter(Item.axle_count == axle_count)

    if board_height_mm is not None:
        q = q.filter(Item.board_height_mm == board_height_mm)

    if wheel_radius:
        q = q.filter(Item.wheel_radius == wheel_radius)

    if has_tent is not None:
        q = q.filter(Item.has_tent == has_tent)

    if tent_hight_mm is not None:
        q = q.filter(Item.tent_hight_mm == tent_hight_mm)

    if has_jockey_wheel is not None:
        q = q.filter(Item.has_jockey_wheel == has_jockey_wheel)

    return q.first()   # можно потом усложнить (если несколько совпадений)

# ========= НОМЕНКЛАТУРА =========

@main_bp.route('/items')
@login_required
def items_list():
    """Список номенклатуры (прицепы + комплектующие)."""
    items = (
        Item.query
        .order_by(Item.item_type, Item.article, Item.name)
        .all()
    )
    return render_template('items_list.html', items=items)


def _fill_item_form_choices(form: ItemForm) -> None:
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order, ProductCategory.name).all()
    form.product_category_id.choices = [(0, '— по умолчанию —')] + [(c.id, c.name) for c in categories]


@main_bp.route('/items/new', methods=['GET', 'POST'])
@login_required
def item_create():
    """Создание новой позиции номенклатуры."""
    form = ItemForm()
    _fill_item_form_choices(form)
    if request.method == 'GET':
        form.requires_vin.data = True

    if form.validate_on_submit():
        item_type = form.item_type.data
        article = form.article.data.strip() if form.article.data else None
        category = ProductCategory.query.get(form.product_category_id.data) if form.product_category_id.data else _default_product_category_for_item_type(item_type)

        # Для прицепов артикул обязателен
        if item_type == 'TRAILER' and not article:
            flash('Для прицепа обязательно укажите артикул', 'danger')
            return render_template('item_form.html', form=form)

        # Проверка уникальности (тип + артикул)
        if article:
            existing = Item.query.filter_by(article=article, item_type=item_type).first()
            if existing:
                flash('Такая позиция номенклатуры уже существует (артикул + тип)', 'danger')
                return render_template('item_form.html', form=form)

        # --- Тент: главный источник истины — высота тента ---
        tent_h = form.tent_height_mm.data  # может быть None или int

        if tent_h in (None, 0):
            has_tent = False
            tent_value = None
        else:
            has_tent = True
            tent_value = tent_h

        # Подкатное колесо: yes/no -> True/False/None
        has_jockey_wheel = None
        if form.has_jockey_wheel.data == 'yes':
            has_jockey_wheel = True
        elif form.has_jockey_wheel.data == 'no':
            has_jockey_wheel = False

        item = Item(
            item_type=item_type,
            article=article,
            name=form.name.data.strip(),
            axle_count=form.axle_count.data,
            board_height_mm=form.board_height_mm.data,
            wheel_radius=form.wheel_radius.data.strip() if form.wheel_radius.data else None,
            has_tent=has_tent,
            tent_hight_mm=tent_value,  # <--- ключевой момент
            has_jockey_wheel=has_jockey_wheel,
            size_external=form.size_external.data.strip() if form.size_external.data else None,
            size_body=form.size_body.data.strip() if form.size_body.data else None,
            base_price=form.base_price.data,
            unit=form.unit.data.strip() if form.unit.data else 'шт',
            product_category_id=category.id if category else None,
            is_sellable=bool(form.is_sellable.data),
            requires_vin=bool(form.requires_vin.data if form.requires_vin.data is not None else item_type == 'TRAILER'),
            is_active=form.is_active.data,
        )
        db.session.add(item)
        db.session.commit()

        flash('Позиция номенклатуры успешно добавлена', 'success')
        return redirect(url_for('main.items_list'))

    return render_template('item_form.html', form=form)



@main_bp.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def item_edit(item_id):
    item = Item.query.get_or_404(item_id)
    form = ItemForm()
    _fill_item_form_choices(form)

    if request.method == 'GET':
        form.item_type.data = item.item_type
        form.product_category_id.data = item.product_category_id or 0
        form.is_sellable.data = item.is_sellable
        form.requires_vin.data = item.requires_vin
        form.article.data = item.article
        form.name.data = item.name
        form.axle_count.data = item.axle_count
        form.board_height_mm.data = item.board_height_mm
        form.wheel_radius.data = item.wheel_radius

        form.has_tent.data = (
            'yes' if item.has_tent is True else
            'no' if item.has_tent is False else
            ''
        )

        # Высота тента в форму
        form.tent_height_mm.data = item.tent_hight_mm or 0

        form.has_jockey_wheel.data = (
            'yes' if item.has_jockey_wheel is True else
            'no' if item.has_jockey_wheel is False else
            ''
        )

        form.size_external.data = item.size_external
        form.size_body.data = item.size_body
        form.base_price.data = item.base_price
        form.unit.data = item.unit
        form.is_active.data = item.is_active

    if form.validate_on_submit():
        item_type = form.item_type.data
        article = form.article.data.strip() if form.article.data else None
        category = ProductCategory.query.get(form.product_category_id.data) if form.product_category_id.data else _default_product_category_for_item_type(item_type)

        if item_type == 'TRAILER' and not article:
            flash('Для прицепа обязательно укажите артикул', 'danger')
            return render_template('item_form.html', form=form, title='Редактирование номенклатуры')

        # Проверка уникальности при изменении типа/артикула
        if article and (article != item.article or item_type != item.item_type):
            existing = Item.query.filter_by(article=article, item_type=item_type).first()
            if existing and existing.id != item.id:
                flash('Такая позиция номенклатуры уже существует (артикул + тип)', 'danger')
                return render_template('item_form.html', form=form, title='Редактирование номенклатуры')

        # --- Тент: снова опираемся только на высоту ---
        tent_h = form.tent_height_mm.data

        if tent_h in (None, 0):
            item.has_tent = False
            item.tent_hight_mm = None
        else:
            item.has_tent = True
            item.tent_hight_mm = tent_h

        # Подкатное колесо
        has_jockey_wheel = None
        if form.has_jockey_wheel.data == 'yes':
            has_jockey_wheel = True
        elif form.has_jockey_wheel.data == 'no':
            has_jockey_wheel = False

        item.item_type = item_type
        item.product_category_id = category.id if category else None
        item.is_sellable = bool(form.is_sellable.data)
        item.requires_vin = bool(form.requires_vin.data if form.requires_vin.data is not None else item_type == 'TRAILER')
        item.article = article
        item.name = form.name.data.strip()
        item.axle_count = form.axle_count.data
        item.board_height_mm = form.board_height_mm.data
        item.wheel_radius = form.wheel_radius.data.strip() if form.wheel_radius.data else None
        item.has_jockey_wheel = has_jockey_wheel
        item.size_external = form.size_external.data.strip() if form.size_external.data else None
        item.size_body = form.size_body.data.strip() if form.size_body.data else None
        item.base_price = form.base_price.data
        item.unit = form.unit.data.strip() if form.unit.data else 'шт'
        item.is_active = form.is_active.data

        db.session.commit()
        flash('Позиция номенклатуры обновлена', 'success')
        return redirect(url_for('main.items_list'))

    return render_template('item_form.html', form=form, title='Редактирование номенклатуры')



@main_bp.route('/items/<int:item_id>/delete')
@login_required
def item_delete(item_id):
    item = Item.query.get_or_404(item_id)

    # если к позиции привязаны прицепы — лучше не удалять
    if item.trailers:  # relationship Trailer.item
        flash('Нельзя удалить номенклатуру, к которой привязаны прицепы', 'danger')
        return redirect(url_for('main.items_list'))

    db.session.delete(item)
    db.session.commit()
    flash('Позиция номенклатуры удалена', 'success')
    return redirect(url_for('main.items_list'))


# ========= СПРАВОЧНИКИ КОНФИГУРАТОРА ПРИЦЕПОВ =========

TRAILER_CATALOG_SECTIONS = {
    'groups': {
        'title': 'Группы', 'model': TrailerProductGroup,
        'fields': ['code', 'name', 'name_prefix', 'otss_number', 'otss_type', 'otts_valid_from', 'otts_valid_to', 'vehicle_category', 'product_category_id', 'axle_count', 'wheel_count', 'max_mass_kg', 'is_active', 'sort_order', 'comment'],
    },
    'body-sizes': {
        'title': 'Размеры кузова', 'model': TrailerBodySize,
        'fields': ['code', 'name', 'length_mm', 'width_mm', 'article_part', 'is_active', 'sort_order', 'comment'],
    },
    'board-heights': {
        'title': 'Высота борта', 'model': TrailerBoardHeight,
        'fields': ['code', 'name', 'height_mm', 'article_part', 'is_no_board', 'is_active', 'sort_order', 'comment'],
    },
    'wheels': {
        'title': 'Колёса', 'model': TrailerWheelOption,
        'fields': ['code', 'name', 'wheel_size', 'article_part', 'is_active', 'sort_order', 'comment'],
    },
    'hubs': {
        'title': 'Ступицы', 'model': TrailerHubOption,
        'fields': ['code', 'name', 'for_wheel_size', 'article_part', 'is_active', 'sort_order', 'comment'],
    },
    'support-wheels': {
        'title': 'Опорное колесо', 'model': TrailerSupportWheelOption,
        'fields': ['code', 'name', 'article_part', 'is_default', 'is_active', 'sort_order', 'comment'],
    },
    'tents': {
        'title': 'Тенты', 'model': TrailerTentOption,
        'fields': ['code', 'name', 'height_mm', 'article_part', 'is_no_tent', 'is_active', 'sort_order', 'comment'],
    },
    'body-executions': {
        'title': 'Типы кузова', 'model': TrailerBodyExecution,
        'fields': ['code', 'name', 'name_for_title', 'is_active', 'sort_order', 'comment'],
    },
    'special-options': {
        'title': 'Спец. параметры', 'model': TrailerSpecialOption,
        'fields': ['code', 'name', 'option_type', 'article_part', 'is_active', 'sort_order', 'comment'],
    },
    'allowed-options': {
        'title': 'Допустимость параметров', 'model': TrailerAllowedOption,
        'fields': ['group_id', 'option_type', 'option_id', 'is_allowed', 'is_default', 'sort_order', 'comment'],
    },
    'platform-prices': {
        'title': 'Цены платформ', 'model': TrailerPlatformPriceMatrix,
        'fields': ['group_id', 'body_size_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'board-prices': {
        'title': 'Цены бортов', 'model': TrailerBoardPriceMatrix,
        'fields': ['body_size_id', 'board_height_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'tent-prices': {
        'title': 'Цены тентов', 'model': TrailerTentPriceMatrix,
        'fields': ['body_size_id', 'tent_option_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'wheel-prices': {
        'title': 'Цены колёс', 'model': TrailerWheelPriceMatrix,
        'fields': ['group_id', 'wheel_option_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'hub-prices': {
        'title': 'Цены ступиц', 'model': TrailerHubPriceMatrix,
        'fields': ['group_id', 'hub_option_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'support-wheel-prices': {
        'title': 'Цена опорного колеса', 'model': TrailerSupportWheelPriceMatrix,
        'fields': ['support_wheel_option_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'special-prices': {
        'title': 'Цены спец. параметров', 'model': TrailerSpecialOptionPriceMatrix,
        'fields': ['group_id', 'special_option_id', 'price', 'cost_price', 'currency', 'valid_from', 'valid_to', 'is_active', 'comment'],
    },
    'dimensions': {
        'title': 'Габариты', 'model': TrailerDimensionMatrix,
        'fields': ['body_size_id', 'board_height_id', 'overall_length_mm', 'overall_width_mm', 'overall_height_mm', 'inner_length_mm', 'inner_width_mm', 'inner_height_mm', 'overall_dimensions_text', 'inner_dimensions_text', 'is_active', 'comment'],
    },
    'otss': {
        'title': 'ОТТС / VIN-модификации', 'model': TrailerOtssModificationMatrix,
        'fields': ['group_id', 'body_execution_id', 'board_height_id', 'special_option_id', 'otss_number', 'otss_type', 'otss_modification', 'vin_modification_code', 'otts_valid_from', 'otts_valid_to', 'description', 'is_active', 'sort_order', 'comment'],
    },
}

TRAILER_CATALOG_FIELD_LABELS = {
    'code': 'Код', 'name': 'Наименование', 'name_prefix': 'Префикс наименования', 'otss_number': 'Номер ОТТС',
    'otss_type': 'Тип ОТТС', 'vehicle_category': 'ОТТС-категория', 'product_category_id': 'Категория продукции', 'axle_count': 'Оси', 'wheel_count': 'Колёса',
    'max_mass_kg': 'Макс. масса, кг', 'length_mm': 'Длина, мм', 'width_mm': 'Ширина, мм',
    'article_part': 'Код в артикуле', 'height_mm': 'Высота, мм', 'is_no_board': 'Без борта',
    'wheel_size': 'Размер колеса', 'for_wheel_size': 'Под размер', 'is_default': 'По умолчанию',
    'is_no_tent': 'Без тента', 'name_for_title': 'Для наименования', 'option_type': 'Тип параметра',
    'group_id': 'Группа', 'option_id': 'Параметр', 'is_allowed': 'Разрешён', 'body_size_id': 'Размер кузова',
    'board_height_id': 'Высота борта', 'price': 'Цена', 'cost_price': 'Себестоимость', 'currency': 'Валюта',
    'valid_from': 'Действует с', 'valid_to': 'Действует до', 'wheel_option_id': 'Колёса',
    'hub_option_id': 'Ступица', 'support_wheel_option_id': 'Опорное колесо', 'tent_option_id': 'Тент',
    'special_option_id': 'Спец. параметр',
    'overall_length_mm': 'Габаритная длина', 'overall_width_mm': 'Габаритная ширина',
    'overall_height_mm': 'Габаритная высота', 'inner_length_mm': 'Внутренняя длина',
    'inner_width_mm': 'Внутренняя ширина', 'inner_height_mm': 'Внутренняя высота',
    'overall_dimensions_text': 'Габариты текстом', 'inner_dimensions_text': 'Внутренние размеры текстом',
    'body_execution_id': 'Тип кузова',
    'otss_modification': 'Модификация ОТТС', 'vin_modification_code': 'VIN-модификация',
    'otts_valid_from': 'Дата начала ОТТС', 'otts_valid_to': 'Дата окончания ОТТС',
    'description': 'Описание', 'is_active': 'Активен', 'sort_order': 'Сортировка', 'comment': 'Комментарий',
}

TRAILER_CATALOG_FK_MODELS = {
    'group_id': TrailerProductGroup,
    'product_category_id': ProductCategory,
    'body_size_id': TrailerBodySize,
    'board_height_id': TrailerBoardHeight,
    'wheel_option_id': TrailerWheelOption,
    'hub_option_id': TrailerHubOption,
    'support_wheel_option_id': TrailerSupportWheelOption,
    'tent_option_id': TrailerTentOption,
    'body_execution_id': TrailerBodyExecution,
    'special_option_id': TrailerSpecialOption,
}

TRAILER_CATALOG_BOOL_FIELDS = {'is_active', 'is_no_board', 'is_default', 'is_no_tent', 'is_allowed'}
TRAILER_CATALOG_INT_FIELDS = {
    'axle_count', 'wheel_count', 'max_mass_kg', 'length_mm', 'width_mm', 'height_mm', 'sort_order', 'option_id',
    'overall_length_mm', 'overall_width_mm', 'overall_height_mm', 'inner_length_mm', 'inner_width_mm', 'inner_height_mm',
}
TRAILER_CATALOG_MONEY_FIELDS = {'price', 'cost_price'}
TRAILER_CATALOG_DATE_FIELDS = {'valid_from', 'valid_to', 'otts_valid_from', 'otts_valid_to'}

TRAILER_ALLOWED_OPTION_GROUPS = [
    ('body_size', 'Размеры кузова', TrailerBodySize),
    ('board_height', 'Высота борта', TrailerBoardHeight),
    ('wheel', 'Колёса', TrailerWheelOption),
    ('hub', 'Ступицы', TrailerHubOption),
    ('support_wheel', 'Опорное колесо', TrailerSupportWheelOption),
    ('tent', 'Тенты', TrailerTentOption),
    ('body_execution', 'Типы кузова', TrailerBodyExecution),
    ('special', 'Спец. параметры', TrailerSpecialOption),
]


def _catalog_sections_for_template():
    sections = [{'key': key, 'title': value['title']} for key, value in TRAILER_CATALOG_SECTIONS.items() if key != 'allowed-options']
    sections.insert(9, {'key': 'allowed-matrix', 'title': 'Допустимость параметров'})
    return sections


def _catalog_label(obj):
    if not obj:
        return ''
    code = getattr(obj, 'code', None)
    name = getattr(obj, 'name', None)
    if code and name:
        return f'{code} - {name}'
    if code:
        return code
    return str(getattr(obj, 'id', ''))


def _catalog_field_value(obj, field):
    value = getattr(obj, field, None)
    if field in TRAILER_CATALOG_FK_MODELS:
        related = getattr(obj, field[:-3], None)
        return _catalog_label(related) if related else value
    if field == 'option_id' and getattr(obj, 'option_type', None):
        model = {
            'body_size': TrailerBodySize, 'board_height': TrailerBoardHeight, 'wheel': TrailerWheelOption,
            'hub': TrailerHubOption, 'support_wheel': TrailerSupportWheelOption, 'tent': TrailerTentOption,
            'body_execution': TrailerBodyExecution, 'special': TrailerSpecialOption,
        }.get(obj.option_type)
        related = model.query.get(value) if model and value else None
        return _catalog_label(related) if related else value
    if field in TRAILER_CATALOG_BOOL_FIELDS:
        return 'Да' if value else 'Нет'
    if field in TRAILER_CATALOG_MONEY_FIELDS and value is not None:
        return f'{float(value):.0f}'
    return value if value is not None else ''


def _catalog_choices(field):
    model = TRAILER_CATALOG_FK_MODELS.get(field)
    if not model:
        return []
    return model.query.order_by(getattr(model, 'sort_order', model.id), model.id).all()


def _catalog_text_choices(section_key, field):
    if section_key not in ('groups', 'otss'):
        return []
    if field == 'otss_number':
        return [
            {'value': row.number, 'label': f'{row.number} — мод. {row.modification} — {row.name}'}
            for row in OTTS.query.filter_by(is_active=True).order_by(OTTS.number, OTTS.modification, OTTS.name).all()
        ]
    if section_key == 'otss' and field == 'otss_modification':
        rows = (
            OTTS.query
            .filter_by(is_active=True)
            .order_by(OTTS.modification, OTTS.number)
            .all()
        )
        seen = set()
        choices = []
        for row in rows:
            if row.modification in seen:
                continue
            seen.add(row.modification)
            choices.append({'value': row.modification, 'label': row.modification})
        return choices
    return []


def _apply_catalog_form(obj, fields):
    for field in fields:
        if field in TRAILER_CATALOG_BOOL_FIELDS:
            setattr(obj, field, field in request.form)
            continue
        raw = request.form.get(field)
        if raw == '':
            raw = None
        if field in TRAILER_CATALOG_INT_FIELDS or field.endswith('_id'):
            value = int(raw) if raw not in (None, '') else None
        elif field in TRAILER_CATALOG_MONEY_FIELDS:
            if raw is None:
                value = None
            else:
                cleaned = raw.replace(' ', '').replace('\xa0', '').replace(',', '.')
                try:
                    value = cleaned if cleaned == '' else Decimal(cleaned)
                except Exception:
                    label = TRAILER_CATALOG_FIELD_LABELS.get(field, field)
                    raise ValueError(f'Поле "{label}" должно быть числом. Примеры: 30 000 или 30,5')
        elif field in TRAILER_CATALOG_DATE_FIELDS:
            value = datetime.strptime(raw, '%Y-%m-%d').date() if raw else None
        else:
            value = raw
        setattr(obj, field, value)


@main_bp.route('/catalog/trailer-config')
@role_required('director')
def trailer_config_catalog():
    return redirect(url_for('main.trailer_config_catalog_section', section='groups'))


@main_bp.route('/catalog/trailer-config/<section>')
@role_required('director')
def trailer_config_catalog_section(section):
    if section == 'allowed-matrix':
        return redirect(url_for('main.trailer_config_allowed_matrix'))
    cfg = TRAILER_CATALOG_SECTIONS.get(section)
    if not cfg:
        abort(404)
    model = cfg['model']
    rows = model.query.order_by(getattr(model, 'sort_order', model.id), model.id).all()
    return render_template(
        'trailer_config_catalog.html',
        sections=_catalog_sections_for_template(),
        active_section=section,
        section=cfg,
        rows=rows,
        field_labels=TRAILER_CATALOG_FIELD_LABELS,
        value_getter=_catalog_field_value,
    )


def _allowed_option_checkbox_groups(group):
    blocks = []
    for option_type, title, model in TRAILER_ALLOWED_OPTION_GROUPS:
        options = model.query.filter_by(is_active=True).order_by(model.sort_order, model.id).all()
        rows = {
            row.option_id: row for row in TrailerAllowedOption.query.filter_by(
                group_id=group.id,
                option_type=option_type,
            ).all()
        }
        blocks.append({
            'option_type': option_type,
            'title': title,
            'options': options,
            'allowed_ids': {option_id for option_id, row in rows.items() if row.is_allowed},
        })
    return blocks


@main_bp.route('/catalog/trailer-config/allowed-matrix', methods=['GET', 'POST'])
@role_required('director')
def trailer_config_allowed_matrix():
    groups = TrailerProductGroup.query.filter_by(is_active=True).order_by(TrailerProductGroup.sort_order).all()
    requested_group = request.values.get('group_code')
    group = TrailerProductGroup.query.filter_by(code=requested_group).first() if requested_group else (groups[0] if groups else None)
    form = FlaskForm()

    if group and form.validate_on_submit():
        for option_type, _, model in TRAILER_ALLOWED_OPTION_GROUPS:
            selected_ids = {
                int(value) for value in request.form.getlist(f'{option_type}[]')
                if value and value.isdigit()
            }
            options = model.query.filter_by(is_active=True).all()
            for option in options:
                row = TrailerAllowedOption.query.filter_by(
                    group_id=group.id,
                    option_type=option_type,
                    option_id=option.id,
                ).first()
                if row is None:
                    row = TrailerAllowedOption(
                        group_id=group.id,
                        option_type=option_type,
                        option_id=option.id,
                        sort_order=getattr(option, 'sort_order', 0),
                    )
                    db.session.add(row)
                row.is_allowed = option.id in selected_ids
                row.is_default = False
                row.sort_order = getattr(option, 'sort_order', 0)
        db.session.commit()
        flash('Матрица допустимости сохранена', 'success')
        return redirect(url_for('main.trailer_config_allowed_matrix', group_code=group.code))

    return render_template(
        'trailer_config_allowed_matrix.html',
        form=form,
        sections=_catalog_sections_for_template(),
        active_section='allowed-matrix',
        groups=groups,
        selected_group=group,
        blocks=_allowed_option_checkbox_groups(group) if group else [],
    )


@main_bp.route('/catalog/trailer-config/<section>/new', methods=['GET', 'POST'])
@role_required('director')
def trailer_config_catalog_create(section):
    cfg = TRAILER_CATALOG_SECTIONS.get(section)
    if not cfg:
        abort(404)
    form = FlaskForm()
    if form.validate_on_submit():
        obj = cfg['model']()
        try:
            _apply_catalog_form(obj, cfg['fields'])
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'trailer_config_catalog_form.html',
                form=form,
                sections=_catalog_sections_for_template(),
                active_section=section,
                section=cfg,
                obj=obj,
                field_labels=TRAILER_CATALOG_FIELD_LABELS,
                choices_getter=_catalog_choices,
                text_choices_getter=_catalog_text_choices,
                bool_fields=TRAILER_CATALOG_BOOL_FIELDS,
                date_fields=TRAILER_CATALOG_DATE_FIELDS,
            )
        db.session.add(obj)
        try:
            db.session.commit()
            flash('Запись добавлена', 'success')
            return redirect(url_for('main.trailer_config_catalog_section', section=section))
        except IntegrityError:
            db.session.rollback()
            flash('Не удалось сохранить: проверьте уникальность кода или комбинации полей', 'danger')
    return render_template(
        'trailer_config_catalog_form.html',
        form=form,
        sections=_catalog_sections_for_template(),
        active_section=section,
        section=cfg,
        obj=None,
        field_labels=TRAILER_CATALOG_FIELD_LABELS,
        choices_getter=_catalog_choices,
        text_choices_getter=_catalog_text_choices,
        bool_fields=TRAILER_CATALOG_BOOL_FIELDS,
        date_fields=TRAILER_CATALOG_DATE_FIELDS,
    )


@main_bp.route('/catalog/trailer-config/<section>/<int:row_id>/edit', methods=['GET', 'POST'])
@role_required('director')
def trailer_config_catalog_edit(section, row_id):
    cfg = TRAILER_CATALOG_SECTIONS.get(section)
    if not cfg:
        abort(404)
    obj = cfg['model'].query.get_or_404(row_id)
    form = FlaskForm()
    if form.validate_on_submit():
        try:
            _apply_catalog_form(obj, cfg['fields'])
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'trailer_config_catalog_form.html',
                form=form,
                sections=_catalog_sections_for_template(),
                active_section=section,
                section=cfg,
                obj=obj,
                field_labels=TRAILER_CATALOG_FIELD_LABELS,
                choices_getter=_catalog_choices,
                text_choices_getter=_catalog_text_choices,
                bool_fields=TRAILER_CATALOG_BOOL_FIELDS,
                date_fields=TRAILER_CATALOG_DATE_FIELDS,
            )
        try:
            db.session.commit()
            flash('Запись обновлена', 'success')
            return redirect(url_for('main.trailer_config_catalog_section', section=section))
        except IntegrityError:
            db.session.rollback()
            flash('Не удалось сохранить: проверьте уникальность кода или комбинации полей', 'danger')
    return render_template(
        'trailer_config_catalog_form.html',
        form=form,
        sections=_catalog_sections_for_template(),
        active_section=section,
        section=cfg,
        obj=obj,
        field_labels=TRAILER_CATALOG_FIELD_LABELS,
        choices_getter=_catalog_choices,
        text_choices_getter=_catalog_text_choices,
        bool_fields=TRAILER_CATALOG_BOOL_FIELDS,
        date_fields=TRAILER_CATALOG_DATE_FIELDS,
    )


@main_bp.route('/catalog/trailer-config/<section>/<int:row_id>/toggle', methods=['POST'])
@role_required('director')
def trailer_config_catalog_toggle(section, row_id):
    cfg = TRAILER_CATALOG_SECTIONS.get(section)
    if not cfg:
        abort(404)
    obj = cfg['model'].query.get_or_404(row_id)
    if not hasattr(obj, 'is_active'):
        flash('У этой записи нет признака активности', 'warning')
        return redirect(url_for('main.trailer_config_catalog_section', section=section))
    obj.is_active = not obj.is_active
    db.session.commit()
    flash('Статус активности изменён', 'success')
    return redirect(url_for('main.trailer_config_catalog_section', section=section))


@main_bp.route('/catalog/trailer-config/test', methods=['GET', 'POST'])
@role_required('director', 'manager')
def trailer_config_test():
    from trailer_configurator import build_trailer_configuration_result

    config = {
        'group_code': request.values.get('group_code', '002'),
        'body_size_code': request.values.get('body_size_code', '2515'),
        'board_height_code': request.values.get('board_height_code', 'E50'),
        'wheel_code': request.values.get('wheel_code', 'Q14'),
        'hub_code': request.values.get('hub_code', ''),
        'support_wheel_code': request.values.get('support_wheel_code', 'OK'),
        'tent_code': request.values.get('tent_code', '90'),
        'body_execution_code': request.values.get('body_execution_code', 'BOARD'),
        'special_options': request.values.getlist('special_options'),
    }
    result = build_trailer_configuration_result(config)
    selected_group = TrailerProductGroup.query.filter_by(code=config['group_code']).first()
    config_options = _filtered_trailer_config_options(config)
    body_sizes = config_options['body_sizes']
    board_heights = config_options['board_heights']
    wheels = config_options['wheels']
    hubs = config_options['hubs']
    support_wheels = config_options['support_wheels']
    tents = config_options['tents']
    executions = config_options['executions']
    specials = config_options['specials']
    option_warnings = list(config_options.get('option_warnings') or [])

    def warn_if_selected_unavailable(label, selected_code, options):
        if not selected_code:
            return
        available_codes = {option.code for option in options}
        if selected_code not in available_codes:
            option_warnings.append(f'Выбранный параметр {label} "{selected_code}" недоступен для текущей конфигурации.')

    warn_if_selected_unavailable('размер кузова', config['body_size_code'], body_sizes)
    warn_if_selected_unavailable('высота борта', config['board_height_code'], board_heights)
    warn_if_selected_unavailable('колесо', config['wheel_code'], wheels)
    warn_if_selected_unavailable('ступица', config['hub_code'], hubs)
    warn_if_selected_unavailable('опорное колесо', config['support_wheel_code'], support_wheels)
    warn_if_selected_unavailable('тент', config['tent_code'], tents)
    warn_if_selected_unavailable('тип кузова', config['body_execution_code'], executions)
    special_codes = {option.code for option in specials}
    for special_code in config['special_options']:
        if special_code not in special_codes:
            option_warnings.append(f'Выбранный параметр спец. параметр "{special_code}" недоступен для текущей конфигурации.')

    return render_template(
        'trailer_config_test.html',
        config=config,
        result=result,
        option_warnings=option_warnings,
        groups=TrailerProductGroup.query.filter_by(is_active=True).order_by(TrailerProductGroup.sort_order).all(),
        body_sizes=body_sizes,
        board_heights=board_heights,
        wheels=wheels,
        hubs=hubs,
        support_wheels=support_wheels,
        tents=tents,
        executions=executions,
        specials=specials,
    )


@main_bp.route('/manager/trailer-picker', methods=['GET', 'POST'])
@role_required('director', 'manager')
def manager_trailer_picker():
    from trailer_configurator import build_trailer_configuration_result

    target_order_id = request.values.get('order_id', type=int)
    target_order = CustomerOrder.query.get(target_order_id) if target_order_id else None
    if target_order:
        _ensure_can_manage_order(target_order)
        if target_order.status == 'cancelled' or target_order.documents_issued or target_order.is_shipped:
            flash('Позиции можно менять только до выдачи документов и отгрузки.', 'danger')
            return redirect(url_for('main.order_detail', order_id=target_order.id))

    config = _config_from_request_values(request.values)
    if not config.get('body_size_code'):
        config.update({
            'group_code': config.get('group_code') or '002',
            'body_execution_code': config.get('body_execution_code') or 'BOARD',
            'body_size_code': '2515',
            'board_height_code': 'E50',
            'wheel_code': 'Q14',
            'support_wheel_code': 'OK',
            'tent_code': '90',
        })
    result = build_trailer_configuration_result(config)
    result['config'] = config
    snapshot = _snapshot_from_result(result)
    inventory = _trailer_inventory_rows(result.get('article'), config.get('body_size_code'), config.get('board_height_code'), config.get('group_code'))
    redirect_params = dict(config)
    if target_order:
        redirect_params['order_id'] = target_order.id

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        warehouse_id = request.form.get('warehouse_id', type=int) or (target_order.warehouse_id if target_order else None) or (current_user.warehouse_id if current_user.is_manager else None)
        customer_id = request.form.get('customer_id', type=int) or (target_order.customer_id if target_order else None)
        if action in ('stock_order', 'production_order') and not customer_id:
            flash('Выберите клиента для создания заказа.', 'danger')
            return redirect(url_for('main.manager_trailer_picker', **redirect_params))

        if action == 'stock_order':
            trailer = Trailer.query.get(request.form.get('trailer_id', type=int))
            if not trailer or not _trailer_available_for_sale(trailer):
                flash('Выбранный прицеп уже недоступен для продажи.', 'danger')
                return redirect(url_for('main.manager_trailer_picker', **redirect_params))
            idem_key, duplicate = _reserve_idempotency_key()
            if duplicate:
                duplicate_url = url_for('main.order_detail', order_id=target_order.id) if target_order else url_for('main.orders_list')
                return _duplicate_redirect(idem_key, duplicate_url)
            if target_order:
                source_type = 'TRANSFER' if target_order.warehouse_id and trailer.warehouse_id != target_order.warehouse_id else 'STOCK'
                order_line = _create_order_line_from_trailer(target_order, trailer, source_type)
                if not target_order.item_id:
                    target_order.item_id = trailer.item_id
                if not target_order.trailer_id:
                    target_order.trailer_id = trailer.id
                    target_order.source_warehouse_id = trailer.warehouse_id
                target_order.fulfillment_source = target_order.fulfillment_source or ('stock' if source_type == 'STOCK' else 'other_warehouse')
                _sync_order_totals_from_lines(target_order)
                _refresh_order_status(target_order)
                add_order_event(target_order, 'order_line_added', new_value=trailer.vin, comment=f'Добавлена позиция #{order_line.line_no} из подбора прицепа')
                add_order_event(target_order, 'trailer_reserved', new_value=trailer.vin, comment='Резерв из подбора прицепа')
                _finish_idempotency(idem_key, 'CustomerOrderLine', order_line.id)
                db.session.commit()
                flash('Позиция из наличия добавлена в заказ и зарезервирована.', 'success')
                return redirect(url_for('main.order_detail', order_id=target_order.id))
            order = CustomerOrder(
                order_number=_next_number('ORD', CustomerOrder, 'order_number'),
                customer_id=customer_id,
                item_id=trailer.item_id,
                trailer_id=trailer.id,
                warehouse_id=warehouse_id or trailer.warehouse_id,
                source_warehouse_id=trailer.warehouse_id,
                assigned_user_id=current_user.id if current_user.is_manager else None,
                created_by_user_id=current_user.id,
                quantity=1,
                price=trailer.item.base_price if trailer.item else None,
                prepayment_percent=30,
                status='waiting_payment',
                fulfillment_source='stock',
            )
            db.session.add(order)
            db.session.flush()
            order_line = _sync_primary_order_line(order, _order_snapshot(order))
            db.session.flush()
            trailer.status = 'RESERVED'
            trailer.lifecycle_status = 'reserved'
            db.session.add(Reservation(order_id=order.id, order_line_id=order_line.id, trailer_id=trailer.id, item_id=trailer.item_id, source_type='STOCK' if trailer.warehouse_id == order.warehouse_id else 'TRANSFER', status='ACTIVE', priority=10, note='Резерв из подбора прицепа'))
            add_order_event(order, 'order_created', new_value=order.status, comment='Создан из подбора прицепа')
            add_order_event(order, 'trailer_reserved', new_value=trailer.vin, comment='Резерв из подбора прицепа')
            _finish_idempotency(idem_key, 'CustomerOrder', order.id)
            db.session.commit()
            flash('Заказ из наличия создан, прицеп зарезервирован.', 'success')
            return redirect(url_for('main.order_detail', order_id=order.id))

        if action == 'production_order':
            if result.get('errors'):
                flash('Исправьте ошибки конфигурации перед созданием заказа.', 'danger')
                return redirect(url_for('main.manager_trailer_picker', **redirect_params))
            item = get_or_create_configured_item(result)
            quantity = max(request.form.get('quantity', type=int) or 1, 1)
            idem_key, duplicate = _reserve_idempotency_key()
            if duplicate:
                duplicate_url = url_for('main.order_detail', order_id=target_order.id) if target_order else url_for('main.orders_list')
                return _duplicate_redirect(idem_key, duplicate_url)
            if target_order:
                line, need = _create_order_line_from_item_for_production(
                    target_order,
                    item,
                    snapshot,
                    quantity=quantity,
                    unit_price=snapshot.get('calculated_price'),
                    note='Позиция добавлена из подбора прицепа',
                )
                if not target_order.item_id:
                    target_order.item_id = item.id
                target_order.fulfillment_source = 'production' if not target_order.trailer_id else target_order.fulfillment_source
                _sync_order_totals_from_lines(target_order)
                _refresh_order_status(target_order)
                add_order_event(target_order, 'order_line_added', new_value=item.article or item.name, comment=f'Добавлена позиция #{line.line_no} из подбора прицепа')
                add_order_event(target_order, 'production_need_created', new_value=need.quantity, comment=f'Потребность создана по позиции #{line.line_no}')
                _finish_idempotency(idem_key, 'CustomerOrderLine', line.id)
                db.session.commit()
                flash('Новая конфигурация добавлена отдельной позицией заказа.', 'success')
                return redirect(url_for('main.order_detail', order_id=target_order.id))
            order = CustomerOrder(
                order_number=_next_number('ORD', CustomerOrder, 'order_number'),
                customer_id=customer_id,
                item_id=item.id,
                warehouse_id=warehouse_id,
                assigned_user_id=current_user.id if current_user.is_manager else None,
                created_by_user_id=current_user.id,
                quantity=quantity,
                price=snapshot.get('calculated_price'),
                prepayment_percent=30,
                status='waiting_production',
                fulfillment_source='production',
            )
            _apply_snapshot(order, snapshot)
            db.session.add(order)
            db.session.flush()
            order_line = _sync_primary_order_line(order, snapshot)
            order_line.quantity = quantity
            order_line.total_price = (order_line.unit_price * quantity) if order_line.unit_price is not None else None
            db.session.flush()
            need = SupplyNeed(order_id=order.id, order_line_id=order_line.id, item_id=item.id, warehouse_id=order.warehouse_id, quantity=quantity, status='NEW', need_type='CUSTOMER_ORDER', priority=10, note='Потребность создана из подбора прицепа')
            _apply_snapshot(need, snapshot)
            db.session.add(need)
            _sync_order_totals_from_lines(order)
            add_order_event(order, 'order_created', new_value=order.status, comment='Создан из подбора прицепа')
            add_order_event(order, 'production_need_created', new_value='NEW', comment='Потребность создана из подбора прицепа')
            _finish_idempotency(idem_key, 'CustomerOrder', order.id)
            db.session.commit()
            flash('Заказ в производство создан.', 'success')
            return redirect(url_for('main.order_detail', order_id=order.id))

        if action == 'stock_replenishment':
            if result.get('errors'):
                flash('Исправьте ошибки конфигурации перед пополнением склада.', 'danger')
                return redirect(url_for('main.manager_trailer_picker', **config))
            item = get_or_create_configured_item(result)
            need = SupplyNeed(
                need_type='STOCK_REPLENISHMENT',
                order_id=None,
                item_id=item.id,
                warehouse_id=warehouse_id,
                quantity=request.form.get('quantity', type=int) or 1,
                status='NEW',
                priority=100,
                note='Пополнение создано из подбора прицепа',
            )
            _apply_snapshot(need, snapshot)
            db.session.add(need)
            db.session.commit()
            flash('Заявка на пополнение склада создана.', 'success')
            return redirect(url_for('main.stock_replenishment_list'))

    return render_template(
        'manager_trailer_picker.html',
        target_order=target_order,
        config=config,
        result=result,
        inventory=inventory,
        config_options=_trailer_config_form_context(config),
        customer_options=_customer_options(limit=80),
        warehouses=Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all(),
    )


# ========= КЛИЕНТЫ =========

@main_bp.route('/customers', methods=['GET'])
@login_required
def customers_list():
    _block_production_commercial_access()
    """Список клиентов: поиск + переключатель Все/ФЛ/ЮЛ."""
    search = request.args.get('q', '').strip()
    type_filter = request.args.get('type', 'all')  # all | person | company

    query = Customer.query

    if type_filter == 'person':
        query = query.filter(Customer.customer_type == 'PERSON')
    elif type_filter == 'company':
        query = query.filter(Customer.customer_type == 'COMPANY')

    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Customer.name.ilike(like),
                Customer.contact_person.ilike(like),
                Customer.iin_bin.ilike(like),
                Customer.phone.ilike(like),
            )
        )

    customers = (
        query
        .order_by(Customer.customer_type, Customer.name)
        .all()
    )

    return render_template(
        'customers.html',
        customers=customers,
        search=search,
        type_filter=type_filter,
    )


@main_bp.route('/api/customers/search')
@login_required
def api_customers_search():
    _block_production_commercial_access()
    q = (request.args.get('q') or '').strip()
    query = Customer.query.filter_by(is_active=True)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(
            Customer.name.ilike(like),
            Customer.phone.ilike(like),
            Customer.iin_bin.ilike(like),
            Customer.contact_person.ilike(like),
        ))
    customers = query.order_by(Customer.name).limit(20).all()
    return jsonify({
        'ok': True,
        'customers': [
            {
                'id': customer.id,
                'label': _customer_label(customer),
                'name': customer.name,
                'phone': customer.phone or '',
                'iin_bin': customer.iin_bin or '',
                'customer_type': customer.customer_type,
            }
            for customer in customers
        ],
    })

@main_bp.route('/customers/new', methods=['GET', 'POST'])
@role_required('manager')
def customer_create():
    form = CustomerForm()
    return_to = request.args.get('return_to', '').strip()
    order_id = request.args.get('order_id', type=int)

    if form.validate_on_submit():
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.customers_list'))
        is_company = (form.customer_type.data == 'COMPANY')
        opf = _norm_str(getattr(form, "opf", None).data if hasattr(form, "opf") else None)
        is_ip = (is_company and (opf or '').upper() == 'ИП')
        duplicate_customer = _find_duplicate_customer_from_form(form)
        if duplicate_customer:
            _finish_idempotency(idem_key, 'Customer', duplicate_customer.id)
            db.session.commit()
            flash('Клиент с таким ИИН/БИН или телефоном уже есть. Он подставлен в заказ.', 'warning')
            return _customer_return_redirect(duplicate_customer, return_to, order_id)

        customer = Customer(
            customer_type=form.customer_type.data,
            name=(form.name.data or '').strip(),
            contact_person=_norm_str(form.contact_person.data),
            iin_bin=_norm_str(form.iin_bin.data),
            phone=_norm_str(form.phone.data),
            email=_norm_str(form.email.data),
            address=_norm_str(form.address.data),
            is_active=bool(form.is_active.data),

            # ОПФ
            opf=opf if is_company else None,

            # Документ сохраняем только для ФЛ
            doc_type=_norm_str(form.doc_type.data) if not is_company else None,
            doc_number=_norm_str(form.doc_number.data) if not is_company else None,
            doc_issue_date=form.doc_issue_date.data if not is_company else None,
            doc_issuer=_norm_str(form.doc_issuer.data) if not is_company else None,

            # Реквизиты (только для COMPANY)
            bank_account=_norm_str(form.bank_account.data) if is_company else None,
            bank_name=_norm_str(form.bank_name.data) if is_company else None,
            bank_bic=_norm_str(form.bank_bic.data) if is_company else None,

            # Руководитель (для ИП не храним, чтобы не было дублей)
            director_position=_norm_str(form.director_position.data) if (is_company and not is_ip) else None,
            director_fio=_norm_str(form.director_fio.data) if (is_company and not is_ip) else None,
        )

        # ИП: контактное лицо тоже не нужно
        if is_ip:
            customer.contact_person = None

        db.session.add(customer)
        db.session.flush()
        _finish_idempotency(idem_key, 'Customer', customer.id)
        db.session.commit()
        flash('Клиент создан', 'success')
        return _customer_return_redirect(customer, return_to, order_id)

    back_url = _customer_back_url(return_to, order_id)
    return render_template('customer_form.html', form=form, title='Новый клиент', back_url=back_url)


@main_bp.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@role_required('manager')
def customer_edit(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    form = CustomerForm(obj=customer)
    return_to = request.args.get('return_to', '').strip()
    order_id = request.args.get('order_id', type=int)

    if form.validate_on_submit():
        duplicate_customer = _find_duplicate_customer_from_form(form, exclude_id=customer.id)
        if duplicate_customer:
            flash('Другой клиент уже использует этот ИИН/БИН или телефон.', 'danger')
            back_url = _customer_back_url(return_to, order_id)
            return render_template('customer_form.html', form=form, title='Редактирование клиента', back_url=back_url)

        form.populate_obj(customer)

        # нормализация строк
        customer.name = (customer.name or '').strip()
        customer.contact_person = _norm_str(customer.contact_person)
        customer.iin_bin = _norm_str(customer.iin_bin)
        customer.phone = _norm_str(customer.phone)
        customer.email = _norm_str(customer.email)
        customer.address = _norm_str(customer.address)

        customer.opf = _norm_str(getattr(customer, "opf", None))

        customer.doc_type = _norm_str(customer.doc_type)
        customer.doc_number = _norm_str(customer.doc_number)
        customer.doc_issuer = _norm_str(customer.doc_issuer)

        customer.bank_account = _norm_str(customer.bank_account)
        customer.bank_name = _norm_str(customer.bank_name)
        customer.bank_bic = _norm_str(customer.bank_bic)
        customer.director_position = _norm_str(customer.director_position)
        customer.director_fio = _norm_str(customer.director_fio)

        # логика по типу
        if customer.customer_type == 'PERSON':
            customer.opf = None
            customer.bank_account = None
            customer.bank_name = None
            customer.bank_bic = None
            customer.director_position = None
            customer.director_fio = None
        else:
            # COMPANY
            opf_upper = (customer.opf or '').upper()
            if opf_upper == 'ИП':
                customer.director_position = None
                customer.director_fio = None
                customer.contact_person = None

            # документы для COMPANY не храним
            customer.doc_type = None
            customer.doc_number = None
            customer.doc_issue_date = None
            customer.doc_issuer = None

        db.session.commit()
        flash('Клиент обновлён', 'success')
        return _customer_return_redirect(customer, return_to, order_id)

    back_url = _customer_back_url(return_to, order_id)
    return render_template('customer_form.html', form=form, title='Редактирование клиента', back_url=back_url)


@main_bp.route('/customers/<int:customer_id>/delete')
@admin_required
def customer_delete(customer_id):
    customer = Customer.query.get_or_404(customer_id)

    # если есть договоры — не даём удалить
    if customer.sales_contracts:
        flash('Нельзя удалить клиента, по которому есть договоры', 'danger')
    else:
        db.session.delete(customer)
        db.session.commit()
        flash('Клиент удалён', 'success')

    return redirect(url_for('main.customers_list'))

# ========= ВСПОМОГАТЕЛЬНОЕ: НОМЕР ДОГОВОРА =========

def is_contract_number_unique(number: str, exclude_id: int | None = None) -> bool:
    if not number:
        return True
    q = SalesContract.query.filter(SalesContract.contract_number == number)
    if exclude_id:
        q = q.filter(SalesContract.id != exclude_id)
    return q.count() == 0

def _norm_str(s: str | None) -> str | None:
    s = (s or '').strip()
    return s or None


def _phone_digits(value: str | None) -> str:
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def _find_duplicate_customer_from_form(form: CustomerForm, exclude_id: int | None = None):
    iin_bin = _norm_str(form.iin_bin.data)
    phone_digits = _phone_digits(form.phone.data)

    if iin_bin:
        query = Customer.query.filter(Customer.iin_bin == iin_bin)
        if exclude_id:
            query = query.filter(Customer.id != exclude_id)
        customer = query.order_by(Customer.id.asc()).first()
        if customer:
            return customer

    if len(phone_digits) >= 7:
        query = Customer.query.filter(Customer.phone.isnot(None), Customer.phone != '')
        if exclude_id:
            query = query.filter(Customer.id != exclude_id)
        for customer in query.order_by(Customer.id.asc()).all():
            if _phone_digits(customer.phone) == phone_digits:
                return customer
    return None


def _customer_back_url(return_to: str, order_id: int | None = None) -> str:
    if return_to == 'order_edit' and order_id:
        return url_for('main.order_edit', order_id=order_id)
    if return_to == 'order_create':
        return url_for('main.order_create')
    return url_for('main.customers_list')


def _customer_return_redirect(customer: Customer, return_to: str, order_id: int | None = None):
    if return_to == 'order_edit' and order_id:
        return redirect(url_for('main.order_edit', order_id=order_id, customer_id=customer.id))
    if return_to == 'order_create':
        return redirect(url_for('main.order_create', customer_id=customer.id))
    return redirect(url_for('main.customer_edit', customer_id=customer.id))


def get_next_contract_number() -> str:
    """
    Простой автонумератор: берём все заполненные contract_number,
    вытаскиваем цифры, max + 1.
    ⚠️ Не защищает от гонок на 100%, поэтому на commit ловим IntegrityError.
    """
    numbers = []

    rows = (
        SalesContract.query
        .with_entities(SalesContract.contract_number)
        .filter(SalesContract.contract_number.isnot(None), SalesContract.contract_number != '')
        .all()
    )

    for (cn,) in rows:
        s = ''.join(ch for ch in str(cn) if ch.isdigit())
        if not s:
            continue
        try:
            numbers.append(int(s))
        except ValueError:
            continue

    return str(max(numbers) + 1) if numbers else '1'


# ========= ДОГОВОРЫ / ПРОДАЖИ =========

def _article_group_code(article: str | None) -> str | None:
    if not article:
        return None
    return str(article).split('-', 1)[0].strip() or None


def _contract_line_config(line: SalesContractLine | None) -> dict:
    if not line:
        return {}
    source = line.order_line.config_snapshot_json if line.order_line and line.order_line.config_snapshot_json else None
    if not source and line.item and getattr(line.item, 'config_snapshot_json', None):
        source = line.item.config_snapshot_json
    if not source:
        return {}
    try:
        data = json.loads(source)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _select_contract_template(contract: SalesContract, contract_lines: list[SalesContractLine]) -> ContractTemplate | None:
    first_line = contract_lines[0] if contract_lines else None
    config = _contract_line_config(first_line)
    item = first_line.item if first_line and first_line.item else (contract.trailer.item if contract.trailer else None)
    group_code = (
        config.get('group_code')
        or _article_group_code(first_line.article_snapshot if first_line else None)
        or _article_group_code(item.article if item else None)
    )
    raw_customer_type = contract.customer.customer_type if contract.customer and contract.customer.customer_type else None
    customer_type = getattr(raw_customer_type, 'value', raw_customer_type) if raw_customer_type else None
    markers = {
        'product_group_code': group_code,
        'otss_number': (first_line.otss_number if first_line else None) or (contract.order.otss_number if contract.order else None),
        'otss_type': (first_line.otss_type if first_line else None) or (contract.order.otss_type if contract.order else None),
        'otss_modification': (first_line.otss_modification if first_line else None) or (contract.order.otss_modification if contract.order else None),
        'body_execution_code': config.get('body_execution_code'),
        'customer_type': customer_type,
    }
    templates = ContractTemplate.query.filter_by(template_type='sale', is_active=True).all()
    best_template = None
    best_score = -1
    for template in templates:
        score = 0
        mismatch = False
        for field, value in markers.items():
            template_value = getattr(template, field, None)
            if template_value:
                if value and str(template_value) == str(value):
                    score += 2
                else:
                    mismatch = True
                    break
        if mismatch:
            continue
        if template.is_default:
            score += 1
        score += int(template.sort_order or 0)
        if score > best_score:
            best_template = template
            best_score = score
    return best_template


def _contract_template_render_name(template: ContractTemplate | None) -> str:
    if not template or not template.content_path:
        return 'contract_print.html'
    path = template.content_path.replace('\\', '/').strip()
    parts = [part for part in path.split('/') if part]
    if not path.endswith('.html') or path.startswith('/') or '..' in parts:
        return 'contract_print.html'
    try:
        current_app.jinja_env.get_template(path)
    except Exception:
        return 'contract_print.html'
    return path


def _contract_template_for_contract(contract: SalesContract | None) -> ContractTemplate | None:
    if not contract:
        return None
    lines = contract.lines.order_by(SalesContractLine.line_no.asc(), SalesContractLine.id.asc()).all()
    return _select_contract_template(contract, lines)


def _apply_contract_template_form(template: ContractTemplate, form: ContractTemplateForm) -> None:
    template.code = (form.code.data or '').strip()
    template.name = (form.name.data or '').strip()
    template.template_type = form.template_type.data or 'sale'
    template.product_group_code = (form.product_group_code.data or '').strip() or None
    template.otss_number = (form.otss_number.data or '').strip() or None
    template.otss_type = (form.otss_type.data or '').strip() or None
    template.otss_modification = (form.otss_modification.data or '').strip() or None
    template.body_execution_code = (form.body_execution_code.data or '').strip() or None
    template.customer_type = (form.customer_type.data or '').strip() or None
    template.content_path = (form.content_path.data or '').strip() or None
    template.is_default = bool(form.is_default.data)
    template.is_active = bool(form.is_active.data)
    template.sort_order = int(form.sort_order.data or 0)
    template.comment = (form.comment.data or '').strip() or None


def _build_contract_context(contract_id: int) -> dict:
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_access_contract(contract)
    customer = contract.customer
    trailer = contract.trailer
    effective_vin = get_order_effective_vin(contract.order) if contract.order else (trailer.vin if trailer else '')
    item = trailer.item if trailer else (contract.order.item if contract.order else None)
    contract_lines = contract.lines.order_by(SalesContractLine.line_no.asc(), SalesContractLine.id.asc()).all()
    contract_template = _select_contract_template(contract, contract_lines)

    size_external = getattr(item, 'size_external', None)
    size_body     = getattr(item, 'size_body', None)
    axle_count    = getattr(item, 'axle_count', None)
    payload       = getattr(item, 'payload_kg', None)
    full_mass_kg  = getattr(item, 'full_mass_kg', None)

    modification_code = _extract_modification_from_vin(effective_vin) if effective_vin else None

    otts = None
    if modification_code:
        otts = OTTS.query.filter_by(modification=modification_code).first()

    if not full_mass_kg and otts and otts.full_mass_kg:
        full_mass_kg = otts.full_mass_kg

    if axle_count is None and otts and otts.axle_count is not None:
        axle_count = otts.axle_count

    # --- склад менеджера (для вывода вместо города) ---
    warehouse_name = None
    try:
        if current_user.is_authenticated and getattr(current_user, "warehouse", None):
            warehouse_name = current_user.warehouse.name
    except Exception:
        warehouse_name = None

    return dict(
        contract=contract,
        customer=customer,
        trailer=trailer,
        item=item,
        otts=otts,
        modification_code=modification_code,
        size_external=size_external,
        size_body=size_body,
        axle_count=axle_count,
        payload=payload,
        full_mass_kg=full_mass_kg,
        warehouse_name=warehouse_name,  # <-- это используешь в шаблоне
        effective_vin=effective_vin,
        contract_lines=contract_lines,
        contract_template=contract_template,
        contract_template_name=_contract_template_render_name(contract_template),
    )

@main_bp.route('/contracts')
@login_required
def contracts_list():
    _block_production_commercial_access()
    """Список договоров / продаж. Менеджеры видят только свой склад/свои заказы."""
    warehouse_id = request.args.get('warehouse_id', type=int)

    q = (request.args.get('q') or '').strip()          # общий поиск: номер/клиент
    vin = (request.args.get('vin') or '').strip()      # поиск по VIN/артикулу
    paid = request.args.get('paid', '')                # '', '1', '0'
    shipped = request.args.get('shipped', '')          # '', '1', '0'

    warehouses = Warehouse.query.order_by(Warehouse.name).all()

    query = (
        SalesContract.query
        .outerjoin(CustomerOrder, CustomerOrder.id == SalesContract.order_id)
        .outerjoin(Trailer, SalesContract.trailer_id == Trailer.id)
        .outerjoin(Item, Item.id == Trailer.item_id)
        .outerjoin(Customer, Customer.id == SalesContract.customer_id)
        .outerjoin(Warehouse, Warehouse.id == Trailer.warehouse_id)
        .outerjoin(VinRegistry, VinRegistry.customer_order_id == CustomerOrder.id)
    )
    if current_user.is_manager:
        query = query.filter(or_(
            CustomerOrder.assigned_user_id == current_user.id,
            CustomerOrder.created_by_user_id == current_user.id,
        ))

    if warehouse_id:
        query = query.filter(Trailer.warehouse_id == warehouse_id)

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                SalesContract.contract_number.ilike(like),
                Customer.name.ilike(like),
                Customer.iin_bin.ilike(like),
                Customer.phone.ilike(like),
            )
        )

    if vin:
        like = f"%{vin}%"
        query = query.filter(
            or_(
                Trailer.vin.ilike(like),
                Item.article.ilike(like),
                VinRegistry.vin_full.ilike(like),
            )
        )

    # --- статусы (можешь убрать, если не надо) ---
    if paid in ('0', '1'):
        query = query.filter(SalesContract.is_paid == (paid == '1'))

    if shipped in ('0', '1'):
        query = query.filter(SalesContract.is_shipped == (shipped == '1'))
    # -------------------------------------------

    contracts = (
        query
        .order_by(SalesContract.contract_date.desc().nullslast(), SalesContract.id.desc())
        .distinct()
        .all()
    )

    return render_template(
        'contracts_list.html',
        contracts=contracts,
        warehouses=warehouses,
        warehouse_id=warehouse_id,
        q=q,
        vin=vin,
        paid=paid,
        shipped=shipped,
    )


@main_bp.route('/contracts/templates')
@role_required('director')
def contract_templates_list():
    templates = ContractTemplate.query.order_by(ContractTemplate.is_active.desc(), ContractTemplate.sort_order.desc(), ContractTemplate.name.asc()).all()
    return render_template('contract_templates_list.html', templates=templates)


@main_bp.route('/contracts/templates/new', methods=['GET', 'POST'])
@role_required('director')
def contract_template_create():
    form = ContractTemplateForm()
    if form.validate_on_submit():
        template = ContractTemplate()
        _apply_contract_template_form(template, form)
        db.session.add(template)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Шаблон с таким кодом уже существует.', 'danger')
            return render_template('contract_template_form.html', form=form, form_title='Новый шаблон договора')
        flash('Шаблон договора создан.', 'success')
        return redirect(url_for('main.contract_templates_list'))
    return render_template('contract_template_form.html', form=form, form_title='Новый шаблон договора')


@main_bp.route('/contracts/templates/<int:template_id>/edit', methods=['GET', 'POST'])
@role_required('director')
def contract_template_edit(template_id):
    template = ContractTemplate.query.get_or_404(template_id)
    form = ContractTemplateForm(obj=template)
    if request.method == 'GET':
        form.customer_type.data = template.customer_type or ''
    if form.validate_on_submit():
        _apply_contract_template_form(template, form)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Шаблон с таким кодом уже существует.', 'danger')
            return render_template('contract_template_form.html', form=form, form_title='Редактирование шаблона договора', template=template)
        flash('Шаблон договора сохранён.', 'success')
        return redirect(url_for('main.contract_templates_list'))
    return render_template('contract_template_form.html', form=form, form_title='Редактирование шаблона договора', template=template)


@main_bp.route('/contracts/new', methods=['GET', 'POST'])
@login_required
def contract_create():
    _block_production_commercial_access()
    flash('Договор создаётся только из карточки заказа.', 'warning')
    return redirect(url_for('main.orders_list'))


@main_bp.route('/contracts/<int:contract_id>/edit', methods=['GET', 'POST'])
@login_required
def contract_edit(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    if not can_manage_contract(contract):
        _block_production_commercial_access()
        flash('Редактирование договора недоступно для вашей роли или после выдачи документов.', 'danger')
        if contract.order_id and can_access_contract(contract):
            return redirect(url_for('main.order_detail', order_id=contract.order_id))
        abort(403)
    form = SalesContractForm(contract_id=contract.id)

    # ----- Клиенты -----
    customers = (
        Customer.query
        .filter_by(is_active=True)
        .order_by(Customer.customer_type, Customer.name)
        .all()
    )
    form.customer_id.choices = [
        (c.id, f"{'ФЛ' if c.customer_type == 'PERSON' else 'ЮЛ'} — {c.name}")
        for c in customers
    ]

    # ----- Прицепы: для редактирования можно показать все -----
    trailers = Trailer.query.order_by(Trailer.vin).all()
    form.trailer_id.choices = [
        (t.id, f"{t.vin} — {t.item.article if t.item else ''}")
        for t in trailers
    ]
    form.warehouse_id.choices = [(0, '— из заказа / прицепа —')] + [
        (w.id, w.name)
        for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    ]
    form.assigned_user_id.choices = [(0, '— из заказа / не указан —')] + [
        (u.id, u.full_name or u.username)
        for u in User.query.order_by(User.full_name, User.username).all()
    ]

    if request.method == 'GET':
        form.contract_date.data = contract.contract_date
        form.contract_number.data = contract.contract_number or ''
        if contract.customer_id:
            form.customer_id.data = contract.customer_id
        if contract.trailer_id:
            form.trailer_id.data = contract.trailer_id
        form.warehouse_id.data = contract.warehouse_id or 0
        form.assigned_user_id.data = contract.assigned_user_id or 0
        form.price.data = float(contract.price) if contract.price is not None else None
        form.payment_method.data = contract.payment_method or ''
        form.is_paid.data = contract.is_paid
        form.is_shipped.data = contract.is_shipped

    if form.validate_on_submit():
        old_trailer = contract.trailer
        new_trailer = Trailer.query.get(form.trailer_id.data)

        if not new_trailer:
            flash('Прицеп не найден', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')
        if contract.order and (form.customer_id.data != contract.order.customer_id or form.trailer_id.data != contract.order.trailer_id):
            flash('Договор из заказа должен оставаться связан с клиентом и VIN этого заказа.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # защита: на новом прицепе не должно быть другого договора
        exists_other = (
            SalesContract.query
            .filter(SalesContract.trailer_id == new_trailer.id, SalesContract.id != contract.id)
            .first()
        )
        if exists_other:
            flash('На выбранный прицеп уже существует другой договор.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # --- НОМЕР ДОГОВОРА: устойчиво к пробелам/формату ---
        old_db_value = contract.contract_number  # сохраняем как есть (может быть None)
        old_norm = (old_db_value or '').strip()
        new_norm = (form.contract_number.data or '').strip()
        proposed_contract_number = old_db_value if not new_norm or new_norm == old_norm else new_norm

        # если поле очистили — НЕ меняем номер
        if not new_norm:
            contract.contract_number = old_db_value
        else:
            # если по сути не изменили (только пробелы/формат) — НЕ меняем
            if new_norm == old_norm:
                contract.contract_number = old_db_value
            else:
                # реально изменили — проверяем уникальность
                if not is_contract_number_unique(new_norm, exclude_id=contract.id):
                    form.contract_number.errors.append('Такой номер договора уже существует. Введите другой.')
                    return render_template('contract_form.html', form=form, form_title='Редактирование договора')
                contract.contract_number = new_norm

        key_fields_before = (
            old_db_value,
            contract.contract_date,
            contract.customer_id,
            contract.trailer_id,
            contract.order_id,
            str(contract.price or ''),
            contract.payment_method or '',
            contract.source or '',
        )
        key_fields_after = (
            proposed_contract_number,
            form.contract_date.data,
            form.customer_id.data,
            new_trailer.id,
            contract.order_id,
            str(form.price.data or ''),
            _norm_str(form.payment_method.data) or '',
            contract.source or '',
        )
        key_fields_changed = key_fields_before != key_fields_after
        if key_fields_changed and contract.sigex_last_status == 'done':
            contract.contract_number = old_db_value
            flash('Договор уже подписан через SIGEX/eGov QR. Ключевые поля нельзя менять обычным способом.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # если поменяли прицеп — старый вернуть в IN_STOCK (если других договоров нет)
        if old_trailer and old_trailer.id != new_trailer.id:
            other_cnt = (
                SalesContract.query
                .filter(SalesContract.trailer_id == old_trailer.id, SalesContract.id != contract.id)
                .count()
            )
            if other_cnt == 0:
                old_trailer.status = 'RESERVED' if _active_reservation_for_trailer(old_trailer.id) else 'IN_STOCK'

        contract.contract_date = form.contract_date.data
        contract.customer_id = form.customer_id.data
        contract.trailer_id = new_trailer.id
        contract.warehouse_id = form.warehouse_id.data or None
        contract.assigned_user_id = form.assigned_user_id.data or None
        contract.price = form.price.data
        contract.payment_method = _norm_str(form.payment_method.data)
        if current_user.is_admin:
            contract.is_paid = bool(form.is_paid.data)
            contract.is_shipped = bool(form.is_shipped.data)

        if key_fields_changed and contract.sigex_last_status != 'done':
            contract.sigex_document_id = None
            contract.sigex_operation_id = None
            contract.sigex_expire_at = None
            contract.sigex_last_status = None
            contract.sigex_last_sign_id = None

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Конфликт сохранения (номер/прицеп). Проверь данные и попробуй снова.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        flash('Договор обновлён', 'success')
        if contract.order_id:
            return redirect(url_for('main.order_detail', order_id=contract.order_id))
        return redirect(url_for('main.contracts_list'))

    return render_template('contract_form.html', form=form, form_title='Редактирование договора')

@main_bp.route('/contracts/<int:contract_id>/delete', methods=['POST'])
@login_required
def contract_delete(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    trailer = contract.trailer
    order_id = contract.order_id

    if contract.order and (contract.order.documents_issued or contract.order.is_shipped):
        flash('Нельзя удалить договор после выдачи документов или отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=contract.order_id))

    db.session.delete(contract)
    db.session.flush()

    if trailer:
        other_cnt = SalesContract.query.filter_by(trailer_id=trailer.id).count()
        if other_cnt == 0:
            trailer.status = 'RESERVED' if _active_reservation_for_trailer(trailer.id) else 'IN_STOCK'

    db.session.commit()
    flash('Договор удалён', 'success')
    if order_id:
        return redirect(url_for('main.order_detail', order_id=order_id))
    return redirect(url_for('main.contracts_list'))


@main_bp.route('/contracts/<int:contract_id>/print')
@login_required
def contract_print(contract_id):
    ctx = _build_contract_context(contract_id)
    return render_template(ctx.get('contract_template_name') or 'contract_print.html', **ctx)


@main_bp.route('/contracts/<int:contract_id>/pdf')
@login_required
def contract_pdf(contract_id):
    ctx = _build_contract_context(contract_id)
    if HTML is not None:
        from flask import make_response, request
        html = render_template(ctx.get('contract_template_name') or 'contract_print.html', **ctx)
        pdf = HTML(string=html, base_url=request.host_url).write_pdf()

        contract = ctx['contract']
        filename = f"contract_{contract.contract_number or contract.id}.pdf"

        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'inline; filename={filename}'
        return response

    return redirect(url_for('main.contract_print', contract_id=contract_id))


def _contract_pdf_bytes(contract_id: int) -> bytes:
    # ВАЖНО: для SIGEX нужно именно bytes документа.
    # Тут подразумевается, что WeasyPrint установлен и HTML != None как у тебя.
    ctx = _build_contract_context(contract_id)
    html = render_template(ctx.get('contract_template_name') or 'contract_print.html', **ctx)

    if HTML is None:
        raise RuntimeError("WeasyPrint (HTML) is not available. Install/configure WeasyPrint on server.")

    pdf = HTML(string=html, base_url=request.host_url).write_pdf()
    return pdf

def _sigex_title_for_contract(contract) -> str:
    num = contract.contract_number or contract.id
    return f"Договор_{num}.pdf"


def _sigex_json_error(exc, status_code=502):
    current_app.logger.exception("SIGEX/eGov QR integration error")
    message = "Ошибка SIGEX/eGov QR. Проверьте настройку интеграции и повторите действие."
    if "WeasyPrint" in str(exc):
        message = "PDF для SIGEX сейчас недоступен. Проверьте генерацию PDF на сервере."
    return jsonify({"ok": False, "error": message}), status_code


def _prepare_sigex_contract_document(contract) -> str:
    if HTML is None:
        raise RuntimeError("WeasyPrint is not available. HTML print/PDF fallback is available, SIGEX PDF is disabled.")

    if contract.sigex_document_id:
        return contract.sigex_document_id

    title = _sigex_title_for_contract(contract)
    payload = {
        "title": title,
        "description": f"Договор №{contract.contract_number or contract.id}",
        "settings": {
            "private": False,
            "signaturesLimit": 2,
            "switchToPrivateAfterLimitReached": True,
            "tempStorageAfterRegistration": 86400000,
        },
    }

    reg = sigex_post_json("/api", payload)
    document_id = reg["documentId"]
    pdf_bytes = _contract_pdf_bytes(contract.id)
    sigex_post_octet(f"/api/{document_id}/data", pdf_bytes)

    contract.sigex_document_id = document_id
    contract.sigex_last_status = "document_ready"
    contract.sigex_expire_at = datetime.utcnow() + timedelta(hours=24)
    return document_id


def _sigex_document_id_from_response(response_payload: dict) -> str:
    document_id = response_payload.get("documentId") or response_payload.get("id")
    if not document_id:
        current_app.logger.error("SIGEX document registration returned no document id: %s", response_payload)
        raise RuntimeError("SIGEX не вернул идентификатор документа.")
    return str(document_id)


def _register_sigex_contract_with_org_signature(contract, signature: str, sign_type: str) -> dict:
    if HTML is None:
        raise RuntimeError("WeasyPrint is not available. HTML print/PDF fallback is available, SIGEX PDF is disabled.")

    title = _sigex_title_for_contract(contract)
    payload = {
        "title": title,
        "description": f"Договор №{contract.contract_number or contract.id}",
        "signType": sign_type,
        "signature": signature,
        "settings": {
            "private": False,
            "signaturesLimit": 2,
            "switchToPrivateAfterLimitReached": True,
            "tempStorageAfterRegistration": 86400000,
        },
    }
    reg = sigex_post_json("/api", payload)
    document_id = _sigex_document_id_from_response(reg)
    pdf_bytes = _contract_pdf_bytes(contract.id)
    sigex_post_octet(f"/api/{document_id}/data", pdf_bytes)
    reg["documentId"] = document_id
    return reg


def _sync_sigex_operation_status(contract, response_payload: dict) -> str | None:
    status = response_payload.get("status")
    old_status = contract.sigex_last_status
    if status:
        contract.sigex_last_status = status
    if status == "done":
        sign_id = response_payload.get("signId")
        if sign_id is not None:
            contract.sigex_last_sign_id = sign_id
        if old_status != "done" and contract.order:
            add_order_event(
                contract.order,
                "contract_signed",
                old_value=old_status,
                new_value="sigex_signed",
                comment="Договор подписан клиентом через eGov QR / SIGEX",
            )
    return status


def _active_sigex_qr_payload(contract) -> dict:
    return {
        "description": f"Подпишите договор №{contract.contract_number or contract.id}",
        "meta": [
            {"name": "Номер договора", "value": str(contract.contract_number or contract.id)},
            {"name": "Сумма", "value": str(contract.price or "")},
        ],
    }


@main_bp.route('/contracts/<int:contract_id>/sign')
@login_required
def contract_sign(contract_id):
    """
    Страница подписи через eGov QR / SIGEX.
    """
    ctx = _build_contract_context(contract_id)
    contract = ctx["contract"]
    _ensure_can_manage_contract(contract)
    return render_template("contract_sign.html", contract=contract)

@main_bp.route('/contracts/<int:contract_id>/sigex/pdf_base64')
@login_required
def contract_sigex_pdf_base64(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    try:
        pdf_bytes = _contract_pdf_bytes(contract_id)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"pdfBase64": base64.b64encode(pdf_bytes).decode("utf-8")})

@main_bp.route('/contracts/<int:contract_id>/sigex/preregister', methods=['POST'])
@login_required
def contract_sigex_preregister(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    try:
        _contract_pdf_bytes(contract_id)
    except RuntimeError as exc:
        return _sigex_json_error(exc, 503)
    except Exception as exc:
        return _sigex_json_error(exc)

    return jsonify({
        "ok": True,
        "documentId": contract.sigex_document_id,
        "status": contract.sigex_last_status or "pdf_ready",
    })

@main_bp.route('/contracts/<int:contract_id>/sigex/add_org_signature', methods=['POST'])
@login_required
def contract_sigex_add_org_signature(contract_id):
    """
    Сюда фронт пришлёт CMS подпись (base64) от NCALayer.
    """
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)

    data = request.get_json(silent=True) or {}
    signature = data.get("signature")
    sign_type = data.get("signType", "cms")

    if not signature:
        abort(400, "signature is required")

    try:
        if contract.sigex_document_id:
            res = sigex_post_json(f"/api/{contract.sigex_document_id}", {
                "signType": sign_type,
                "signature": signature,
            })
            document_id = contract.sigex_document_id
        else:
            res = _register_sigex_contract_with_org_signature(contract, signature, sign_type)
            document_id = res["documentId"]
    except Exception as exc:
        db.session.rollback()
        return _sigex_json_error(exc)

    contract.sigex_document_id = document_id
    contract.sigex_last_sign_id = res.get("signId")
    contract.sigex_last_status = "org_signed"
    contract.sigex_expire_at = datetime.utcnow() + timedelta(hours=24)
    db.session.commit()

    return jsonify({
        "ok": True,
        "documentId": contract.sigex_document_id,
        "signId": res.get("signId"),
        "status": contract.sigex_last_status,
    })

@main_bp.route('/contracts/<int:contract_id>/sigex/start_qr', methods=['POST'])
@login_required
def contract_sigex_start_qr(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    last_status = (contract.sigex_last_status or '').lower()
    if last_status == 'done':
        return jsonify({"ok": False, "error": "Договор уже подписан через SIGEX/eGov QR."}), 400
    if contract.sigex_operation_id and last_status not in ('canceled', 'cancelled', 'expired', 'fail'):
        return jsonify({"ok": False, "error": "По договору уже есть активный QR-запрос. Проверьте статус или отмените QR."}), 400
    if not (contract.sigex_last_sign_id or contract.sigex_last_status == "org_signed"):
        return jsonify({"ok": False, "error": "Сначала подпишите договор ЭЦП организации через NCALayer."}), 400
    try:
        document_id = _prepare_sigex_contract_document(contract)
        res = sigex_post_json(f"/api/{document_id}/egovQr", _active_sigex_qr_payload(contract))
        operation_id = res.get("operationId")
        if not operation_id:
            raise RuntimeError("SIGEX не вернул operationId для QR-подписания.")
        contract.sigex_operation_id = operation_id
        contract.sigex_last_status = "qr_started"
        db.session.commit()
    except RuntimeError as exc:
        db.session.rollback()
        return _sigex_json_error(exc, 503)
    except Exception as exc:
        db.session.rollback()
        return _sigex_json_error(exc)

    return jsonify(res)

@main_bp.route('/contracts/<int:contract_id>/sigex/qr_status')
@login_required
def contract_sigex_qr_status(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_access_contract(contract)
    if not (contract.sigex_document_id and contract.sigex_operation_id):
        abort(400, "No active operation")

    try:
        res = sigex_get_json(f"/api/{contract.sigex_document_id}/egovOperation/{contract.sigex_operation_id}")
        _sync_sigex_operation_status(contract, res)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return _sigex_json_error(exc)

    return jsonify(res)


@main_bp.route('/contracts/<int:contract_id>/sigex/cancel_qr', methods=['POST'])
@login_required
def contract_sigex_cancel_qr(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    if not (contract.sigex_document_id and contract.sigex_operation_id):
        return jsonify({"ok": True, "status": contract.sigex_last_status or "no_active_operation"})

    try:
        sigex_delete_json(f"/api/{contract.sigex_document_id}/egovOperation/{contract.sigex_operation_id}")
        contract.sigex_operation_id = None
        contract.sigex_last_status = "canceled"
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return _sigex_json_error(exc)

    return jsonify({"ok": True, "status": contract.sigex_last_status})

@main_bp.route('/contracts/<int:contract_id>/sigex/ddc')
@login_required
def contract_sigex_ddc(contract_id):
    """
    Карточка электронного документа (DDC) — можно дать ссылку менеджеру.
    """
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_access_contract(contract)
    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    params = {
        "fileName": _sigex_title_for_contract(contract),
        "withoutDocumentVisualization": "false",
        "withoutSignaturesVisualization": "false",
        "withoutQRCodesInSignaturesVisualization": "false",
        "withoutID": "false",
        "qrWithIDLink": "false",
        "withLabelVerified": "true",
        "language": "ru",
    }

    pdf_bytes = _contract_pdf_bytes(contract.id)
    res = sigex_post_octet(f"/api/{contract.sigex_document_id}/buildDDC", pdf_bytes, params=params)

    ddc_b64 = res["ddc"]
    ddc_bytes = base64.b64decode(ddc_b64)

    filename = f"ddc_{contract.contract_number or contract.id}.pdf"
    response = make_response(ddc_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


# ========= CRM / ЛИДЫ / ЗАКАЗЫ =========

def _next_number(prefix: str, model, attr: str) -> str:
    last = db.session.query(model).order_by(model.id.desc()).first()
    next_id = (last.id + 1) if last else 1
    return f"{prefix}-{next_id:06d}"


def _refresh_order_status(order: CustomerOrder) -> None:
    paid = order.confirmed_paid_amount
    price = float(order.price or 0)
    required_prepayment = price * float(order.prepayment_percent or 0) / 100

    if order.status == 'cancelled':
        return

    if order.is_shipped:
        order.status = 'shipped'
        return

    if order.documents_issued:
        order.status = 'sold_not_shipped'
        return

    if price > 0 and paid >= price and order.trailer_id:
        if order.trailer and order.warehouse_id and order.trailer.warehouse_id == order.warehouse_id:
            order.status = 'ready_to_ship'
        elif order.status not in ('waiting_transfer', 'in_transit', 'in_production', 'produced_waiting_vin', 'waiting_production'):
            order.status = 'confirmed'
        return

    if paid <= 0:
        if order.trailer_id:
            order.status = 'waiting_payment'
        elif order.fulfillment_source == 'production' or order.supply_needs.count() > 0:
            order.status = 'waiting_production'
        elif order.fulfillment_source in ('transit', 'other_warehouse'):
            order.status = 'waiting_arrival'
        else:
            order.status = 'waiting_payment'
        return

    if price > 0 and paid < price:
        if required_prepayment and paid >= required_prepayment:
            if order.supply_needs.count() > 0 or order.fulfillment_source == 'production':
                order.status = 'waiting_production'
            elif order.fulfillment_source == 'other_warehouse':
                order.status = 'waiting_transfer'
            elif order.fulfillment_source == 'transit':
                order.status = 'waiting_arrival'
            else:
                order.status = 'prepaid'
        else:
            order.status = 'waiting_payment'
        return

    if price > 0 and paid >= price:
        if order.trailer_id and order.trailer and order.warehouse_id and order.trailer.warehouse_id == order.warehouse_id:
            order.status = 'ready_to_ship'
        elif order.supply_needs.count() > 0 or order.fulfillment_source == 'production':
            order.status = 'waiting_production'
        elif order.fulfillment_source == 'other_warehouse':
            order.status = 'waiting_transfer'
        elif order.fulfillment_source == 'transit':
            order.status = 'waiting_arrival'
        else:
            order.status = 'confirmed'


def _active_reservation_for_trailer(trailer_id: int, exclude_order_id: int | None = None):
    q = Reservation.query.filter(
        Reservation.trailer_id == trailer_id,
        Reservation.status == 'ACTIVE',
    )
    if exclude_order_id:
        q = q.filter(Reservation.order_id != exclude_order_id)
    return q.first()


def _item_label(item: Item) -> str:
    parts = [item.article or '', item.name or '']
    details = []
    if item.size_body:
        details.append(str(item.size_body))
    if item.axle_count:
        details.append(f'{item.axle_count} оси')
    if item.wheel_radius:
        details.append(str(item.wheel_radius))
    label = ' — '.join(part for part in parts if part)
    if details:
        label = f'{label} ({", ".join(details)})'
    return label or f'Модель #{item.id}'


def _customer_label(customer: Customer) -> str:
    parts = [customer.name or f'Клиент #{customer.id}']
    details = []
    if customer.phone:
        details.append(customer.phone)
    if customer.iin_bin:
        details.append(customer.iin_bin)
    if customer.customer_type:
        details.append('ФЛ' if customer.customer_type == 'PERSON' else 'ЮЛ')
    return f"{parts[0]} ({', '.join(details)})" if details else parts[0]


def _customer_options(limit: int | None = None):
    query = Customer.query.filter_by(is_active=True).order_by(Customer.name)
    if limit:
        query = query.limit(limit)
    return [
        {
            'id': customer.id,
            'label': _customer_label(customer),
            'name': customer.name,
            'phone': customer.phone or '',
            'iin_bin': customer.iin_bin or '',
            'customer_type': customer.customer_type,
        }
        for customer in query.all()
    ]


def _find_customer_by_search(search: str | None):
    text = (search or '').strip()
    if not text:
        return None
    normalized = text.lower()
    customers = Customer.query.filter_by(is_active=True).order_by(Customer.name).all()
    for customer in customers:
        if _customer_label(customer).lower() == normalized:
            return customer
    exact = [
        customer for customer in customers
        if (customer.phone and customer.phone.lower() == normalized)
        or (customer.iin_bin and customer.iin_bin.lower() == normalized)
        or (customer.name and customer.name.lower() == normalized)
    ]
    if len(exact) == 1:
        return exact[0]
    contains = [
        customer for customer in customers
        if normalized in _customer_label(customer).lower()
    ]
    return contains[0] if len(contains) == 1 else None


def _apply_customer_search(form: CustomerOrderForm) -> None:
    customer_id = form.customer_id.data or 0
    if request.method == 'POST' and (not customer_id or customer_id == 0):
        customer = _find_customer_by_search(form.customer_search.data)
        if customer:
            form.customer_id.data = customer.id


def _set_customer_search_label(form: CustomerOrderForm) -> None:
    customer_id = form.customer_id.data or 0
    if customer_id and not form.customer_search.data:
        customer = Customer.query.get(customer_id)
        if customer:
            form.customer_search.data = _customer_label(customer)


def _trailer_item_options():
    return [
        {'id': item.id, 'label': _item_label(item)}
        for item in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]


def _order_trailer_options(current_order_id: int | None = None, current_trailer_id: int | None = None):
    source_warehouse_ids = [warehouse.id for warehouse in _trailer_source_warehouses()]
    trailers = (
        Trailer.query
        .filter(
            or_(Trailer.warehouse_id.in_(source_warehouse_ids), Trailer.id == current_trailer_id) if source_warehouse_ids else Trailer.id == current_trailer_id,
            or_(Trailer.status == 'IN_STOCK', Trailer.id == current_trailer_id),
            or_(Trailer.id == current_trailer_id, Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Trailer.vin)
        .all()
    )
    result = []
    for trailer in trailers:
        if trailer.id != current_trailer_id and not _trailer_available_for_sale(trailer, exclude_order_id=current_order_id):
            continue
        result.append({
            'id': trailer.id,
            'item_id': trailer.item_id,
            'warehouse_id': trailer.warehouse_id,
            'vin': trailer.vin or '',
            'article': trailer.item.article if trailer.item else '',
            'name': trailer.item.name if trailer.item else '',
            'warehouse': trailer.warehouse.name if trailer.warehouse else '',
            'status': trailer.status or '',
            'price': float(trailer.item.base_price or 0) if trailer.item else 0,
            'label': f'{trailer.vin} — {trailer.item.article if trailer.item else ""} — {trailer.warehouse.name if trailer.warehouse else ""}',
        })
    return result


def _find_item_by_search(search: str | None):
    text = (search or '').strip()
    if not text:
        return None
    normalized = text.lower()
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    for item in items:
        if _item_label(item).lower() == normalized:
            return item
    exact_article = [item for item in items if (item.article or '').lower() == normalized]
    if len(exact_article) == 1:
        return exact_article[0]
    contains = [
        item for item in items
        if normalized in (_item_label(item).lower())
    ]
    return contains[0] if len(contains) == 1 else None


def _apply_item_search(form, search_attr: str, item_attr: str) -> None:
    search = getattr(form, search_attr).data or ''
    item_id = getattr(form, item_attr).data or 0
    if request.method == 'POST' and (not item_id or item_id == 0):
        item = _find_item_by_search(search)
        if item:
            getattr(form, item_attr).data = item.id


def _set_item_search_label(form, search_attr: str, item_attr: str) -> None:
    item_id = getattr(form, item_attr).data or 0
    if item_id and not getattr(form, search_attr).data:
        item = Item.query.get(item_id)
        if item:
            getattr(form, search_attr).data = _item_label(item)


def _fill_order_form_choices(form: CustomerOrderForm, item_id_prefill: int | None = None, current_order_id: int | None = None) -> None:
    leads = Lead.query.order_by(Lead.created_at.desc(), Lead.id.desc()).all()
    form.lead_id.choices = [(0, '— без заявки —')] + [(l.id, f'#{l.id} {l.customer_name} / {l.channel or l.source_channel}') for l in leads]
    form.customer_id.choices = [(0, '— выберите клиента —')] + [(c.id, _customer_label(c)) for c in Customer.query.filter_by(is_active=True).order_by(Customer.name).all()]
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    form.item_id.choices = [(0, '— выберите модель —')] + [(i.id, _item_label(i)) for i in items]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in _sales_warehouses()]
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in User.query.order_by(User.full_name, User.username).all()]
    if form.fulfillment_source.data in ('other_warehouse', 'transit'):
        form.fulfillment_source.data = 'stock' if form.fulfillment_source.data == 'other_warehouse' else 'later'

    _apply_item_search(form, 'item_search', 'item_id')
    _apply_customer_search(form)
    form.trailer_id.choices = [(0, '— подобрать позже / под заказ —')] + [
        (option['id'], f'{option["vin"]} — {option["article"]} — {option["warehouse"]} — {option["status"]}')
        for option in _order_trailer_options(current_order_id, getattr(form, 'trailer_id').data or None)
    ]
    _set_item_search_label(form, 'item_search', 'item_id')
    _set_customer_search_label(form)


def _fill_lead_form_choices(form: LeadForm) -> None:
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    form.desired_item_id.choices = [(0, '— не выбрано —')] + [(i.id, _item_label(i)) for i in items]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in User.query.order_by(User.full_name, User.username).all()]
    _apply_item_search(form, 'desired_item_search', 'desired_item_id')
    _set_item_search_label(form, 'desired_item_search', 'desired_item_id')


def _fill_kaspi_import_form_choices(form: KaspiOrderImportForm) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не выбран —')] + [(w.id, w.name) for w in warehouses]
    users = User.query.order_by(User.full_name, User.username).all()
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in users]


def _fill_kaspi_list_import_form_choices(form: KaspiOrderListImportForm) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не выбран —')] + [(w.id, w.name) for w in warehouses]
    users = User.query.order_by(User.full_name, User.username).all()
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in users]


def _kaspi_dt(value) -> datetime | None:
    if value in (None, ''):
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts)


def _kaspi_ms(value: date | None, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    dt = datetime.combine(value, time.max if end_of_day else time.min)
    return int(dt.timestamp() * 1000)


def _parse_kaspi_filter_date(value) -> date | None:
    text = (value or '').strip() if isinstance(value, str) else value
    if not text:
        return None
    if isinstance(text, date):
        return text
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%d.%m.%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError('Дата должна быть в формате YYYY-MM-DD, MM/DD/YYYY или DD.MM.YYYY.')


def _kaspi_customer_name(attrs: dict) -> str:
    customer = attrs.get('customer') or {}
    parts = [
        customer.get('lastName'),
        customer.get('firstName'),
        customer.get('middleName'),
    ]
    name = ' '.join(str(part).strip() for part in parts if part)
    return name or customer.get('name') or 'Клиент Kaspi'


def _kaspi_customer_phone(attrs: dict) -> str | None:
    customer = attrs.get('customer') or {}
    return customer.get('cellPhone') or customer.get('phone') or customer.get('mobilePhone')


def _kaspi_address_text(attrs: dict) -> str | None:
    address = attrs.get('deliveryAddress') or {}
    return address.get('formattedAddress') or ', '.join(
        str(address.get(key)).strip()
        for key in ('town', 'streetName', 'streetNumber', 'building')
        if address.get(key)
    ) or None


def _kaspi_entry_summary(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        attrs = entry.get('attributes') or {}
        category = attrs.get('category') or {}
        product = attrs.get('product') or {}
        codes = [
            attrs.get('code'),
            attrs.get('merchantProductCode'),
            attrs.get('sku'),
            attrs.get('article'),
            product.get('code'),
            product.get('merchantProductCode'),
            product.get('sku'),
        ]
        code_text = next((str(code).strip() for code in codes if code), '')
        title = (
            product.get('name')
            or product.get('title')
            or attrs.get('name')
            or attrs.get('title')
            or category.get('title')
            or f"Позиция {attrs.get('entryNumber', '')}".strip()
        )
        qty = attrs.get('quantity') or 1
        total = attrs.get('totalPrice')
        prefix = f'[{code_text}] ' if code_text else ''
        lines.append(f'{prefix}{title} — {qty} шт. — {total or 0} ₸')
    return '\n'.join(lines)


def _kaspi_order_text(order_data: dict, entries: list[dict]) -> str:
    attrs = order_data.get('attributes') or {}
    lines = [
        f"Kaspi заказ {attrs.get('code') or order_data.get('id')}",
        f"Сумма: {attrs.get('totalPrice') or 0} ₸",
        f"Оплата: {attrs.get('paymentMode') or '—'}",
        f"Доставка: {attrs.get('deliveryMode') or '—'}",
    ]
    address = _kaspi_address_text(attrs)
    if address:
        lines.append(f'Адрес: {address}')
    entry_summary = _kaspi_entry_summary(entries)
    if entry_summary:
        lines.append('')
        lines.append(entry_summary)
    return '\n'.join(lines)


def _find_item_for_kaspi_entries(entries: list[dict]) -> Item | None:
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).all()
    for entry in entries:
        attrs = entry.get('attributes') or {}
        candidates = [
            attrs.get('code'),
            attrs.get('merchantProductCode'),
            attrs.get('sku'),
            attrs.get('article'),
        ]
        product = attrs.get('product') or {}
        candidates.extend([product.get('code'), product.get('merchantProductCode'), product.get('sku')])
        for candidate in candidates:
            code = (candidate or '').strip()
            if not code:
                continue
            found = next((item for item in items if (item.article or '').strip().lower() == code.lower()), None)
            if found:
                return found
    return None


def _existing_kaspi_lead(order_data: dict, fallback_code: str | None = None) -> Lead | None:
    attrs = order_data.get('attributes') or {}
    order_id = str(order_data.get('id') or '').strip()
    order_code = str(attrs.get('code') or fallback_code or '').strip()
    conditions = []
    if order_id:
        conditions.append(Lead.external_lead_id == order_id)
    if order_code:
        conditions.append(Lead.external_chat_id == order_code)
    if not conditions:
        return None
    return (
        Lead.query
        .filter(Lead.source_channel == 'KASPI', or_(*conditions))
        .order_by(Lead.id.desc())
        .first()
    )


def _create_kaspi_lead(order_data: dict, entries: list[dict], warehouse_id: int | None, assigned_user_id: int | None, fallback_code: str | None = None) -> Lead:
    attrs = order_data.get('attributes') or {}
    order_id = order_data.get('id')
    order_code = attrs.get('code') or fallback_code or order_id
    selected_item = _find_item_for_kaspi_entries(entries)
    message_text = _kaspi_order_text(order_data, entries)
    source_payload = {
        'order': order_data,
        'entries': entries,
    }
    created_at = _kaspi_dt(attrs.get('creationDate')) or datetime.utcnow()
    address = attrs.get('deliveryAddress') or {}
    lead = Lead(
        created_at=created_at,
        updated_at=datetime.utcnow(),
        status='NEW',
        conversation_status='new',
        priority='NORMAL',
        channel='kaspi',
        source_channel='KASPI',
        source_name='Kaspi Магазин',
        source_platform='kaspi_shop',
        source_account='KASPI_SHOP_TOKEN',
        external_chat_id=str(order_code) if order_code else None,
        external_lead_id=str(order_id) if order_id else str(order_code),
        source_payload=json.dumps(source_payload, ensure_ascii=False),
        customer_name=_kaspi_customer_name(attrs),
        phone=_kaspi_customer_phone(attrs),
        text=message_text,
        customer_city=address.get('town') if isinstance(address, dict) else None,
        interest_text=_kaspi_entry_summary(entries) or None,
        desired_item_id=selected_item.id if selected_item else None,
        desired_model=selected_item.name if selected_item else None,
        article_snapshot=selected_item.article if selected_item else None,
        product_name_snapshot=selected_item.name if selected_item else None,
        calculated_price=attrs.get('totalPrice'),
        warehouse_id=warehouse_id or None,
        assigned_user_id=assigned_user_id or None,
        comment='Импортировано из Kaspi Магазина. Токен хранится только в переменных окружения сервера.',
        last_message_at=datetime.utcnow(),
        last_message_text=message_text[:500],
        unread_count=1,
    )
    db.session.add(lead)
    db.session.flush()
    db.session.add(LeadMessage(
        lead_id=lead.id,
        created_at=datetime.utcnow(),
        direction='IN',
        sender_type='client',
        channel='KASPI',
        external_message_id=f'kaspi-order-{order_id or order_code}',
        sender_name=lead.customer_name,
        sender_contact=lead.phone,
        text=message_text,
        payload_json=json.dumps(source_payload, ensure_ascii=False),
        is_read=False,
    ))
    return lead


def _order_future_availability(item_id: int | None, warehouse_id: int | None):
    if not item_id or not warehouse_id:
        return None
    stock = [
        trailer for trailer in Trailer.query.filter(
            Trailer.item_id == item_id,
            Trailer.warehouse_id == warehouse_id,
            Trailer.status == 'IN_STOCK',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        ).order_by(Trailer.vin).all()
        if _trailer_available_for_sale(trailer)
    ]
    stock_needs = (
        SupplyNeed.query
        .filter(
            SupplyNeed.item_id == item_id,
            SupplyNeed.warehouse_id == warehouse_id,
            SupplyNeed.need_type.in_(['STOCK_REPLENISHMENT', 'WAREHOUSE_STOCK']),
            SupplyNeed.status.in_(['NEW', 'IN_PRODUCTION']),
        )
        .order_by(SupplyNeed.required_by.asc().nullslast(), SupplyNeed.created_at.desc())
        .all()
    )
    production_lines = (
        ProductionRequestLine.query
        .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        .filter(
            ProductionRequestLine.item_id == item_id,
            ProductionRequest.target_warehouse_id == warehouse_id,
            ProductionRequestLine.status.in_(['planned', 'PLANNED', 'in_production', 'partial_ready']),
        )
        .order_by(ProductionRequest.created_at.desc(), ProductionRequestLine.id.desc())
        .all()
    )
    produced_units = (
        ProducedUnit.query
        .filter(
            ProducedUnit.item_id == item_id,
            ProducedUnit.target_warehouse_id == warehouse_id,
            ProducedUnit.status == 'produced_no_vin',
        )
        .order_by(ProducedUnit.produced_at.desc().nullslast(), ProducedUnit.created_at.desc())
        .all()
    )
    inbound_movements = (
        StockMovement.query
        .join(Trailer, Trailer.id == StockMovement.trailer_id)
        .filter(
            Trailer.item_id == item_id,
            StockMovement.to_warehouse_id == warehouse_id,
            StockMovement.status.in_(['sent', 'in_transit']),
        )
        .order_by(StockMovement.arrival_date.asc().nullslast(), StockMovement.created_at.desc())
        .all()
    )
    return {
        'stock': stock,
        'stock_needs': stock_needs,
        'production_lines': production_lines,
        'produced_units': produced_units,
        'inbound_movements': inbound_movements,
    }


def _trailer_config_option_payload(items):
    return [{'code': item.code, 'name': item.name} for item in items]


def _filtered_trailer_config_options(config: dict | None = None) -> dict:
    config = config or {}
    selected_group = TrailerProductGroup.query.filter_by(code=config.get('group_code') or '002').first()
    selected_body_size = TrailerBodySize.query.filter_by(code=config.get('body_size_code') or '').first()
    option_warnings = []

    def allowed_items(option_type, model):
        base_query = model.query.filter_by(is_active=True)
        if not selected_group:
            return base_query.order_by(model.sort_order, model.id).all()
        allowed_ids = [
            row.option_id for row in TrailerAllowedOption.query.filter_by(
                group_id=selected_group.id,
                option_type=option_type,
                is_allowed=True,
            ).all()
        ]
        if not allowed_ids:
            return []
        return base_query.filter(model.id.in_(allowed_ids)).order_by(model.sort_order, model.id).all()

    def matrix_filtered_items(option_type, model, price_model, price_field, warning):
        allowed = allowed_items(option_type, model)
        if not selected_group:
            return allowed
        price_ids = {
            getattr(row, price_field) for row in price_model.query.filter_by(
                group_id=selected_group.id,
                is_active=True,
            ).all()
        }
        if not price_ids:
            option_warnings.append(warning)
        return allowed

    body_sizes = allowed_items('body_size', TrailerBodySize)
    board_heights = allowed_items('board_height', TrailerBoardHeight)
    if selected_body_size:
        board_price_ids = {
            row.board_height_id for row in TrailerBoardPriceMatrix.query.filter_by(
                body_size_id=selected_body_size.id,
                is_active=True,
            ).all()
        }
        if not board_price_ids:
            option_warnings.append(f'Для кузова {selected_body_size.code} не заведены цены бортов.')

    tents = allowed_items('tent', TrailerTentOption)
    if selected_body_size:
        tent_price_ids = {
            row.tent_option_id for row in TrailerTentPriceMatrix.query.filter_by(
                body_size_id=selected_body_size.id,
                is_active=True,
            ).all()
        }
        if not tent_price_ids:
            option_warnings.append(f'Для кузова {selected_body_size.code} не заведены цены тентов.')

    wheels = matrix_filtered_items(
        'wheel',
        TrailerWheelOption,
        TrailerWheelPriceMatrix,
        'wheel_option_id',
        f'Для группы {selected_group.code} не заведены цены колёс.' if selected_group else 'Не выбрана группа для цен колёс.',
    )
    hubs = matrix_filtered_items(
        'hub',
        TrailerHubOption,
        TrailerHubPriceMatrix,
        'hub_option_id',
        f'Для группы {selected_group.code} не заведены цены ступиц.' if selected_group else 'Не выбрана группа для цен ступиц.',
    )

    options = {
        'groups': TrailerProductGroup.query.filter_by(is_active=True).order_by(TrailerProductGroup.sort_order, TrailerProductGroup.code).all(),
        'body_sizes': body_sizes,
        'board_heights': board_heights,
        'wheels': wheels,
        'hubs': hubs,
        'support_wheels': allowed_items('support_wheel', TrailerSupportWheelOption),
        'tents': tents,
        'executions': allowed_items('body_execution', TrailerBodyExecution),
        'specials': allowed_items('special', TrailerSpecialOption),
        'option_warnings': option_warnings,
    }
    return options


def _trailer_config_form_context(config: dict | None = None):
    if config:
        return _filtered_trailer_config_options(config)
    return {
        'groups': TrailerProductGroup.query.filter_by(is_active=True).order_by(TrailerProductGroup.sort_order, TrailerProductGroup.code).all(),
        'body_sizes': TrailerBodySize.query.filter_by(is_active=True).order_by(TrailerBodySize.sort_order, TrailerBodySize.code).all(),
        'board_heights': TrailerBoardHeight.query.filter_by(is_active=True).order_by(TrailerBoardHeight.sort_order, TrailerBoardHeight.code).all(),
        'wheels': TrailerWheelOption.query.filter_by(is_active=True).order_by(TrailerWheelOption.sort_order, TrailerWheelOption.code).all(),
        'hubs': TrailerHubOption.query.filter_by(is_active=True).order_by(TrailerHubOption.sort_order, TrailerHubOption.code).all(),
        'support_wheels': TrailerSupportWheelOption.query.filter_by(is_active=True).order_by(TrailerSupportWheelOption.sort_order, TrailerSupportWheelOption.code).all(),
        'tents': TrailerTentOption.query.filter_by(is_active=True).order_by(TrailerTentOption.sort_order, TrailerTentOption.code).all(),
        'executions': TrailerBodyExecution.query.filter_by(is_active=True).order_by(TrailerBodyExecution.sort_order, TrailerBodyExecution.code).all(),
        'specials': TrailerSpecialOption.query.filter_by(is_active=True).order_by(TrailerSpecialOption.sort_order, TrailerSpecialOption.code).all(),
    }


def _config_from_request_values(values):
    getlist = getattr(values, 'getlist', None)
    specials = getlist('special_options') if getlist else values.get('special_options', [])
    if isinstance(specials, str):
        specials = [s for s in specials.split(',') if s]
    return {
        'group_code': values.get('group_code') or '002',
        'body_size_code': values.get('body_size_code') or '',
        'board_height_code': values.get('board_height_code') or '',
        'wheel_code': values.get('wheel_code') or '',
        'hub_code': values.get('hub_code') or '',
        'support_wheel_code': values.get('support_wheel_code') or '',
        'tent_code': values.get('tent_code') or '',
        'body_execution_code': values.get('body_execution_code') or 'BOARD',
        'special_options': specials,
    }


def _request_has_trailer_config(values) -> bool:
    return any((values.get(key) or '').strip() for key in ('group_code', 'body_size_code', 'board_height_code', 'wheel_code', 'hub_code', 'support_wheel_code', 'tent_code', 'body_execution_code'))


def _order_should_build_config(values, fulfillment_source: str) -> bool:
    return (
        fulfillment_source == 'production'
        and (
            (values.get('item_source') or '').strip() == 'config'
            or _request_has_trailer_config(values)
            or not (values.get('item_id') or '').strip()
            or (values.get('item_id') or '').strip() == '0'
        )
    )


def _snapshot_from_result(result: dict) -> dict:
    price = result.get('price') or {}
    dimensions = result.get('dimensions') or {}
    otss = result.get('otss') or {}
    return {
        'article_snapshot': result.get('article') or None,
        'product_name_snapshot': result.get('name') or None,
        'config_snapshot_json': json.dumps(result.get('config') or {}, ensure_ascii=False),
        'calculated_price': price.get('total_price'),
        'price_breakdown_json': json.dumps(price.get('price_breakdown') or [], ensure_ascii=False),
        'overall_dimensions_text': dimensions.get('overall_dimensions_text'),
        'inner_dimensions_text': dimensions.get('inner_dimensions_text'),
        'otss_number': otss.get('otss_number'),
        'otss_type': otss.get('otss_type'),
        'otss_modification': otss.get('otss_modification'),
        'vin_modification_code': otss.get('vin_modification_code'),
    }


def _apply_snapshot(obj, snapshot: dict | None) -> None:
    if not snapshot:
        return
    for key, value in snapshot.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


SNAPSHOT_FIELD_NAMES = (
    'article_snapshot', 'product_name_snapshot', 'config_snapshot_json', 'calculated_price',
    'price_breakdown_json', 'overall_dimensions_text', 'inner_dimensions_text', 'otss_number',
    'otss_type', 'otss_modification', 'vin_modification_code'
)


def _lead_snapshot(lead: Lead) -> dict:
    return {key: getattr(lead, key, None) for key in SNAPSHOT_FIELD_NAMES}


def _order_snapshot(order: CustomerOrder) -> dict:
    return {key: getattr(order, key, None) for key in SNAPSHOT_FIELD_NAMES}


def _primary_order_line(order: CustomerOrder) -> CustomerOrderLine | None:
    if not order or not order.id:
        return None
    return (
        CustomerOrderLine.query
        .filter_by(order_id=order.id)
        .order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc())
        .first()
    )


def _sync_primary_order_line(order: CustomerOrder, snapshot: dict | None = None) -> CustomerOrderLine:
    line = _primary_order_line(order)
    if not line:
        line = CustomerOrderLine(order_id=order.id, line_no=1)
        db.session.add(line)
    item_type = (order.item.item_type if order.item else '') or ''
    line.line_type = 'COMPONENT' if item_type.upper() == 'COMPONENT' else 'TRAILER'
    line.fulfillment_source = order.fulfillment_source
    line.item_id = order.item_id
    line.trailer_id = order.trailer_id
    line.quantity = order.quantity or 1
    line.total_price = order.price
    if order.price is not None and line.quantity:
        line.unit_price = Decimal(order.price) / Decimal(line.quantity)
    elif order.calculated_price is not None:
        line.unit_price = order.calculated_price
    else:
        line.unit_price = None
    line.status = order.status
    line.include_in_vehicle_contract = line.line_type == 'TRAILER'
    line.include_in_realization = True
    line.assembly_status = line.assembly_status or 'not_required'
    _apply_snapshot(line, snapshot or _order_snapshot(order))
    return line


def _sync_order_totals_from_lines(order: CustomerOrder) -> None:
    lines = list(order.lines.order_by(CustomerOrderLine.line_no.asc()).all()) if order and order.id else []
    if not lines:
        return
    order.quantity = sum(line.quantity or 0 for line in lines) or 1
    totals = [line.total_price for line in lines if line.total_price is not None]
    order.price = sum(totals) if totals else None
    first_line = lines[0]
    order.item_id = first_line.item_id
    order.fulfillment_source = first_line.fulfillment_source
    first_reservation = next((row for row in first_line.reservations if row.status == 'ACTIVE'), None)
    if first_reservation and first_reservation.trailer_id:
        order.trailer_id = first_reservation.trailer_id
        order.source_warehouse_id = first_reservation.trailer.warehouse_id if first_reservation.trailer else order.source_warehouse_id


def _next_order_line_no(order: CustomerOrder) -> int:
    max_no = db.session.query(sa.func.max(CustomerOrderLine.line_no)).filter(CustomerOrderLine.order_id == order.id).scalar()
    return int(max_no or 0) + 1


def _line_active_supply_needs(line: CustomerOrderLine) -> list[SupplyNeed]:
    return [
        need for need in line.supply_needs
        if need.status not in ('CANCELLED', 'cancelled', 'DONE', 'done', 'closed', 'void')
    ]


def _order_line_operation_blockers(line: CustomerOrderLine) -> list[str]:
    blockers = []
    if any(row.status == 'ACTIVE' for row in line.reservations):
        blockers.append('есть активный резерв')
    if line.vin_registry_rows:
        blockers.append('есть VIN')
    if _line_active_supply_needs(line):
        blockers.append('есть производственная потребность')
    if line.production_lines:
        blockers.append('есть производственное задание')
    if line.produced_units:
        blockers.append('есть выпуск')
    if line.movements:
        blockers.append('есть перемещение')
    if line.contract_lines:
        blockers.append('есть строка договора')
    if _line_has_any_realization(line):
        blockers.append('есть реализация')
    if line.production_outputs:
        blockers.append('есть производственные результаты')
    return blockers


def _order_line_quantity_blockers(line: CustomerOrderLine) -> list[str]:
    blockers = []
    if any(row.status == 'ACTIVE' for row in line.reservations):
        blockers.append('есть активный резерв')
    if line.vin_registry_rows:
        blockers.append('есть VIN')
    if line.production_lines:
        blockers.append('есть производственное задание')
    if line.produced_units:
        blockers.append('есть выпуск')
    if line.movements:
        blockers.append('есть перемещение')
    if line.contract_lines:
        blockers.append('есть строка договора')
    if _line_has_any_realization(line):
        blockers.append('есть реализация')
    return blockers


def _can_edit_order_line_details(line: CustomerOrderLine) -> tuple[bool, str]:
    if not line.order or line.order.documents_issued or line.order.is_shipped or line.order.status == 'cancelled':
        return False, 'Позиции можно менять только до выдачи документов и отгрузки.'
    if _line_has_any_realization(line):
        return False, 'По позиции уже есть реализация. Меняйте цену, количество и комментарий в документе реализации.'
    return True, ''


def _can_delete_order_line(line: CustomerOrderLine) -> tuple[bool, str]:
    if not line.order or line.order.documents_issued or line.order.is_shipped or line.order.status == 'cancelled':
        return False, 'Позиции можно удалять только до выдачи документов и отгрузки.'
    if line.order.lines.count() <= 1:
        return False, 'Нельзя удалить последнюю позицию заказа.'
    blockers = _order_line_operation_blockers(line)
    if blockers:
        return False, 'Нельзя удалить позицию: ' + ', '.join(blockers) + '.'
    return True, ''


def _can_edit_order_line_quantity(line: CustomerOrderLine) -> tuple[bool, str]:
    if not line.order or line.order.documents_issued or line.order.is_shipped or line.order.status == 'cancelled':
        return False, 'Позиции можно менять только до выдачи документов и отгрузки.'
    blockers = _order_line_quantity_blockers(line)
    if blockers:
        return False, 'Количество нельзя изменить: ' + ', '.join(blockers) + '.'
    return True, ''


def _line_has_posted_realization(line: CustomerOrderLine) -> bool:
    return any(row.realization and row.realization.status == 'posted' for row in line.realization_lines)


def _line_has_any_realization(line: CustomerOrderLine) -> bool:
    return bool(line.realization_lines)


def _active_stock_reservation_for_line(line: CustomerOrderLine) -> Reservation | None:
    return next((row for row in line.reservations if row.status == 'ACTIVE' and row.trailer_id), None)


def can_change_order_line_source(line: CustomerOrderLine) -> tuple[bool, list[str]]:
    blockers = []
    order = line.order
    if not order:
        blockers.append('нет заказа')
        return False, blockers
    if order.status == 'cancelled':
        blockers.append('заказ отменён')
    if order.documents_issued:
        blockers.append('выданы документы')
    if order.is_shipped or line.status == 'shipped':
        blockers.append('заказ или позиция уже отгружены')
    if line.contract_lines:
        blockers.append('есть строка договора')
    if _line_has_any_realization(line):
        blockers.append('есть реализация')
    if line.movements:
        blockers.append('есть перемещение')
    if line.produced_units:
        blockers.append('есть выпуск')
    if line.production_outputs:
        blockers.append('есть производственные результаты')
    if line.assembly_operation_id:
        blockers.append('есть операция комплектации')
    for need in _line_active_supply_needs(line):
        if _supply_need_started(need) or _supply_need_has_produced_output(need):
            blockers.append(f'производство по потребности #{need.id} уже начато')
    return not blockers, blockers


def _ensure_can_change_line_source(line: CustomerOrderLine) -> tuple[bool, str]:
    ok, blockers = can_change_order_line_source(line)
    if not ok:
        return False, 'Нельзя изменить источник обеспечения: ' + '; '.join(blockers) + '.'
    if (line.quantity or 1) != 1:
        return False, 'Смена источника сейчас доступна только для строки с количеством 1. Для нескольких VIN используйте отдельные строки заказа.'
    return True, ''


def _release_line_stock_links(line: CustomerOrderLine, reason: str) -> None:
    order = line.order
    trailer_ids = set()
    if line.trailer_id:
        trailer_ids.add(line.trailer_id)
    reservation = _active_stock_reservation_for_line(line)
    if reservation and reservation.trailer_id:
        trailer_ids.add(reservation.trailer_id)

    vin_rows = list(_active_vin_rows_for_order_line(line))
    trailer_ids.update(row.trailer_id for row in vin_rows if row.trailer_id)

    for row in vin_rows:
        if row.docs_issued_at:
            raise ValueError('Нельзя снять источник: по VIN уже выданы документы.')
        if row.status == 'reserved' and not row.trailer_id:
            _free_reserved_vin_row(row, order, reason)
        else:
            _release_assigned_vin_from_order(row, order, reason)
            row.order_line_id = None

    for row in line.reservations:
        if row.status == 'ACTIVE':
            row.status = 'CANCELLED'
            row.note = ((row.note or '') + f'\nРезерв отменён. Причина: {reason}').strip()

    for trailer_id in trailer_ids:
        trailer = Trailer.query.get(trailer_id)
        if not trailer:
            continue
        if _active_movement_for_trailer(trailer.id):
            raise ValueError('Нельзя снять источник: по прицепу есть активное перемещение.')
        trailer.status = 'IN_STOCK'
        trailer.lifecycle_status = 'in_stock'

    line.trailer_id = None


def change_line_source_production_to_stock(line_id: int, trailer_id: int, user_id: int, comment: str | None = None) -> CustomerOrderLine:
    line = CustomerOrderLine.query.get_or_404(line_id)
    order = line.order
    ok, message = _ensure_can_change_line_source(line)
    if not ok:
        raise ValueError(message)
    if (line.fulfillment_source or '').lower() != 'production':
        raise ValueError('Эта позиция не находится в источнике "производство".')

    trailer = Trailer.query.get_or_404(trailer_id)
    if line.item_id and trailer.item_id != line.item_id:
        raise ValueError('Выбранный прицеп не соответствует номенклатуре строки заказа.')
    if not _trailer_available_for_sale(trailer, exclude_order_id=order.id):
        raise ValueError('Выбранный прицеп уже недоступен для резерва.')

    reason = comment or 'Смена источника обеспечения: производство -> наличие'
    for need in _line_active_supply_needs(line):
        if _supply_need_started(need) or _supply_need_has_produced_output(need):
            raise ValueError(f'Производство по потребности #{need.id} уже начато.')
        _cancel_supply_need_and_production_lines(need, reason, user_id)

    source_type = 'TRANSFER' if order.warehouse_id and trailer.warehouse_id != order.warehouse_id else 'STOCK'
    line.fulfillment_source = 'other_warehouse' if source_type == 'TRANSFER' else 'stock'
    line.trailer_id = trailer.id
    line.item_id = trailer.item_id
    line.status = 'reserved'
    if line.unit_price is None and trailer.item:
        line.unit_price = trailer.item.base_price
        line.total_price = trailer.item.base_price
    _apply_snapshot(line, _line_snapshot_from_item(trailer.item))

    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    db.session.add(Reservation(
        order_id=order.id,
        order_line_id=line.id,
        trailer_id=trailer.id,
        item_id=trailer.item_id,
        source_type=source_type,
        status='ACTIVE',
        priority=10,
        note=reason,
    ))
    row = _ensure_vin_registry_for_trailer(trailer)
    if row:
        row.customer_order_id = order.id
        row.order_line_id = line.id
        row.supply_need_id = None
        if row.status == 'free':
            old_status = row.status
            row.status = 'assigned'
            row.assigned_by_user_id = user_id
            row.assigned_at = datetime.utcnow()
            _add_vin_event(row, 'assigned', old_status, row.status, comment=reason)
        if not order.reserved_vin_registry_id:
            order.reserved_vin_registry_id = row.id

    if not order.trailer_id:
        order.trailer_id = trailer.id
    order.source_warehouse_id = trailer.warehouse_id
    _sync_order_totals_from_lines(order)
    _refresh_order_status(order)
    add_order_event(order, 'trailer_reserved', new_value=trailer.vin or trailer.id, comment=f'Позиция #{line.line_no}: {reason}')
    return line


def change_line_source_stock_to_production(line_id: int, target_item_id: int, qty: int, user_id: int, comment: str | None = None) -> CustomerOrderLine:
    line = CustomerOrderLine.query.get_or_404(line_id)
    order = line.order
    ok, message = _ensure_can_change_line_source(line)
    if not ok:
        raise ValueError(message)
    if (line.fulfillment_source or '').lower() not in ('stock', 'other_warehouse', 'transit'):
        raise ValueError('Эта позиция не находится в источнике "наличие".')
    if _line_has_posted_realization(line):
        raise ValueError('По позиции уже есть проведённая реализация.')

    item = Item.query.get_or_404(target_item_id)
    reason = comment or 'Смена источника обеспечения: наличие -> производство'
    old_trailer_id = line.trailer_id or (_active_stock_reservation_for_line(line).trailer_id if _active_stock_reservation_for_line(line) else None)
    _release_line_stock_links(line, reason)

    quantity = max(int(qty or 1), 1)
    line.fulfillment_source = 'production'
    line.item_id = item.id
    line.quantity = quantity
    line.status = 'waiting_production'
    if line.unit_price is None:
        line.unit_price = item.base_price
    line.total_price = (line.unit_price * quantity) if line.unit_price is not None else None
    _apply_snapshot(line, _line_snapshot_from_item(item))

    need = SupplyNeed(
        order_id=order.id,
        order_line_id=line.id,
        item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity=quantity,
        status='NEW',
        priority=10,
        need_type='CUSTOMER_ORDER',
        required_by=order.expected_date,
        note=reason,
    )
    _apply_snapshot(need, _line_snapshot_from_item(item))
    db.session.add(need)

    if order.trailer_id == old_trailer_id:
        order.trailer_id = None
    _sync_order_totals_from_lines(order)
    if order.trailer_id == old_trailer_id:
        order.trailer_id = None
    if not any(row.trailer_id for row in order.reservations.filter_by(status='ACTIVE').all()):
        order.trailer_id = None
        order.source_warehouse_id = None
    _refresh_order_status(order)
    add_order_event(order, 'production_need_created', new_value=item.article or item.name, comment=f'Позиция #{line.line_no}: {reason}')
    return line


def _active_vin_rows_for_order_line(line: CustomerOrderLine) -> list[VinRegistry]:
    return (
        VinRegistry.query
        .filter(
            VinRegistry.order_line_id == line.id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(VinRegistry.confirmed_at.desc().nullslast(), VinRegistry.assigned_at.desc().nullslast(), VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
        .all()
    )


def _trailers_for_order_line(line: CustomerOrderLine) -> list[Trailer]:
    trailers = []
    seen_ids = set()
    for row in _active_vin_rows_for_order_line(line):
        if row.trailer and row.trailer.id not in seen_ids:
            trailers.append(row.trailer)
            seen_ids.add(row.trailer.id)
    for reservation in line.reservations:
        if reservation.status == 'ACTIVE' and reservation.trailer and reservation.trailer.id not in seen_ids:
            trailers.append(reservation.trailer)
            seen_ids.add(reservation.trailer.id)
    for contract_line in line.contract_lines:
        if contract_line.trailer and contract_line.trailer.id not in seen_ids:
            trailers.append(contract_line.trailer)
            seen_ids.add(contract_line.trailer.id)
    return trailers


def _trailer_for_order_line(line: CustomerOrderLine) -> Trailer | None:
    trailers = _trailers_for_order_line(line)
    return trailers[0] if trailers else None


def _order_lines_document_blockers(order: CustomerOrder) -> list[str]:
    blockers = []
    lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    if not lines:
        blockers.append('нет позиций заказа')
        return blockers
    for line in lines:
        vin_count = len(_active_vin_rows_for_order_line(line))
        if line.line_type == 'TRAILER' and vin_count < (line.quantity or 1):
            blockers.append(f'по позиции #{line.line_no} не хватает VIN: {vin_count}/{line.quantity or 1}')
    return blockers


ORDER_LIST_FILTERS = [
    ('all', 'Все'),
    ('new', 'Новые'),
    ('waiting_vin', 'Ждут VIN'),
    ('waiting_vin_confirm', 'Ждут подтверждения VIN'),
    ('waiting_kit', 'Ждут комплектацию'),
    ('waiting_payment', 'Ждут оплату'),
    ('waiting_contract', 'Ждут договор'),
    ('waiting_docs', 'Ждут документы'),
    ('waiting_realization', 'Ждут реализацию'),
    ('waiting_movement', 'Ждут перемещение'),
    ('ready_to_ship', 'Готовы к отгрузке'),
    ('shipped', 'Отгруженные'),
    ('problem', 'Проблемные'),
]


def _order_primary_line_state(order: CustomerOrder) -> dict:
    lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    if not lines:
        return {'vin_rows': [], 'trailers': [], 'missing_vin': True, 'assigned_unconfirmed': False, 'active_needs': [], 'active_movements': []}

    vin_rows = []
    trailers = []
    active_needs = []
    active_movements = []
    missing_vin = False
    assigned_unconfirmed = False
    seen_trailer_ids = set()

    for line in lines:
        line_vins = _active_vin_rows_for_order_line(line)
        vin_rows.extend(line_vins)
        assigned_unconfirmed = assigned_unconfirmed or any(row.status == 'assigned' for row in line_vins)
        line_trailers = _trailers_for_order_line(line)
        for trailer in line_trailers:
            if trailer and trailer.id not in seen_trailer_ids:
                trailers.append(trailer)
                seen_trailer_ids.add(trailer.id)
        active_needs.extend(_line_active_supply_needs(line))
        active_movements.extend([row for row in line.movements if row.status in ('DRAFT', 'draft', 'sent', 'in_transit')])
        if line.line_type == 'TRAILER' and len(line_vins) < (line.quantity or 1) and not line_trailers:
            missing_vin = True

    return {
        'vin_rows': vin_rows,
        'trailers': trailers,
        'missing_vin': missing_vin,
        'assigned_unconfirmed': assigned_unconfirmed,
        'active_needs': active_needs,
        'active_movements': active_movements,
    }


def _order_needs_transfer(order: CustomerOrder, trailers: list[Trailer]) -> bool:
    if not order.warehouse_id:
        return False
    for trailer in trailers:
        if trailer.warehouse_id and trailer.warehouse_id != order.warehouse_id:
            return True
    return False


def _order_list_state(order: CustomerOrder) -> dict:
    line_state = _order_primary_line_state(order)
    trailers = line_state['trailers']
    source_warehouse = order.source_warehouse
    if not source_warehouse and trailers:
        source_warehouse = trailers[0].warehouse

    if order.status == 'cancelled':
        code, label, blocker, next_action = 'problem', 'Отменена', 'заказ отменён', 'проверить историю'
    elif order.is_shipped or order.status == 'shipped':
        code, label, blocker, next_action = 'shipped', 'Отгружена', '', 'закрыто'
    elif not order.lines.count():
        code, label, blocker, next_action = 'new', 'Новая', 'нет строк заказа', 'добавить позицию'
    elif line_state['assigned_unconfirmed']:
        code, label, blocker, next_action = 'waiting_vin_confirm', 'Ждёт подтверждения VIN', 'VIN назначен, но не подтверждён', 'подтвердить нанесение VIN'
    elif line_state['missing_vin']:
        code, label, blocker, next_action = 'waiting_vin', 'Ждёт VIN', 'нет активного VIN по строке', 'назначить или привязать VIN'
    elif any((need.status or '').upper() in ('NEW', 'PLANNED', 'SENT_TO_PRODUCTION', 'IN_PRODUCTION', 'PARTIALLY_DONE') for need in line_state['active_needs']):
        code, label, blocker, next_action = 'waiting_kit', 'Ждёт комплектацию', 'есть активная производственная потребность', 'завершить производство/комплектацию'
    elif order.remaining_amount and order.remaining_amount > 0:
        code, label, blocker, next_action = 'waiting_payment', 'Ждёт оплату', 'есть остаток к оплате', 'получить оплату'
    elif not SalesContract.query.filter_by(order_id=order.id).first():
        code, label, blocker, next_action = 'waiting_contract', 'Ждёт договор', 'договор не создан', 'создать договор'
    elif order.document_status not in ('documents_issued', 'documents_ready'):
        code, label, blocker, next_action = 'waiting_docs', 'Ждёт документы', 'документы не выданы', 'подготовить договор/документы'
    elif order.realization_status != 'realized':
        code, label, blocker, next_action = 'waiting_realization', 'Ждёт реализацию', 'реализация не проведена', 'создать и провести реализацию'
    elif _order_needs_transfer(order, trailers):
        code, label, blocker, next_action = 'waiting_movement', 'Ждёт перемещение', 'техника не на складе выдачи', 'создать перемещение'
    else:
        code, label, blocker, next_action = 'ready_to_ship', 'Готова к отгрузке', '', 'провести реализацию/закрыть выдачу'

    return {
        'code': code,
        'label': label,
        'blocker': blocker,
        'next_action': next_action,
        'source_warehouse': source_warehouse,
        'trailers': trailers,
        'vin': ', '.join([row.vin_full or row.serial7 for row in line_state['vin_rows'] if row.vin_full or row.serial7]) or (trailers[0].vin if trailers else ''),
    }


def _order_line_can_ship(line: CustomerOrderLine) -> tuple[bool, str]:
    order = line.order
    if not order or not order.documents_issued or order.status == 'cancelled':
        return False, 'Сначала должны быть выданы документы.'
    if line.status == 'shipped':
        return False, 'Позиция уже отгружена.'
    if line.line_type == 'TRAILER':
        trailers = _trailers_for_order_line(line)
        if len(trailers) < (line.quantity or 1):
            return False, 'По позиции нет привязанного прицепа/VIN.'
        for trailer in trailers:
            if trailer.status != 'SOLD':
                return False, 'Прицеп ещё не переведён в SOLD.'
            if not current_user.is_admin and order.warehouse_id and trailer.warehouse_id != order.warehouse_id:
                return False, 'Прицеп ещё не на складе выдачи.'
            if _trailer_is_customer_shipped(trailer):
                return False, 'Прицеп уже отгружен клиенту.'
    return True, ''


def _refresh_order_shipment_state(order: CustomerOrder) -> None:
    lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    if lines and all(line.status == 'shipped' for line in lines):
        order.is_shipped = True
        order.shipped_at = order.shipped_at or datetime.utcnow()
        order.status = 'shipped'
        contract = SalesContract.query.filter_by(order_id=order.id).first()
        if contract:
            contract.is_shipped = True


def _apply_realization_shipment_effect(realization: SalesRealization) -> None:
    order = realization.order
    affected_lines = []
    for line in realization.lines:
        if line.order_line:
            line.order_line.status = 'shipped'
            affected_lines.append(line.order_line)
            for reservation in line.order_line.reservations:
                if reservation.status == 'ACTIVE':
                    reservation.status = 'CLOSED'
        if line.inventory_effect == 'trailer_unit' and line.trailer:
            line.trailer.status = 'SOLD'
            line.trailer.lifecycle_status = 'customer_shipped'

    if order:
        _refresh_order_shipment_state(order)
        if affected_lines and not order.is_shipped:
            order.is_shipped = True
            order.shipped_at = order.shipped_at or datetime.utcnow()
            order.status = 'shipped'
        contract = SalesContract.query.filter_by(order_id=order.id).first()
        if contract:
            contract.is_shipped = bool(order.is_shipped)


def _revert_realization_shipment_effect(realization: SalesRealization) -> None:
    order = realization.order
    for line in realization.lines:
        if line.order_line and line.order_line.status == 'shipped':
            line.order_line.status = 'NEW'
        if line.inventory_effect == 'trailer_unit' and line.trailer:
            line.trailer.status = 'RESERVED'
            line.trailer.lifecycle_status = 'reserved'
            for reservation in line.trailer.reservations:
                if order and reservation.order_id == order.id and reservation.status == 'CLOSED':
                    reservation.status = 'ACTIVE'

    if order:
        order.is_shipped = False
        order.shipped_at = None
        contract = SalesContract.query.filter_by(order_id=order.id).first()
        if contract:
            contract.is_shipped = False
        _refresh_order_status(order)


def _line_snapshot_from_item(item: Item | None) -> dict:
    if not item:
        return {}
    return {
        'article_snapshot': item.article,
        'product_name_snapshot': item.name,
        'calculated_price': item.base_price,
        'overall_dimensions_text': item.size_external,
        'inner_dimensions_text': item.size_body,
    }


def _create_order_line_from_trailer(order: CustomerOrder, trailer: Trailer, source_type: str = 'STOCK') -> CustomerOrderLine:
    line = CustomerOrderLine(
        order_id=order.id,
        line_no=_next_order_line_no(order),
        line_type='TRAILER',
        fulfillment_source='stock' if source_type == 'STOCK' else 'other_warehouse',
        item_id=trailer.item_id,
        quantity=1,
        unit_price=trailer.item.base_price if trailer.item else None,
        total_price=trailer.item.base_price if trailer.item else None,
        status='reserved',
    )
    _apply_snapshot(line, _line_snapshot_from_item(trailer.item))
    db.session.add(line)
    db.session.flush()
    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    reservation = Reservation(
        order_id=order.id,
        order_line_id=line.id,
        trailer_id=trailer.id,
        item_id=trailer.item_id,
        source_type=source_type,
        status='ACTIVE',
        priority=10,
        note='Резерв по позиции заказа',
    )
    db.session.add(reservation)
    row = _ensure_vin_registry_for_trailer(trailer)
    if row:
        row.customer_order_id = order.id
        row.order_line_id = line.id
        if row.status == 'free':
            row.status = 'assigned'
    return line


def _create_order_line_from_item_for_production(
    order: CustomerOrder,
    item: Item,
    snapshot: dict | None = None,
    quantity: int = 1,
    unit_price=None,
    note: str | None = None,
) -> tuple[CustomerOrderLine, SupplyNeed]:
    quantity = max(int(quantity or 1), 1)
    snapshot = snapshot or _line_snapshot_from_item(item)
    if unit_price is None:
        unit_price = snapshot.get('calculated_price') if snapshot else None
    if unit_price is None:
        unit_price = item.base_price
    line = CustomerOrderLine(
        order_id=order.id,
        line_no=_next_order_line_no(order),
        line_type='TRAILER' if str(item.item_type or '').upper() == 'TRAILER' else 'COMPONENT',
        fulfillment_source='production',
        item_id=item.id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=(unit_price * quantity) if unit_price is not None else None,
        status='waiting_production',
        note=note,
    )
    _apply_snapshot(line, snapshot)
    db.session.add(line)
    db.session.flush()
    need = SupplyNeed(
        order_id=order.id,
        order_line_id=line.id,
        item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity=quantity,
        status='NEW',
        priority=10,
        need_type='CUSTOMER_ORDER',
        required_by=order.expected_date,
        note='Потребность создана по позиции заказа',
    )
    _apply_snapshot(need, snapshot)
    db.session.add(need)
    return line, need


def _order_delete_blockers(order: CustomerOrder) -> list[str]:
    blockers = []
    if order.documents_issued:
        blockers.append('выданы документы')
    if order.is_shipped:
        blockers.append('заказ отгружен')
    if order.trailer_id:
        blockers.append('привязан VIN/прицеп')
    if order.reserved_vin_registry_id:
        blockers.append('зарезервирован VIN')
    if SalesContract.query.filter_by(order_id=order.id).count():
        blockers.append('есть договор')
    if OrderPayment.query.filter_by(order_id=order.id).count():
        blockers.append('есть оплаты')
    if Reservation.query.filter_by(order_id=order.id).count():
        blockers.append('есть резервы')
    if SupplyNeed.query.filter_by(order_id=order.id).count():
        blockers.append('есть потребности производства/снабжения')
    if StockMovement.query.filter_by(order_id=order.id).count():
        blockers.append('есть перемещения')
    if ProducedUnit.query.filter_by(order_id=order.id).count():
        blockers.append('есть выпущенные единицы')
    if VinRegistry.query.filter(
        or_(
            VinRegistry.customer_order_id == order.id,
            VinRegistry.docs_issued_order_id == order.id,
        )
    ).count():
        blockers.append('есть записи в реестре VIN')
    if getattr(order, 'payment_schedule', None) is not None and order.payment_schedule.count():
        blockers.append('есть график оплат')

    line_ids = [line.id for line in order.lines.all()]
    if line_ids:
        if ProductionRequestLine.query.filter(ProductionRequestLine.order_line_id.in_(line_ids)).count():
            blockers.append('есть строки производственного задания')
        if StockMovement.query.filter(StockMovement.order_line_id.in_(line_ids)).count():
            blockers.append('есть перемещения по строкам')
        if ProducedUnit.query.filter(ProducedUnit.order_line_id.in_(line_ids)).count():
            blockers.append('есть выпущенные единицы по строкам')
        if VinRegistry.query.filter(VinRegistry.order_line_id.in_(line_ids)).count():
            blockers.append('есть VIN по строкам')

    return blockers


def _create_contract_lines_from_order(contract: SalesContract, order: CustomerOrder) -> None:
    existing = SalesContractLine.query.filter_by(sales_contract_id=contract.id).first()
    if existing:
        return
    lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    if not lines:
        lines = [_sync_primary_order_line(order, _order_snapshot(order))]
        db.session.flush()
    for source_line in lines:
        if not getattr(source_line, 'include_in_vehicle_contract', False):
            continue
        vin_row = (
            VinRegistry.query
            .filter(
                VinRegistry.order_line_id == source_line.id,
                VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
            )
            .order_by(VinRegistry.confirmed_at.desc().nullslast(), VinRegistry.assigned_at.desc().nullslast(), VinRegistry.id.desc())
            .first()
        )
        reservation = next((row for row in getattr(source_line, 'reservations', []) if row.status == 'ACTIVE' and row.trailer_id), None)
        trailer = vin_row.trailer if vin_row and vin_row.trailer else (reservation.trailer if reservation else None)
        contract_line = SalesContractLine(
            sales_contract_id=contract.id,
            order_line_id=source_line.id,
            trailer_id=trailer.id if trailer else None,
            item_id=source_line.item_id,
            vin_registry_id=vin_row.id if vin_row else None,
            line_no=source_line.line_no,
            quantity=source_line.quantity or 1,
            unit_price=source_line.unit_price,
            total_price=source_line.total_price,
            article_snapshot=source_line.article_snapshot,
            product_name_snapshot=source_line.product_name_snapshot,
            otss_number=source_line.otss_number,
            otss_type=source_line.otss_type,
            otss_modification=source_line.otss_modification,
            vin_modification_code=source_line.vin_modification_code,
            vin_full=vin_row.vin_full if vin_row else (trailer.vin if trailer else None),
        )
        db.session.add(contract_line)
        if vin_row:
            vin_row.sales_contract_id = contract.id


def get_order_effective_vin(order: CustomerOrder) -> str:
    if order.trailer and order.trailer.vin:
        return order.trailer.vin
    row = (
        VinRegistry.query
        .filter(
            VinRegistry.customer_order_id == order.id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
        .first()
    )
    return row.vin_full if row else ''


@main_bp.app_template_global('get_order_effective_vin')
def get_order_effective_vin_template(order: CustomerOrder) -> str:
    return get_order_effective_vin(order)


@main_bp.app_template_global('production_config_summary')
def production_config_summary(line: ProductionRequestLine) -> list[str]:
    raw = getattr(line, 'config_snapshot_json', None)
    if not raw and getattr(line, 'supply_need', None):
        raw = line.supply_need.config_snapshot_json
    return _config_snapshot_summary(raw)


@main_bp.app_template_global('production_vin_row_for_line')
def production_vin_row_for_line(line: ProductionRequestLine) -> VinRegistry | None:
    need = getattr(line, 'supply_need', None)
    order = need.order if need and need.order_id else None
    return _vin_registry_for_order_or_need(order, need)


@main_bp.app_template_global('production_vin_row_for_unit')
def production_vin_row_for_unit(unit: ProducedUnit) -> VinRegistry | None:
    context = _produced_unit_context(unit)
    return context.get('vin_registry')


def _active_supply_need_for_order(order_id: int | None):
    if not order_id:
        return None
    return (
        SupplyNeed.query
        .filter(
            SupplyNeed.order_id == order_id,
            SupplyNeed.status.in_(['NEW', 'PLANNED', 'SENT_TO_PRODUCTION', 'IN_PRODUCTION', 'PARTIALLY_DONE']),
        )
        .order_by(SupplyNeed.created_at.desc())
        .first()
    )


def order_can_request_production(order: CustomerOrder) -> tuple[bool, str]:
    if not order or order.status in ('cancelled', 'canceled', 'closed', 'done') or order.is_shipped or order.documents_issued:
        return False, 'Заказ уже обеспечен или закрыт. Заявка в производство не требуется.'
    if order.trailer_id:
        return False, 'По заказу уже выбран готовый прицеп. Заявка в производство не требуется.'
    existing = _active_supply_need_for_order(order.id)
    if existing:
        return False, f'По этому заказу уже есть активная заявка в производство №{existing.id}.'
    if not order.item_id:
        return False, 'Для заявки в производство нужна выбранная конфигурация или номенклатура.'
    return True, ''


def _order_vin_modification_code(order: CustomerOrder) -> str:
    if order.vin_modification_code:
        return order.vin_modification_code
    need = order.supply_needs.order_by(SupplyNeed.created_at.desc()).first() if order and order.id else None
    if need and need.vin_modification_code:
        return need.vin_modification_code
    if order.item and order.item.article:
        # Fallback for old rows. The configured snapshot is preferred.
        return ''
    return ''


def _order_line_vin_modification_code(line: CustomerOrderLine | None, order: CustomerOrder | None = None) -> str:
    if line and line.vin_modification_code:
        return line.vin_modification_code
    if line and line.supply_needs:
        for need in line.supply_needs:
            if need.vin_modification_code:
                return need.vin_modification_code
    return _order_vin_modification_code(order or (line.order if line else None))


def _active_vin_registry_for_order_line(order_line_id: int | None):
    if not order_line_id:
        return None
    return (
        VinRegistry.query
        .filter(
            VinRegistry.order_line_id == order_line_id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
        .first()
    )


def _active_vin_registry_count_for_order_line(order_line_id: int | None) -> int:
    if not order_line_id:
        return 0
    return (
        VinRegistry.query
        .filter(
            VinRegistry.order_line_id == order_line_id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .count()
    )


def _order_line_vin_capacity_left(line: CustomerOrderLine | None) -> int:
    if not line:
        return 0
    return max((line.quantity or 1) - _active_vin_registry_count_for_order_line(line.id), 0)


def _parse_vin_full(vin_full: str) -> tuple[dict | None, str | None]:
    vin = (vin_full or '').strip().upper()
    if len(vin) != 17:
        return None, 'VIN должен быть длиной 17 символов.'
    prefix = vin[:3]
    modification = vin[3:9]
    year_code = vin[9:10]
    serial7 = vin[-7:]
    if prefix != 'MX4':
        return None, 'VIN должен начинаться с MX4.'
    if len(modification) != 6:
        return None, 'VIN-модификация должна быть длиной 6 символов.'
    if not serial7.isdigit():
        return None, 'Последние 7 символов VIN должны быть цифрами.'
    return {
        'vin_full': vin,
        'prefix': prefix,
        'vin_modification_code': modification,
        'year_code': year_code,
        'serial7': serial7,
    }, None


def _normalize_serial7(serial7: str) -> tuple[str | None, str | None]:
    serial = (serial7 or '').strip()
    if not serial.isdigit() or len(serial) != 7:
        return None, 'serial7 должен состоять из 7 цифр.'
    return serial, None


def _serial7_from_vin_full(vin_full: str | None) -> str | None:
    vin = (vin_full or '').strip().upper()
    if len(vin) != 17 or not vin.startswith('MX4'):
        return None
    serial = vin[-7:]
    return serial if serial.isdigit() else None


def _max_known_serial7_value() -> int:
    max_value = 0
    used_registry_rows = VinRegistry.query.filter(or_(VinRegistry.vin_full.isnot(None), VinRegistry.status != 'free')).with_entities(VinRegistry.serial7).all()
    for (serial,) in used_registry_rows:
        if serial and str(serial).isdigit():
            max_value = max(max_value, int(serial))
    for (vin,) in Trailer.query.with_entities(Trailer.vin).filter(Trailer.vin.isnot(None)).all():
        serial = _serial7_from_vin_full(vin)
        if serial:
            max_value = max(max_value, int(serial))
    return max_value


def _next_serial7() -> str:
    candidate = _max_known_serial7_value() + 1
    while True:
        serial = f'{candidate:07d}'
        row = VinRegistry.query.filter_by(serial7=serial).first()
        if not row or (row.status == 'free' and not row.vin_full):
            return serial
        candidate += 1


def _free_vin_row_for_reservation() -> VinRegistry:
    serial = _next_serial7()
    row = VinRegistry.query.filter_by(serial7=serial).first()
    if row:
        return row
    row = VinRegistry(serial7=serial, status='free', prefix='MX4', source='generated')
    db.session.add(row)
    db.session.flush()
    _add_vin_event(row, 'created', None, 'free', comment='Автоматически сгенерирован serial7')
    return row


def _add_vin_event(vin: VinRegistry, event_type: str, old_status: str | None = None, new_status: str | None = None, comment: str | None = None) -> None:
    db.session.add(VinRegistryEvent(
        vin_registry_id=vin.id,
        event_type=event_type,
        old_status=old_status,
        new_status=new_status,
        customer_order_id=vin.customer_order_id,
        order_line_id=vin.order_line_id,
        sales_contract_id=vin.sales_contract_id,
        trailer_id=vin.trailer_id,
        user_id=current_user.id if current_user and current_user.is_authenticated else None,
        comment=comment,
    ))


def _active_vin_registry_for_order(order_id: int | None):
    if not order_id:
        return None
    return (
        VinRegistry.query
        .filter(
            VinRegistry.customer_order_id == order_id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
        .first()
    )


def _supply_need_has_production(need: SupplyNeed) -> bool:
    return bool(need.production_lines)


def _supply_need_started(need: SupplyNeed) -> bool:
    for line in need.production_lines:
        if (line.status or '').lower() in ('in_production', 'partial_ready', 'ready', 'closed') or (line.produced_qty or 0) > 0 or line.started_at:
            return True
        if line.produced_units.count() > 0:
            return True
    return False


def _supply_need_has_produced_output(need: SupplyNeed) -> bool:
    for line in need.production_lines:
        if (line.produced_qty or 0) > 0:
            return True
        if (line.status or '').lower() in ('ready', 'closed'):
            return True
        if line.produced_units.count() > 0:
            return True
    return False


def _cancel_supply_need_and_production_lines(need: SupplyNeed, reason: str, user_id: int | None = None) -> None:
    now = datetime.utcnow()
    need.status = 'CANCELLED'
    need.cancelled_at = now
    need.cancelled_by_user_id = user_id
    need.cancel_reason = reason

    touched_requests = set()
    for line in need.production_lines:
        line.status = 'CANCELLED'
        line.cancelled_at = now
        line.cancelled_by_user_id = user_id
        line.cancel_reason = reason
        line.note = ((line.note or '') + f'\n{reason}').strip()
        if line.production_request_id:
            touched_requests.add(line.production_request_id)

    for request_id in touched_requests:
        production_request = ProductionRequest.query.get(request_id)
        if production_request and all((line.status or '').upper() == 'CANCELLED' for line in production_request.lines):
            production_request.status = 'CANCELLED'

def _ensure_can_cancel_order_reservation(order: CustomerOrder) -> None:
    if current_user.is_admin or current_user.is_director:
        return
    if current_user.is_manager and can_access_order(order):
        return
    abort(403)

def _order_has_issued_documents_or_shipment(order: CustomerOrder) -> bool:
    return bool(
        order.documents_issued
        or order.is_shipped
        or (order.status or '').lower() in ('shipped', 'done', 'customer_shipped')
    )


def _cancel_order_active_reservations(order: CustomerOrder, trailer_id: int | None, reason: str) -> None:
    query = order.reservations.filter_by(status='ACTIVE')
    if trailer_id:
        query = query.filter(Reservation.trailer_id == trailer_id)
    for reservation in query.all():
        reservation.status = 'CANCELLED'
        reservation.note = ((reservation.note or '') + f'\nРезерв отменён. Причина: {reason}').strip()


def _free_reserved_vin_row(row: VinRegistry, order: CustomerOrder, reason: str) -> None:
    old_status = row.status
    _add_vin_event(row, 'reservation_cancelled', old_status, 'free', comment=reason)
    row.customer_order_id = None
    row.supply_need_id = None
    row.reserved_by_user_id = None
    row.assigned_by_user_id = None
    row.confirmed_by_user_id = None
    row.reserved_at = None
    row.assigned_at = None
    row.confirmed_at = None
    row.status = 'free'
    row.vin_full = None
    row.vin_modification_code = None
    row.year_code = None
    if order.reserved_vin_registry_id == row.id:
        order.reserved_vin_registry_id = None


def _release_assigned_vin_from_order(row: VinRegistry, order: CustomerOrder, reason: str) -> None:
    old_status = row.status
    _add_vin_event(row, 'reservation_cancelled', old_status, row.status, comment=reason)
    row.customer_order_id = None
    row.supply_need_id = None
    row.reserved_by_user_id = None
    row.reserved_at = None
    if order.reserved_vin_registry_id == row.id:
        order.reserved_vin_registry_id = None


def _release_order_produced_units_without_trailer(order: CustomerOrder, reason: str) -> None:
    for unit in ProducedUnit.query.filter_by(order_id=order.id, trailer_id=None).all():
        if unit.status == 'vin_assigned':
            unit.status = 'produced_no_vin'
        unit.order_id = None
        unit.note = ((unit.note or '') + f'\nКлиентский резерв снят. Причина: {reason}').strip()


def _release_order_trailer_and_vin(order: CustomerOrder, reason: str) -> tuple[bool, str]:
    if _order_has_issued_documents_or_shipment(order):
        return False, 'Нельзя отменить резерв: по заказу уже выданы документы или выполнена отгрузка.'

    vin_rows = VinRegistry.query.filter(VinRegistry.customer_order_id == order.id).all()
    trailer_ids = {order.trailer_id} if order.trailer_id else set()
    trailer_ids.update(row.trailer_id for row in vin_rows if row.trailer_id)

    for trailer_id in list(trailer_ids):
        vin_rows += [row for row in VinRegistry.query.filter(VinRegistry.trailer_id == trailer_id).all() if row not in vin_rows]
    trailer_ids.update(row.trailer_id for row in vin_rows if row.trailer_id)

    for trailer_id in trailer_ids:
        if _active_movement_for_trailer(trailer_id):
            return False, 'Нельзя отменить резерв: по прицепу есть активное перемещение.'
    for row in vin_rows:
        if row.docs_issued_at:
            return False, 'Нельзя отменить резерв: по VIN уже выданы документы.'

    for row in vin_rows:
        if row.status == 'reserved' and not row.trailer_id:
            _free_reserved_vin_row(row, order, reason)
        elif row.status in ('assigned', 'confirmed') and row.trailer_id:
            _release_assigned_vin_from_order(row, order, reason)
        elif row.status in ('assigned', 'confirmed') and not row.trailer_id:
            _free_reserved_vin_row(row, order, reason)
        else:
            row.customer_order_id = None
            row.supply_need_id = None

    _release_order_produced_units_without_trailer(order, reason)
    _cancel_order_active_reservations(order, order.trailer_id, reason)
    for trailer_id in trailer_ids:
        trailer = Trailer.query.get(trailer_id)
        if trailer:
            trailer.status = 'IN_STOCK'
            trailer.lifecycle_status = 'in_stock'
    order.trailer_id = None
    order.source_warehouse_id = None
    order.fulfillment_source = 'later'
    _refresh_order_status(order)
    add_order_event(order, 'reservation_cancelled', new_value='trailer_reservation_cancelled', comment=reason)
    return True, 'Резерв прицепа снят. Прицеп возвращён в свободное наличие.'

def _attach_produced_unit_to_existing_trailer(unit: ProducedUnit, trailer: Trailer, order: CustomerOrder | None, vin_registry_row: VinRegistry | None, user_id: int | None = None) -> tuple[bool, str]:
    if not trailer.vin:
        return False, 'У существующего прицепа нет VIN.'
    if vin_registry_row and vin_registry_row.vin_full and trailer.vin != vin_registry_row.vin_full:
        return False, 'VIN-реестр связан с другим VIN. Нельзя связать выпуск с этим прицепом.'
    if trailer.item_id != unit.item_id:
        return False, 'Существующий прицеп с этим VIN не соответствует модели выпуска.'
    linked_units = getattr(trailer, 'produced_unit', None)
    if linked_units is None:
        linked_units = []
    elif isinstance(linked_units, ProducedUnit):
        linked_units = [linked_units]
    for linked_unit in linked_units:
        if linked_unit.id != unit.id:
            return False, f'Этот VIN уже связан с выпуском ProducedUnit #{linked_unit.id}.'
    if order:
        if order.trailer_id and order.trailer_id != trailer.id:
            return False, 'У заказа уже закреплён другой прицеп.'
        other_order = _active_order_for_trailer(trailer.id, exclude_order_id=order.id)
        if other_order:
            return False, f'Этот VIN уже закреплён за заказом {other_order.order_number}.'
    else:
        other_order = _active_order_for_trailer(trailer.id)
        if other_order:
            return False, f'Этот VIN уже закреплён за заказом {other_order.order_number}.'

    unit.status = 'vin_assigned'
    unit.trailer_id = trailer.id
    if order:
        unit.order_id = order.id
        if not order.trailer_id:
            order.trailer_id = trailer.id
        if order.status != 'cancelled':
            _refresh_order_status(order)
        add_order_event(
            order,
            'trailer_assigned',
            new_value=trailer.vin,
            comment=f'Выпуск производства #{unit.id} связан с уже существующим Trailer #{trailer.id}. Дубликат прицепа не создавался.',
        )

    if vin_registry_row:
        old_vin_status = vin_registry_row.status
        vin_registry_row.trailer_id = trailer.id
        vin_registry_row.status = 'assigned' if vin_registry_row.status != 'confirmed' else vin_registry_row.status
        vin_registry_row.assigned_by_user_id = user_id
        vin_registry_row.assigned_at = vin_registry_row.assigned_at or datetime.utcnow()
        if order and not vin_registry_row.customer_order_id:
            vin_registry_row.customer_order_id = order.id
        line = unit.production_request_line
        if line and line.supply_need and not vin_registry_row.supply_need_id:
            vin_registry_row.supply_need_id = line.supply_need.id
        _add_vin_event(vin_registry_row, 'assigned', old_vin_status, vin_registry_row.status, comment='VIN связан с выпуском производства через существующий Trailer')

    return True, 'Выпуск производства связан с уже существующим прицепом. Новый Trailer не создавался.'

def get_or_create_configured_item(config_result: dict) -> Item:
    article = (config_result.get('article') or '').strip()
    if not article:
        raise ValueError('Конфигуратор не собрал артикул.')
    config = config_result.get('config') or {}
    group = TrailerProductGroup.query.filter_by(code=config.get('group_code')).first()
    category = group.product_category if group and group.product_category else _default_product_category_for_item_type('TRAILER')
    existing = Item.query.filter_by(article=article).first()
    if existing:
        changed = False
        if not existing.product_category_id and category:
            existing.product_category_id = category.id
            changed = True
        if group and not existing.group_id:
            existing.group_id = group.id
            changed = True
        if group and existing.max_mass_kg is None:
            existing.max_mass_kg = group.max_mass_kg
            changed = True
        if not existing.requires_vin:
            existing.requires_vin = True
            changed = True
        if changed:
            db.session.flush()
        return existing
    body_size = TrailerBodySize.query.filter_by(code=config.get('body_size_code')).first()
    board = TrailerBoardHeight.query.filter_by(code=config.get('board_height_code')).first()
    wheel = TrailerWheelOption.query.filter_by(code=config.get('wheel_code')).first()
    hub = TrailerHubOption.query.filter_by(code=config.get('hub_code')).first()
    tent = TrailerTentOption.query.filter_by(code=config.get('tent_code')).first()
    support = TrailerSupportWheelOption.query.filter_by(code=config.get('support_wheel_code')).first()
    dimensions = config_result.get('dimensions') or {}
    price = config_result.get('price') or {}
    item = Item(
        article=article,
        name=config_result.get('name') or article,
        item_type='TRAILER',
        product_category_id=category.id if category else None,
        group_id=group.id if group else None,
        max_mass_kg=group.max_mass_kg if group else None,
        is_sellable=True,
        requires_vin=True,
        body_length_mm=body_size.length_mm if body_size else None,
        body_width_mm=body_size.width_mm if body_size else None,
        board_height_mm=board.height_mm if board else None,
        axle_count=group.axle_count if group else None,
        wheel_radius=wheel.code if wheel else None,
        has_tent=bool(tent and (tent.code or '').upper() not in ('NO', 'NONE', '0')),
        tent_hight_mm=(int(''.join(ch for ch in (tent.code if tent else '') if ch.isdigit()) or 0) * 10) if tent else None,
        has_jockey_wheel=bool(support and (support.code or '').upper() not in ('NO', 'NONE', '0')),
        hub_type=hub.name if hub else None,
        size_external=dimensions.get('overall_dimensions_text'),
        size_body=dimensions.get('inner_dimensions_text') or (body_size.name if body_size else None),
        base_price=price.get('total_price'),
        is_active=True,
    )
    db.session.add(item)
    db.session.flush()
    return item


def _configured_item_from_request(values):
    from trailer_configurator import build_trailer_configuration_result

    config = _config_from_request_values(values)
    result = build_trailer_configuration_result(config)
    result['config'] = config
    errors = result.get('errors') or []
    if errors:
        return None, None, errors
    item = get_or_create_configured_item(result)
    snapshot = _snapshot_from_result(result)
    return item, snapshot, []


def _order_price_from_form_or_config(form: CustomerOrderForm, configured_snapshot: dict | None):
    if form.price.data is not None:
        return form.price.data
    if not configured_snapshot:
        return None
    unit_price = configured_snapshot.get('calculated_price')
    if unit_price is None:
        return None
    return Decimal(unit_price) * Decimal(form.quantity.data or 1)


def _sync_editable_order_production_need(order: CustomerOrder, snapshot: dict | None):
    need = (
        order.supply_needs
        .filter(SupplyNeed.status.in_(['NEW', 'PLANNED']))
        .order_by(SupplyNeed.created_at.desc(), SupplyNeed.id.desc())
        .first()
    )
    if not need:
        return None
    if _active_production_line_for_need(need.id):
        return 'Производственная заявка уже запущена; количество в потребности нужно менять в производстве.'
    need.item_id = order.item_id
    order_line = _primary_order_line(order) or _sync_primary_order_line(order, snapshot or _order_snapshot(order))
    db.session.flush()
    need.order_line_id = order_line.id
    need.warehouse_id = order.warehouse_id
    need.quantity = order.quantity
    need.required_by = order.expected_date
    _apply_snapshot(need, snapshot or _order_snapshot(order))
    return None


def _trailer_inventory_rows(article: str | None = None, body_size_code: str | None = None, board_height_code: str | None = None, group_code: str | None = None):
    trailers = [
        trailer for trailer in Trailer.query.filter(
            Trailer.status == 'IN_STOCK',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        ).order_by(Trailer.vin).all()
        if _trailer_available_for_sale(trailer)
    ]
    exact, body_match, size_match = [], [], []
    for trailer in trailers:
        item_article = trailer.item.article if trailer.item else ''
        row = {
            'id': trailer.id,
            'vin': trailer.vin or '',
            'article': item_article,
            'name': trailer.item.name if trailer.item else '',
            'warehouse': trailer.warehouse.name if trailer.warehouse else '',
            'warehouse_id': trailer.warehouse_id,
            'price': float(trailer.item.base_price or 0) if trailer.item else 0,
            'status': trailer.status or '',
        }
        if article and item_article == article:
            exact.append(row)
        elif group_code and body_size_code and board_height_code and item_article.startswith(f'{group_code}-{body_size_code}{board_height_code}'):
            body_match.append(row)
        elif group_code and body_size_code and item_article.startswith(f'{group_code}-{body_size_code}'):
            size_match.append(row)
    return {'exact': exact, 'body_match': body_match, 'size_match': size_match}


def _available_trailer_rows(warehouse_id: int | None = None, show_all_warehouses: bool = False, item_id: int | None = None, article: str | None = None):
    query = Trailer.query.filter(
        Trailer.status == 'IN_STOCK',
        or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
    ).order_by(Trailer.vin)
    if item_id:
        query = query.filter(Trailer.item_id == item_id)
    if warehouse_id and not show_all_warehouses:
        query = query.filter(Trailer.warehouse_id == warehouse_id)
    rows = []
    for trailer in query.all():
        if article and (not trailer.item or trailer.item.article != article):
            continue
        if not _trailer_available_for_sale(trailer):
            continue
        rows.append({
            'id': trailer.id,
            'vin': trailer.vin or '',
            'article': trailer.item.article if trailer.item else '',
            'name': trailer.item.name if trailer.item else '',
            'warehouse_id': trailer.warehouse_id,
            'warehouse_name': trailer.warehouse.name if trailer.warehouse else '',
            'price': float(trailer.item.base_price or 0) if trailer.item else 0,
            'status': trailer.status or '',
        })
    return rows


def _config_preview_payload(result: dict) -> dict:
    price = result.get('price') or {}
    dimensions = result.get('dimensions') or {}
    otss = result.get('otss') or {}
    config = result.get('config') or {}
    return {
        'article': result.get('article') or '',
        'name': result.get('name') or '',
        'price': price.get('total_price') or 0,
        'price_breakdown': price.get('price_breakdown') or [],
        'missing_components': price.get('missing_components') or [],
        'overall_dimensions_text': dimensions.get('overall_dimensions_text') or '',
        'inner_dimensions_text': dimensions.get('inner_dimensions_text') or '',
        'otss_number': otss.get('otss_number') or '',
        'otss_type': otss.get('otss_type') or '',
        'otss_modification': otss.get('otss_modification') or '',
        'vin_modification_code': otss.get('vin_modification_code') or '',
        'errors': result.get('errors') or [],
        'warnings': result.get('warnings') or [],
        'inventory': _trailer_inventory_rows(result.get('article'), config.get('body_size_code'), config.get('board_height_code'), config.get('group_code')),
    }


def _config_options_payload(config: dict) -> dict:
    options = _filtered_trailer_config_options(config)
    return {
        'groups': _trailer_config_option_payload(options['groups']),
        'body_sizes': _trailer_config_option_payload(options['body_sizes']),
        'board_heights': _trailer_config_option_payload(options['board_heights']),
        'wheels': _trailer_config_option_payload(options['wheels']),
        'hubs': _trailer_config_option_payload(options['hubs']),
        'support_wheels': _trailer_config_option_payload(options['support_wheels']),
        'tents': _trailer_config_option_payload(options['tents']),
        'executions': _trailer_config_option_payload(options['executions']),
        'specials': _trailer_config_option_payload(options['specials']),
        'option_warnings': options.get('option_warnings') or [],
    }


def _render_order_form(form: CustomerOrderForm, title: str):
    config = _config_from_request_values(request.form) if request.method == 'POST' else {'group_code': '002', 'body_execution_code': 'BOARD'}
    return render_template(
        'order_form.html',
        form=form,
        title=title,
        customer_options=_customer_options(limit=50),
        item_options=_trailer_item_options(),
        trailer_options=_order_trailer_options(getattr(form, 'order_id', None)),
        availability=_order_future_availability(form.item_id.data or None, form.warehouse_id.data or None),
        config_options=_trailer_config_form_context(config),
    )


def _render_order_header_form(form: CustomerOrderForm, order: CustomerOrder, title: str):
    order_lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    return render_template(
        'order_header_form.html',
        form=form,
        order=order,
        title=title,
        customer_options=_customer_options(limit=50),
        order_lines=order_lines,
    )


def _fill_supply_need_form_choices(form: SupplyNeedForm) -> None:
    form.order_id.choices = [(0, '— без заказа / на склад —')] + [
        (o.id, f'{o.order_number} — {o.customer.name if o.customer else ""}') for o in CustomerOrder.query.order_by(CustomerOrder.created_at.desc()).all()
    ]
    form.item_id.choices = [
        (i.id, f'{i.article or ""} — {i.name}') for i in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]


def _fill_stock_replenishment_form_choices(form: StockReplenishmentForm) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    if getattr(current_user, 'is_manager', False) and current_user.warehouse_id:
        warehouses = [w for w in warehouses if w.id == current_user.warehouse_id]
    form.warehouse_id.choices = [(w.id, w.name) for w in warehouses]
    form.item_id.choices = [(0, '— собрать через конфигуратор —')] + [
        (i.id, f'{i.article or ""} — {i.name}')
        for i in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]


def _active_production_line_for_need(need_id: int | None, exclude_line_id: int | None = None):
    if not need_id:
        return None
    query = ProductionRequestLine.query.filter(
        ProductionRequestLine.supply_need_id == need_id,
        ~ProductionRequestLine.status.in_(['ready', 'closed', 'cancelled', 'canceled']),
    )
    if exclude_line_id:
        query = query.filter(ProductionRequestLine.id != exclude_line_id)
    return query.first()


def _production_need_label(need: SupplyNeed) -> str:
    item_label = ''
    if need.item:
        item_label = ' — '.join(part for part in [need.item.article, need.item.name] if part)
    parts = [f'#{need.id}', status_label(need.need_type), item_label]
    if need.order:
        customer_name = need.order.customer.name if need.order.customer else 'клиент'
        parts.append(f'заказ {need.order.order_number} / {customer_name}')
    elif need.warehouse:
        parts.append(f'на склад {need.warehouse.name}')
    parts.append(f'{need.quantity or 0} шт.')
    if need.required_by:
        parts.append(f'срок {date_format(need.required_by)}')
    if need.note:
        parts.append(need.note[:80])
    return ' — '.join(str(part) for part in parts if part)


def _fill_production_line_form_choices(form: ProductionRequestLineForm, target_warehouse_id: int | None = None, current_line_id: int | None = None) -> None:
    needs_query = SupplyNeed.query.filter(SupplyNeed.status.in_(['NEW', 'IN_PRODUCTION']))
    if target_warehouse_id:
        needs_query = needs_query.filter(or_(SupplyNeed.warehouse_id == target_warehouse_id, SupplyNeed.warehouse_id.is_(None)))
    needs = [
        need for need in needs_query.order_by(SupplyNeed.priority.asc(), SupplyNeed.required_by.asc().nullslast(), SupplyNeed.created_at.desc()).all()
        if not _active_production_line_for_need(need.id, exclude_line_id=current_line_id)
    ]
    form.supply_need_id.choices = [(0, '— без потребности —')] + [
        (need.id, _production_need_label(need)) for need in needs
    ]
    form.item_id.choices = [
        (i.id, f'{i.article or ""} — {i.name}') for i in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]


def _stock_movement_trailer_label(trailer: Trailer) -> str:
    parts = [trailer.vin]
    if trailer.item:
        parts.append(trailer.item.article or trailer.item.name or '')
    if trailer.warehouse:
        parts.append(trailer.warehouse.name)
    parts.append(status_label(trailer.status))
    return ' — '.join(part for part in parts if part)


STOCK_MOVEMENT_ACTIVE_TYPE_CHOICES = [
    ('warehouse_transfer', 'Между складами'),
    ('production_arrival', 'Поступление с производства'),
]
STOCK_MOVEMENT_FILTER_TYPE_CHOICES = STOCK_MOVEMENT_ACTIVE_TYPE_CHOICES + [
    ('customer_shipment', 'Отгрузка клиенту (архив)'),
]


def _stock_movement_trailer_options(from_warehouse_id: int | None = None, q: str | None = None, current_trailer_id: int | None = None, exclude_movement_id: int | None = None, limit: int = 50):
    query = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .join(Warehouse, Warehouse.id == Trailer.warehouse_id)
        .filter(
            Trailer.status.in_(['IN_STOCK', 'RESERVED', 'SOLD']),
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
    )
    if from_warehouse_id:
        query = query.filter(Trailer.warehouse_id == from_warehouse_id)
    text = (q or '').strip()
    if text:
        like = f'%{text}%'
        query = query.filter(or_(Trailer.vin.ilike(like), Item.article.ilike(like), Item.name.ilike(like), Warehouse.name.ilike(like)))
    trailers = query.order_by(Trailer.vin).limit(limit).all()
    if current_trailer_id and all(trailer.id != current_trailer_id for trailer in trailers):
        current_trailer = Trailer.query.get(current_trailer_id)
        if current_trailer:
            trailers.insert(0, current_trailer)
    return [
        {
            'id': trailer.id,
            'label': _stock_movement_trailer_label(trailer),
            'vin': trailer.vin,
            'item': trailer.item.article if trailer.item else '',
            'warehouse_id': trailer.warehouse_id,
            'warehouse': trailer.warehouse.name if trailer.warehouse else '',
            'status': trailer.status,
            'status_label': status_label(trailer.status),
            'status_badge': status_badge_class(trailer.status),
        }
        for trailer in trailers
        if trailer.id == current_trailer_id or _trailer_available_for_movement(trailer, from_warehouse_id=from_warehouse_id, exclude_movement_id=exclude_movement_id)
    ]


def _find_stock_movement_trailer_by_search(search: str | None, from_warehouse_id: int | None = None, exclude_movement_id: int | None = None):
    text = (search or '').strip()
    if not text:
        return None
    normalized = text.lower()
    options = _stock_movement_trailer_options(from_warehouse_id=from_warehouse_id, q=text, exclude_movement_id=exclude_movement_id, limit=100)
    exact = [option for option in options if option['label'].lower() == normalized or option['vin'].lower() == normalized]
    if len(exact) == 1:
        return Trailer.query.get(exact[0]['id'])
    contains = [option for option in options if normalized in option['label'].lower()]
    if len(contains) == 1:
        return Trailer.query.get(contains[0]['id'])
    return None


def _apply_stock_movement_trailer_search(form: StockMovementForm, exclude_movement_id: int | None = None) -> None:
    trailer_id = form.trailer_id.data or 0
    if request.method == 'POST' and (not trailer_id or trailer_id == 0):
        trailer = _find_stock_movement_trailer_by_search(
            form.trailer_search.data,
            from_warehouse_id=form.from_warehouse_id.data or None,
            exclude_movement_id=exclude_movement_id,
        )
        if trailer:
            form.trailer_id.data = trailer.id


def _set_stock_movement_trailer_search_label(form: StockMovementForm) -> None:
    trailer_id = form.trailer_id.data or 0
    if trailer_id and not form.trailer_search.data:
        trailer = Trailer.query.get(trailer_id)
        if trailer:
            form.trailer_search.data = _stock_movement_trailer_label(trailer)


def _fill_stock_movement_form_choices(form: StockMovementForm, current_movement_id: int | None = None, include_customer_shipment: bool = False) -> None:
    source_warehouses = _trailer_source_warehouses()
    target_warehouses = _sales_warehouses()
    form.from_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in source_warehouses]
    form.to_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in target_warehouses]
    form.movement_type.choices = STOCK_MOVEMENT_FILTER_TYPE_CHOICES if include_customer_shipment else STOCK_MOVEMENT_ACTIVE_TYPE_CHOICES
    _apply_stock_movement_trailer_search(form, exclude_movement_id=current_movement_id)
    current_trailer_id = form.trailer_id.data or 0
    form.trailer_id.choices = [(0, '— без VIN —')] + [
        (option['id'], option['label'])
        for option in _stock_movement_trailer_options(
            from_warehouse_id=form.from_warehouse_id.data or None,
            q=form.trailer_search.data,
            current_trailer_id=current_trailer_id,
            exclude_movement_id=current_movement_id,
        )
    ]
    form.order_id.choices = [(0, '— без заказа —')] + [
        (o.id, f'{o.order_number} — {o.customer.name if o.customer else ""}') for o in CustomerOrder.query.order_by(CustomerOrder.created_at.desc()).all()
        if not current_user.is_manager or o.assigned_user_id == current_user.id
    ]
    _set_stock_movement_trailer_search_label(form)


def _fill_stock_movement_batch_form_choices(form: StockMovementBatchForm) -> None:
    form.from_warehouse_id.choices = [(w.id, w.name) for w in _trailer_source_warehouses()]
    form.to_warehouse_id.choices = [(w.id, w.name) for w in _sales_warehouses()]


def _selected_trailers_from_request() -> list[Trailer]:
    seen = set()
    trailers = []
    for raw_id in request.form.getlist('trailer_ids'):
        try:
            trailer_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if trailer_id in seen:
            continue
        trailer = Trailer.query.get(trailer_id)
        if trailer:
            trailers.append(trailer)
            seen.add(trailer_id)
    return trailers


def _movement_order_for_trailer(trailer: Trailer):
    active_reservation = _active_reservation_for_trailer(trailer.id)
    if active_reservation and active_reservation.order_id:
        return active_reservation.order
    return (
        CustomerOrder.query
        .filter(
            CustomerOrder.trailer_id == trailer.id,
            CustomerOrder.documents_issued == True,
            CustomerOrder.is_shipped == False,
            CustomerOrder.status != 'cancelled',
        )
        .order_by(CustomerOrder.documents_issued_at.desc().nullslast(), CustomerOrder.created_at.desc())
        .first()
    )


@main_bp.route('/leads')
@login_required
def leads_list():
    _block_production_commercial_access()
    status = request.args.get('status', '').strip()
    channel = request.args.get('channel', '').strip()
    q = request.args.get('q', '').strip()

    query = Lead.query

    if not current_user.is_admin and current_user.warehouse_id:
        query = query.filter(or_(Lead.warehouse_id == current_user.warehouse_id, Lead.warehouse_id.is_(None)))

    if status:
        query = query.filter(Lead.status == status)
    if channel:
        query = query.filter(or_(Lead.channel == channel, Lead.source_channel == channel.upper()))
    if q:
        like = f'%{q}%'
        query = query.filter(or_(Lead.customer_name.ilike(like), Lead.phone.ilike(like), Lead.desired_model.ilike(like), Lead.text.ilike(like)))

    leads = query.order_by(Lead.created_at.desc(), Lead.id.desc()).all()
    return render_template('leads_list.html', leads=leads, status=status, channel=channel, q=q)


@main_bp.route('/integrations/kaspi/orders/import', methods=['GET', 'POST'])
@login_required
def kaspi_order_import():
    _block_production_commercial_access()
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager):
        abort(403)

    form = KaspiOrderImportForm()
    list_form = KaspiOrderListImportForm(prefix='list')
    _fill_kaspi_import_form_choices(form)
    _fill_kaspi_list_import_form_choices(list_form)
    if request.method == 'GET':
        if current_user.warehouse_id:
            form.warehouse_id.data = current_user.warehouse_id
            list_form.warehouse_id.data = current_user.warehouse_id
        if current_user.is_manager:
            form.assigned_user_id.data = current_user.id
            list_form.assigned_user_id.data = current_user.id
        list_form.date_from.data = (date.today() - timedelta(days=7)).isoformat()
        list_form.date_to.data = date.today().isoformat()

    client = KaspiShopClient(
        token=current_app.config.get('KASPI_SHOP_TOKEN'),
        base_url=current_app.config.get('KASPI_SHOP_API_BASE_URL'),
    )

    if form.validate_on_submit():
        order_code = (form.order_code.data or '').strip()

        try:
            order_data = client.get_order_by_code(order_code)
            if not order_data:
                flash('Kaspi не вернул заказ с таким номером.', 'warning')
                return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)
            order_id = order_data.get('id')
            entries = client.get_order_entries(order_id) if order_id else []
        except KaspiClientError as exc:
            flash(str(exc), 'danger')
            return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)

        existing = _existing_kaspi_lead(order_data, order_code)
        if existing:
            flash('Этот заказ Kaspi уже есть в заявках.', 'info')
            return redirect(url_for('main.lead_detail', lead_id=existing.id))

        lead = _create_kaspi_lead(order_data, entries, form.warehouse_id.data, form.assigned_user_id.data, order_code)
        db.session.commit()
        flash('Заказ Kaspi импортирован как заявка и входящее сообщение.', 'success')
        return redirect(url_for('main.lead_detail', lead_id=lead.id))

    return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)


@main_bp.route('/integrations/kaspi/orders/import-list', methods=['POST'])
@login_required
def kaspi_order_import_list():
    _block_production_commercial_access()
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager):
        abort(403)

    form = KaspiOrderImportForm()
    list_form = KaspiOrderListImportForm(prefix='list')
    _fill_kaspi_import_form_choices(form)
    _fill_kaspi_list_import_form_choices(list_form)
    client = KaspiShopClient(
        token=current_app.config.get('KASPI_SHOP_TOKEN'),
        base_url=current_app.config.get('KASPI_SHOP_API_BASE_URL'),
    )
    if not list_form.validate_on_submit():
        details = []
        for field_name, errors in list_form.errors.items():
            details.append(f'{getattr(list_form, field_name).label.text}: {", ".join(errors)}')
        flash('Проверьте параметры списка Kaspi: ' + '; '.join(details), 'danger')
        return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)

    try:
        date_from = _parse_kaspi_filter_date(list_form.date_from.data)
        date_to = _parse_kaspi_filter_date(list_form.date_to.data)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)

    try:
        payload = client.list_orders(
            state=list_form.state.data,
            status=(list_form.status.data or None),
            creation_from_ms=_kaspi_ms(date_from),
            creation_to_ms=_kaspi_ms(date_to, end_of_day=True),
            page_number=list_form.page_number.data or 0,
            page_size=min(list_form.page_size.data or 5, 5),
        )
    except KaspiClientError as exc:
        flash(str(exc), 'danger')
        return render_template('kaspi_order_import.html', form=form, list_form=list_form, kaspi_configured=client.is_configured)

    imported = 0
    skipped = 0
    for order_data in payload.get('data') or []:
        if _existing_kaspi_lead(order_data):
            skipped += 1
            continue
        order_id = order_data.get('id')
        try:
            entries = client.get_order_entries(order_id) if order_id else []
        except KaspiClientError:
            entries = []
        _create_kaspi_lead(order_data, entries, list_form.warehouse_id.data, list_form.assigned_user_id.data)
        imported += 1
    db.session.commit()
    total = (payload.get('meta') or {}).get('totalCount')
    tail = f' Всего по фильтру Kaspi: {total}.' if total is not None else ''
    flash(f'Импортировано заявок Kaspi: {imported}. Пропущено дублей: {skipped}.{tail}', 'success')
    return redirect(url_for('main.leads_list', channel='KASPI'))


@main_bp.route('/leads/new', methods=['GET', 'POST'])
@login_required
def lead_create():
    _block_production_commercial_access()
    form = LeadForm()
    _fill_lead_form_choices(form)

    if request.method == 'GET':
        if current_user.warehouse_id:
            form.warehouse_id.data = current_user.warehouse_id
        if current_user.is_manager:
            form.assigned_user_id.data = current_user.id
        form.created_at.data = date.today()

    if form.validate_on_submit():
        configured_snapshot = None
        if request.form.get('item_source') == 'unknown':
            form.desired_item_id.data = 0
        elif request.form.get('item_source') == 'config':
            configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
            if config_errors:
                for error in config_errors:
                    flash(error, 'danger')
                return render_template('lead_form.html', form=form, title='Новая заявка', item_options=_trailer_item_options(), config_options=_trailer_config_form_context(), item_source_initial='config')
            form.desired_item_id.data = configured_item.id
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.leads_list'))
        lead = Lead(
            created_at=datetime.combine(form.created_at.data or date.today(), datetime.min.time()),
            channel=form.source_channel.data,
            source_channel=form.source_channel.data.upper(),
            source_name=(form.source_name.data or '').strip() or None,
            source_platform=(form.source_platform.data or '').strip() or None,
            customer_name=(form.customer_name.data or '').strip(),
            phone=(form.phone.data or '').strip() or None,
            messenger_username=(form.messenger_username.data or '').strip() or None,
            desired_item_id=form.desired_item_id.data or None,
            desired_model=(form.desired_model.data or '').strip() or None,
            desired_specs=(form.desired_specs.data or '').strip() or None,
            warehouse_id=form.warehouse_id.data or None,
            assigned_user_id=form.assigned_user_id.data or None,
            status=form.status.data,
            text=(form.text.data or '').strip() or None,
            comment=(form.comment.data or '').strip() or None,
        )
        db.session.add(lead)
        db.session.flush()
        if configured_snapshot:
            _apply_snapshot(lead, configured_snapshot)
        _finish_idempotency(idem_key, 'Lead', lead.id)
        db.session.commit()
        flash('Заявка создана', 'success')
        if request.args.get('return_to') == 'manager_workspace' or current_user.is_manager:
            return_tab = request.args.get('return_tab') if request.args.get('return_tab') in {'orders', 'funnel', 'leads', 'conversations', 'assistant'} else 'leads'
            return redirect(url_for('main.manager_workspace', tab=return_tab))
        return redirect(url_for('main.leads_list'))

    return render_template('lead_form.html', form=form, title='Новая заявка', item_options=_trailer_item_options(), config_options=_trailer_config_form_context(), item_source_initial='config')


def _ensure_customer_for_lead(lead: Lead):
    if lead.customer_id:
        return lead.customer
    customer = Customer.query.filter_by(phone=lead.phone).first() if lead.phone else None
    if not customer:
        customer = Customer(
            customer_type='PERSON',
            name=lead.customer_name or 'Новый контакт',
            phone=lead.phone,
            is_active=True,
        )
        db.session.add(customer)
        db.session.flush()
    lead.customer_id = customer.id
    return customer


@main_bp.route('/conversations')
@login_required
def conversations_list():
    if current_user.is_production or current_user.is_logistics:
        abort(403)

    channel = request.args.get('channel', '').strip().lower()
    status = request.args.get('status', '').strip()
    assigned = request.args.get('assigned', '').strip()
    warehouse_id = request.args.get('warehouse_id', type=int)
    q = request.args.get('q', '').strip()

    query = _conversation_visible_query()

    if channel:
        query = query.filter(Lead.channel == channel)
    if status:
        query = query.filter(Lead.conversation_status == status)
    if assigned == 'mine':
        query = query.filter(Lead.assigned_user_id == current_user.id)
    elif assigned == 'unassigned':
        query = query.filter(Lead.assigned_user_id.is_(None))
    if warehouse_id:
        if current_user.is_manager and current_user.warehouse_id and warehouse_id != current_user.warehouse_id:
            abort(403)
        query = query.filter(Lead.warehouse_id == warehouse_id)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(
            Lead.customer_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.messenger_username.ilike(like),
            Lead.interest_text.ilike(like),
            Lead.last_message_text.ilike(like),
            Lead.text.ilike(like),
        ))

    conversations = (
        query
        .order_by(Lead.unread_count.desc(), Lead.last_message_at.desc().nullslast(), Lead.created_at.desc())
        .all()
    )
    warehouses_query = Warehouse.query.filter_by(is_active=True)
    if current_user.is_manager and current_user.warehouse_id:
        warehouses_query = warehouses_query.filter(Warehouse.id == current_user.warehouse_id)
    warehouses = warehouses_query.order_by(Warehouse.name).all()
    managers_query = User.query.filter(User.role == 'manager')
    if current_user.is_manager:
        managers_query = managers_query.filter(User.id == current_user.id)
    managers = managers_query.order_by(User.full_name, User.username).all()
    return render_template(
        'conversations_list.html',
        conversations=conversations,
        warehouses=warehouses,
        managers=managers,
        channel=channel,
        status=status,
        assigned=assigned,
        warehouse_id=warehouse_id,
        q=q,
    )


@main_bp.route('/conversations/<int:lead_id>')
@login_required
def conversation_detail(lead_id):
    if current_user.is_production or current_user.is_logistics:
        abort(403)
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_access_conversation(lead)

    unread_messages = LeadMessage.query.filter_by(lead_id=lead.id, direction='IN', is_read=False).all()
    for message in unread_messages:
        message.is_read = True
    if unread_messages or lead.unread_count:
        lead.unread_count = 0
        db.session.commit()

    messages = lead.messages.order_by(LeadMessage.created_at.asc(), LeadMessage.id.asc()).all()
    related_orders = CustomerOrder.query.filter_by(lead_id=lead.id).order_by(CustomerOrder.created_at.desc()).all()
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    managers = User.query.filter(User.role == 'manager').order_by(User.full_name, User.username).all()
    stock_by_warehouse = []
    if lead.desired_item_id:
        for warehouse in warehouses:
            count = Trailer.query.filter_by(item_id=lead.desired_item_id, warehouse_id=warehouse.id, status='IN_STOCK').count()
            stock_by_warehouse.append((warehouse, count))

    return render_template(
        'conversation_detail.html',
        lead=lead,
        messages=messages,
        related_orders=related_orders,
        warehouses=warehouses,
        managers=managers,
        stock_by_warehouse=stock_by_warehouse,
    )


@main_bp.route('/conversations/<int:lead_id>/reply', methods=['POST'])
@login_required
def conversation_reply(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_manage_conversation(lead)
    text = (request.form.get('text') or '').strip()
    if not text:
        flash('Введите текст ответа.', 'warning')
        return redirect(url_for('main.conversation_detail', lead_id=lead.id))

    if current_user.is_manager and not lead.assigned_user_id:
        lead.assigned_user_id = current_user.id
        if not lead.warehouse_id and current_user.warehouse_id:
            lead.warehouse_id = current_user.warehouse_id

    # TODO: здесь будет отправка ответа через WhatsApp/Instagram/Telegram API.
    db.session.add(LeadMessage(
        lead_id=lead.id,
        direction='OUT',
        sender_type='manager',
        channel=lead.channel or 'manual',
        text=text,
        is_read=True,
        user_id=current_user.id,
    ))
    lead.last_message_at = datetime.utcnow()
    lead.last_message_text = text
    lead.conversation_status = 'waiting_client'
    db.session.commit()
    flash('Ответ сохранён.', 'success')
    return redirect(url_for('main.conversation_detail', lead_id=lead.id))


@main_bp.route('/conversations/<int:lead_id>/assign-to-me', methods=['POST'])
@login_required
def conversation_assign_to_me(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_manage_conversation(lead)
    if not (current_user.is_manager or current_user.is_admin):
        abort(403)
    lead.assigned_user_id = current_user.id
    if not lead.warehouse_id and current_user.warehouse_id:
        lead.warehouse_id = current_user.warehouse_id
    lead.conversation_status = 'manager_handling'
    _add_lead_system_message(lead, f'Диалог назначен на менеджера {current_user.full_name or current_user.username}.')
    db.session.commit()
    flash('Диалог назначен на вас.', 'success')
    return redirect(url_for('main.conversation_detail', lead_id=lead.id))


@main_bp.route('/conversations/<int:lead_id>/assign', methods=['POST'])
@role_required('director')
def conversation_assign(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    manager_id = request.form.get('assigned_user_id', type=int) or None
    warehouse_id = request.form.get('warehouse_id', type=int) or None
    manager = User.query.get(manager_id) if manager_id else None
    if manager and manager.role != 'manager':
        flash('Назначить можно только пользователя с ролью manager.', 'danger')
        return redirect(url_for('main.conversation_detail', lead_id=lead.id))
    lead.assigned_user_id = manager.id if manager else None
    lead.warehouse_id = warehouse_id
    lead.conversation_status = 'manager_handling' if manager else 'manager_needed'
    manager_name = manager.full_name or manager.username if manager else 'не назначен'
    _add_lead_system_message(lead, f'Ответственный менеджер: {manager_name}.')
    db.session.commit()
    flash('Назначение обновлено.', 'success')
    return redirect(url_for('main.conversation_detail', lead_id=lead.id))


@main_bp.route('/conversations/<int:lead_id>/close', methods=['POST'])
@login_required
def conversation_close(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_manage_conversation(lead)
    lead.conversation_status = 'closed'
    lead.status = 'CLOSED'
    _add_lead_system_message(lead, 'Диалог закрыт.')
    db.session.commit()
    flash('Диалог закрыт.', 'success')
    return redirect(url_for('main.conversation_detail', lead_id=lead.id))


@main_bp.route('/conversations/<int:lead_id>/mark-spam', methods=['POST'])
@login_required
def conversation_mark_spam(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_manage_conversation(lead)
    lead.conversation_status = 'spam'
    lead.status = 'SPAM'
    _add_lead_system_message(lead, 'Диалог отмечен как спам.')
    db.session.commit()
    flash('Диалог отмечен как спам.', 'success')
    return redirect(url_for('main.conversations_list'))


@main_bp.route('/conversations/<int:lead_id>/create-order', methods=['GET', 'POST'])
@login_required
def conversation_create_order(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    _ensure_can_manage_conversation(lead)
    _ensure_customer_for_lead(lead)
    if current_user.is_manager:
        if not lead.assigned_user_id:
            lead.assigned_user_id = current_user.id
        if not lead.warehouse_id and current_user.warehouse_id:
            lead.warehouse_id = current_user.warehouse_id
    _add_lead_system_message(lead, 'Открыта форма создания заказа из диалога.')
    db.session.commit()
    return redirect(url_for('main.order_create', lead_id=lead.id, return_to='conversation'))


@main_bp.route('/reports/conversations')
@role_required('director')
def conversation_report():
    today_start = datetime.combine(date.today(), time.min)
    month_start = datetime(date.today().year, date.today().month, 1)
    leads = Lead.query.all()
    orders_by_lead = {
        order.lead_id for order in CustomerOrder.query.filter(CustomerOrder.lead_id.isnot(None)).all()
    }
    channels = ['whatsapp', 'instagram', 'website', 'telegram', 'manual', 'phone', 'other']
    rows = []
    for channel_name in channels:
        channel_leads = [lead for lead in leads if (lead.channel or '').lower() == channel_name]
        converted = [lead for lead in channel_leads if lead.id in orders_by_lead or lead.conversation_status == 'order_created']
        rows.append({
            'channel': channel_name,
            'total': len(channel_leads),
            'converted': len(converted),
            'conversion': round((len(converted) / len(channel_leads) * 100), 1) if channel_leads else 0,
        })
    today_count = Lead.query.filter(Lead.created_at >= today_start).count()
    month_count = Lead.query.filter(Lead.created_at >= month_start).count()
    new_count = Lead.query.filter(Lead.conversation_status == 'new').count()
    unread_count = db.session.query(sa.func.coalesce(sa.func.sum(Lead.unread_count), 0)).scalar() or 0
    converted_count = len(orders_by_lead)
    conversion = round((converted_count / len(leads) * 100), 1) if leads else 0
    return render_template(
        'conversation_report.html',
        rows=rows,
        today_count=today_count,
        month_count=month_count,
        new_count=new_count,
        unread_count=unread_count,
        converted_count=converted_count,
        conversion=conversion,
    )


@main_bp.route('/orders')
@login_required
def orders_list():
    _block_production_commercial_access()
    status = (request.args.get('status') or 'all').strip()
    q = request.args.get('q', '').strip()

    query = CustomerOrder.query.join(Customer, Customer.id == CustomerOrder.customer_id).outerjoin(Item, Item.id == CustomerOrder.item_id)
    if current_user.is_manager:
        query = query.filter(or_(
            CustomerOrder.assigned_user_id == current_user.id,
            CustomerOrder.created_by_user_id == current_user.id,
        ))
    elif not current_user.can_view_all and current_user.warehouse_id:
        query = query.filter(or_(CustomerOrder.warehouse_id == current_user.warehouse_id, CustomerOrder.warehouse_id.is_(None)))

    legacy_status = status if status and status not in {code for code, _label in ORDER_LIST_FILTERS} else ''
    if legacy_status:
        query = query.filter(CustomerOrder.status == legacy_status)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(CustomerOrder.order_number.ilike(like), Customer.name.ilike(like), Item.article.ilike(like), Item.name.ilike(like)))

    orders = query.order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc()).all()
    rows = []
    for order in orders:
        state = _order_list_state(order)
        if status != 'all' and not legacy_status and state['code'] != status:
            continue
        rows.append({'order': order, 'state': state})
    return render_template('orders_list.html', rows=rows, status=status, q=q, order_filters=ORDER_LIST_FILTERS)


@main_bp.route('/api/order-availability')
@login_required
def api_order_availability():
    _block_production_commercial_access()
    item_id = request.args.get('item_id', type=int)
    warehouse_id = request.args.get('warehouse_id', type=int)
    availability = _order_future_availability(item_id, warehouse_id)
    if not availability:
        return jsonify({'ok': True, 'rows': [], 'summary': {'stock': 0, 'ordered': 0, 'production': 0, 'inbound': 0}})

    rows = []
    for trailer in availability['stock']:
        rows.append({'source': 'В наличии', 'value': trailer.vin, 'date': '', 'status': status_label(trailer.status)})
    for need in availability['stock_needs']:
        rows.append({'source': 'Заказано на склад', 'value': f'{need.quantity} шт.', 'date': date_format(need.required_by), 'status': status_label(need.status)})
    for line in availability['production_lines']:
        rows.append({
            'source': 'В производстве',
            'value': f'{max((line.quantity or 0) - (line.produced_qty or 0), 0)} шт.',
            'date': date_format(line.supply_need.required_by) if line.supply_need else '',
            'status': status_label(line.status),
        })
    for unit in availability['produced_units']:
        rows.append({'source': 'Выпущено без VIN', 'value': f'#{unit.id}', 'date': date_format(unit.produced_at), 'status': status_label(unit.status)})
    for movement in availability['inbound_movements']:
        rows.append({'source': 'В пути', 'value': movement.trailer.vin if movement.trailer else '', 'date': date_format(movement.arrival_date), 'status': status_label(movement.status)})

    return jsonify({
        'ok': True,
        'summary': {
            'stock': len(availability['stock']),
            'ordered': len(availability['stock_needs']),
            'production': len(availability['production_lines']),
            'inbound': len(availability['inbound_movements']),
        },
        'rows': rows,
    })


@main_bp.route('/api/trailer-config-preview')
@login_required
def api_trailer_config_preview():
    if current_user.is_production:
        abort(403)
    from trailer_configurator import build_trailer_configuration_result

    config = _config_from_request_values(request.args)
    result = build_trailer_configuration_result(config)
    result['config'] = config
    return jsonify({'ok': True, 'result': _config_preview_payload(result), 'options': _config_options_payload(config)})


@main_bp.route('/api/available-trailers')
@login_required
def api_available_trailers():
    if current_user.is_production:
        abort(403)
    warehouse_id = request.args.get('warehouse_id', type=int)
    item_id = request.args.get('item_id', type=int)
    article = (request.args.get('article') or '').strip() or None
    show_all = (request.args.get('show_all_warehouses') or '').lower() in ('1', 'true', 'yes', 'on')
    return jsonify({
        'ok': True,
        'trailers': _available_trailer_rows(
            warehouse_id=warehouse_id,
            show_all_warehouses=show_all,
            item_id=item_id,
            article=article,
        ),
    })


@main_bp.route('/api/movement-trailers/search')
@role_required('director', 'manager')
def api_movement_trailers_search():
    from_warehouse_id = request.args.get('from_warehouse_id', type=int)
    q = (request.args.get('q') or '').strip()
    current_trailer_id = request.args.get('current_trailer_id', type=int)
    exclude_movement_id = request.args.get('exclude_movement_id', type=int)
    return jsonify({
        'ok': True,
        'trailers': _stock_movement_trailer_options(
            from_warehouse_id=from_warehouse_id,
            q=q,
            current_trailer_id=current_trailer_id,
            exclude_movement_id=exclude_movement_id,
            limit=30,
        ),
    })


@main_bp.route('/orders/new', methods=['GET', 'POST'])
@login_required
def order_create():
    _block_production_commercial_access()
    form = CustomerOrderForm()
    item_id_prefill = request.args.get('item_id', type=int)
    trailer_id_prefill = request.args.get('trailer_id', type=int)
    lead_id_prefill = request.args.get('lead_id', type=int)
    customer_id_prefill = request.args.get('customer_id', type=int)

    if request.method == 'GET':
        form.order_number.data = _next_number('ORD', CustomerOrder, 'order_number')
        form.order_date.data = date.today()
        form.status.data = 'waiting_payment'
        form.fulfillment_source.data = 'later'
        form.prepayment_percent.data = 30
        if current_user.warehouse_id:
            form.warehouse_id.data = current_user.warehouse_id
        if current_user.is_manager:
            form.assigned_user_id.data = current_user.id
        if lead_id_prefill:
            lead = Lead.query.get(lead_id_prefill)
            if lead:
                form.lead_id.data = lead.id
                if lead.warehouse_id and not current_user.is_manager:
                    form.warehouse_id.data = lead.warehouse_id
                if lead.customer_id:
                    form.customer_id.data = lead.customer_id
                if lead.desired_item_id:
                    form.item_id.data = lead.desired_item_id
                if lead.article_snapshot:
                    form.fulfillment_source.data = 'production'
        if customer_id_prefill:
            customer = Customer.query.get(customer_id_prefill)
            if customer:
                form.customer_id.data = customer.id
                form.customer_search.data = _customer_label(customer)
        if item_id_prefill:
            form.item_id.data = item_id_prefill
        if trailer_id_prefill:
            trailer = Trailer.query.get(trailer_id_prefill)
            if trailer:
                form.trailer_id.data = trailer.id
                form.item_id.data = trailer.item_id
                form.fulfillment_source.data = 'stock'

    _fill_order_form_choices(form, item_id_prefill=item_id_prefill)

    if form.validate_on_submit():
        fulfillment_source = (form.fulfillment_source.data or 'later').strip() or 'later'
        order_number = (form.order_number.data or '').strip() or _next_number('ORD', CustomerOrder, 'order_number')
        if CustomerOrder.query.filter_by(order_number=order_number).first():
            flash('Такой номер заказа уже существует', 'danger')
            return _render_order_form(form, 'Новый заказ')
        selected_trailer = Trailer.query.get(form.trailer_id.data) if form.trailer_id.data else None
        configured_item = None
        configured_snapshot = None
        if fulfillment_source == 'production':
            selected_trailer = None
            form.trailer_id.data = 0
            if _order_should_build_config(request.form, fulfillment_source):
                configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
                if config_errors:
                    for error in config_errors:
                        flash(error, 'danger')
                    return _render_order_form(form, 'Новый заказ')
                form.item_id.data = configured_item.id
            elif form.item_id.data:
                configured_item = Item.query.get(form.item_id.data)
            if not configured_item and lead_id_prefill:
                lead = Lead.query.get(lead_id_prefill)
                if lead and lead.article_snapshot and lead.desired_item_id:
                    configured_item = lead.desired_item
                    configured_snapshot = _lead_snapshot(lead)
                    form.item_id.data = lead.desired_item_id
            if not configured_item:
                flash('Для заказа в производство выберите номенклатуру или соберите прицеп через конфигуратор.', 'danger')
                return _render_order_form(form, 'Новый заказ')
        elif fulfillment_source == 'stock':
            if not selected_trailer:
                flash('Для продажи из наличия выберите конкретный прицеп из таблицы.', 'danger')
                return _render_order_form(form, 'Новый заказ')
            if (form.quantity.data or 1) > 1:
                flash('Для продажи из наличия можно выбрать только 1 прицеп, потому что заказ привязывается к одному VIN. Для двух прицепов используйте производство или отдельные заказы.', 'danger')
                return _render_order_form(form, 'Новый заказ')
            form.item_id.data = selected_trailer.item_id
        else:
            selected_trailer = None
            form.trailer_id.data = 0
            form.item_id.data = 0

        if selected_trailer and _active_reservation_for_trailer(selected_trailer.id):
            flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
            return _render_order_form(form, 'Новый заказ')
        if selected_trailer and not _trailer_available_for_sale(selected_trailer):
            flash('Можно резервировать только прицеп в наличии.', 'danger')
            return _render_order_form(form, 'Новый заказ')
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.orders_list'))

        order = CustomerOrder(
            order_number=order_number,
            created_at=datetime.combine(form.order_date.data or date.today(), datetime.min.time()),
            lead_id=form.lead_id.data or None,
            customer_id=form.customer_id.data,
            item_id=form.item_id.data or None,
            trailer_id=form.trailer_id.data or None,
            warehouse_id=form.warehouse_id.data or None,
            source_warehouse_id=selected_trailer.warehouse_id if selected_trailer else None,
            created_by_user_id=current_user.id,
            assigned_user_id=form.assigned_user_id.data or None,
            quantity=form.quantity.data or 1,
            price=_order_price_from_form_or_config(form, configured_snapshot),
            prepayment_percent=form.prepayment_percent.data,
            status='waiting_payment',
            fulfillment_source=fulfillment_source,
            expected_date=form.expected_date.data,
            planned_ship_date=form.planned_ship_date.data,
            planned_ship_comment=(form.planned_ship_comment.data or '').strip() or None,
            note=(form.note.data or '').strip() or None,
            manager_comment=(form.manager_comment.data or '').strip() or None,
        )
        db.session.add(order)
        db.session.flush()
        if not configured_snapshot and order.lead_id:
            source_lead = Lead.query.get(order.lead_id)
            if source_lead and source_lead.article_snapshot:
                configured_snapshot = _lead_snapshot(source_lead)
        if configured_snapshot:
            _apply_snapshot(order, configured_snapshot)
        order_line = _sync_primary_order_line(order, configured_snapshot or _order_snapshot(order))
        db.session.flush()
        _finish_idempotency(idem_key, 'CustomerOrder', order.id)
        add_order_event(order, 'order_created', new_value=order.status, comment=f'Создан из заявки #{order.lead_id}' if order.lead_id else 'Заказ создан')

        if order.lead_id:
            lead = Lead.query.get(order.lead_id)
            if lead:
                lead.status = 'CONVERTED'
                lead.conversation_status = 'order_created'
                if not lead.customer_id:
                    lead.customer_id = order.customer_id
                _add_lead_system_message(lead, f'Создан заказ {order.order_number}.')

        if selected_trailer:
            selected_trailer.status = 'RESERVED'
            selected_trailer.lifecycle_status = 'reserved'
            if not order.warehouse_id:
                order.warehouse_id = selected_trailer.warehouse_id
            order.source_warehouse_id = selected_trailer.warehouse_id
            source_type = 'TRANSFER' if selected_trailer.warehouse_id != order.warehouse_id else 'STOCK'
            reservation = Reservation(
                order_id=order.id,
                order_line_id=order_line.id,
                trailer_id=selected_trailer.id,
                item_id=order.item_id,
                source_type=source_type,
                status='ACTIVE',
                priority=10,
                note='Резерв при создании заказа',
            )
            db.session.add(reservation)
            order.fulfillment_source = 'stock'
            if source_type == 'TRANSFER':
                order.status = 'waiting_transfer'
            add_order_event(order, 'trailer_reserved', new_value=selected_trailer.vin, comment='Резерв при создании заказа')
        elif fulfillment_source == 'production':
            can_request, message = order_can_request_production(order)
            if not can_request:
                flash(message, 'warning')
                db.session.rollback()
                return _render_order_form(form, 'Новый заказ')
            need = SupplyNeed(
                order_id=order.id,
                order_line_id=order_line.id,
                item_id=order.item_id,
                warehouse_id=order.warehouse_id,
                quantity=order.quantity,
                status='NEW',
                priority=10,
                need_type='CUSTOMER_ORDER',
                required_by=order.expected_date,
                note='Потребность создана автоматически из заказа клиента',
            )
            _apply_snapshot(need, configured_snapshot)
            db.session.add(need)
            add_order_event(order, 'production_need_created', new_value='NEW', comment='Потребность создана автоматически из заказа клиента')

        _refresh_order_status(order)
        order_line.status = order.status
        if selected_trailer and order.source_warehouse_id and order.warehouse_id and order.source_warehouse_id != order.warehouse_id:
            order.status = 'waiting_transfer'
            order_line.status = order.status
        db.session.commit()
        flash('Заказ создан', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    return _render_order_form(form, 'Новый заказ')


@main_bp.route('/orders/<int:order_id>/payments/new', methods=['GET', 'POST'])
@login_required
def order_payment_create(order_id):
    _block_production_commercial_access()
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    form = OrderPaymentForm()

    if request.method == 'GET':
        form.paid_at.data = date.today()
        if order.remaining_amount > 0 and order.confirmed_paid_amount > 0:
            form.stage.data = 'FINAL'
        elif order.remaining_amount == float(order.price or 0):
            form.stage.data = 'PREPAYMENT'
        else:
            form.stage.data = 'FULL'

    if form.validate_on_submit():
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
        paid_at = form.paid_at.data
        payment = OrderPayment(
            order_id=order.id,
            paid_at=datetime.combine(paid_at, datetime.min.time()) if paid_at else datetime.utcnow(),
            stage=form.stage.data,
            method=form.method.data,
            amount=form.amount.data,
            status=form.status.data,
            transaction_ref=(form.transaction_ref.data or '').strip() or None,
            payment_link=(form.payment_link.data or '').strip() or None,
            note=(form.note.data or '').strip() or None,
        )
        db.session.add(payment)
        db.session.flush()
        _finish_idempotency(idem_key, 'OrderPayment', payment.id)

        # Производство создаётся только при явном режиме "Заказать в производство".
        if payment.status == 'CONFIRMED' and order.fulfillment_source == 'production' and order.trailer_id is None and order.supply_needs.count() == 0:
            can_request, message = order_can_request_production(order)
            if not can_request:
                flash(message, 'warning')
            else:
                order_line = _sync_primary_order_line(order, _order_snapshot(order))
                db.session.flush()
                need = SupplyNeed(
                    order_id=order.id,
                    order_line_id=order_line.id,
                    item_id=order.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=order.quantity,
                    status='NEW',
                    priority=10,
                    need_type='CUSTOMER_ORDER',
                    required_by=order.expected_date,
                    note='Потребность создана при подтверждении оплаты',
                )
                _apply_snapshot(need, _order_snapshot(order))
                db.session.add(need)
                order.fulfillment_source = order.fulfillment_source or 'production'

        _refresh_order_status(order)
        add_order_event(order, 'payment_added', new_value=payment.amount, comment=payment.note)
        db.session.commit()
        flash('Платеж добавлен', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    return render_template('order_payment_form.html', form=form, order=order, title='Платеж по заказу')


@main_bp.route('/orders/<int:order_id>/payments/add', methods=['POST'])
@login_required
def order_payment_add(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    form = OrderPaymentForm()
    if not form.validate_on_submit():
        flash('Не удалось добавить оплату. Проверьте сумму и дату.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    paid_at = form.paid_at.data
    payment = OrderPayment(
        order_id=order.id,
        paid_at=datetime.combine(paid_at, datetime.min.time()) if paid_at else datetime.utcnow(),
        stage=form.stage.data,
        method=form.method.data,
        amount=form.amount.data,
        status=form.status.data,
        transaction_ref=(form.transaction_ref.data or '').strip() or None,
        payment_link=(form.payment_link.data or '').strip() or None,
        note=(form.note.data or '').strip() or None,
    )
    db.session.add(payment)
    db.session.flush()
    _finish_idempotency(idem_key, 'OrderPayment', payment.id)
    old_status = order.status
    _refresh_order_status(order)
    if old_status != order.status:
        add_order_event(order, 'order_status_changed', old_value=old_status, new_value=order.status, comment='После добавления оплаты')
    add_order_event(order, 'payment_added', new_value=payment.amount, comment=payment.note)
    db.session.commit()
    flash('Оплата добавлена', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/payments/<int:payment_id>/cancel', methods=['POST'])
@admin_required
def order_payment_cancel(order_id, payment_id):
    order = CustomerOrder.query.get_or_404(order_id)
    payment = OrderPayment.query.filter_by(id=payment_id, order_id=order.id).first_or_404()
    if payment.status == 'CANCELED':
        flash('Оплата уже отменена.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    old_status = payment.status
    payment.status = 'CANCELED'
    _refresh_order_status(order)
    add_order_event(order, 'payment_cancelled', old_value=old_status, new_value='CANCELED', comment=request.form.get('reason') or None)
    _finish_idempotency(idem_key, 'OrderPayment', payment.id)
    db.session.commit()
    flash('Оплата отменена', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/supply-needs')
@role_required('manager', 'director')
def supply_needs_list():
    status = request.args.get('status', '').strip()
    query = SupplyNeed.query.join(Item, Item.id == SupplyNeed.item_id)

    if not current_user.is_admin and current_user.warehouse_id:
        query = query.filter(or_(SupplyNeed.warehouse_id == current_user.warehouse_id, SupplyNeed.warehouse_id.is_(None)))
    if status:
        query = query.filter(SupplyNeed.status == status)

    needs = query.order_by(SupplyNeed.priority.asc(), SupplyNeed.created_at.desc()).all()
    return render_template('supply_needs_list.html', needs=needs, status=status)


def _stock_replenishment_stats(need: SupplyNeed):
    lines = list(need.production_lines or [])
    units = (
        ProducedUnit.query
        .join(ProductionRequestLine, ProductionRequestLine.id == ProducedUnit.production_request_line_id)
        .filter(ProductionRequestLine.supply_need_id == need.id)
        .all()
    )
    accepted_qty = sum(
        1 for unit in units
        if unit.trailer and unit.trailer.warehouse_id == need.warehouse_id and unit.trailer.status == 'IN_STOCK'
    )
    produced_qty = len(units)
    in_production_qty = sum(line.quantity or 0 for line in lines)
    production_requests = {}
    for line in lines:
        if line.production_request:
            production_requests[line.production_request_id] = line.production_request
    return {
        'in_production_qty': in_production_qty,
        'produced_qty': produced_qty,
        'accepted_qty': accepted_qty,
        'remaining_qty': max((need.quantity or 0) - accepted_qty, 0),
        'production_requests': sorted(production_requests.values(), key=lambda pr: pr.created_at or datetime.min, reverse=True),
    }


@main_bp.route('/stock-replenishment')
@role_required('manager', 'director')
def stock_replenishment_list():
    status = request.args.get('status', '').strip()
    query = SupplyNeed.query.filter(SupplyNeed.need_type.in_(['STOCK_REPLENISHMENT', 'WAREHOUSE_STOCK']))
    if current_user.is_manager:
        if not current_user.warehouse_id:
            flash('Пользователь не привязан к складу.', 'warning')
            return redirect(url_for('main.manager_workspace'))
        query = query.filter(SupplyNeed.warehouse_id == current_user.warehouse_id)
    if status:
        query = query.filter(SupplyNeed.status == status)
    needs = query.order_by(SupplyNeed.created_at.desc(), SupplyNeed.id.desc()).all()
    stats = {need.id: _stock_replenishment_stats(need) for need in needs}
    return render_template('stock_replenishment_list.html', needs=needs, stats=stats, status=status)


@main_bp.route('/stock-replenishment/new', methods=['GET', 'POST'])
@role_required('manager', 'director')
def stock_replenishment_create():
    form = StockReplenishmentForm()
    _fill_stock_replenishment_form_choices(form)

    if current_user.is_manager and not current_user.warehouse_id:
        flash('Пользователь не привязан к складу. Обратитесь к администратору.', 'warning')
        return redirect(url_for('main.manager_workspace'))

    if request.method == 'GET' and current_user.is_manager:
        form.warehouse_id.data = current_user.warehouse_id

    if form.validate_on_submit():
        warehouse_id = current_user.warehouse_id if current_user.is_manager else form.warehouse_id.data
        if not warehouse_id:
            flash('Выберите склад назначения.', 'danger')
            return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())
        configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
        if config_errors:
            for error in config_errors:
                flash(error, 'danger')
            return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.stock_replenishment_list'))
        need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT',
            order_id=None,
            item_id=configured_item.id,
            warehouse_id=warehouse_id,
            quantity=form.quantity.data,
            required_by=form.required_by.data,
            status='NEW',
            priority=100,
            note=(form.comment.data or '').strip() or None,
        )
        _apply_snapshot(need, configured_snapshot)
        db.session.add(need)
        db.session.flush()
        _finish_idempotency(idem_key, 'SupplyNeed', need.id)
        db.session.commit()
        flash('Заявка на пополнение склада создана.', 'success')
        return redirect(url_for('main.stock_replenishment_list'))

    return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())


@main_bp.route('/supply-needs/<int:need_id>/cancel', methods=['POST'])
@login_required
def supply_need_cancel(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    if current_user.is_admin or current_user.is_director:
        pass
    elif current_user.is_manager and _is_stock_replenishment_need(need) and current_user.warehouse_id == need.warehouse_id:
        pass
    else:
        abort(403)
    reason = (request.form.get('cancel_reason') or request.form.get('reason') or '').strip()
    if not reason:
        flash('Для отмены укажите причину.', 'danger')
        return redirect(request.referrer or url_for('main.stock_replenishment_list'))
    if (need.status or '').upper() not in ('NEW', 'PLANNED', 'DRAFT') or _supply_need_started(need):
        flash('Заявка уже связана с производством. Отменить её нельзя, можно снять резерв под заказ или отменить ошибочное производство.', 'warning')
    else:
        old_status = need.status
        _cancel_supply_need_and_production_lines(need, reason, current_user.id)
        if need.order:
            add_order_event(need.order, 'production_need_cancelled', old_value=old_status, new_value='CANCELLED', comment=f'Заявка #{need.id} отменена. Причина: {reason}')
            _refresh_order_status(need.order)
        db.session.commit()
        flash('Потребность отменена.', 'success')
    return redirect(request.referrer or url_for('main.stock_replenishment_list'))

@main_bp.route('/supply-needs/<int:need_id>/cleanup-production', methods=['POST'])
@login_required
def supply_need_cleanup_production(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    if not (current_user.is_admin or current_user.is_director):
        abort(403)
    reason = (request.form.get('cancel_reason') or request.form.get('reason') or '').strip()
    if not reason:
        flash('Укажите причину отмены ошибочного производства.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if not need.production_lines:
        flash('По этой потребности нет производственных строк.', 'warning')
        return redirect(request.referrer or url_for('main.supply_needs_list'))

    order = need.order
    active_vin = _vin_registry_for_order_or_need(order=order, need=need)
    if active_vin:
        flash('Нельзя очистить производство: по заказу или заявке уже есть активный VIN.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if order and (order.documents_issued or order.trailer_id or order.is_shipped):
        flash('Нельзя очистить производство: по заказу уже есть документы, прицеп или отгрузка.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if _supply_need_started(need):
        flash('Производство уже начато. Отменить производственную строку нельзя. Можно снять резерв под заказ, если VIN и документы ещё не оформлены.', 'warning')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if _supply_need_has_produced_output(need):
        flash('Нельзя очистить производство: по заявке уже есть выпуск или готовая строка.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))

    old_status = need.status
    cleanup_reason = f'Ошибочное производство отменено. Причина: {reason}'
    _cancel_supply_need_and_production_lines(need, cleanup_reason, current_user.id)
    if need.order:
        add_order_event(
            need.order,
            'production_need_cancelled',
            old_value=old_status,
            new_value='CANCELLED',
            comment=f'Ошибочная заявка на производство #{need.id} отменена. Документы, VIN и прицеп не изменялись. Причина: {reason}',
        )
        _refresh_order_status(need.order)
    db.session.commit()
    flash('Ошибочная заявка на производство отменена. Заказ, документы, VIN и прицеп не изменялись.', 'success')
    return redirect(request.referrer or url_for('main.supply_needs_list'))

@main_bp.route('/supply-needs/<int:need_id>/release-order-reserve', methods=['POST'])
@login_required
def supply_need_release_order_reserve(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager):
        abort(403)
    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('Укажите причину снятия резерва.', 'danger')
        return redirect(request.referrer or url_for('main.stock_replenishment_list'))
    order = need.order
    if need.need_type != 'CUSTOMER_ORDER' or not order:
        flash('Эта заявка не закреплена за заказом клиента.', 'warning')
        return redirect(request.referrer or url_for('main.stock_replenishment_list'))
    active_vin = _active_vin_registry_for_order(order.id)
    if order.documents_issued or order.is_shipped:
        flash('Нельзя снять резерв: по заказу уже есть документы / отгрузка.', 'danger')
        return redirect(request.referrer or url_for('main.stock_replenishment_list'))
    if order.trailer_id:
        ok, message = _release_order_trailer_and_vin(order, reason)
        if not ok:
            flash(message, 'danger')
            return redirect(request.referrer or url_for('main.stock_replenishment_list'))
        active_vin = _active_vin_registry_for_order(order.id)
    if active_vin and (order.status or '').lower() in ('cancelled', 'canceled') and active_vin.status in ('reserved', 'assigned', 'confirmed') and not active_vin.docs_issued_at and not active_vin.trailer_id:
        ok, release_message = _release_order_trailer_and_vin(order, f'Резерв VIN отменён при снятии резерва производства. Причина: {reason}')
        if not ok:
            flash(release_message, 'danger')
            return redirect(request.referrer or url_for('main.stock_replenishment_list'))
        add_order_event(order, 'reservation_cancelled', old_value=active_vin.serial7, new_value='VIN reserve cancelled', comment=f'Резерв VIN отменён при снятии резерва производства. Причина: {reason}')
        active_vin = None
    if active_vin:
        if active_vin.docs_issued_at:
            flash('По заказу уже выданы документы по VIN. Требуется отдельная процедура отмены документов.', 'danger')
        else:
            flash('По заказу есть зарезервированный VIN. Сначала отмените резерв VIN или оставьте заказ без снятия резерва.', 'danger')
        return redirect(request.referrer or url_for('main.stock_replenishment_list'))
    old_order_id = need.order_id
    need.order_id = None
    need.need_type = 'STOCK_REPLENISHMENT'
    need.note = ((need.note or '') + f'\nРезерв под заказ #{old_order_id} снят. Причина: {reason}').strip()
    add_order_event(order, 'production_need_created', old_value=str(old_order_id), new_value='STOCK_REPLENISHMENT', comment=f'Резерв под заказ клиента снят. Производство продолжается на склад. Причина: {reason}')
    db.session.commit()
    flash('Резерв под заказ снят. Заявка переведена в пополнение склада.', 'success')
    return redirect(request.referrer or url_for('main.stock_replenishment_list'))


@main_bp.route('/leads/<int:lead_id>')
@login_required
def lead_detail(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    return render_template('lead_detail.html', lead=lead)


@main_bp.route('/leads/<int:lead_id>/edit', methods=['GET', 'POST'])
@login_required
def lead_edit(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    form = LeadForm(obj=lead)
    _fill_lead_form_choices(form)

    if request.method == 'GET':
        form.created_at.data = lead.created_at.date() if lead.created_at else date.today()
        form.source_channel.data = lead.channel or (lead.source_channel or 'MANUAL').lower()
        form.source_name.data = lead.source_name or lead.source_account
        form.desired_item_id.data = lead.desired_item_id or 0
        _set_item_search_label(form, 'desired_item_search', 'desired_item_id')
        form.warehouse_id.data = lead.warehouse_id or 0
        form.assigned_user_id.data = lead.assigned_user_id or 0

    if form.validate_on_submit():
        configured_snapshot = None
        if request.form.get('item_source') == 'unknown':
            form.desired_item_id.data = 0
        elif request.form.get('item_source') == 'config':
            configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
            if config_errors:
                for error in config_errors:
                    flash(error, 'danger')
                return render_template('lead_form.html', form=form, title='Редактирование заявки', item_options=_trailer_item_options(), config_options=_trailer_config_form_context(), item_source_initial='config')
            form.desired_item_id.data = configured_item.id
        lead.created_at = datetime.combine(form.created_at.data or date.today(), datetime.min.time())
        lead.channel = form.source_channel.data
        lead.source_channel = form.source_channel.data.upper()
        lead.source_name = (form.source_name.data or '').strip() or None
        lead.source_platform = (form.source_platform.data or '').strip() or None
        lead.customer_name = (form.customer_name.data or '').strip()
        lead.phone = (form.phone.data or '').strip() or None
        lead.messenger_username = (form.messenger_username.data or '').strip() or None
        lead.desired_item_id = form.desired_item_id.data or None
        lead.desired_model = (form.desired_model.data or '').strip() or None
        lead.desired_specs = (form.desired_specs.data or '').strip() or None
        lead.warehouse_id = form.warehouse_id.data or None
        lead.assigned_user_id = form.assigned_user_id.data or None
        lead.status = form.status.data
        lead.text = (form.text.data or '').strip() or None
        lead.comment = (form.comment.data or '').strip() or None
        if configured_snapshot:
            _apply_snapshot(lead, configured_snapshot)
        db.session.commit()
        flash('Заявка обновлена', 'success')
        return redirect(url_for('main.lead_detail', lead_id=lead.id))

    if lead.article_snapshot:
        item_source_initial = 'config'
    elif lead.desired_item_id:
        item_source_initial = 'existing'
    else:
        item_source_initial = 'unknown'
    return render_template('lead_form.html', form=form, title='Редактирование заявки', item_options=_trailer_item_options(), config_options=_trailer_config_form_context(), item_source_initial=item_source_initial)


@main_bp.route('/leads/<int:lead_id>/create-order', methods=['GET', 'POST'])
@login_required
def lead_create_order(lead_id):
    _block_production_commercial_access()
    lead = Lead.query.get_or_404(lead_id)
    _ensure_customer_for_lead(lead)
    if current_user.is_manager:
        if not lead.assigned_user_id:
            lead.assigned_user_id = current_user.id
        if not lead.warehouse_id and current_user.warehouse_id:
            lead.warehouse_id = current_user.warehouse_id
    db.session.commit()
    return redirect(url_for('main.order_create', lead_id=lead.id, return_to='manager_workspace'))


@main_bp.route('/leads/<int:lead_id>/delete', methods=['POST'])
@login_required
def lead_delete(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    if lead.orders:
        flash('Нельзя удалить заявку, по которой уже есть заказ.', 'danger')
    else:
        db.session.delete(lead)
        db.session.commit()
        flash('Заявка удалена', 'success')
    return redirect(url_for('main.leads_list'))


@main_bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    _block_production_commercial_access()
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_access_order(order)
    payment_form = OrderPaymentForm()
    payment_form.paid_at.data = date.today()

    stock_query = Trailer.query.filter(
        Trailer.item_id == order.item_id,
        Trailer.status == 'IN_STOCK',
        or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
    ).order_by(Trailer.vin)
    if order.warehouse_id:
        stock_query = stock_query.filter(Trailer.warehouse_id == order.warehouse_id)
    elif current_user.is_manager:
        stock_query = stock_query.filter(Trailer.warehouse_id == current_user.warehouse_id)
    available_stock_trailers = [
        trailer for trailer in stock_query.all()
        if _trailer_available_for_sale(trailer, exclude_order_id=order.id)
    ]
    other_warehouse_trailers = [
        trailer for trailer in (
        Trailer.query
        .filter(
            Trailer.item_id == order.item_id,
            Trailer.status == 'IN_STOCK',
            Trailer.warehouse_id != order.warehouse_id,
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Trailer.vin)
        .all()
        )
        if _trailer_available_for_sale(trailer, exclude_order_id=order.id)
    ]
    movements = StockMovement.query.filter_by(order_id=order.id).order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()
    events = order.events.order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc()).all()
    payments = order.payments.order_by(OrderPayment.created_at.desc()).all()
    reservations = order.reservations.order_by(Reservation.created_at.desc()).all()
    supply_needs = order.supply_needs.order_by(SupplyNeed.created_at.desc()).all()
    order_lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    order_line_rows = []
    for line in order_lines:
        vin_rows = (
            VinRegistry.query
            .filter(
                VinRegistry.order_line_id == line.id,
                VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
            )
            .order_by(VinRegistry.confirmed_at.desc().nullslast(), VinRegistry.assigned_at.desc().nullslast(), VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
            .all()
        )
        active_reservation = next((row for row in getattr(line, 'reservations', []) if row.status == 'ACTIVE'), None)
        active_needs = [row for row in getattr(line, 'supply_needs', []) if row.status in ('NEW', 'PLANNED', 'IN_PRODUCTION', 'SENT_TO_PRODUCTION', 'PARTIALLY_DONE')]
        production_qty = sum(need.quantity or 0 for need in active_needs)
        vin_count = len(vin_rows)
        shortage_qty = max((line.quantity or 1) - production_qty, 0) if line.fulfillment_source == 'production' else 0
        can_edit_qty, edit_qty_message = _can_edit_order_line_quantity(line)
        can_edit_details, edit_details_message = _can_edit_order_line_details(line)
        can_delete_line, delete_message = _can_delete_order_line(line)
        can_ship_line, ship_message = _order_line_can_ship(line)
        can_change_source, source_blockers = can_change_order_line_source(line)
        source_change_trailers = []
        vin_link_candidates = []
        can_create_vin_from_trailer = False
        vin_link_message = ''
        if can_change_source and (line.fulfillment_source or '').lower() == 'production' and (line.quantity or 1) == 1:
            source_change_trailers = [
                trailer for trailer in (
                    Trailer.query
                    .filter(
                        Trailer.item_id == line.item_id,
                        Trailer.status == 'IN_STOCK',
                        or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
                    )
                    .order_by(Trailer.vin)
                    .limit(80)
                    .all()
                )
                if _trailer_available_for_sale(trailer, exclude_order_id=order.id)
            ]
        if (
            can_manage_order(order)
            and line.line_type == 'TRAILER'
            and not vin_rows
            and not order.documents_issued
            and not order.is_shipped
            and order.status != 'cancelled'
            and not _line_has_any_realization(line)
        ):
            vin_link_candidates = _vin_link_candidates_for_line(order, line)
            can_create_vin_from_trailer = bool(
                line.trailer
                and line.trailer.vin
                and not VinRegistry.query.filter_by(vin_full=line.trailer.vin).first()
            )
            if not vin_link_candidates and not can_create_vin_from_trailer and line.trailer and line.trailer.vin:
                vin_link_message = 'По прицепу строки нет свободной активной записи VIN-реестра.'
        order_line_rows.append({
            'line': line,
            'vin_rows': vin_rows,
            'vin_count': vin_count,
            'reservation': active_reservation,
            'supply_need': active_needs[0] if active_needs else None,
            'supply_needs': active_needs,
            'production_qty': production_qty,
            'shortage_qty': shortage_qty,
            'trailer': (vin_rows[0].trailer if vin_rows and vin_rows[0].trailer else (active_reservation.trailer if active_reservation else None)),
            'can_edit_quantity': can_edit_qty and can_edit_details,
            'edit_quantity_message': edit_details_message or edit_qty_message,
            'can_edit_details': can_edit_details,
            'edit_details_message': edit_details_message,
            'can_delete': can_delete_line,
            'delete_message': delete_message,
            'can_ship': can_ship_line,
            'ship_message': ship_message,
            'can_change_source': can_change_source and (line.quantity or 1) == 1,
            'source_change_message': '; '.join(source_blockers) if source_blockers else '',
            'source_change_trailers': source_change_trailers,
            'vin_link_candidates': vin_link_candidates,
            'can_create_vin_from_trailer': can_create_vin_from_trailer,
            'vin_link_message': vin_link_message,
        })
    add_line_stock_trailers = [
        trailer for trailer in (
            Trailer.query
            .filter(
                Trailer.status == 'IN_STOCK',
                or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
            )
            .order_by(Trailer.vin)
            .limit(80)
            .all()
        )
        if _trailer_available_for_sale(trailer, exclude_order_id=order.id)
    ]
    add_line_items = Item.query.filter_by(is_active=True).order_by(Item.article.asc().nullslast(), Item.name.asc()).limit(150).all()
    order_contract = SalesContract.query.filter_by(order_id=order.id).first()
    order_contract_template = _contract_template_for_contract(order_contract)
    document_blockers = _order_lines_document_blockers(order)
    order_vin_row = _active_vin_registry_for_order(order.id)
    future_production_lines = []
    future_produced_unit_rows = []
    future_ready_trailer_rows = []
    future_inbound_movements = []
    if order.warehouse_id:
        future_production_lines = (
            ProductionRequestLine.query
            .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
            .filter(
                ProductionRequest.target_warehouse_id == order.warehouse_id,
                ProductionRequestLine.item_id == order.item_id,
                ProductionRequestLine.status.in_(['planned', 'PLANNED', 'in_production', 'partial_ready']),
            )
            .order_by(ProductionRequest.created_at.desc(), ProductionRequestLine.id.desc())
            .all()
        )
        produced_units = (
            ProducedUnit.query
            .filter(
                ProducedUnit.target_warehouse_id == order.warehouse_id,
                ProducedUnit.item_id == order.item_id,
                ProducedUnit.status == 'produced_no_vin',
            )
            .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
            .all()
        )
        for unit in produced_units:
            context = _produced_unit_context(unit)
            attached_order = context.get('order')
            if attached_order and attached_order.id == order.id:
                attach_state = 'current'
            elif attached_order and _order_is_open_for_attachment(attached_order):
                attach_state = 'other'
            else:
                attach_state = 'free'
            context['attach_state'] = attach_state
            context['can_attach'] = (
                can_manage_order(order)
                and attach_state in ('free', 'current')
                and attach_state != 'current'
                and not order.trailer_id
                and _order_is_open_for_attachment(order)
                and not unit.trailer_id
                and _ensure_item_matches_order(unit.item_id, order)
                and _ensure_target_matches_order_warehouse(unit.target_warehouse_id, order)
            )
            future_produced_unit_rows.append(context)
        production_warehouse = _default_production_warehouse()
        if production_warehouse:
            ready_units = (
                ProducedUnit.query
                .join(Trailer, Trailer.id == ProducedUnit.trailer_id)
                .filter(
                    ProducedUnit.target_warehouse_id == order.warehouse_id,
                    ProducedUnit.item_id == order.item_id,
                    ProducedUnit.status == 'vin_assigned',
                    Trailer.warehouse_id == production_warehouse.id,
                    or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
                )
                .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
                .all()
            )
            ready_trailer_ids = set()
            for unit in ready_units:
                trailer = unit.trailer
                if not trailer or _active_movement_for_trailer(trailer.id):
                    continue
                ready_trailer_ids.add(trailer.id)
                if trailer.status == 'SOLD' and order.trailer_id != trailer.id:
                    continue
                attach_state = _order_attachment_status_for_trailer(trailer, order)
                context = _produced_unit_context(unit)
                context['trailer'] = trailer
                context['attach_state'] = attach_state
                context['can_attach'] = (
                    can_manage_order(order)
                    and attach_state == 'free'
                    and not order.trailer_id
                    and _order_is_open_for_attachment(order)
                    and bool(trailer.vin)
                    and trailer.status != 'SOLD'
                    and _ensure_item_matches_order(trailer.item_id, order)
                    and not _trailer_is_customer_shipped(trailer)
                    and not _active_movement_for_trailer(trailer.id)
                )
                future_ready_trailer_rows.append(context)
            extra_ready_trailers = (
                Trailer.query
                .filter(
                    Trailer.item_id == order.item_id,
                    Trailer.warehouse_id == production_warehouse.id,
                    Trailer.status.in_(['IN_STOCK', 'RESERVED']),
                    or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
                )
                .order_by(Trailer.created_at.desc(), Trailer.id.desc())
                .all()
            )
            for trailer in extra_ready_trailers:
                if trailer.id in ready_trailer_ids or _active_movement_for_trailer(trailer.id):
                    continue
                attach_state = _order_attachment_status_for_trailer(trailer, order)
                context = {
                    'unit': None,
                    'line': None,
                    'need': None,
                    'order': order if attach_state == 'current' else None,
                    'need_type': 'CUSTOMER_ORDER',
                    'target_warehouse': order.warehouse,
                    'trailer': trailer,
                    'attach_state': attach_state,
                    'can_attach': (
                        can_manage_order(order)
                        and attach_state == 'free'
                        and not order.trailer_id
                        and _order_is_open_for_attachment(order)
                        and bool(trailer.vin)
                        and _ensure_item_matches_order(trailer.item_id, order)
                        and not _trailer_is_customer_shipped(trailer)
                    ),
                }
                future_ready_trailer_rows.append(context)
        future_inbound_movements = (
            StockMovement.query
            .join(Trailer, Trailer.id == StockMovement.trailer_id)
            .filter(
                StockMovement.to_warehouse_id == order.warehouse_id,
                StockMovement.status.in_(['sent', 'in_transit']),
                Trailer.item_id == order.item_id,
                or_(StockMovement.order_id.is_(None), StockMovement.order_id == order.id),
            )
            .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
            .all()
        )
    return render_template(
        'order_detail.html',
        order=order,
        payment_form=payment_form,
        available_stock_trailers=available_stock_trailers,
        other_warehouse_trailers=other_warehouse_trailers,
        movements=movements,
        events=events,
        payments=payments,
        reservations=reservations,
        supply_needs=supply_needs,
        order_line_rows=order_line_rows,
        add_line_stock_trailers=add_line_stock_trailers,
        add_line_items=add_line_items,
        order_contract=order_contract,
        order_contract_template=order_contract_template,
        document_blockers=document_blockers,
        order_vin_row=order_vin_row,
        future_production_lines=future_production_lines,
        future_produced_unit_rows=future_produced_unit_rows,
        future_ready_trailer_rows=future_ready_trailer_rows,
        future_inbound_movements=future_inbound_movements,
        can_manage=can_manage_order(order),
    )


@main_bp.route('/orders/<int:order_id>/delete', methods=['POST'])
@admin_required
def order_delete(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    blockers = _order_delete_blockers(order)
    if blockers:
        flash('Заказ нельзя удалить: ' + '; '.join(blockers) + '.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    order_number = order.order_number
    db.session.delete(order)
    db.session.commit()
    flash(f'Заказ {order_number} удалён.', 'success')
    return redirect(url_for('main.orders_list'))


@main_bp.route('/orders/<int:order_id>/edit', methods=['GET', 'POST'])
@login_required
def order_edit(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    form = CustomerOrderForm(order_id=order.id, obj=order)
    _fill_order_form_choices(form, item_id_prefill=order.item_id, current_order_id=order.id)

    if request.method == 'GET':
        form.order_date.data = order.created_at.date() if order.created_at else date.today()
        form.lead_id.data = order.lead_id or 0
        customer_id_prefill = request.args.get('customer_id', type=int)
        if customer_id_prefill:
            customer = Customer.query.get(customer_id_prefill)
            if customer:
                form.customer_id.data = customer.id
                form.customer_search.data = _customer_label(customer)
        else:
            form.customer_id.data = order.customer_id
            _set_customer_search_label(form)
        form.trailer_id.data = order.trailer_id or 0
        form.warehouse_id.data = order.warehouse_id or 0
        form.assigned_user_id.data = order.assigned_user_id or 0
        form.quantity.data = order.quantity or 1
        form.price.data = order.price
        form.fulfillment_source.data = order.fulfillment_source or 'later'

    if form.validate_on_submit():
        if order.lines.count() > 0:
            order.order_number = (form.order_number.data or '').strip() or order.order_number
            if form.order_date.data:
                order.created_at = datetime.combine(form.order_date.data, time.min)
            order.lead_id = form.lead_id.data or None
            order.customer_id = form.customer_id.data
            order.warehouse_id = form.warehouse_id.data or None
            order.assigned_user_id = form.assigned_user_id.data or None
            order.expected_date = form.expected_date.data
            order.planned_ship_date = form.planned_ship_date.data
            order.planned_ship_comment = (form.planned_ship_comment.data or '').strip() or None
            order.note = (form.note.data or '').strip() or None
            order.manager_comment = (form.manager_comment.data or '').strip() or None
            order.prepayment_percent = form.prepayment_percent.data
            _refresh_order_status(order)
            add_order_event(order, 'comment_added', comment='Обновлена шапка заказа без изменения строк, VIN, резервов и потребностей')
            db.session.commit()
            flash('Шапка заказа обновлена. Строки, VIN, резервы и потребности не изменялись.', 'success')
            return redirect(url_for('main.order_detail', order_id=order.id))

        old_status = order.status
        old_trailer_id = order.trailer_id
        old_trailer = Trailer.query.get(old_trailer_id) if old_trailer_id else None
        fulfillment_source = (form.fulfillment_source.data or '').strip() or None
        configured_item = None
        configured_snapshot = None
        if fulfillment_source == 'production':
            form.trailer_id.data = 0
            if _order_should_build_config(request.form, fulfillment_source):
                configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
                if config_errors:
                    for error in config_errors:
                        flash(error, 'danger')
                    return _render_order_form(form, 'Редактирование заказа')
                form.item_id.data = configured_item.id
            elif form.item_id.data:
                configured_item = Item.query.get(form.item_id.data)
            if not configured_item:
                flash('Для заказа в производство выберите номенклатуру или соберите прицеп через конфигуратор.', 'danger')
                return _render_order_form(form, 'Редактирование заказа')

        new_trailer_id = form.trailer_id.data or None
        if new_trailer_id and _active_reservation_for_trailer(new_trailer_id, exclude_order_id=order.id):
            flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        selected_trailer = Trailer.query.get(new_trailer_id) if new_trailer_id else None
        if selected_trailer and (form.quantity.data or 1) > 1:
            flash('Для продажи из наличия можно выбрать только 1 прицеп, потому что заказ привязывается к одному VIN. Для двух прицепов используйте производство или отдельные заказы.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and selected_trailer.item_id != form.item_id.data:
            flash('Выбранный VIN не соответствует выбранной модели.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and selected_trailer.status == 'SOLD' and selected_trailer.id != old_trailer_id:
            flash('Проданный прицеп нельзя выбрать для новой продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and selected_trailer.status not in ('IN_STOCK', 'RESERVED', 'SOLD'):
            flash('Выбранный прицеп сейчас недоступен для заказа.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and (form.fulfillment_source.data or 'stock') != 'other_warehouse' and form.warehouse_id.data and selected_trailer.warehouse_id != form.warehouse_id.data:
            flash('Для продажи из наличия выбранный VIN должен находиться на складе продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and form.fulfillment_source.data == 'other_warehouse' and form.warehouse_id.data and selected_trailer.warehouse_id == form.warehouse_id.data:
            flash('Для сценария с другого склада выберите VIN не со склада продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')

        order.order_number = (form.order_number.data or '').strip() or order.order_number
        if form.order_date.data:
            order.created_at = datetime.combine(form.order_date.data, time.min)
        order.lead_id = form.lead_id.data or None
        order.customer_id = form.customer_id.data
        order.item_id = form.item_id.data
        order.warehouse_id = form.warehouse_id.data or None
        order.assigned_user_id = form.assigned_user_id.data or None
        order.quantity = form.quantity.data or 1
        order.price = _order_price_from_form_or_config(form, configured_snapshot) or order.price
        order.prepayment_percent = form.prepayment_percent.data
        order.fulfillment_source = fulfillment_source
        order.expected_date = form.expected_date.data
        order.planned_ship_date = form.planned_ship_date.data
        order.planned_ship_comment = (form.planned_ship_comment.data or '').strip() or None
        order.note = (form.note.data or '').strip() or None
        order.manager_comment = (form.manager_comment.data or '').strip() or None
        if configured_snapshot:
            _apply_snapshot(order, configured_snapshot)
        order_line = _sync_primary_order_line(order, configured_snapshot or _order_snapshot(order))
        db.session.flush()

        ok, message = _sync_order_trailer_links(order, old_trailer, selected_trailer, 'STOCK')
        if not ok:
            flash(message, 'danger')
            return _render_order_form(form, 'Редактирование заказа')

        if new_trailer_id:
            order.fulfillment_source = order.fulfillment_source or 'stock'
        elif order.fulfillment_source == 'production' and order.supply_needs.count() == 0:
            can_request, message = order_can_request_production(order)
            if not can_request:
                flash(message, 'warning')
                return _render_order_form(form, 'Редактирование заказа')
            need = SupplyNeed(order_id=order.id, order_line_id=order_line.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity, status='NEW', priority=10, need_type='CUSTOMER_ORDER', required_by=order.expected_date)
            _apply_snapshot(need, configured_snapshot or _order_snapshot(order))
            db.session.add(need)
        elif order.fulfillment_source == 'production':
            warning = _sync_editable_order_production_need(order, configured_snapshot)
            if warning:
                flash(warning, 'warning')

        _refresh_order_status(order)
        order_line.status = order.status
        if old_status != order.status:
            add_order_event(order, 'order_status_changed', old_value=old_status, new_value=order.status)
        if old_trailer_id != new_trailer_id and new_trailer_id:
            add_order_event(order, 'trailer_reserved', new_value=new_trailer_id)
        db.session.commit()
        flash('Заказ обновлен', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    if order.lines.count() > 0:
        return _render_order_header_form(form, order, 'Редактирование шапки заказа')
    return _render_order_form(form, 'Редактирование заказа')


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/update', methods=['POST'])
@login_required
def order_line_update(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        flash('Позиции можно менять только до выдачи документов и отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    can_edit_details, edit_details_message = _can_edit_order_line_details(line)
    if not can_edit_details:
        flash(edit_details_message, 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    quantity = max(request.form.get('quantity', type=int) or 1, 1)
    unit_price_raw = (request.form.get('unit_price') or '').strip()
    try:
        unit_price = Decimal(unit_price_raw.replace(',', '.')) if unit_price_raw else None
    except Exception:
        flash('Цена указана неверно.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    if quantity != (line.quantity or 1):
        can_edit_qty, message = _can_edit_order_line_quantity(line)
        if not can_edit_qty:
            flash(message, 'danger')
            return redirect(url_for('main.order_detail', order_id=order.id))
        old_qty = line.quantity or 1
        line.quantity = quantity
        active_needs = _line_active_supply_needs(line)
        if len(active_needs) == 1 and active_needs[0].status in ('NEW', 'PLANNED', 'planned'):
            active_needs[0].quantity = quantity
        add_order_event(order, 'comment_added', old_value=str(old_qty), new_value=str(quantity), comment=f'Изменено количество позиции #{line.line_no}')

    old_price = line.unit_price
    line.unit_price = unit_price
    line.total_price = (unit_price * (line.quantity or 1)) if unit_price is not None else None
    line.note = (request.form.get('note') or '').strip() or None
    _sync_order_totals_from_lines(order)
    _refresh_order_status(order)
    add_order_event(order, 'comment_added', old_value=str(old_price or ''), new_value=str(unit_price or ''), comment=f'Обновлена позиция #{line.line_no}')
    db.session.commit()
    flash('Позиция заказа обновлена.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/delete', methods=['POST'])
@login_required
def order_line_delete(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    can_delete, message = _can_delete_order_line(line)
    if not can_delete:
        flash(message, 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    old_line_no = line.line_no
    old_label = line.article_snapshot or (line.item.article if line.item else '') or f'#{old_line_no}'
    db.session.delete(line)
    db.session.flush()
    remaining_lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    for idx, row in enumerate(remaining_lines, start=1):
        row.line_no = idx
    _sync_order_totals_from_lines(order)
    _refresh_order_status(order)
    add_order_event(order, 'comment_added', old_value=old_label, comment=f'Удалена позиция заказа #{old_line_no}')
    db.session.commit()
    flash('Позиция заказа удалена.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/add-stock', methods=['POST'])
@login_required
def order_line_add_stock(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        flash('Позиции можно менять только до выдачи документов и отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    trailer = Trailer.query.get_or_404(request.form.get('trailer_id', type=int))
    if not _trailer_available_for_sale(trailer, exclude_order_id=order.id):
        flash('Этот прицеп уже недоступен для резерва.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    source_type = 'TRANSFER' if order.warehouse_id and trailer.warehouse_id != order.warehouse_id else 'STOCK'
    line = _create_order_line_from_trailer(order, trailer, source_type)
    if not order.item_id:
        order.item_id = trailer.item_id
    if not order.trailer_id:
        order.trailer_id = trailer.id
        order.source_warehouse_id = trailer.warehouse_id
    order.fulfillment_source = 'stock'
    _sync_order_totals_from_lines(order)
    _refresh_order_status(order)
    _finish_idempotency(idem_key, 'CustomerOrderLine', line.id)
    add_order_event(order, 'order_line_added', new_value=trailer.vin, comment=f'Добавлена позиция #{line.line_no} из наличия')
    db.session.commit()
    flash('Позиция из наличия добавлена и зарезервирована.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/add-production', methods=['POST'])
@login_required
def order_line_add_production(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        flash('Позиции можно менять только до выдачи документов и отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    item = Item.query.get_or_404(request.form.get('item_id', type=int))
    quantity = max(request.form.get('quantity', type=int) or 1, 1)
    unit_price_raw = (request.form.get('unit_price') or '').strip()
    try:
        unit_price = Decimal(unit_price_raw.replace(',', '.')) if unit_price_raw else item.base_price
    except Exception:
        flash('Цена указана неверно.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    line = CustomerOrderLine(
        order_id=order.id,
        line_no=_next_order_line_no(order),
        line_type='TRAILER' if str(item.item_type or '').upper() == 'TRAILER' else 'COMPONENT',
        fulfillment_source='production',
        item_id=item.id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=(unit_price * quantity) if unit_price is not None else None,
        status='waiting_production',
        note=(request.form.get('note') or '').strip() or None,
    )
    _apply_snapshot(line, _line_snapshot_from_item(item))
    db.session.add(line)
    db.session.flush()
    need = SupplyNeed(
        order_id=order.id,
        order_line_id=line.id,
        item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity=quantity,
        status='NEW',
        priority=10,
        need_type='CUSTOMER_ORDER',
        required_by=order.expected_date,
        note='Потребность создана по позиции заказа',
    )
    _apply_snapshot(need, _line_snapshot_from_item(item))
    db.session.add(need)
    if not order.item_id:
        order.item_id = item.id
    order.fulfillment_source = 'production' if not order.trailer_id else order.fulfillment_source
    _sync_order_totals_from_lines(order)
    _refresh_order_status(order)
    _finish_idempotency(idem_key, 'CustomerOrderLine', line.id)
    add_order_event(order, 'order_line_added', new_value=item.article or item.name, comment=f'Добавлена позиция #{line.line_no} в производство')
    db.session.commit()
    flash('Позиция в производство добавлена, потребность создана.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/create-missing-production', methods=['POST'])
@login_required
def order_line_create_missing_production(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        flash('Производственную потребность можно менять только до выдачи документов и отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    active_needs = [need for need in line.supply_needs if need.status in ('NEW', 'PLANNED', 'IN_PRODUCTION', 'SENT_TO_PRODUCTION', 'PARTIALLY_DONE')]
    production_qty = sum(need.quantity or 0 for need in active_needs)
    missing_qty = max((line.quantity or 1) - production_qty, 0)
    if missing_qty <= 0:
        flash('По этой позиции уже создано производство на всё количество.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    need = SupplyNeed(
        order_id=order.id,
        order_line_id=line.id,
        item_id=line.item_id,
        warehouse_id=order.warehouse_id,
        quantity=missing_qty,
        status='NEW',
        priority=10,
        need_type='CUSTOMER_ORDER',
        required_by=order.expected_date,
        note=f'Досоздана недостающая потребность по позиции #{line.line_no}',
    )
    _apply_snapshot(need, {key: getattr(line, key, None) for key in SNAPSHOT_FIELD_NAMES})
    db.session.add(need)
    _finish_idempotency(idem_key, 'SupplyNeed', need.id)
    add_order_event(order, 'production_need_created', new_value=missing_qty, comment=f'Досоздано производство по позиции #{line.line_no}')
    db.session.commit()
    flash(f'Создана недостающая потребность: {missing_qty} шт.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/source/stock', methods=['POST'])
@login_required
def order_line_source_to_stock(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    trailer_id = request.form.get('trailer_id', type=int)
    comment = (request.form.get('comment') or '').strip() or None
    if not trailer_id:
        flash('Выберите прицеп из наличия для смены источника.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    try:
        change_line_source_production_to_stock(line.id, trailer_id, current_user.id, comment)
        db.session.commit()
        flash('Источник позиции изменён на наличие. Производственная потребность отменена.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/source/production', methods=['POST'])
@login_required
def order_line_source_to_production(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    target_item_id = request.form.get('item_id', type=int) or line.item_id
    quantity = request.form.get('quantity', type=int) or line.quantity or 1
    comment = (request.form.get('comment') or '').strip() or None
    if not target_item_id:
        flash('Выберите номенклатуру для производства.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    try:
        change_line_source_stock_to_production(line.id, target_item_id, quantity, current_user.id, comment)
        db.session.commit()
        flash('Источник позиции изменён на производство. Старый резерв VIN/прицепа снят.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/link-vin', methods=['POST'])
@login_required
def order_line_link_vin(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    try:
        if request.form.get('create_from_trailer'):
            vin_row = _create_vin_registry_from_line_trailer(order, line)
        else:
            vin_row = VinRegistry.query.get_or_404(request.form.get('vin_id', type=int))
        _link_vin_registry_to_order_line(order, line, vin_row)
        _refresh_order_status(order)
        db.session.commit()
        flash('VIN привязан к строке заказа. Реализация будет создана по этой строке.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('main.order_detail', order_id=order.id))


def _posted_realization_for_trailer(trailer_id: int | None):
    if not trailer_id:
        return None
    return (
        SalesRealizationLine.query
        .join(SalesRealization, SalesRealization.id == SalesRealizationLine.realization_id)
        .filter(
            SalesRealization.status == 'posted',
            SalesRealizationLine.trailer_id == trailer_id,
            SalesRealizationLine.inventory_effect == 'trailer_unit',
        )
        .first()
    )


def _order_realization_lines_source(order: CustomerOrder) -> list[CustomerOrderLine]:
    lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
    if not lines:
        lines = [_sync_primary_order_line(order, _order_snapshot(order))]
        db.session.flush()
    return [line for line in lines if getattr(line, 'include_in_realization', True)]


def _refresh_order_realization_status(order: CustomerOrder) -> None:
    posted = [row for row in order.sales_realizations if row.status == 'posted']
    if not posted:
        order.realization_status = 'not_started'
        order.realized_at = None
        return
    source_lines = _order_realization_lines_source(order)
    realized_line_ids = {
        line.order_line_id
        for realization in posted
        for line in realization.lines
        if line.order_line_id
    }
    if source_lines and all(line.id in realized_line_ids for line in source_lines):
        order.realization_status = 'realized'
        order.realized_at = max((row.posted_at for row in posted if row.posted_at), default=datetime.utcnow())
    else:
        order.realization_status = 'partial'


def _vin_link_candidates_for_line(order: CustomerOrder, line: CustomerOrderLine) -> list[VinRegistry]:
    trailer_id = line.trailer_id
    if not trailer_id:
        active_reservation = next((row for row in getattr(line, 'reservations', []) if row.status == 'ACTIVE' and row.trailer_id), None)
        trailer_id = active_reservation.trailer_id if active_reservation else None
    if not trailer_id:
        return []
    return (
        VinRegistry.query
        .filter(
            VinRegistry.trailer_id == trailer_id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
            or_(VinRegistry.order_line_id.is_(None), VinRegistry.order_line_id == line.id),
            or_(VinRegistry.customer_order_id.is_(None), VinRegistry.customer_order_id == order.id),
        )
        .order_by(
            VinRegistry.confirmed_at.desc().nullslast(),
            VinRegistry.assigned_at.desc().nullslast(),
            VinRegistry.reserved_at.desc().nullslast(),
            VinRegistry.id.desc(),
        )
        .all()
    )


def _link_vin_registry_to_order_line(order: CustomerOrder, line: CustomerOrderLine, vin_row: VinRegistry) -> None:
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        raise ValueError('VIN строки можно менять только до документов и отгрузки.')
    if line.line_type != 'TRAILER':
        raise ValueError('VIN можно привязать только к строке техники.')
    if _line_has_any_realization(line):
        raise ValueError('VIN строки нельзя менять: по строке уже есть реализация.')
    if vin_row.status not in ('reserved', 'assigned', 'confirmed'):
        raise ValueError('Можно привязать только активный VIN: reserved, assigned или confirmed.')
    if vin_row.order_line_id and vin_row.order_line_id != line.id:
        raise ValueError('Этот VIN уже привязан к другой строке заказа.')
    if vin_row.customer_order_id and vin_row.customer_order_id != order.id:
        raise ValueError('Этот VIN уже привязан к другому заказу.')
    if not vin_row.trailer:
        raise ValueError('У VIN нет физического прицепа. Сначала привяжите VIN к прицепу в VIN-реестре.')
    trailer = vin_row.trailer
    if line.trailer_id and line.trailer_id != trailer.id:
        raise ValueError('Прицеп в строке заказа не совпадает с прицепом VIN-реестра.')
    if line.item_id and trailer.item_id and line.item_id != trailer.item_id:
        raise ValueError('Номенклатура строки заказа не совпадает с номенклатурой прицепа.')
    if trailer.vin and vin_row.vin_full and trailer.vin != vin_row.vin_full:
        raise ValueError('VIN в реестре не совпадает с VIN физического прицепа.')
    if _posted_realization_for_trailer(trailer.id):
        raise ValueError('Этот VIN уже есть в проведённой реализации.')

    old_trailer_id = line.trailer_id
    line.trailer_id = trailer.id
    line.fulfillment_source = line.fulfillment_source or 'stock'
    line.status = 'vin_confirmed' if vin_row.status == 'confirmed' else 'vin_assigned'
    vin_row.order_line_id = line.id
    vin_row.customer_order_id = order.id

    if order.lines.count() == 1 and order.trailer_id != trailer.id:
        order.trailer_id = trailer.id
        order.source_warehouse_id = trailer.warehouse_id
    elif not order.trailer_id:
        order.trailer_id = trailer.id
        order.source_warehouse_id = trailer.warehouse_id

    if not any(row.status == 'ACTIVE' and row.trailer_id == trailer.id for row in line.reservations):
        db.session.add(Reservation(
            order_id=order.id,
            order_line_id=line.id,
            trailer_id=trailer.id,
            item_id=line.item_id,
            status='ACTIVE',
            source_type='VIN_LINK',
            priority=10,
        ))
    add_order_event(
        order,
        'trailer_reserved',
        old_value=str(old_trailer_id or ''),
        new_value=vin_row.vin_full or trailer.vin or str(trailer.id),
        comment=f'VIN привязан к позиции #{line.line_no}',
    )
    _add_vin_event(vin_row, 'linked_to_order_line', vin_row.status, vin_row.status, comment=f'Привязан к заказу {order.order_number}, позиция #{line.line_no}')


def _create_vin_registry_from_line_trailer(order: CustomerOrder, line: CustomerOrderLine) -> VinRegistry:
    if not line.trailer or not line.trailer.vin:
        raise ValueError('У строки нет прицепа с VIN.')
    existing = VinRegistry.query.filter_by(vin_full=line.trailer.vin).first()
    if existing:
        return existing
    serial = (line.trailer.vin or '')[-7:]
    if len(serial) != 7:
        raise ValueError('VIN прицепа не содержит корректный 7-значный порядковый номер.')
    serial_owner = VinRegistry.query.filter_by(serial7=serial).first()
    if serial_owner:
        raise ValueError(f'Порядковый номер {serial} уже есть в VIN-реестре.')
    vin_row = VinRegistry(
        vin_full=line.trailer.vin,
        prefix=(line.trailer.vin or 'MX4')[:3],
        serial7=serial,
        status='confirmed',
        customer_order_id=order.id,
        order_line_id=line.id,
        trailer_id=line.trailer.id,
        confirmed_by_user_id=current_user.id,
        confirmed_at=datetime.utcnow(),
        source='legacy_repair',
        comment='Создано из VIN физического прицепа при привязке строки заказа',
    )
    db.session.add(vin_row)
    db.session.flush()
    _add_vin_event(vin_row, 'created_from_trailer', None, 'confirmed', comment=f'Создано из прицепа для заказа {order.order_number}, позиция #{line.line_no}')
    return vin_row


def _resolve_realization_trailer_for_line(order: CustomerOrder, source_line: CustomerOrderLine):
    if source_line.line_type != 'TRAILER':
        return None, None, None

    vin_row = (
        VinRegistry.query
        .filter(
            VinRegistry.order_line_id == source_line.id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(
            VinRegistry.confirmed_at.desc().nullslast(),
            VinRegistry.assigned_at.desc().nullslast(),
            VinRegistry.id.desc(),
        )
        .first()
    )
    if not vin_row:
        return None, None, f'Позиция #{source_line.line_no}: нет активного VIN в реестре по этой строке заказа. Привяжите правильный VIN к строке перед созданием реализации.'

    trailer = vin_row.trailer

    if source_line.trailer_id and trailer and source_line.trailer_id != trailer.id:
        return None, None, f'Позиция #{source_line.line_no}: прицеп в строке заказа не совпадает с прицепом VIN-реестра.'

    if vin_row and vin_row.trailer_id and trailer and vin_row.trailer_id != trailer.id:
        return None, None, f'Позиция #{source_line.line_no}: VIN связан с другим прицепом. Проверьте строку заказа и VIN-реестр.'

    if trailer and vin_row and vin_row.vin_full and trailer.vin and trailer.vin != vin_row.vin_full:
        return None, None, f'Позиция #{source_line.line_no}: VIN строки не совпадает с VIN прицепа.'

    if not trailer:
        return None, None, f'Позиция #{source_line.line_no}: нет однозначно привязанного прицепа/VIN для реализации. Откройте строку заказа и привяжите правильный VIN/прицеп перед созданием реализации.'

    if _posted_realization_for_trailer(trailer.id):
        return None, None, f'Позиция #{source_line.line_no}: VIN {trailer.vin or trailer.id} уже есть в проведённой реализации.'

    return trailer, vin_row, None


def _build_sales_realization_from_order(order: CustomerOrder) -> SalesRealization:
    realization = SalesRealization(
        number=_next_number('RLS', SalesRealization, 'number'),
        realization_date=date.today(),
        order_id=order.id,
        customer_id=order.customer_id,
        warehouse_id=order.warehouse_id,
        assigned_user_id=order.assigned_user_id,
        status='draft',
        created_by_user_id=current_user.id,
        total_amount=0,
    )
    db.session.add(realization)
    db.session.flush()
    total = Decimal('0')
    source_lines = _order_realization_lines_source(order)
    for idx, source_line in enumerate(source_lines, start=1):
        item = source_line.item
        line_type = 'component'
        inventory_effect = 'ship_from_stock'
        trailer = None
        vin_row = None
        if source_line.line_type == 'TRAILER':
            line_type = 'trailer'
            inventory_effect = 'trailer_unit'
            trailer, vin_row, error = _resolve_realization_trailer_for_line(order, source_line)
            if error:
                raise ValueError(error)
        if item and item.is_internal_bom_item:
            continue
        line_total = source_line.total_price or ((source_line.unit_price or 0) * Decimal(source_line.quantity or 1))
        total += Decimal(line_total or 0)
        db.session.add(SalesRealizationLine(
            realization_id=realization.id,
            line_no=idx,
            line_type=line_type,
            order_line_id=source_line.id,
            trailer_id=trailer.id if trailer else None,
            vin_registry_id=vin_row.id if vin_row else None,
            item_id=source_line.item_id,
            warehouse_id=order.warehouse_id,
            quantity=source_line.quantity or 1,
            unit=item.unit if item else 'шт',
            unit_price=source_line.unit_price,
            total_price=line_total,
            inventory_effect=inventory_effect,
            article_snapshot=source_line.article_snapshot or (item.article if item else None),
            product_name_snapshot=source_line.product_name_snapshot or (item.name if item else None),
            vin_full=(vin_row.vin_full if vin_row else (trailer.vin if trailer else None)),
        ))
    realization.total_amount = total
    return realization


@main_bp.route('/realizations')
@role_required('manager', 'director')
def sales_realizations_list():
    query = SalesRealization.query
    if current_user.is_manager:
        query = query.filter(SalesRealization.assigned_user_id == current_user.id)
    realizations = query.order_by(SalesRealization.realization_date.desc(), SalesRealization.id.desc()).limit(300).all()
    return render_template('sales_realizations_list.html', realizations=realizations)


@main_bp.route('/realizations/<int:realization_id>')
@role_required('manager', 'director')
def sales_realization_detail(realization_id):
    realization = SalesRealization.query.get_or_404(realization_id)
    if current_user.is_manager and realization.assigned_user_id != current_user.id:
        abort(403)
    return render_template('sales_realization_detail.html', realization=realization)


@main_bp.route('/realizations/<int:realization_id>/edit', methods=['GET', 'POST'])
@role_required('manager', 'director')
def sales_realization_edit(realization_id):
    realization = SalesRealization.query.get_or_404(realization_id)
    if current_user.is_manager and realization.assigned_user_id != current_user.id:
        abort(403)
    if realization.status not in ('draft', 'posted'):
        flash('Редактировать можно только черновик или проведённую реализацию.', 'danger')
        return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))

    form = FlaskForm()
    if form.validate_on_submit():
        old_total = realization.total_amount
        realization_date_raw = (request.form.get('realization_date') or '').strip()
        if realization_date_raw:
            try:
                realization.realization_date = datetime.strptime(realization_date_raw, '%Y-%m-%d').date()
            except ValueError:
                flash('Дата реализации указана неверно.', 'danger')
                return render_template('sales_realization_form.html', form=form, realization=realization)

        total = Decimal('0')
        for line in realization.lines:
            qty = line.quantity or 1
            if line.inventory_effect != 'trailer_unit':
                qty_raw = (request.form.get(f'line_{line.id}_quantity') or '').strip()
                if qty_raw:
                    try:
                        qty = Decimal(qty_raw.replace(',', '.'))
                    except Exception:
                        flash(f'Количество в строке #{line.line_no} указано неверно.', 'danger')
                        return render_template('sales_realization_form.html', form=form, realization=realization)
                    if qty <= 0:
                        flash(f'Количество в строке #{line.line_no} должно быть больше 0.', 'danger')
                        return render_template('sales_realization_form.html', form=form, realization=realization)
                    line.quantity = qty

            price_raw = (request.form.get(f'line_{line.id}_unit_price') or '').strip()
            try:
                unit_price = Decimal(price_raw.replace(',', '.')) if price_raw else Decimal('0')
            except Exception:
                flash(f'Цена в строке #{line.line_no} указана неверно.', 'danger')
                return render_template('sales_realization_form.html', form=form, realization=realization)
            line.unit_price = unit_price
            line.total_price = unit_price * Decimal(line.quantity or 1)
            line.comment = (request.form.get(f'line_{line.id}_comment') or '').strip() or None
            total += Decimal(line.total_price or 0)

        realization.total_amount = total
        realization.updated_at = datetime.utcnow()
        if realization.order:
            add_order_event(
                realization.order,
                'comment_added',
                old_value=str(old_total or ''),
                new_value=str(realization.total_amount or ''),
                comment=f'Отредактирована реализация {realization.number or realization.id}',
            )
        db.session.commit()
        flash('Реализация обновлена. VIN и прицепы не изменялись.', 'success')
        return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))

    return render_template('sales_realization_form.html', form=form, realization=realization)


@main_bp.route('/orders/<int:order_id>/realizations/new', methods=['POST'])
@login_required
def order_realization_create(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.status == 'cancelled' or order.is_shipped:
        flash('Реализацию нельзя создать по отменённому или уже отгруженному заказу.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    try:
        realization = _build_sales_realization_from_order(order)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    _finish_idempotency(idem_key, 'SalesRealization', realization.id)
    add_order_event(order, 'realization_created', new_value=realization.number, comment='Создана реализация товаров и услуг')
    db.session.commit()
    flash('Реализация создана в черновике.', 'success')
    return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))


@main_bp.route('/realizations/<int:realization_id>/delete', methods=['POST'])
@role_required('manager', 'director')
def sales_realization_delete(realization_id):
    realization = SalesRealization.query.get_or_404(realization_id)
    if current_user.is_manager and realization.assigned_user_id != current_user.id:
        abort(403)
    if realization.status == 'posted' and not (current_user.is_admin or current_user.is_director):
        flash('Проведённую реализацию может удалить только директор или админ. Менеджер может редактировать проведённую реализацию.', 'danger')
        return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))
    if realization.status not in ('draft', 'posted'):
        flash('Удалить можно только черновик или проведённую реализацию.', 'danger')
        return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))
    order = realization.order
    number = realization.number or realization.id
    was_posted = realization.status == 'posted'
    if was_posted:
        _revert_realization_shipment_effect(realization)
    db.session.delete(realization)
    if order:
        _refresh_order_realization_status(order)
        add_order_event(
            order,
            'realization_cancelled',
            old_value=number,
            comment='Удалена проведённая реализация с откатом отгрузки' if was_posted else 'Удалён черновик реализации',
        )
    db.session.commit()
    flash('Реализация удалена.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id) if order else url_for('main.sales_realizations_list'))


@main_bp.route('/realizations/<int:realization_id>/post', methods=['POST'])
@role_required('manager', 'director')
def sales_realization_post(realization_id):
    realization = SalesRealization.query.get_or_404(realization_id)
    if current_user.is_manager and realization.assigned_user_id != current_user.id:
        abort(403)
    if realization.status != 'draft':
        flash('Провести можно только черновик реализации.', 'warning')
        return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))
    for line in realization.lines:
        if line.inventory_effect == 'trailer_unit' and _posted_realization_for_trailer(line.trailer_id):
            flash(f'VIN {line.vin_full or line.trailer_id} уже реализован.', 'danger')
            return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))
    realization.status = 'posted'
    realization.posted_at = datetime.utcnow()
    realization.posted_by_user_id = current_user.id
    order = realization.order
    _apply_realization_shipment_effect(realization)
    if order:
        _refresh_order_realization_status(order)
        add_order_event(order, 'realization_posted', new_value=realization.number, comment='Проведена реализация товаров и услуг; заказ закрыт как отгрузка')
    db.session.commit()
    flash('Реализация проведена, заказ закрыт как отгруженный.', 'success')
    return redirect(url_for('main.sales_realization_detail', realization_id=realization.id))


@main_bp.route('/orders/<int:order_id>/contract/new', methods=['POST'])
@login_required
def order_contract_create(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.status == 'cancelled' or order.documents_issued or order.is_shipped:
        flash('Договор можно создать только до выдачи документов и физической отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    effective_vin = get_order_effective_vin(order)
    if not effective_vin:
        flash('Для договора нужен конкретный прицеп/VIN или зарезервированный VIN.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    existing = SalesContract.query.filter_by(order_id=order.id).first()
    if existing:
        flash('Договор по этому заказу уже создан.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    trailer_contract = SalesContract.query.filter_by(trailer_id=order.trailer_id).first() if order.trailer_id else None
    if trailer_contract:
        stale_contract_relinked = False
        if trailer_contract.order_id and trailer_contract.order_id != order.id:
            linked_order = CustomerOrder.query.get(trailer_contract.order_id)
            linked_order_still_uses_trailer = bool(linked_order and linked_order.trailer_id == order.trailer_id)
            if linked_order_still_uses_trailer and _order_has_issued_documents_or_shipment(linked_order):
                linked_number = linked_order.order_number if linked_order else trailer_contract.order_id
                flash(f'На этот прицеп уже существует договор по заказу {linked_number}; по нему уже есть документы или отгрузка.', 'danger')
                return redirect(url_for('main.order_detail', order_id=order.id))
            if linked_order_still_uses_trailer:
                _cancel_order_active_reservations(linked_order, order.trailer_id, f'Прицеп перенесён в заказ {order.order_number}')
                linked_order.trailer_id = None
                linked_order.source_warehouse_id = None
                linked_order.fulfillment_source = 'later'
                _refresh_order_status(linked_order)
                add_order_event(
                    linked_order,
                    'reservation_cancelled',
                    old_value=order.trailer.vin if order.trailer else order.trailer_id,
                    new_value=order.order_number,
                    comment=f'Прицеп снят со старого заказа при создании нового договора по заказу {order.order_number}',
                )
            elif linked_order and linked_order.trailer_id:
                replacement_trailer_contract = (
                    SalesContract.query
                    .filter(SalesContract.trailer_id == linked_order.trailer_id, SalesContract.id != trailer_contract.id)
                    .first()
                )
                if replacement_trailer_contract:
                    replacement_number = linked_order.order_number if linked_order else trailer_contract.order_id
                    flash(f'Договор заказа {replacement_number} нельзя перенести на его текущий прицеп: на нём уже есть другой договор.', 'danger')
                    return redirect(url_for('main.order_detail', order_id=order.id))
                trailer_contract.trailer_id = linked_order.trailer_id
                trailer_contract.customer_id = linked_order.customer_id
                trailer_contract.price = linked_order.price
                trailer_contract.is_shipped = bool(linked_order.is_shipped)
                for row in VinRegistry.query.filter_by(trailer_id=linked_order.trailer_id).all():
                    row.customer_order_id = linked_order.id
                    row.sales_contract_id = trailer_contract.id
                stale_contract_relinked = True
                add_order_event(
                    linked_order,
                    'trailer_assigned',
                    old_value=order.trailer.vin if order.trailer else order.trailer_id,
                    new_value=linked_order.trailer.vin if linked_order.trailer else linked_order.trailer_id,
                    comment=f'Договор перепривязан к текущему прицепу заказа; старый прицеп освобождён для заказа {order.order_number}',
                )
            elif linked_order:
                add_order_event(
                    linked_order,
                    'reservation_cancelled',
                    old_value=order.trailer.vin if order.trailer else order.trailer_id,
                    new_value=order.order_number,
                    comment=f'Устаревшая связь договора с прицепом снята при создании договора по заказу {order.order_number}',
                )
            for row in VinRegistry.query.filter_by(trailer_id=order.trailer_id).all():
                if row.customer_order_id == trailer_contract.order_id:
                    row.customer_order_id = None
                if row.sales_contract_id == trailer_contract.id:
                    row.sales_contract_id = None
                if row.docs_issued_order_id == trailer_contract.order_id:
                    row.docs_issued_order_id = None
                    row.docs_issued_at = None
            if not stale_contract_relinked:
                trailer_contract.trailer_id = None
        if trailer_contract.order_id == order.id:
            flash('Договор по этому заказу уже создан.', 'warning')
            return redirect(url_for('main.order_detail', order_id=order.id))
        trailer_contract.trailer_id = None
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    contract = SalesContract(
        contract_number=get_next_contract_number(),
        contract_date=date.today(),
        customer_id=order.customer_id,
        trailer_id=order.trailer_id or None,
        order_id=order.id,
        price=order.price,
        payment_method='order',
        source='customer_order',
        is_paid=(order.remaining_amount == 0 and float(order.price or 0) > 0),
        is_shipped=bool(order.is_shipped),
    )
    db.session.add(contract)
    db.session.flush()
    _create_contract_lines_from_order(contract, order)
    if order.trailer_id:
        for row in VinRegistry.query.filter_by(trailer_id=order.trailer_id).all():
            row.sales_contract_id = contract.id
            if not row.customer_order_id:
                row.customer_order_id = order.id
    _finish_idempotency(idem_key, 'SalesContract', contract.id)
    order.document_status = 'contract_ready'
    add_order_event(order, 'contract_ready', new_value=contract.contract_number or contract.id)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if SalesContract.query.filter_by(order_id=order.id).first():
            flash('Договор по этому заказу уже создан.', 'warning')
        else:
            flash('Не удалось создать договор: конфликт номера, прицепа или заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    flash('Договор создан из заказа. Прицеп не переведен в SOLD этим действием.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/ship', methods=['POST'])
@login_required
def order_ship(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    flash('Отдельная отгрузка больше не используется: проведение реализации закрывает отгрузку по заказу.', 'warning')
    return redirect(url_for('main.order_detail', order_id=order.id))
    if order.is_shipped:
        flash('Заказ уже физически отгружен.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not can_ship_order(order) or not order.trailer_id or order.status == 'cancelled':
        abort(403)
    if not order.trailer:
        abort(400)
    contract = SalesContract.query.filter_by(order_id=order.id).first()
    if not order.documents_issued or not contract:
        flash('Сначала выдайте документы и зафиксируйте юридическую продажу.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.trailer.status != 'SOLD':
        flash('Юридическая продажа не зафиксирована: прицеп ещё не SOLD.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _posted_realization_for_trailer(order.trailer_id):
        flash('Нельзя отгрузить: реализация товаров и услуг не проведена.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.status not in ('sold_not_shipped', 'ready_to_ship', 'arrived'):
        flash('Заказ пока не находится в статусе, допустимом для физической отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not current_user.is_admin and order.trailer.warehouse_id != order.warehouse_id:
        flash('Прицеп ещё не на складе выдачи.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    order.is_shipped = True
    order.shipped_at = datetime.utcnow()
    order.status = 'shipped'
    if order.trailer:
        order.trailer.lifecycle_status = 'customer_shipped'
    contract.is_shipped = True
    for reservation in order.reservations.filter_by(status='ACTIVE').all():
        reservation.status = 'CLOSED'
    shipment = StockMovement(movement_type='customer_shipment', status='arrived', order_id=order.id, trailer_id=order.trailer_id, from_warehouse_id=order.warehouse_id, departure_date=date.today(), arrival_date=date.today(), received_at=datetime.utcnow(), note=f'Отгрузка клиенту по заказу №{order.order_number}')
    db.session.add(shipment)
    db.session.flush()
    _finish_idempotency(idem_key, 'StockMovement', shipment.id)
    add_order_event(order, 'shipped', new_value=order.trailer.vin if order.trailer else order.trailer_id)
    db.session.commit()
    flash('Прицеп физически отгружен клиенту.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/lines/<int:line_id>/ship', methods=['POST'])
@login_required
def order_line_ship(order_id, line_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    flash('Отдельная отгрузка позиции больше не используется: проведение реализации закрывает отгрузку.', 'warning')
    return redirect(url_for('main.order_detail', order_id=order.id))
    line = CustomerOrderLine.query.filter_by(id=line_id, order_id=order.id).first_or_404()
    if not can_ship_order(order) or order.status == 'cancelled':
        abort(403)
    contract = SalesContract.query.filter_by(order_id=order.id).first()
    if not order.documents_issued or not contract:
        flash('Сначала выдайте документы и зафиксируйте юридическую продажу.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    can_ship_line, ship_message = _order_line_can_ship(line)
    if not can_ship_line:
        flash(ship_message or 'Позицию пока нельзя отгрузить.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    trailers = _trailers_for_order_line(line)
    line.status = 'shipped'
    for reservation in line.reservations:
        if reservation.status == 'ACTIVE':
            reservation.status = 'CLOSED'
    first_movement_id = None
    for trailer in trailers:
        trailer.lifecycle_status = 'customer_shipped'
        shipment = StockMovement(
            movement_type='customer_shipment',
            status='arrived',
            order_id=order.id,
            order_line_id=line.id,
            trailer_id=trailer.id,
            from_warehouse_id=order.warehouse_id,
            departure_date=date.today(),
            arrival_date=date.today(),
            received_at=datetime.utcnow(),
            note=f'Отгрузка клиенту по заказу №{order.order_number}, позиция #{line.line_no}',
        )
        db.session.add(shipment)
        db.session.flush()
        if first_movement_id is None:
            first_movement_id = shipment.id
    _refresh_order_shipment_state(order)
    if first_movement_id:
        _finish_idempotency(idem_key, 'StockMovement', first_movement_id)
    else:
        _finish_idempotency(idem_key, 'CustomerOrderLine', line.id)
    add_order_event(
        order,
        'shipped',
        new_value=', '.join(trailer.vin for trailer in trailers if trailer.vin) or f'позиция #{line.line_no}',
        comment=f'Физическая отгрузка позиции #{line.line_no}',
    )
    db.session.commit()
    flash('Позиция заказа физически отгружена клиенту.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/reserve-trailer', methods=['POST'])
@login_required
def order_reserve_trailer(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.is_shipped or order.status == 'cancelled':
        abort(400)
    trailer = Trailer.query.get_or_404(request.form.get('trailer_id', type=int))
    if current_user.is_manager and trailer.warehouse_id != current_user.warehouse_id:
        abort(403)
    if trailer.item_id != order.item_id:
        flash('Выбранный VIN не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if trailer.warehouse_id != order.warehouse_id:
        flash('Для резерва из наличия VIN должен быть на складе продажи.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _trailer_available_for_sale(trailer, exclude_order_id=order.id):
        flash('Этот прицеп уже недоступен для резерва.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    old_trailer_id = order.trailer_id
    old_trailer = Trailer.query.get(old_trailer_id) if old_trailer_id else None
    order.fulfillment_source = 'stock'
    ok, message = _sync_order_trailer_links(order, old_trailer, trailer, 'STOCK')
    if not ok:
        flash(message, 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    db.session.flush()
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    _refresh_order_status(order)
    add_order_event(order, 'trailer_reserved', old_value=old_trailer_id, new_value=trailer.vin)
    db.session.commit()
    flash('Прицеп зарезервирован', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/request-transfer', methods=['POST'])
@login_required
def order_request_transfer(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.is_shipped or order.status == 'cancelled':
        abort(400)
    trailer = Trailer.query.get_or_404(request.form.get('trailer_id', type=int))
    order_line_id = request.form.get('order_line_id', type=int)
    order_line = None
    if order_line_id:
        order_line = CustomerOrderLine.query.filter_by(id=order_line_id, order_id=order.id).first_or_404()
    target_item_id = order_line.item_id if order_line else order.item_id
    if trailer.item_id != target_item_id:
        flash('Выбранный VIN не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    reserved_for_this_order = trailer.status == 'RESERVED' and (order.trailer_id == trailer.id or (order_line and order_line.trailer_id == trailer.id))
    if trailer.status not in ('IN_STOCK', 'RESERVED') or (trailer.status == 'RESERVED' and not reserved_for_this_order) or trailer.warehouse_id == order.warehouse_id:
        flash('Для запроса перемещения нужен свободный или уже зарезервированный под эту сделку прицеп на другом складе.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not reserved_for_this_order and not _trailer_available_for_sale(trailer, exclude_order_id=order.id):
        flash('Этот прицеп уже зарезервирован.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    old_trailer = Trailer.query.get(order.trailer_id) if order.trailer_id else None
    order.status = 'waiting_transfer'
    order.source_warehouse_id = trailer.warehouse_id
    if order_line:
        order_line.trailer_id = trailer.id
        order_line.fulfillment_source = 'other_warehouse'
        order_line.status = 'waiting_transfer'
        if not any(row.status == 'ACTIVE' and row.trailer_id == trailer.id for row in order_line.reservations):
            db.session.add(Reservation(
                order_id=order.id,
                order_line_id=order_line.id,
                trailer_id=trailer.id,
                item_id=order_line.item_id,
                status='ACTIVE',
                source_type='TRANSFER',
                priority=10,
            ))
        if trailer.status == 'IN_STOCK':
            trailer.status = 'RESERVED'
            trailer.lifecycle_status = 'reserved'
        if order.lines.count() == 1 and order.trailer_id != trailer.id:
            order.trailer_id = trailer.id
            order.fulfillment_source = 'other_warehouse'
        elif not order.trailer_id:
            order.trailer_id = trailer.id
    else:
        order.fulfillment_source = 'other_warehouse'
        ok, message = _sync_order_trailer_links(order, old_trailer, trailer, 'TRANSFER')
        if not ok:
            flash(message, 'danger')
            return redirect(url_for('main.order_detail', order_id=order.id))
        order_line = _primary_order_line(order)
    if not _active_movement_for_trailer(trailer.id):
        movement = StockMovement(
            from_warehouse_id=trailer.warehouse_id,
            to_warehouse_id=order.warehouse_id,
            trailer_id=trailer.id,
            item_id=trailer.item_id,
            order_id=order.id,
            order_line_id=order_line.id if order_line else None,
            movement_type='warehouse_transfer',
            status='draft',
            note='Перемещение под сделку',
        )
        db.session.add(movement)
    db.session.flush()
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    add_order_event(order, 'transfer_requested', new_value=trailer.vin, comment='Прицеп зарезервирован на другом складе. Нужна отправка.')
    db.session.commit()
    flash('Прицеп зарезервирован на другом складе. Нужна отправка.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/attach-transit', methods=['POST'])
@login_required
def order_attach_transit(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.trailer_id or order.is_shipped or order.status == 'cancelled':
        flash('Нельзя закрепить прицеп в пути: у заказа уже есть VIN, заказ отгружен или отменён.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    movement_id = request.form.get('movement_id', type=int)
    movement = StockMovement.query.get_or_404(movement_id)
    trailer = movement.trailer
    if not trailer or movement.status not in ('sent', 'in_transit'):
        flash('Можно закрепить только активное входящее перемещение.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if movement.order_id and movement.order_id != order.id:
        flash('Это перемещение уже закреплено за другим заказом.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if trailer.item_id != order.item_id:
        flash('Прицеп в пути не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.warehouse_id and movement.to_warehouse_id != order.warehouse_id:
        flash('Прицеп едет не на склад этого заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _active_reservation_for_trailer(trailer.id, exclude_order_id=order.id):
        flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    movement.order_id = order.id
    order.trailer_id = trailer.id
    order.fulfillment_source = 'transit'
    old_status = order.status
    order.status = 'in_transit'
    db.session.add(Reservation(
        order_id=order.id,
        trailer_id=trailer.id,
        item_id=order.item_id,
        status='ACTIVE',
        source_type='TRANSIT',
        priority=10,
        note='Прицеп в пути закреплён из карточки заказа',
    ))
    add_order_event(order, 'trailer_assigned', old_value=old_status, new_value=trailer.vin, comment='Закреплён прицеп в пути')
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    flash('Прицеп в пути закреплён за заказом.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/attach-produced-unit/<int:unit_id>', methods=['POST'])
@login_required
def order_attach_produced_unit(order_id, unit_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    unit = ProducedUnit.query.get_or_404(unit_id)
    if not _order_is_open_for_attachment(order):
        flash('Нельзя закрепить единицу: заказ отменён или уже отгружен.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.trailer_id:
        flash('У заказа уже есть конкретный VIN.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if unit.status != 'produced_no_vin' or unit.trailer_id:
        flash('Можно закрепить только выпущенную единицу без VIN.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    other_order = _active_order_for_produced_unit(unit, exclude_order_id=order.id)
    if other_order:
        flash(f'Эта единица уже закреплена за заказом {other_order.order_number}.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if unit.order_id == order.id:
        flash('Эта выпущенная единица уже закреплена за этим заказом.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _ensure_item_matches_order(unit.item_id, order):
        flash('Выпущенная единица не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _ensure_target_matches_order_warehouse(unit.target_warehouse_id, order):
        flash('Выпущенная единица предназначена для другого склада.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    unit.order_id = order.id
    order.fulfillment_source = 'production'
    if order.status not in ('sold_not_shipped',):
        order.status = 'produced_waiting_vin'
    add_order_event(order, 'trailer_assigned', new_value=f'ProducedUnit #{unit.id}', comment='Выпущенная единица без VIN закреплена за заказом')
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    flash('Выпущенная единица без VIN закреплена за заказом.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/attach-ready-trailer/<int:trailer_id>', methods=['POST'])
@login_required
def order_attach_ready_trailer(order_id, trailer_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    trailer = Trailer.query.get_or_404(trailer_id)
    if not _order_is_open_for_attachment(order):
        flash('Нельзя закрепить VIN: заказ отменён или уже отгружен.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.trailer_id and order.trailer_id != trailer.id:
        flash('У заказа уже закреплён другой VIN.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.trailer_id == trailer.id:
        flash('Этот VIN уже закреплён за этим заказом.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not trailer.vin:
        flash('У прицепа нет VIN.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _trailer_is_customer_shipped(trailer):
        flash('Физически отгруженный прицеп нельзя закрепить.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    production_warehouse = _default_production_warehouse()
    if not production_warehouse or trailer.warehouse_id != production_warehouse.id:
        flash('Закрепить можно только готовый VIN на производственном складе.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _ensure_item_matches_order(trailer.item_id, order):
        flash('VIN не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _active_movement_for_trailer(trailer.id):
        flash('Этот прицеп уже находится в активном перемещении.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    other_order = _active_order_for_trailer(trailer.id, exclude_order_id=order.id)
    if other_order:
        flash(f'Этот VIN уже закреплён за заказом {other_order.order_number}.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _active_reservation_for_trailer(trailer.id, exclude_order_id=order.id):
        flash('Этот VIN уже зарезервирован под другой активный заказ.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if trailer.status == 'SOLD' and order.trailer_id != trailer.id:
        flash('Проданный VIN нельзя закрепить за другим заказом.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    old_trailer_id = order.trailer_id
    order.trailer_id = trailer.id
    order.fulfillment_source = 'production'
    unit = getattr(trailer, 'produced_unit', None)
    if unit is not None and not isinstance(unit, ProducedUnit):
        unit = unit[0] if len(unit) else None
    if unit:
        unit.order_id = order.id
    _set_trailer_status_for_order(trailer, order)
    _ensure_order_reservation(order, trailer, 'PRODUCTION')
    movement = None
    if order.warehouse_id and trailer.warehouse_id != order.warehouse_id:
        movement = StockMovement.query.filter(
            StockMovement.trailer_id == trailer.id,
            StockMovement.order_id == order.id,
            StockMovement.status.in_(['sent', 'in_transit']),
        ).first()
        if not movement:
            movement = StockMovement(
                movement_type='warehouse_transfer',
                status='in_transit',
                from_warehouse_id=trailer.warehouse_id,
                to_warehouse_id=order.warehouse_id,
                trailer_id=trailer.id,
                item_id=trailer.item_id,
                order_id=order.id,
                departure_date=date.today(),
                note=f'Перемещение под заказ №{order.order_number}',
            )
            db.session.add(movement)
        if trailer.status != 'SOLD':
            trailer.status = 'IN_TRANSIT'
        trailer.lifecycle_status = 'in_transit'
        if not order.documents_issued:
            order.status = 'in_transit'
    else:
        if order.warehouse_id:
            trailer.lifecycle_status = 'ready_production_warehouse'
            _refresh_order_status(order)
        else:
            flash('Склад выдачи / назначения не задан. Сначала укажите склад выдачи в заказе.', 'warning')
    add_order_event(order, 'trailer_assigned', old_value=old_trailer_id, new_value=trailer.vin, comment='Готовый VIN на производственном складе закреплён за заказом')
    if movement:
        add_order_event(order, 'transfer_started', new_value=trailer.vin, comment='Автоматически создано перемещение под заказ')
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    flash('Готовый прицеп с VIN закреплен за заказом.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/create-production-need', methods=['POST'])
@login_required
def order_create_production_need(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    can_request, message = order_can_request_production(order)
    if not can_request:
        flash(message, 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    order_line = _sync_primary_order_line(order, _order_snapshot(order))
    db.session.flush()
    need = SupplyNeed(order_id=order.id, order_line_id=order_line.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity or 1, required_by=order.expected_date, status='NEW', need_type='CUSTOMER_ORDER', priority=10, note='Потребность создана из карточки заказа')
    _apply_snapshot(need, {
        'article_snapshot': order.article_snapshot,
        'product_name_snapshot': order.product_name_snapshot,
        'config_snapshot_json': order.config_snapshot_json,
        'calculated_price': order.calculated_price,
        'price_breakdown_json': order.price_breakdown_json,
        'overall_dimensions_text': order.overall_dimensions_text,
        'inner_dimensions_text': order.inner_dimensions_text,
        'otss_number': order.otss_number,
        'otss_type': order.otss_type,
        'otss_modification': order.otss_modification,
        'vin_modification_code': order.vin_modification_code,
    })
    db.session.add(need)
    db.session.flush()
    _finish_idempotency(idem_key, 'SupplyNeed', need.id)
    order.fulfillment_source = 'production'
    old_status = order.status
    order.status = 'waiting_production'
    add_order_event(order, 'production_need_created', old_value=old_status, new_value='waiting_production')
    db.session.commit()
    flash('Потребность в производство создана', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


def _mark_order_document(order: CustomerOrder, status: str, event_type: str, message: str):
    _ensure_can_manage_order(order)
    if status == 'documents_issued':
        abort(400)
    if order.documents_issued or order.is_shipped:
        flash('Документальный статус уже закрыт юридической продажей или физической отгрузкой.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.document_status == status:
        flash('Это действие уже отмечено.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    old_status = order.document_status
    order.document_status = status
    add_order_event(order, event_type, old_value=old_status, new_value=status)
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    flash(message, 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/mark-invoice-sent', methods=['POST'])
@login_required
def order_mark_invoice_sent(order_id):
    return _mark_order_document(CustomerOrder.query.get_or_404(order_id), 'invoice_sent', 'invoice_sent', 'Счёт отмечен как отправленный')


@main_bp.route('/orders/<int:order_id>/mark-contract-ready', methods=['POST'])
@login_required
def order_mark_contract_ready(order_id):
    return _mark_order_document(CustomerOrder.query.get_or_404(order_id), 'contract_ready', 'contract_ready', 'Договор отмечен как готовый')


@main_bp.route('/orders/<int:order_id>/mark-documents-ready', methods=['POST'])
@login_required
def order_mark_documents_ready(order_id):
    return _mark_order_document(CustomerOrder.query.get_or_404(order_id), 'documents_ready', 'documents_ready', 'Документы отмечены как готовые')


@main_bp.route('/orders/<int:order_id>/reserve-vin', methods=['POST'])
@login_required
def order_reserve_vin(order_id):
    if not (current_user.is_admin or current_user.is_director):
        abort(403)
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_access_order(order)
    if order.is_shipped or order.documents_issued or order.status in ('cancelled', 'canceled', 'closed', 'done'):
        flash('Нельзя зарезервировать VIN по закрытому или отгруженному заказу.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _active_vin_registry_for_order(order.id):
        flash('По заказу уже есть активный VIN.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    modification = _order_vin_modification_code(order)
    if not modification:
        flash('В заказе нет VIN-модификации. Сначала сохраните конфигурацию.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    year_code = (request.form.get('year_code') or '').strip().upper()
    if len(year_code) != 1:
        flash('Укажите код года выпуска одним символом.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    row = _free_vin_row_for_reservation()
    old_status = row.status
    row.prefix = 'MX4'
    row.vin_modification_code = modification
    row.year_code = year_code
    row.vin_full = f'{row.prefix}{modification}{year_code}{row.serial7}'
    row.status = 'reserved'
    row.customer_order_id = order.id
    order_line = _primary_order_line(order)
    row.order_line_id = order_line.id if order_line else None
    active_need = _active_supply_need_for_order(order.id)
    row.supply_need_id = active_need.id if active_need else (order.supply_needs.order_by(SupplyNeed.created_at.desc()).first().id if order.supply_needs.count() else None)
    row.reserved_by_user_id = current_user.id
    row.reserved_at = datetime.utcnow()
    row.source = row.source or 'order_reservation'
    row.comment = (request.form.get('comment') or '').strip() or row.comment
    order.reserved_vin_registry_id = row.id
    _add_vin_event(row, 'reserved', old_status, row.status, comment=row.comment)
    add_order_event(order, 'trailer_reserved', new_value=row.vin_full, comment='Зарезервирован VIN под заказ')
    db.session.commit()
    flash(f'VIN {row.vin_full} зарезервирован под заказ.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/cancel-vin-reservation', methods=['POST'])
@login_required
def order_cancel_vin_reservation(order_id):
    if not (current_user.is_admin or current_user.is_director):
        abort(403)
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_cancel_order_reservation(order)
    row = _active_vin_registry_for_order(order.id)
    if not row:
        flash('По заказу нет активного VIN-резерва.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _order_has_issued_documents_or_shipment(order) or row.docs_issued_at:
        flash('Нельзя отменить резерв: по заказу уже выданы документы или выполнена отгрузка.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    reason = (request.form.get('comment') or request.form.get('reason') or '').strip() or 'Отмена резерва VIN из карточки заказа'
    if row.status == 'reserved' and not row.trailer_id:
        _free_reserved_vin_row(row, order, reason)
        add_order_event(order, 'reservation_cancelled', old_value=row.serial7, new_value='VIN reserve cancelled', comment=reason)
        db.session.commit()
        flash('Резерв VIN отменён. serial7 возвращён в свободные.', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if row.status in ('assigned', 'confirmed'):
        ok, message = _release_order_trailer_and_vin(order, reason)
        if ok:
            db.session.commit()
            if row.trailer_id:
                flash('Клиентский резерв снят. VIN остался на физическом прицепе, прицеп вернулся в наличие.', 'success')
            else:
                flash('Резерв VIN отменён. Выпущенная единица снова ожидает присвоения VIN.', 'success')
        else:
            flash(message, 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    flash('Этот VIN нельзя отменить обычной кнопкой.', 'danger')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/cancel-trailer-reservation', methods=['POST'])
@login_required
def order_cancel_trailer_reservation(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_cancel_order_reservation(order)
    reason = (request.form.get('reason') or request.form.get('cancel_reason') or '').strip()
    if not reason:
        flash('Укажите причину отмены резерва прицепа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not order.trailer_id:
        flash('В заказе нет выбранного прицепа.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    ok, message = _release_order_trailer_and_vin(order, reason)
    if not ok:
        flash(message, 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    db.session.commit()
    flash(message, 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))

def _vin_product_category_code(row: VinRegistry) -> str:
    if row.trailer and row.trailer.item and row.trailer.item.product_category:
        return row.trailer.item.product_category.code
    if row.order_line and row.order_line.item and row.order_line.item.product_category:
        return row.order_line.item.product_category.code
    if row.customer_order and row.customer_order.item and row.customer_order.item.product_category:
        return row.customer_order.item.product_category.code
    return 'unknown'


def _vin_product_category_name(row: VinRegistry) -> str:
    code = _vin_product_category_code(row)
    category = ProductCategory.query.filter_by(code=code).first() if code and code != 'unknown' else None
    return category.name if category else 'Не определено'


def _vin_registry_order(row: VinRegistry) -> CustomerOrder | None:
    if row.customer_order:
        return row.customer_order
    if row.order_line:
        return row.order_line.order
    return None


@main_bp.route('/logistics/vin-registry', methods=['GET', 'POST'])
@role_required('logistics', 'director', 'manager')
def vin_registry_list():
    if request.method == 'POST' and current_user.is_manager:
        abort(403)
    if request.method == 'POST':
        raw = (request.form.get('vin_input') or '').strip()
        source = (request.form.get('source') or 'manual').strip() or 'manual'
        errors = []
        created = 0
        for line in [part.strip() for part in raw.replace(',', '\n').splitlines() if part.strip()]:
            if len(line) == 17:
                parsed, error = _parse_vin_full(line)
                if error:
                    errors.append(f'{line}: {error}')
                    continue
                if VinRegistry.query.filter(or_(VinRegistry.serial7 == parsed['serial7'], VinRegistry.vin_full == parsed['vin_full'])).first():
                    errors.append(f'{line}: VIN или serial7 уже есть в реестре.')
                    continue
                row = VinRegistry(status='free', source=source, **parsed)
            else:
                serial, error = _normalize_serial7(line)
                if error:
                    errors.append(f'{line}: {error}')
                    continue
                if VinRegistry.query.filter_by(serial7=serial).first():
                    errors.append(f'{line}: serial7 уже есть в реестре.')
                    continue
                row = VinRegistry(serial7=serial, status='free', source=source, prefix='MX4')
            db.session.add(row)
            db.session.flush()
            _add_vin_event(row, 'uploaded', None, row.status, comment='Загрузка VIN/serial7')
            created += 1
        db.session.commit()
        if created:
            flash(f'Добавлено VIN/serial7: {created}.', 'success')
        for error in errors[:10]:
            flash(error, 'warning')
        if len(errors) > 10:
            flash(f'Ещё ошибок: {len(errors) - 10}.', 'warning')
        return redirect(url_for('main.vin_registry_list'))

    status = (request.args.get('status') or '').strip()
    year = (request.args.get('year') or '').strip().upper()
    modification = (request.args.get('modification') or '').strip()
    serial = (request.args.get('serial7') or '').strip()
    docs = (request.args.get('docs') or '').strip()
    q = (request.args.get('q') or '').strip()
    category = (request.args.get('category') or ('light_trailer' if current_user.is_manager else 'all')).strip()
    query = VinRegistry.query
    if current_user.is_manager:
        manager_order_ids = CustomerOrder.query.with_entities(CustomerOrder.id).filter(CustomerOrder.assigned_user_id == current_user.id)
        manager_order_line_ids = (
            CustomerOrderLine.query
            .join(CustomerOrder, CustomerOrder.id == CustomerOrderLine.order_id)
            .with_entities(CustomerOrderLine.id)
            .filter(CustomerOrder.assigned_user_id == current_user.id)
        )
        query = query.filter(or_(
            VinRegistry.customer_order_id.in_(manager_order_ids),
            VinRegistry.order_line_id.in_(manager_order_line_ids),
        ))
    if status:
        query = query.filter(VinRegistry.status == status)
    if year:
        query = query.filter(VinRegistry.year_code == year)
    if modification:
        query = query.filter(VinRegistry.vin_modification_code == modification)
    if serial:
        query = query.filter(VinRegistry.serial7.ilike(f'%{serial}%'))
    if docs == 'issued':
        query = query.filter(VinRegistry.docs_issued_at.isnot(None))
    elif docs == 'not_issued':
        query = query.filter(VinRegistry.docs_issued_at.is_(None))
    if q:
        like = f'%{q}%'
        matching_order_line_ids = (
            CustomerOrderLine.query
            .join(CustomerOrder, CustomerOrder.id == CustomerOrderLine.order_id)
            .outerjoin(Customer, Customer.id == CustomerOrder.customer_id)
            .with_entities(CustomerOrderLine.id)
            .filter(or_(CustomerOrder.order_number.ilike(like), Customer.name.ilike(like)))
        )
        query = query.outerjoin(CustomerOrder, CustomerOrder.id == VinRegistry.customer_order_id).outerjoin(Customer, Customer.id == CustomerOrder.customer_id).filter(or_(
            VinRegistry.vin_full.ilike(like),
            VinRegistry.serial7.ilike(like),
            CustomerOrder.order_number.ilike(like),
            Customer.name.ilike(like),
            VinRegistry.order_line_id.in_(matching_order_line_ids),
        ))
    rows = query.order_by(
        VinRegistry.serial7.desc().nullslast(),
        VinRegistry.vin_full.desc().nullslast(),
        VinRegistry.id.desc(),
    ).limit(300).all()
    if category and category != 'all':
        rows = [row for row in rows if _vin_product_category_code(row) == category]
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order, ProductCategory.name).all()
    return render_template('vin_registry_list.html', rows=rows, filters={'status': status, 'year': year, 'modification': modification, 'serial7': serial, 'docs': docs, 'q': q, 'category': category}, categories=categories, vin_category_code=_vin_product_category_code, vin_category_name=_vin_product_category_name)


@main_bp.route('/logistics/vin-registry/<int:vin_id>')
@role_required('logistics', 'director', 'manager')
def vin_registry_detail(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if current_user.is_manager:
        order = _vin_registry_order(row)
        if not order or order.assigned_user_id != current_user.id:
            abort(403)
    orders = CustomerOrder.query.filter(
        CustomerOrder.status.notin_(['cancelled', 'canceled', 'closed', 'done', 'shipped']),
        CustomerOrder.documents_issued == False,
        CustomerOrder.is_shipped == False,
    ).order_by(CustomerOrder.created_at.desc()).limit(100).all()
    order_line_options = []
    for order in orders:
        lines = order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all()
        if not lines:
            continue
        for line in lines:
            reserved_count = _active_vin_registry_count_for_order_line(line.id)
            capacity_left = max((line.quantity or 1) - reserved_count, 0)
            if capacity_left <= 0:
                continue
            order_line_options.append({'order': order, 'line': line, 'reserved_count': reserved_count, 'capacity_left': capacity_left})
    trailers = Trailer.query.order_by(Trailer.created_at.desc()).limit(150).all()
    return render_template('vin_registry_detail.html', row=row, orders=orders, order_line_options=order_line_options, trailers=trailers, events=row.events.order_by(VinRegistryEvent.created_at.desc()).all())


@main_bp.route('/logistics/vin-registry/<int:vin_id>/reserve', methods=['POST'])
@role_required('logistics', 'director')
def vin_registry_reserve(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status != 'free':
        flash('Резервировать можно только свободный VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    order_line = CustomerOrderLine.query.get(request.form.get('order_line_id', type=int)) if request.form.get('order_line_id', type=int) else None
    order = order_line.order if order_line else CustomerOrder.query.get(request.form.get('customer_order_id', type=int))
    if not order or order.is_shipped or order.documents_issued or order.status in ('cancelled', 'canceled', 'closed', 'done'):
        flash('Этот заказ нельзя связать с VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if order_line and _order_line_vin_capacity_left(order_line) <= 0:
        flash('По выбранной позиции уже зарезервированы все VIN по количеству.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if not order_line and _active_vin_registry_for_order(order.id):
        flash('У заказа уже есть активный VIN. Для второго VIN выберите конкретную позицию заказа.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    modification = _order_line_vin_modification_code(order_line, order)
    year_code = (request.form.get('year_code') or row.year_code or '').strip().upper()
    if row.vin_full and modification and row.vin_modification_code and row.vin_modification_code != modification:
        flash('VIN-модификация выбранного VIN не соответствует заказу.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if not row.vin_full:
        if not modification or len(year_code) != 1:
            flash('Для serial7 без полного VIN нужен заказ с VIN-модификацией и код года.', 'danger')
            return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
        row.prefix = 'MX4'
        row.vin_modification_code = modification
        row.year_code = year_code
        row.vin_full = f'{row.prefix}{modification}{year_code}{row.serial7}'
    old_status = row.status
    row.status = 'reserved'
    row.customer_order_id = order.id
    row.order_line_id = order_line.id if order_line else None
    active_need = next((need for need in order_line.supply_needs if need.status in ('NEW', 'PLANNED', 'IN_PRODUCTION', 'SENT_TO_PRODUCTION', 'PARTIALLY_DONE')), None) if order_line else _active_supply_need_for_order(order.id)
    row.supply_need_id = active_need.id if active_need else None
    row.reserved_by_user_id = current_user.id
    row.reserved_at = datetime.utcnow()
    row.comment = (request.form.get('comment') or '').strip() or row.comment
    if not order.reserved_vin_registry_id:
        order.reserved_vin_registry_id = row.id
    _add_vin_event(row, 'reserved', old_status, row.status, comment=row.comment)
    db.session.commit()
    flash('VIN зарезервирован под заказ.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/cancel-reservation', methods=['POST'])
@role_required('logistics', 'director')
def vin_registry_cancel_reservation(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status != 'reserved':
        flash('Отменить можно только резерв VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.docs_issued_at:
        flash('Нельзя отменить резерв VIN: по нему уже выданы документы.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.trailer_id:
        flash('Нельзя отменить резерв VIN: VIN уже привязан к физическому прицепу.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    old_status = row.status
    order = row.customer_order
    if order and order.reserved_vin_registry_id == row.id:
        order.reserved_vin_registry_id = None
    row.customer_order_id = None
    row.order_line_id = None
    row.supply_need_id = None
    row.reserved_by_user_id = None
    row.reserved_at = None
    row.status = 'free'
    if not row.trailer_id:
        row.vin_full = None
        row.vin_modification_code = None
        row.year_code = None
    _add_vin_event(row, 'reservation_cancelled', old_status, row.status, comment=(request.form.get('comment') or '').strip() or None)
    db.session.commit()
    flash('Резерв VIN отменён.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/assign', methods=['POST'])
@role_required('logistics', 'director')
def vin_registry_assign(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status not in ('free', 'reserved') or not row.vin_full:
        flash('Привязать можно свободный или зарезервированный полный VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    trailer = Trailer.query.get(request.form.get('trailer_id', type=int))
    if not trailer or VinRegistry.query.filter(VinRegistry.trailer_id == trailer.id, VinRegistry.id != row.id, VinRegistry.status != 'void').first():
        flash('Выбранный прицеп недоступен для привязки VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if trailer.vin and trailer.vin != row.vin_full:
        flash('У прицепа уже есть другой VIN. Привязка разных VIN к одному прицепу запрещена.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if trailer.status == 'SOLD' and not (row.customer_order and row.customer_order.trailer_id == trailer.id):
        flash('Проданный прицеп нельзя привязать к этому VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    expected_item_id = row.order_line.item_id if row.order_line_id and row.order_line else (row.customer_order.item_id if row.customer_order else None)
    if expected_item_id and trailer.item_id != expected_item_id:
        flash('Прицеп не соответствует номенклатуре заказа.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    old_status = row.status
    trailer.vin = row.vin_full
    row.trailer_id = trailer.id
    row.status = 'assigned'
    row.assigned_by_user_id = current_user.id
    row.assigned_at = datetime.utcnow()
    if row.customer_order and not row.customer_order.trailer_id:
        row.customer_order.trailer_id = trailer.id
    _add_vin_event(row, 'assigned', old_status, row.status, comment=(request.form.get('comment') or '').strip() or None)
    db.session.commit()
    flash('VIN привязан к прицепу.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/confirm', methods=['POST'])
@role_required('manager', 'director')
def vin_registry_confirm(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status != 'assigned':
        flash('Подтвердить нанесение можно только после назначения VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if not row.trailer or row.trailer.vin != row.vin_full:
        flash('Нельзя подтвердить VIN: у связанного прицепа другой VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    order = _vin_registry_order(row)
    if current_user.is_manager and (not order or order.assigned_user_id != current_user.id):
        abort(403)
    old_status = row.status
    row.status = 'confirmed'
    row.confirmed_by_user_id = current_user.id
    row.confirmed_at = datetime.utcnow()
    _add_vin_event(row, 'confirmed', old_status, row.status, comment=(request.form.get('comment') or '').strip() or None)
    db.session.commit()
    flash('Нанесение VIN подтверждено.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/void', methods=['POST'])
@role_required('director')
def vin_registry_void(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    reason = (request.form.get('comment') or '').strip()
    if not reason:
        flash('Для аннулирования укажите причину.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.docs_issued_at:
        flash('По этому VIN уже выданы документы. Требуется отдельная процедура отмены документов.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.status in ('assigned', 'confirmed'):
        flash('Нельзя аннулировать VIN после назначения или подтверждения нанесения.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    old_status = row.status
    row.status = 'void'
    row.void_by_user_id = current_user.id
    row.void_at = datetime.utcnow()
    row.comment = reason
    _add_vin_event(row, 'voided', old_status, row.status, comment=reason)
    db.session.commit()
    flash('VIN аннулирован.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/admin-edit', methods=['POST'])
@admin_required
def vin_registry_admin_edit(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    old_status = row.status
    old_vin = row.vin_full
    vin_full = (request.form.get('vin_full') or '').strip().upper()
    serial7 = (request.form.get('serial7') or '').strip()
    status = (request.form.get('status') or row.status or 'free').strip()
    comment = (request.form.get('comment') or '').strip() or None

    if vin_full:
        parsed, error = _parse_vin_full(vin_full)
        if error:
            flash(error, 'danger')
            return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
        duplicate = VinRegistry.query.filter(
            VinRegistry.id != row.id,
            or_(VinRegistry.vin_full == parsed['vin_full'], VinRegistry.serial7 == parsed['serial7']),
        ).first()
        if duplicate:
            flash('Такой VIN или serial7 уже есть в реестре.', 'danger')
            return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
        row.prefix = parsed['prefix']
        row.vin_modification_code = parsed['vin_modification_code']
        row.year_code = parsed['year_code']
        row.serial7 = parsed['serial7']
        row.vin_full = parsed['vin_full']
    else:
        serial, error = _normalize_serial7(serial7)
        if error:
            flash(error, 'danger')
            return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
        duplicate = VinRegistry.query.filter(VinRegistry.id != row.id, VinRegistry.serial7 == serial).first()
        if duplicate:
            flash('Такой serial7 уже есть в реестре.', 'danger')
            return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
        row.serial7 = serial
        row.vin_full = None
        row.vin_modification_code = None
        row.year_code = None

    row.status = status
    row.comment = comment or row.comment
    if row.trailer and row.vin_full and row.trailer.vin != row.vin_full:
        row.trailer.vin = row.vin_full
    _add_vin_event(row, 'admin_edited', old_status, row.status, comment=comment or f'{old_vin or row.serial7} -> {row.vin_full or row.serial7}')
    db.session.commit()
    flash('VIN обновлён администратором.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/admin-delete', methods=['POST'])
@admin_required
def vin_registry_admin_delete(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.trailer_id or row.sales_contract_id or row.docs_issued_order_id or row.docs_issued_at:
        flash('VIN уже связан с прицепом, договором или документами. Удаление заблокировано, используйте корректировку или аннулирование.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.customer_order_id or row.supply_need_id:
        flash('VIN связан с заказом или заявкой. Сначала снимите резерв.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    db.session.delete(row)
    db.session.commit()
    flash('VIN удалён из реестра.', 'success')
    return redirect(url_for('main.vin_registry_list'))


@main_bp.route('/orders/<int:order_id>/issue-documents', methods=['POST'])
@login_required
def order_issue_documents(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.documents_issued:
        flash('Документы по этому заказу уже выданы.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.status == 'cancelled':
        flash('Нельзя выдать документы по отменённому заказу.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.is_shipped:
        flash('Заказ уже физически отгружен.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    document_blockers = _order_lines_document_blockers(order)
    if document_blockers:
        flash('Нельзя выдать документы: ' + '; '.join(document_blockers) + '.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    contract = SalesContract.query.filter_by(order_id=order.id).first()
    if not contract:
        flash('Сначала создайте договор.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if float(order.price or 0) <= 0:
        flash('Нельзя выдать документы: сумма заказа должна быть больше 0.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if order.remaining_amount > 0:
        flash('Нельзя выдать документы: заказ оплачен не полностью.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    old_document_status = order.document_status
    old_order_status = order.status
    order.document_status = 'documents_issued'
    order.documents_issued = True
    order.documents_issued_at = datetime.utcnow()
    order.status = 'sold_not_shipped'
    vin_was_reserved = False
    issued_vin_rows = []
    for line in order.lines.order_by(CustomerOrderLine.line_no.asc(), CustomerOrderLine.id.asc()).all():
        if line.line_type == 'TRAILER':
            line.status = 'sold_not_shipped'
        for trailer in _trailers_for_order_line(line):
            trailer.status = 'SOLD'
        line_vin_rows = _active_vin_rows_for_order_line(line)
        for vin_row in line_vin_rows:
            old_vin_status = vin_row.status
            vin_was_reserved = vin_was_reserved or vin_row.status == 'reserved'
            vin_row.docs_issued_at = datetime.utcnow()
            vin_row.docs_issued_order_id = order.id
            vin_row.sales_contract_id = contract.id
            issued_vin_rows.append(vin_row)
            _add_vin_event(vin_row, 'docs_issued', old_vin_status, vin_row.status, comment='Документы выданы по заказу')
        contract_lines = [row for row in line.contract_lines if row.sales_contract_id == contract.id]
        for contract_line in contract_lines:
            if not contract_line.trailer_id:
                trailer = _trailer_for_order_line(line)
                if trailer:
                    contract_line.trailer_id = trailer.id
                    contract_line.vin_full = trailer.vin
            if not contract_line.vin_registry_id and line_vin_rows:
                contract_line.vin_registry_id = line_vin_rows[0].id
                contract_line.vin_full = line_vin_rows[0].vin_full
    if order.trailer:
        order.trailer.status = 'SOLD'
    contract.is_paid = True
    contract.is_shipped = False
    comment = 'Юридическая продажа зафиксирована'
    if issued_vin_rows:
        comment = 'Юридическая продажа зафиксирована по VIN позиций заказа'
    add_order_event(order, 'documents_issued', old_value=old_document_status, new_value='documents_issued', comment=comment)
    if old_order_status != order.status:
        add_order_event(order, 'order_status_changed', old_value=old_order_status, new_value=order.status)
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    if vin_was_reserved:
        flash('VIN зарезервирован, но ещё не подтверждён производством. Проверьте, что производство нанесёт именно этот VIN.', 'warning')
    flash('Документы выданы. Позиции заказа юридически проданы, но ещё не отгружены клиенту.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/comment', methods=['POST'])
@login_required
def order_add_comment(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_access_order(order)
    if not (current_user.is_admin or current_user.is_director or can_manage_order(order)):
        abort(403)
    comment = (request.form.get('manager_comment') or '').strip()
    order.manager_comment = comment or None
    add_order_event(order, 'comment_added', comment=comment or None)
    db.session.commit()
    flash('Комментарий сохранён', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def order_cancel(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.status == 'cancelled':
        flash('Заказ уже отменён.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not current_user.is_admin and (order.confirmed_paid_amount > 0 or order.is_shipped):
        flash('Менеджер может отменить только заказ без подтверждённых оплат и без отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))
    reason = (request.form.get('cancel_reason') or '').strip() or None
    order.status = 'cancelled'
    order.cancelled_at = datetime.utcnow()
    order.cancel_reason = reason
    if order.trailer and order.trailer.status == 'RESERVED':
        order.trailer.status = 'IN_STOCK'
        order.trailer.lifecycle_status = 'arrived' if order.trailer.warehouse_id == order.warehouse_id else order.trailer.lifecycle_status
    for reservation in order.reservations.filter_by(status='ACTIVE').all():
        reservation.status = 'CANCELLED'
    for need in order.supply_needs.filter_by(status='NEW').all():
        need.status = 'CANCELLED'
    add_order_event(order, 'cancelled', new_value='cancelled', comment=reason)
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    flash('Заказ отменён', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/supply-needs/new', methods=['GET', 'POST'])
@role_required('manager', 'director')
def supply_need_create():
    form = SupplyNeedForm()
    _fill_supply_need_form_choices(form)
    if form.validate_on_submit():
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.supply_needs_list'))
        need_type = form.need_type.data
        need = SupplyNeed(
            need_type=need_type,
            status=form.status.data,
            order_id=None if need_type == 'STOCK_REPLENISHMENT' else (form.order_id.data or None),
            item_id=form.item_id.data,
            warehouse_id=form.warehouse_id.data or None,
            quantity=form.quantity.data,
            priority=form.priority.data,
            required_by=form.required_by.data,
            note=(form.note.data or '').strip() or None,
        )
        db.session.add(need)
        db.session.flush()
        _finish_idempotency(idem_key, 'SupplyNeed', need.id)
        db.session.commit()
        flash('Потребность создана', 'success')
        return redirect(url_for('main.supply_needs_list'))
    return render_template('supply_need_form.html', form=form, title='Новая потребность')


@main_bp.route('/supply-needs/<int:need_id>/edit', methods=['GET', 'POST'])
@role_required('manager', 'director')
def supply_need_edit(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    form = SupplyNeedForm(obj=need)
    _fill_supply_need_form_choices(form)
    if request.method == 'GET':
        form.order_id.data = need.order_id or 0
        form.warehouse_id.data = need.warehouse_id or 0
    if form.validate_on_submit():
        need.need_type = form.need_type.data
        need.status = form.status.data
        need.order_id = None if need.need_type == 'STOCK_REPLENISHMENT' else (form.order_id.data or None)
        need.item_id = form.item_id.data
        need.warehouse_id = form.warehouse_id.data or None
        need.quantity = form.quantity.data
        need.priority = form.priority.data
        need.required_by = form.required_by.data
        need.note = (form.note.data or '').strip() or None
        db.session.commit()
        flash('Потребность обновлена', 'success')
        return redirect(url_for('main.supply_needs_list'))
    return render_template('supply_need_form.html', form=form, title='Редактирование потребности')


@main_bp.route('/supply-needs/<int:need_id>/create-production-request', methods=['POST'])
@role_required('director', 'manager')
def supply_need_create_production_request(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    if current_user.is_manager and current_user.warehouse_id and need.warehouse_id not in (None, current_user.warehouse_id):
        abort(403)
    if need.status != 'NEW':
        flash('Заявку на производство можно создать только из новой потребности.', 'warning')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    existing_line = _active_production_line_for_need(need.id)
    if existing_line:
        flash('По этой потребности уже есть производственная строка.', 'warning')
        return redirect(url_for('main.production_request_detail', request_id=existing_line.production_request_id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.supply_needs_list'))

    is_stock_need = _is_stock_replenishment_need(need)
    note_prefix = 'Пополнение склада' if is_stock_need else 'Клиентский заказ'
    pr = ProductionRequest(
        request_number=_next_number('PR', ProductionRequest, 'request_number'),
        status='approved',
        target_warehouse_id=need.warehouse_id,
        note=f'{note_prefix}. Создана из потребности #{need.id}',
    )
    db.session.add(pr)
    db.session.flush()
    _finish_idempotency(idem_key, 'ProductionRequest', pr.id)
    db.session.add(ProductionRequestLine(
        production_request_id=pr.id,
        supply_need_id=need.id,
        order_line_id=need.order_line_id,
        production_workshop_id=need.production_workshop_id,
        item_id=need.item_id,
        quantity=need.quantity,
        produced_qty=0,
        status='planned',
        note=need.note,
    ))
    need.status = 'IN_PRODUCTION'
    if need.order:
        need.order.status = 'in_production'
        add_order_event(need.order, 'production_started', new_value=pr.request_number, comment='Создана заявка на производство')
    db.session.commit()
    if is_stock_need:
        flash('Заявка на производство для пополнения склада создана.', 'success')
    else:
        flash('Заявка на производство создана из потребности клиента.', 'success')
    return redirect(url_for('main.production_request_detail', request_id=pr.id))


@main_bp.route('/production-requests')
@role_required('director', 'manager')
def production_requests_list():
    status = request.args.get('status', '').strip()
    query = ProductionRequest.query
    if status:
        query = query.filter_by(status=status)
    requests = query.order_by(ProductionRequest.created_at.desc(), ProductionRequest.id.desc()).all()
    return render_template('production_requests_list.html', requests=requests, status=status)


@main_bp.route('/production-requests/new', methods=['GET', 'POST'])
@role_required('director', 'manager')
def production_request_create():
    form = ProductionRequestForm()
    form.target_warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    if request.method == 'GET':
        form.request_number.data = _next_number('PR', ProductionRequest, 'request_number')
        form.status.data = 'draft'
    if form.validate_on_submit():
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.production_requests_list'))
        pr = ProductionRequest(request_number=(form.request_number.data or '').strip() or _next_number('PR', ProductionRequest, 'request_number'), status=form.status.data, target_warehouse_id=form.target_warehouse_id.data or None, note=(form.note.data or '').strip() or None)
        db.session.add(pr)
        db.session.flush()
        _finish_idempotency(idem_key, 'ProductionRequest', pr.id)
        db.session.commit()
        flash('Заявка на производство создана', 'success')
        return redirect(url_for('main.production_request_detail', request_id=pr.id))
    return render_template('production_request_form.html', form=form, title='Новая заявка на производство')


@main_bp.route('/production-requests/<int:request_id>', methods=['GET', 'POST'])
@role_required('director', 'manager')
def production_request_detail(request_id):
    pr = ProductionRequest.query.get_or_404(request_id)
    form = ProductionRequestLineForm()
    _fill_production_line_form_choices(form, target_warehouse_id=pr.target_warehouse_id)
    if form.validate_on_submit():
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.production_request_detail', request_id=pr.id))
        selected_need = SupplyNeed.query.get(form.supply_need_id.data) if form.supply_need_id.data else None
        if selected_need and _active_production_line_for_need(selected_need.id):
            flash('Эта потребность уже привязана к активной строке производства.', 'warning')
            return redirect(url_for('main.production_request_detail', request_id=pr.id))
        if selected_need and pr.target_warehouse_id and selected_need.warehouse_id and selected_need.warehouse_id != pr.target_warehouse_id:
            flash('Склад потребности не совпадает со складом заявки на производство.', 'danger')
            return redirect(url_for('main.production_request_detail', request_id=pr.id))
        if selected_need and not pr.target_warehouse_id and selected_need.warehouse_id:
            pr.target_warehouse_id = selected_need.warehouse_id

        line_item_id = selected_need.item_id if selected_need else form.item_id.data
        line_quantity = selected_need.quantity if selected_need else form.quantity.data
        line_note = (form.note.data or '').strip() or (selected_need.note if selected_need else None)
        line = ProductionRequestLine(
            production_request_id=pr.id,
            supply_need_id=selected_need.id if selected_need else None,
            order_line_id=selected_need.order_line_id if selected_need else None,
            production_workshop_id=selected_need.production_workshop_id if selected_need else None,
            item_id=line_item_id,
            quantity=line_quantity,
            status=form.status.data,
            note=line_note,
        )
        db.session.add(line)
        if selected_need:
            selected_need.status = 'IN_PRODUCTION'
            if selected_need.order:
                old_status = selected_need.order.status
                selected_need.order.status = 'in_production'
                if old_status != selected_need.order.status:
                    add_order_event(
                        selected_need.order,
                        'production_started',
                        old_value=old_status,
                        new_value='in_production',
                        comment=f'Добавлена строка производства в заявку {pr.request_number}',
                    )
        db.session.flush()
        _finish_idempotency(idem_key, 'ProductionRequestLine', line.id)
        db.session.commit()
        flash('Позиция добавлена', 'success')
        return redirect(url_for('main.production_request_detail', request_id=pr.id))
    return render_template('production_request_detail.html', pr=pr, form=form)


@main_bp.route('/production-requests/<int:request_id>/edit', methods=['GET', 'POST'])
@role_required('director', 'manager')
def production_request_edit(request_id):
    pr = ProductionRequest.query.get_or_404(request_id)
    form = ProductionRequestForm(obj=pr)
    form.target_warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    if request.method == 'GET':
        form.target_warehouse_id.data = pr.target_warehouse_id or 0
    if form.validate_on_submit():
        pr.request_number = (form.request_number.data or '').strip() or pr.request_number
        pr.status = form.status.data
        pr.target_warehouse_id = form.target_warehouse_id.data or None
        pr.note = (form.note.data or '').strip() or None
        db.session.commit()
        flash('Заявка на производство обновлена', 'success')
        return redirect(url_for('main.production_request_detail', request_id=pr.id))
    return render_template('production_request_form.html', form=form, title='Редактирование заявки на производство')


@main_bp.route('/stock-movements')
@role_required('manager', 'director')
def stock_movements_list():
    status = request.args.get('status', '').strip()
    movement_type = request.args.get('movement_type', '').strip()
    from_warehouse_id = request.args.get('from_warehouse_id', type=int)
    to_warehouse_id = request.args.get('to_warehouse_id', type=int)
    scope = request.args.get('scope', '').strip()
    batch_key = request.args.get('batch_key', '').strip()
    query = StockMovement.query
    scope_warehouse_id = current_user.warehouse_id if current_user.is_manager else (to_warehouse_id or from_warehouse_id)
    if scope == 'incoming' and scope_warehouse_id:
        query = query.filter(StockMovement.to_warehouse_id == scope_warehouse_id)
    elif scope == 'outgoing' and scope_warehouse_id:
        query = query.filter(StockMovement.from_warehouse_id == scope_warehouse_id)
    elif scope == 'in_transit':
        query = query.filter(StockMovement.status.in_(['sent', 'in_transit']))
    elif scope == 'needs_receive':
        query = query.filter(StockMovement.status.in_(['sent', 'in_transit']))
        if scope_warehouse_id:
            query = query.filter(StockMovement.to_warehouse_id == scope_warehouse_id)
    elif scope == 'history':
        query = query.filter(StockMovement.status.in_(['arrived', 'cancelled']))
    if status:
        query = query.filter_by(status=status)
    if movement_type:
        query = query.filter_by(movement_type=movement_type)
    if from_warehouse_id:
        query = query.filter_by(from_warehouse_id=from_warehouse_id)
    if to_warehouse_id:
        query = query.filter_by(to_warehouse_id=to_warehouse_id)
    if batch_key:
        query = query.filter_by(batch_key=batch_key)
    movements = query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()
    return render_template(
        'stock_movements_list.html',
        movements=movements,
        status=status,
        movement_type=movement_type,
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        scope=scope,
        batch_key=batch_key,
        source_warehouses=_trailer_source_warehouses(),
        target_warehouses=_sales_warehouses(),
        movement_type_choices=STOCK_MOVEMENT_FILTER_TYPE_CHOICES,
        status_choices=StockMovementForm().status.choices,
    )


@main_bp.route('/stock-movements/new', methods=['GET', 'POST'])
@role_required('director', 'manager')
def stock_movement_create():
    form = StockMovementForm()
    _fill_stock_movement_form_choices(form)
    if form.validate_on_submit():
        if not _validate_stock_movement_selection(form):
            return render_template('stock_movement_form.html', form=form, title='Новое перемещение', trailer_options=_stock_movement_trailer_options(form.from_warehouse_id.data or None, form.trailer_search.data, form.trailer_id.data or None))
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.stock_movements_list'))
        movement = StockMovement(
            from_warehouse_id=form.from_warehouse_id.data or None,
            to_warehouse_id=form.to_warehouse_id.data or None,
            trailer_id=form.trailer_id.data or None,
            item_id=Trailer.query.get(form.trailer_id.data).item_id if form.trailer_id.data and Trailer.query.get(form.trailer_id.data) else None,
            order_id=form.order_id.data or None,
            movement_type=form.movement_type.data,
            status=form.status.data,
            departure_date=form.departure_date.data,
            arrival_date=form.arrival_date.data,
            note=(form.note.data or '').strip() or None,
        )
        db.session.add(movement)
        _apply_sent_stock_movement(movement)
        _apply_arrived_stock_movement(movement)
        db.session.flush()
        _finish_idempotency(idem_key, 'StockMovement', movement.id)
        db.session.commit()
        flash('Перемещение создано', 'success')
        return redirect(url_for('main.stock_movements_list'))
    return render_template('stock_movement_form.html', form=form, title='Новое перемещение', trailer_options=_stock_movement_trailer_options(form.from_warehouse_id.data or None, form.trailer_search.data, form.trailer_id.data or None))


@main_bp.route('/stock-movements/batch/new', methods=['GET', 'POST'])
@role_required('director', 'manager')
def stock_movement_batch_create():
    form = StockMovementBatchForm()
    _fill_stock_movement_batch_form_choices(form)
    selected_trailers = _selected_trailers_from_request() if request.method == 'POST' else []

    if request.method == 'GET':
        production_warehouse = _default_production_warehouse()
        if current_user.warehouse_id:
            form.from_warehouse_id.data = current_user.warehouse_id
        elif production_warehouse:
            form.from_warehouse_id.data = production_warehouse.id
        form.status.data = 'in_transit'
        form.movement_type.data = 'warehouse_transfer'
        form.departure_date.data = date.today()

    if form.validate_on_submit():
        if not _validate_batch_movement_selection(form, selected_trailers):
            return render_template(
                'stock_movement_batch_form.html',
                form=form,
                title='Партийное перемещение',
                selected_trailers=selected_trailers,
                available_trailers=_stock_movement_trailer_options(form.from_warehouse_id.data or None, limit=100),
            )
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.stock_movements_list'))

        batch_key = f"SMB-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{current_user.id}-{uuid4().hex[:6].upper()}"
        movements = []
        for trailer in selected_trailers:
            order = _movement_order_for_trailer(trailer)
            movement = StockMovement(
                batch_key=batch_key,
                from_warehouse_id=form.from_warehouse_id.data,
                to_warehouse_id=form.to_warehouse_id.data,
                trailer_id=trailer.id,
                item_id=trailer.item_id,
                order_id=order.id if order else None,
                movement_type=form.movement_type.data,
                status=form.status.data,
                departure_date=form.departure_date.data,
                arrival_date=form.arrival_date.data,
                note=(form.note.data or '').strip() or None,
            )
            db.session.add(movement)
            _apply_sent_stock_movement(movement)
            _apply_arrived_stock_movement(movement)
            movements.append(movement)
            if movement.order:
                add_order_event(movement.order, 'transfer_started', new_value=batch_key, comment=f'Партийное перемещение VIN {trailer.vin}')
        db.session.flush()
        _finish_idempotency(idem_key, 'StockMovementBatch', movements[0].id if movements else None)
        db.session.commit()
        flash(f'Партия перемещения {batch_key} создана: {len(movements)} прицепов.', 'success')
        return redirect(url_for('main.stock_movements_list', batch_key=batch_key))

    return render_template(
        'stock_movement_batch_form.html',
        form=form,
        title='Партийное перемещение',
        selected_trailers=selected_trailers,
        available_trailers=_stock_movement_trailer_options(form.from_warehouse_id.data or None, limit=100),
    )


@main_bp.route('/stock-movements/<int:movement_id>/edit', methods=['GET', 'POST'])
@role_required('director', 'manager')
def stock_movement_edit(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    form = StockMovementForm(obj=movement)
    if request.method == 'GET':
        form.from_warehouse_id.data = movement.from_warehouse_id or 0
        form.to_warehouse_id.data = movement.to_warehouse_id or 0
        form.trailer_id.data = movement.trailer_id or 0
        form.order_id.data = movement.order_id or 0
    _fill_stock_movement_form_choices(form, current_movement_id=movement.id, include_customer_shipment=(movement.movement_type == 'customer_shipment'))
    if form.validate_on_submit():
        if not _validate_stock_movement_selection(form, current_movement_id=movement.id):
            return render_template('stock_movement_form.html', form=form, title='Редактирование перемещения', trailer_options=_stock_movement_trailer_options(form.from_warehouse_id.data or None, form.trailer_search.data, form.trailer_id.data or None, movement.id))
        movement.from_warehouse_id = form.from_warehouse_id.data or None
        movement.to_warehouse_id = form.to_warehouse_id.data or None
        movement.trailer_id = form.trailer_id.data or None
        movement.item_id = movement.trailer.item_id if movement.trailer else None
        movement.order_id = form.order_id.data or None
        movement.movement_type = form.movement_type.data
        movement.status = form.status.data
        movement.departure_date = form.departure_date.data
        movement.arrival_date = form.arrival_date.data
        movement.note = (form.note.data or '').strip() or None
        _apply_sent_stock_movement(movement)
        _apply_arrived_stock_movement(movement)
        db.session.commit()
        flash('Перемещение обновлено', 'success')
        return redirect(url_for('main.stock_movements_list'))
    return render_template('stock_movement_form.html', form=form, title='Редактирование перемещения', trailer_options=_stock_movement_trailer_options(form.from_warehouse_id.data or None, form.trailer_search.data, form.trailer_id.data or None, movement.id), movement=movement)


def _apply_arrived_stock_movement(movement: StockMovement) -> None:
    if movement.status != 'arrived':
        return
    movement.received_at = movement.received_at or datetime.utcnow()
    movement.arrival_date = movement.arrival_date or date.today()
    if movement.trailer and movement.to_warehouse_id:
        movement.trailer.warehouse_id = movement.to_warehouse_id
        if movement.trailer.status == 'IN_TRANSIT':
            movement.trailer.status = 'IN_STOCK'
        movement.trailer.lifecycle_status = 'arrived'
    if movement.order:
        if movement.trailer_id and not movement.order.trailer_id:
            movement.order.trailer_id = movement.trailer_id
            active = _active_reservation_for_trailer(movement.trailer_id, exclude_order_id=movement.order_id)
            if not active:
                db.session.add(Reservation(order_id=movement.order.id, trailer_id=movement.trailer_id, item_id=movement.order.item_id, status='ACTIVE', source_type='TRANSIT', priority=10))
                if movement.trailer:
                    movement.trailer.status = 'RESERVED'
        movement.order.status = 'sold_not_shipped' if movement.order.documents_issued else 'ready_to_ship'


def _apply_sent_stock_movement(movement: StockMovement) -> None:
    if movement.status not in ('sent', 'in_transit'):
        return
    movement.moved_at = movement.moved_at or datetime.utcnow()
    movement.departure_date = movement.departure_date or date.today()
    if movement.trailer:
        if movement.trailer.status != 'SOLD':
            movement.trailer.status = 'IN_TRANSIT'
        movement.trailer.lifecycle_status = 'in_transit'
    if movement.order:
        movement.order.status = 'in_transit'


def _validate_stock_movement_selection(form: StockMovementForm, current_movement_id: int | None = None) -> bool:
    from_warehouse_id = form.from_warehouse_id.data or None
    to_warehouse_id = form.to_warehouse_id.data or None
    if from_warehouse_id and to_warehouse_id and from_warehouse_id == to_warehouse_id:
        flash('Нельзя создать перемещение на тот же склад.', 'danger')
        return False
    from_warehouse = Warehouse.query.get(from_warehouse_id) if from_warehouse_id else None
    to_warehouse = Warehouse.query.get(to_warehouse_id) if to_warehouse_id else None
    if from_warehouse and not (getattr(from_warehouse, 'can_sell', True) or getattr(from_warehouse, 'is_production', False)):
        flash('Со склада можно выбрать склад продаж или производственный склад.', 'danger')
        return False
    if to_warehouse and not getattr(to_warehouse, 'can_sell', True):
        flash('На склад можно выбрать только склад продаж.', 'danger')
        return False
    trailer_id = form.trailer_id.data or None
    if not trailer_id:
        return True
    trailer = Trailer.query.get(trailer_id)
    if not trailer:
        flash('Выбранный прицеп не найден.', 'danger')
        return False
    if not _trailer_available_for_movement(trailer, from_warehouse_id=from_warehouse_id, exclude_movement_id=current_movement_id):
        flash('Этот прицеп нельзя перемещать: проверьте склад, статус или активное перемещение.', 'danger')
        return False
    return True


def _validate_batch_movement_selection(form: StockMovementBatchForm, trailers: list[Trailer]) -> bool:
    from_warehouse_id = form.from_warehouse_id.data or None
    to_warehouse_id = form.to_warehouse_id.data or None
    if not trailers:
        flash('Добавьте хотя бы один прицеп в партию.', 'danger')
        return False
    if from_warehouse_id and to_warehouse_id and from_warehouse_id == to_warehouse_id:
        flash('Нельзя создать перемещение на тот же склад.', 'danger')
        return False
    from_warehouse = Warehouse.query.get(from_warehouse_id) if from_warehouse_id else None
    to_warehouse = Warehouse.query.get(to_warehouse_id) if to_warehouse_id else None
    if from_warehouse and not (getattr(from_warehouse, 'can_sell', True) or getattr(from_warehouse, 'is_production', False)):
        flash('Со склада можно выбрать склад продаж или производственный склад.', 'danger')
        return False
    if to_warehouse and not getattr(to_warehouse, 'can_sell', True):
        flash('На склад можно выбрать только склад продаж.', 'danger')
        return False
    invalid = [
        trailer.vin for trailer in trailers
        if not _trailer_available_for_movement(trailer, from_warehouse_id=from_warehouse_id)
    ]
    if invalid:
        flash('Нельзя переместить эти прицепы: ' + ', '.join(invalid), 'danger')
        return False
    return True


def _refresh_production_request_status(production_request: ProductionRequest) -> None:
    lines = production_request.lines.all()
    if not lines:
        return
    statuses = {line.status for line in lines}
    if statuses <= {'ready', 'closed'}:
        production_request.status = 'ready'
    elif 'partial_ready' in statuses or 'ready' in statuses:
        production_request.status = 'partial_ready'
    elif 'in_production' in statuses:
        production_request.status = 'in_progress'


@main_bp.route('/production/workspace')
@role_required('production', 'director', 'manager')
def production_workspace():
    active_tab = request.args.get('tab', 'todo')
    production_tabs = {'warehouses', 'frames', 'todo', 'in_work', 'done_today', 'history', 'materials', 'overdue'}
    if active_tab not in production_tabs:
        active_tab = 'todo'
    today_start = datetime.combine(date.today(), time.min)

    base_query = (
        ProductionRequestLine.query
        .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        .outerjoin(SupplyNeed, SupplyNeed.id == ProductionRequestLine.supply_need_id)
    )
    open_lines = base_query.filter(
        ~ProductionRequestLine.status.in_(['ready', 'closed', 'cancelled', 'CANCELLED'])
    ).all()
    warehouse_rows = []
    warehouse_groups = defaultdict(int)
    for line in open_lines:
        warehouse_name = line.production_request.target_warehouse.name if line.production_request and line.production_request.target_warehouse else 'Склад не задан'
        warehouse_groups[warehouse_name] += max((line.quantity or 0) - (line.produced_qty or 0), 0)
    warehouse_rows = [{'label': label, 'quantity': qty} for label, qty in sorted(warehouse_groups.items())]

    frame_groups = defaultdict(int)
    for line in open_lines:
        item = line.item
        if item and item.body_length_mm and item.body_width_mm:
            label = f'{item.body_length_mm}x{item.body_width_mm}'
        elif item and item.size_body:
            label = item.size_body
        else:
            label = 'Размер не задан'
        frame_groups[label] += max((line.quantity or 0) - (line.produced_qty or 0), 0)
    frame_rows = [{'label': label, 'quantity': qty} for label, qty in sorted(frame_groups.items())]

    lines = []
    if active_tab == 'in_work':
        lines = base_query.filter(ProductionRequestLine.status.in_(['in_production', 'partial_ready'])).order_by(ProductionRequest.created_at.asc(), ProductionRequestLine.id.asc()).all()
    elif active_tab == 'overdue':
        lines = base_query.filter(
            ~ProductionRequestLine.status.in_(['ready', 'closed', 'cancelled', 'CANCELLED']),
            SupplyNeed.required_by.isnot(None),
            SupplyNeed.required_by < date.today(),
        ).order_by(SupplyNeed.required_by.asc(), ProductionRequestLine.id.asc()).all()
    elif active_tab == 'materials':
        lines = sorted(open_lines, key=lambda row: (row.supply_need.required_by if row.supply_need and row.supply_need.required_by else date.max, row.id))
    elif active_tab not in ('done_today', 'history', 'warehouses', 'frames'):
        active_tab = 'todo'
        lines = base_query.filter(ProductionRequestLine.status.in_(['planned', 'PLANNED', 'draft', 'DRAFT', 'waiting_production'])).order_by(ProductionRequest.created_at.asc(), ProductionRequestLine.id.asc()).all()

    today_units = (
        ProducedUnit.query
        .filter(or_(ProducedUnit.created_at >= today_start, ProducedUnit.produced_at >= today_start))
        .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
        .all()
    )
    history_units = (
        ProducedUnit.query
        .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
        .limit(80)
        .all()
    )
    return render_template(
        'production_workspace.html',
        lines=lines,
        today_units=today_units,
        history_units=history_units,
        warehouse_rows=warehouse_rows,
        frame_rows=frame_rows,
        active_tab=active_tab,
    )


@main_bp.route('/production/lines/<int:line_id>/start', methods=['POST'])
@role_required('production')
def production_line_start(line_id):
    line = ProductionRequestLine.query.get_or_404(line_id)
    if (line.status or '').lower() in ('in_production', 'partial_ready', 'ready', 'closed', 'cancelled', 'canceled'):
        flash('Позиция уже в работе.', 'warning')
        return redirect(url_for('main.production_workspace', tab='in_work'))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.production_workspace', tab='in_work'))
    line.status = 'in_production'
    line.started_at = line.started_at or datetime.utcnow()
    line.production_request.status = 'in_progress'
    if line.supply_need:
        line.supply_need.status = 'IN_PRODUCTION'
        if line.supply_need.order:
            line.supply_need.order.status = 'in_production'
            add_order_event(line.supply_need.order, 'production_started', new_value=line.production_request.request_number)
    _finish_idempotency(idem_key, 'ProductionRequestLine', line.id)
    db.session.commit()
    flash('Позиция взята в работу.', 'success')
    return redirect(url_for('main.production_workspace', tab='in_work'))


@main_bp.route('/production/lines/<int:line_id>/produce-one', methods=['POST'])
@role_required('production')
def production_line_produce_one(line_id):
    line = ProductionRequestLine.query.get_or_404(line_id)
    if line.produced_qty >= line.quantity:
        flash('По этой позиции уже выпущено нужное количество', 'warning')
        return redirect(url_for('main.production_workspace', tab='in_work'))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.production_workspace', tab='done_today'))
    if (line.status or '').lower() in ('planned', 'draft', 'waiting_production'):
        line.status = 'in_production'
        line.started_at = line.started_at or datetime.utcnow()
        line.production_request.status = 'in_progress'
    line.produced_qty = (line.produced_qty or 0) + 1
    line.produced_at = datetime.utcnow()
    line.status = 'ready' if line.produced_qty >= line.quantity else 'partial_ready'
    if line.status == 'ready':
        line.completed_at = datetime.utcnow()
    produced_unit = ProducedUnit(
        production_request_line_id=line.id,
        order_line_id=line.order_line_id or (line.supply_need.order_line_id if line.supply_need else None),
        production_workshop_id=line.production_workshop_id or (line.supply_need.production_workshop_id if line.supply_need else None),
        item_id=line.item_id,
        target_warehouse_id=line.production_request.target_warehouse_id,
        produced_at=datetime.utcnow(),
        status='produced_no_vin',
    )
    db.session.add(produced_unit)
    db.session.flush()
    _finish_idempotency(idem_key, 'ProducedUnit', produced_unit.id)
    if line.supply_need:
        line.supply_need.status = 'READY' if line.status == 'ready' else 'IN_PRODUCTION'
        if line.supply_need.order:
            line.supply_need.order.status = 'produced_waiting_vin' if line.status == 'ready' else 'in_production'
            add_order_event(line.supply_need.order, 'produced_without_vin', new_value=line.item.article if line.item else line.item_id)
    _refresh_production_request_status(line.production_request)
    db.session.commit()
    flash('Выпущена 1 единица без VIN.', 'success')
    return redirect(url_for('main.production_workspace', tab='done_today'))


@main_bp.route('/production/lines/<int:line_id>/comment', methods=['POST'])
@role_required('production')
def production_line_comment(line_id):
    line = ProductionRequestLine.query.get_or_404(line_id)
    line.production_comment = (request.form.get('production_comment') or '').strip() or None
    db.session.commit()
    flash('Комментарий сохранён', 'success')
    return redirect(url_for('main.production_workspace', tab=request.args.get('tab', 'in_work')))


@main_bp.route('/logistics/workspace')
@role_required('logistics', 'director')
def logistics_workspace():
    if current_user.is_logistics:
        return redirect(url_for('main.vin_registry_list'))
    production_warehouses = _production_warehouses()
    production_warehouse = _default_production_warehouse()
    production_warehouse_warning = None
    if not production_warehouse:
        production_warehouse_warning = 'Производственный склад не задан. Отметьте склад выпуска в справочнике складов.'
        flash(production_warehouse_warning, 'danger')
    elif len(production_warehouses) > 1 and not (
        current_user.warehouse_id and current_user.warehouse_id == production_warehouse.id
    ):
        production_warehouse_warning = 'Найдено несколько производственных складов. Для точности назначьте склад пользователю-логисту.'
    production_needs = (
        SupplyNeed.query
        .filter(SupplyNeed.status == 'NEW')
        .order_by(SupplyNeed.required_by.asc().nullslast(), SupplyNeed.priority.asc(), SupplyNeed.created_at.asc())
        .all()
    )
    production_lines = (
        ProductionRequestLine.query
        .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        .filter(ProductionRequestLine.status.in_(['planned', 'in_production', 'partial_ready', 'PLANNED']))
        .order_by(ProductionRequest.created_at.desc(), ProductionRequestLine.id.desc())
        .all()
    )
    produced_units = ProducedUnit.query.filter_by(status='produced_no_vin').order_by(ProducedUnit.created_at.desc()).all()
    produced_unit_rows = [_produced_unit_context(unit) for unit in produced_units]
    ready_trailers = (
        Trailer.query
        .filter(
            Trailer.warehouse_id == production_warehouse.id,
            Trailer.status.in_(['IN_STOCK', 'RESERVED']),
            or_(Trailer.lifecycle_status.is_(None), ~Trailer.lifecycle_status.in_(['in_transit', 'sold', 'customer_shipped'])),
        )
        .order_by(Trailer.created_at.desc())
        .all()
        if production_warehouse else []
    )
    ready_trailer_rows = []
    for trailer in ready_trailers:
        if _active_movement_for_trailer(trailer.id):
            continue
        context = _trailer_production_context(trailer)
        target_warehouse = context.get('target_warehouse')
        if not target_warehouse or target_warehouse.id == production_warehouse.id:
            continue
        context['trailer'] = trailer
        ready_trailer_rows.append(context)
    sold_not_shipped_trailers = (
        Trailer.query
        .filter(
            Trailer.status == 'SOLD',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Trailer.created_at.desc(), Trailer.id.desc())
        .all()
    )
    sold_not_shipped_rows = []
    for trailer in sold_not_shipped_trailers:
        order = (
            CustomerOrder.query
            .filter(
                CustomerOrder.trailer_id == trailer.id,
                CustomerOrder.documents_issued == True,
                CustomerOrder.is_shipped == False,
                CustomerOrder.status != 'cancelled',
            )
            .order_by(CustomerOrder.documents_issued_at.desc().nullslast(), CustomerOrder.created_at.desc())
            .first()
        )
        target_warehouse = order.warehouse if order else None
        can_send = bool(
            production_warehouse
            and order
            and target_warehouse
            and trailer.warehouse_id == production_warehouse.id
            and target_warehouse.id != trailer.warehouse_id
            and not _trailer_is_customer_shipped(trailer)
            and not _active_movement_for_trailer(trailer.id)
        )
        sold_not_shipped_rows.append({
            'trailer': trailer,
            'order': order,
            'target_warehouse': target_warehouse,
            'can_send': can_send,
        })
    inbound = (
        StockMovement.query
        .outerjoin(Trailer, Trailer.id == StockMovement.trailer_id)
        .filter(
            StockMovement.status.in_(['sent', 'in_transit']),
            or_(Trailer.id.is_(None), Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(StockMovement.created_at.desc())
        .all()
    )
    send_form = SendTrailerForm()
    sendable_sold_rows = [row for row in sold_not_shipped_rows if row.get('can_send')]
    send_form.trailer_id.choices = [
        (row['trailer'].id, f"{row['trailer'].vin} — {row['trailer'].item.article if row['trailer'].item else ''} — {row['target_warehouse'].name if row['target_warehouse'] else 'без назначения'}")
        for row in ready_trailer_rows
    ] + [
        (row['trailer'].id, f"{row['trailer'].vin} — продан, выдача: {row['target_warehouse'].name if row['target_warehouse'] else 'без назначения'}")
        for row in sendable_sold_rows
    ]
    send_form.to_warehouse_id.choices = [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all() if not production_warehouse or w.id != production_warehouse.id]
    send_form.order_id.choices = [(0, '— без заказа —')] + [(o.id, o.order_number) for o in CustomerOrder.query.filter(CustomerOrder.status.notin_(['done', 'cancelled'])).order_by(CustomerOrder.created_at.desc()).all()]
    docs_issued_without_trailer_rows = []
    docs_vins = (
        VinRegistry.query
        .join(CustomerOrder, CustomerOrder.id == VinRegistry.customer_order_id)
        .filter(
            CustomerOrder.documents_issued == True,
            CustomerOrder.trailer_id.is_(None),
            CustomerOrder.status != 'cancelled',
            VinRegistry.docs_issued_at.isnot(None),
            VinRegistry.trailer_id.is_(None),
        )
        .order_by(VinRegistry.docs_issued_at.desc(), VinRegistry.id.desc())
        .limit(50)
        .all()
    )
    for vin_row in docs_vins:
        order = vin_row.customer_order
        docs_issued_without_trailer_rows.append({
            'order': order,
            'vin': vin_row,
            'need': _active_supply_need_for_order(order.id) if order else None,
        })
    assigned_not_confirmed_vins = (
        VinRegistry.query
        .filter(VinRegistry.status == 'assigned', VinRegistry.confirmed_at.is_(None))
        .order_by(VinRegistry.assigned_at.desc().nullslast(), VinRegistry.id.desc())
        .limit(50)
        .all()
    )
    return render_template(
        'logistics_workspace.html',
        production_warehouse=production_warehouse,
        production_warehouses=production_warehouses,
        production_warehouse_warning=production_warehouse_warning,
        production_needs=production_needs,
        production_lines=production_lines,
        produced_units=produced_units,
        produced_unit_rows=produced_unit_rows,
        ready_trailers=ready_trailers,
        ready_trailer_rows=ready_trailer_rows,
        sold_not_shipped_rows=sold_not_shipped_rows,
        inbound=inbound,
        send_form=send_form,
        docs_issued_without_trailer_rows=docs_issued_without_trailer_rows,
        assigned_not_confirmed_vins=assigned_not_confirmed_vins,
    )


@main_bp.route('/logistics/produced-units/<int:unit_id>/assign-vin', methods=['GET', 'POST'])
@role_required('logistics', 'director')
def logistics_assign_vin(unit_id):
    unit = ProducedUnit.query.get_or_404(unit_id)
    form = AssignVinForm()
    context = _produced_unit_context(unit)
    back_url = url_for('main.vin_registry_list') if current_user.is_logistics else url_for('main.logistics_workspace')
    reserved_vin_row = context.get('vin_registry')
    if reserved_vin_row and (reserved_vin_row.status != 'reserved' or not reserved_vin_row.vin_full):
        reserved_vin_row = None
    if unit.status != 'produced_no_vin':
        flash('По этой единице VIN уже присвоен или она недоступна.', 'warning')
        return redirect(back_url)
    if request.method == 'GET':
        form.manufacture_date.data = date.today()
        if reserved_vin_row:
            form.vin.data = reserved_vin_row.vin_full
    if form.validate_on_submit():
        production_warehouse = _default_production_warehouse()
        if not production_warehouse:
            flash('Производственный склад не найден. В справочнике складов отметьте нужный склад как производственный.', 'danger')
            return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row, back_url=back_url)
        vin = reserved_vin_row.vin_full if reserved_vin_row else (form.vin.data or '').strip().upper()
        line = unit.production_request_line
        order = unit.order if unit.order_id else None
        if not order and line and line.supply_need:
            order = line.supply_need.order
        existing_trailer = Trailer.query.filter_by(vin=vin).first()
        vin_registry_row = reserved_vin_row
        parsed = None
        if not vin_registry_row:
            parsed, error = _parse_vin_full(vin)
            if error:
                form.vin.errors.append(error)
                return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row, back_url=back_url)
            vin_registry_row = VinRegistry.query.filter(or_(VinRegistry.vin_full == vin, VinRegistry.serial7 == parsed['serial7'])).first()
            if vin_registry_row and not existing_trailer and vin_registry_row.status not in ('free', 'reserved'):
                form.vin.errors.append('Этот VIN уже занят в реестре.')
                return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row, back_url=back_url)
            if not vin_registry_row:
                vin_registry_row = VinRegistry(status='free', source='logistics_assign', **parsed)
                db.session.add(vin_registry_row)
                db.session.flush()
                _add_vin_event(vin_registry_row, 'created', None, 'free', comment='Создан при присвоении VIN')
            elif not vin_registry_row.vin_full:
                vin_registry_row.prefix = parsed['prefix']
                vin_registry_row.vin_modification_code = parsed['vin_modification_code']
                vin_registry_row.year_code = parsed['year_code']
                vin_registry_row.vin_full = parsed['vin_full']
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, back_url)
        if existing_trailer:
            form.vin.errors.append(
                f'VIN уже есть у прицепа Trailer #{existing_trailer.id} на складе. '
                'Нельзя присвоить этот VIN новой выпущенной единице, иначе получится дубль.'
            )
            return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row, back_url=back_url)
        trailer_status = 'IN_STOCK'
        if order:
            trailer_status = 'SOLD' if order.documents_issued or order.status == 'sold_not_shipped' else 'RESERVED'
        trailer = Trailer(vin=vin, item_id=unit.item_id, warehouse_id=production_warehouse.id, manufacture_date=form.manufacture_date.data, status=trailer_status, lifecycle_status='ready_production_warehouse')
        db.session.add(trailer)
        db.session.flush()
        _finish_idempotency(idem_key, 'Trailer', trailer.id)
        unit.status = 'vin_assigned'
        unit.trailer_id = trailer.id
        if order:
            unit.order_id = order.id
            order.trailer_id = trailer.id
            if order.documents_issued or order.status == 'sold_not_shipped':
                order.status = 'sold_not_shipped'
            elif unit.target_warehouse_id and unit.target_warehouse_id != production_warehouse.id:
                order.status = 'waiting_transfer'
            else:
                order.status = 'ready_to_ship'
            _ensure_order_reservation(order, trailer, 'PRODUCTION')
            add_order_event(order, 'vin_assigned', new_value=vin)
            add_order_event(order, 'trailer_assigned', new_value=vin)
        old_vin_status = vin_registry_row.status
        vin_registry_row.trailer_id = trailer.id
        vin_registry_row.status = 'assigned'
        vin_registry_row.assigned_by_user_id = current_user.id
        vin_registry_row.assigned_at = datetime.utcnow()
        if order and not vin_registry_row.customer_order_id:
            vin_registry_row.customer_order_id = order.id
        if order and not vin_registry_row.order_line_id:
            order_line = _primary_order_line(order)
            if order_line:
                vin_registry_row.order_line_id = order_line.id
        if line and line.supply_need and not vin_registry_row.supply_need_id:
            vin_registry_row.supply_need_id = line.supply_need.id
        _add_vin_event(vin_registry_row, 'assigned', old_vin_status, vin_registry_row.status, comment='VIN привязан к выпущенному прицепу')
        db.session.commit()
        flash('VIN присвоен. Прицеп создан на производственном складе.', 'success')
        return redirect(back_url)
    return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row, back_url=back_url)


@main_bp.route('/logistics/produced-units/<int:unit_id>/change-item', methods=['GET', 'POST'])
@role_required('manager', 'director')
def produced_unit_change_item(unit_id):
    unit = ProducedUnit.query.get_or_404(unit_id)
    back_url = url_for('main.order_detail', order_id=unit.order_id) if current_user.is_manager and unit.order_id else url_for('main.production_workspace')
    if current_user.is_manager and unit.target_warehouse_id != current_user.warehouse_id:
        abort(403)
    ok, message = _can_change_produced_unit_item(unit)
    if not ok:
        flash(message, 'danger')
        return redirect(back_url)

    form = FlaskForm()
    locked_config = _config_from_item(unit.item)
    config = _config_with_defaults(_config_from_request_values(request.values) if request.method == 'POST' else locked_config)
    result = _build_config_result(config)

    if request.method == 'POST':
        if not _locked_config_base_matches(config, locked_config):
            flash('Нельзя менять группу, тип кузова и размер кузова. Измените только комплектацию.', 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию выпущенной единицы',
                current_item=unit.item,
                target_label=f'Выпущенная единица #{unit.id}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        if result.get('errors'):
            for error in result.get('errors') or []:
                flash(error, 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию выпущенной единицы',
                current_item=unit.item,
                target_label=f'Выпущенная единица #{unit.id}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        item = get_or_create_configured_item(result)
        if not _item_base_matches_locked_item(item, unit.item):
            flash('Нельзя менять размер рамы / кузова и количество осей. Выберите комплектацию с тем же размером и осями.', 'danger')
            return render_template(
                'trailer_item_form.html',
                form=form,
                title='Изменить комплектацию выпущенной единицы',
                current_item=unit.item,
                target_label=f'Выпущенная единица #{unit.id}',
                back_url=back_url,
                locked_base=True,
                locked_config=locked_config,
                config=config,
                config_options=_trailer_config_form_context(),
                result=result,
            )
        old_item = unit.item
        old_label = old_item.article if old_item else unit.item_id
        unit.item_id = item.id

        order = unit.order if unit.order_id else None
        if not order and unit.production_request_line and unit.production_request_line.supply_need:
            order = unit.production_request_line.supply_need.order
        if order and not order.documents_issued and not _order_is_shipped(order):
            order.item_id = item.id
            add_order_event(
                order,
                'comment_added',
                old_value=str(old_label or ''),
                new_value=item.article or str(item.id),
                comment='Изменена комплектация выпущенной единицы без VIN',
            )

        db.session.commit()
        flash('Комплектация выпущенной единицы обновлена.', 'success')
        return redirect(back_url)

    return render_template(
        'trailer_item_form.html',
        form=form,
        title='Изменить комплектацию выпущенной единицы',
        current_item=unit.item,
        target_label=f'Выпущенная единица #{unit.id}',
        back_url=back_url,
        locked_base=True,
        locked_config=locked_config,
        config=config,
        config_options=_trailer_config_form_context(),
        result=result,
    )


@main_bp.route('/logistics/send-trailer', methods=['POST'])
@role_required('manager', 'director')
def logistics_send_trailer():
    form = SendTrailerForm()
    back_url = url_for('main.stock_movements_list', scope='outgoing') if current_user.is_manager else url_for('main.logistics_workspace')
    production_warehouse = _default_production_warehouse()
    ready_query = Trailer.query.filter(
        Trailer.warehouse_id == production_warehouse.id,
        Trailer.status.in_(['IN_STOCK', 'RESERVED', 'SOLD']),
        or_(Trailer.lifecycle_status.is_(None), ~Trailer.lifecycle_status.in_(['in_transit', 'customer_shipped'])),
    ) if production_warehouse else Trailer.query.filter(False)
    form.trailer_id.choices = [(t.id, t.vin) for t in ready_query.order_by(Trailer.vin).all()]
    form.to_warehouse_id.choices = [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all() if not production_warehouse or w.id != production_warehouse.id]
    form.order_id.choices = [(0, '— без заказа —')] + [(o.id, o.order_number) for o in CustomerOrder.query.all()]
    if not form.validate_on_submit() or not production_warehouse:
        flash('Не удалось отправить прицеп. Проверьте производственный склад и данные формы.', 'danger')
        return redirect(back_url)
    trailer = Trailer.query.get_or_404(form.trailer_id.data)
    to_warehouse = Warehouse.query.get(form.to_warehouse_id.data)
    if not trailer.vin:
        flash('Нельзя отправить прицеп без VIN.', 'danger')
        return redirect(back_url)
    if not trailer.warehouse_id:
        flash('У прицепа не задан текущий склад.', 'danger')
        return redirect(back_url)
    if not to_warehouse:
        flash('Склад назначения не задан.', 'danger')
        return redirect(back_url)
    if trailer.warehouse_id == to_warehouse.id:
        flash('Нельзя отправить прицеп на тот же склад.', 'danger')
        return redirect(back_url)
    if trailer.warehouse_id != production_warehouse.id or trailer.status not in ('IN_STOCK', 'RESERVED', 'SOLD') or (trailer.lifecycle_status or '') in ('in_transit', 'customer_shipped'):
        flash('Этот прицеп нельзя отправить со склада выпуска.', 'danger')
        return redirect(back_url)
    context = _trailer_production_context(trailer)
    order_id = form.order_id.data or None
    order = CustomerOrder.query.get(order_id) if order_id else None
    if not order and context.get('order'):
        order = context['order']
    if not order and trailer.status == 'SOLD':
        order = _find_order_for_sold_trailer(trailer)
    if current_user.is_manager:
        if order and not can_manage_order(order):
            abort(403)
    target_warehouse = order.warehouse if order and order.warehouse_id else context.get('target_warehouse')
    if not target_warehouse or target_warehouse.id == production_warehouse.id:
        flash('Для этого прицепа склад назначения не задан или совпадает со складом выпуска.', 'danger')
        return redirect(back_url)
    if target_warehouse.id != form.to_warehouse_id.data:
        flash(f'Для этого прицепа склад назначения: {target_warehouse.name}.', 'danger')
        return redirect(back_url)
    if _active_movement_for_trailer(trailer.id):
        flash('Этот прицеп уже находится в активном перемещении.', 'warning')
        return redirect(back_url)
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, back_url)
    if order:
        order_id = order.id
    note = (form.note.data or '').strip() or None
    if order and not note:
        note = f'Перемещение под заказ №{order.order_number}'
    movement = StockMovement(
        movement_type='warehouse_transfer',
        status='in_transit',
        from_warehouse_id=production_warehouse.id,
        to_warehouse_id=form.to_warehouse_id.data,
        trailer_id=trailer.id,
        item_id=trailer.item_id,
        order_id=order_id,
        departure_date=form.departure_date.data or date.today(),
        arrival_date=form.arrival_date.data,
        note=note,
    )
    if trailer.status != 'SOLD':
        trailer.status = 'IN_TRANSIT'
    trailer.lifecycle_status = 'in_transit'
    if movement.order:
        if not movement.order.documents_issued and movement.order.status != 'sold_not_shipped':
            movement.order.status = 'in_transit'
        add_order_event(movement.order, 'transfer_started', new_value=trailer.vin, comment=f'Отправка на склад #{form.to_warehouse_id.data}')
    db.session.add(movement)
    db.session.flush()
    _finish_idempotency(idem_key, 'StockMovement', movement.id)
    db.session.commit()
    if trailer.status == 'SOLD':
        flash('Прицеп продан, но не отгружен клиенту. Создано перемещение до склада выдачи.', 'success')
    else:
        flash('Прицеп отправлен на склад.', 'success')
    return redirect(back_url)


@main_bp.route('/stock-movements/<int:movement_id>/receive', methods=['POST'])
@role_required('manager', 'director')
def stock_movement_receive(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    if not can_receive_movement(movement):
        abort(403)
    if movement.status == 'arrived':
        flash('Перемещение уже принято на склад.', 'warning')
        return redirect(url_for('main.stock_movements_list', scope='needs_receive'))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.stock_movements_list', scope='needs_receive'))
    movement.status = 'arrived'
    movement.received_at = datetime.utcnow()
    _apply_arrived_stock_movement(movement)
    if movement.trailer:
        if movement.order_id:
            if movement.order and movement.order.documents_issued:
                movement.trailer.status = 'SOLD'
            else:
                movement.trailer.status = 'RESERVED' if _active_reservation_for_trailer(movement.trailer.id) else 'IN_STOCK'
        else:
            movement.trailer.status = 'IN_STOCK'
        movement.trailer.lifecycle_status = 'arrived'
    if movement.order:
        movement.order.status = 'sold_not_shipped' if movement.order.documents_issued else 'ready_to_ship'
        add_order_event(movement.order, 'trailer_received', new_value=movement.trailer.vin if movement.trailer else movement.trailer_id)
    _finish_idempotency(idem_key, 'StockMovement', movement.id)
    db.session.commit()
    flash('Прицеп принят на склад', 'success')
    if current_user.is_manager:
        return redirect(url_for('main.stock_movements_list', scope='needs_receive'))
    return redirect(url_for('main.stock_movements_list', scope='needs_receive'))


@main_bp.route('/orders/<int:order_id>/documents-issued', methods=['POST'])
@role_required('manager', 'director')
def order_documents_issued(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_access_order(order)
    flash('Выдача документов выполняется только через действие в карточке заказа.', 'warning')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/director')
@role_required('director')
def director_home():
    return redirect(url_for('main.director_dashboard'))


@main_bp.route('/director/dashboard')
@role_required('director')
def director_dashboard():
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    period = request.args.get('period', 'month')
    warehouse_id = request.args.get('warehouse_id', type=int)
    status_filter = (request.args.get('status') or 'all').strip()
    category_filter = (request.args.get('category') or 'all').strip()
    show_zero = request.args.get('show_zero') == '1'
    today = date.today()

    if period == 'day':
        date_from = today
        date_to = today
    elif period == 'week':
        date_from = today - timedelta(days=today.weekday())
        date_to = today
    elif period == 'year':
        date_from = today.replace(month=1, day=1)
        date_to = today
    elif period == 'custom':
        try:
            date_from = datetime.strptime(request.args.get('date_from') or '', '%Y-%m-%d').date()
        except ValueError:
            date_from = today.replace(day=1)
        try:
            date_to = datetime.strptime(request.args.get('date_to') or '', '%Y-%m-%d').date()
        except ValueError:
            date_to = today
    else:
        period = 'month'
        date_from = today.replace(day=1)
        date_to = today

    period_start = datetime.combine(date_from, time.min)
    period_end = datetime.combine(date_to, time.max)

    def scoped_order_query():
        query = CustomerOrder.query
        if warehouse_id:
            query = query.filter(CustomerOrder.warehouse_id == warehouse_id)
        return query

    def scoped_trailer_query():
        query = Trailer.query
        if warehouse_id:
            query = query.filter(Trailer.warehouse_id == warehouse_id)
        return query

    def scoped_movement_query():
        query = StockMovement.query
        if warehouse_id:
            query = query.filter(or_(StockMovement.from_warehouse_id == warehouse_id, StockMovement.to_warehouse_id == warehouse_id))
        return query

    def legal_sales_between(start_dt: datetime, end_dt: datetime):
        query = (
            scoped_order_query()
            .filter(
                CustomerOrder.documents_issued == True,
                CustomerOrder.documents_issued_at >= start_dt,
                CustomerOrder.documents_issued_at <= end_dt,
                CustomerOrder.status != 'cancelled',
            )
            .order_by(CustomerOrder.documents_issued_at.desc())
        )
        return [order for order in query.all() if get_order_effective_vin(order)]

    today_start = datetime.combine(today, time.min)
    today_end = datetime.combine(today, time.max)
    week_start = datetime.combine(today - timedelta(days=today.weekday()), time.min)
    sales_today = legal_sales_between(today_start, today_end)
    sales_week = legal_sales_between(week_start, today_end)
    sales_period = legal_sales_between(period_start, period_end)

    active_orders = scoped_order_query().filter(CustomerOrder.status.notin_(['done', 'cancelled', 'shipped', 'sold_not_shipped'])).all()
    reserved_count = scoped_trailer_query().filter(Trailer.status == 'RESERVED').count()
    free_count = len([
        trailer for trailer in scoped_trailer_query().filter(Trailer.status == 'IN_STOCK').all()
        if _trailer_available_for_sale(trailer)
    ])
    sold_not_shipped_orders = [
        order for order in scoped_order_query().filter(
            CustomerOrder.documents_issued == True,
            CustomerOrder.is_shipped == False,
            CustomerOrder.status != 'cancelled',
        ).order_by(CustomerOrder.documents_issued_at.desc().nullslast()).all()
        if get_order_effective_vin(order)
    ]
    sold_not_shipped_amount = sum(float(order.price or 0) for order in sold_not_shipped_orders)
    in_transit_movements = scoped_movement_query().filter(StockMovement.status.in_(['sent', 'in_transit'])).order_by(StockMovement.arrival_date.asc().nullslast()).all()
    shipped_period_count = scoped_order_query().filter(
        CustomerOrder.is_shipped == True,
        CustomerOrder.shipped_at >= period_start,
        CustomerOrder.shipped_at <= period_end,
    ).count()

    production_warehouse = _default_production_warehouse()
    active_lines_query = ProductionRequestLine.query.join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id).filter(
        ProductionRequestLine.status.in_(['planned', 'PLANNED', 'in_production', 'partial_ready'])
    )
    if warehouse_id:
        active_lines_query = active_lines_query.filter(ProductionRequest.target_warehouse_id == warehouse_id)
    active_lines = active_lines_query.all()
    production_ordered = sum(line.quantity or 0 for line in active_lines)
    production_left = sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in active_lines)
    production_customer_left = sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in active_lines if not _is_stock_replenishment_need(line.supply_need))
    production_stock_left = sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in active_lines if _is_stock_replenishment_need(line.supply_need))

    produced_no_vin_query = ProducedUnit.query.filter_by(status='produced_no_vin')
    if warehouse_id:
        produced_no_vin_query = produced_no_vin_query.filter(ProducedUnit.target_warehouse_id == warehouse_id)
    produced_no_vin_units = produced_no_vin_query.order_by(ProducedUnit.produced_at.asc().nullslast(), ProducedUnit.id.asc()).all()

    ready_with_vin_query = ProducedUnit.query.join(Trailer, Trailer.id == ProducedUnit.trailer_id).filter(
        ProducedUnit.status == 'vin_assigned',
        Trailer.status.in_(['IN_STOCK', 'RESERVED']),
        or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
    )
    if production_warehouse:
        ready_with_vin_query = ready_with_vin_query.filter(Trailer.warehouse_id == production_warehouse.id)
    if warehouse_id:
        ready_with_vin_query = ready_with_vin_query.filter(ProducedUnit.target_warehouse_id == warehouse_id)
    ready_with_vin_units = ready_with_vin_query.order_by(ProducedUnit.created_at.asc()).all()

    overdue_production = (
        active_lines_query
        .join(SupplyNeed, SupplyNeed.id == ProductionRequestLine.supply_need_id)
        .filter(SupplyNeed.required_by.isnot(None), SupplyNeed.required_by < today)
        .all()
    )
    overdue_movements = scoped_movement_query().filter(StockMovement.status.in_(['sent', 'in_transit']), StockMovement.arrival_date.isnot(None), StockMovement.arrival_date < today).all()

    unprocessed_leads = Lead.query.filter(Lead.status.in_(['NEW', 'IN_PROGRESS'])).filter(or_(Lead.assigned_user_id.is_(None), Lead.conversation_status.in_(['new', 'manager_needed']))).count()
    if warehouse_id:
        unprocessed_leads = Lead.query.filter(
            Lead.status.in_(['NEW', 'IN_PROGRESS']),
            or_(Lead.warehouse_id == warehouse_id, Lead.warehouse_id.is_(None)),
            or_(Lead.assigned_user_id.is_(None), Lead.conversation_status.in_(['new', 'manager_needed'])),
        ).count()
    later_orders = scoped_order_query().filter(
        CustomerOrder.trailer_id.is_(None),
        CustomerOrder.fulfillment_source == 'later',
        CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
    ).order_by(CustomerOrder.created_at.desc()).limit(10).all()
    production_without_trailer = scoped_order_query().filter(
        CustomerOrder.trailer_id.is_(None),
        CustomerOrder.fulfillment_source == 'production',
        CustomerOrder.documents_issued == False,
        CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
    ).order_by(CustomerOrder.created_at.desc()).limit(10).all()
    docs_issued_without_trailer = [
        order for order in scoped_order_query().filter(
            CustomerOrder.trailer_id.is_(None),
            CustomerOrder.documents_issued == True,
            CustomerOrder.status != 'cancelled',
        ).order_by(CustomerOrder.documents_issued_at.desc().nullslast(), CustomerOrder.created_at.desc()).limit(20).all()
        if get_order_effective_vin(order)
    ]
    vin_reserved_query = VinRegistry.query.join(CustomerOrder, CustomerOrder.id == VinRegistry.customer_order_id).filter(
        VinRegistry.status == 'reserved',
        VinRegistry.trailer_id.is_(None),
        CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
    )
    if warehouse_id:
        vin_reserved_query = vin_reserved_query.filter(CustomerOrder.warehouse_id == warehouse_id)
    vin_reserved_without_trailer = vin_reserved_query.order_by(VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc()).limit(20).all()
    paid_without_vin = [
        order for order in scoped_order_query().filter(
            CustomerOrder.documents_issued == False,
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        ).order_by(CustomerOrder.created_at.desc()).all()
        if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount and not get_order_effective_vin(order)
    ]
    vin_assigned_not_confirmed = VinRegistry.query.filter(
        VinRegistry.status == 'assigned',
        VinRegistry.confirmed_at.is_(None),
    ).order_by(VinRegistry.assigned_at.desc().nullslast(), VinRegistry.id.desc()).limit(20).all()
    if warehouse_id:
        vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if _vin_registry_order(row) and _vin_registry_order(row).warehouse_id == warehouse_id]
    orders_without_trailer = later_orders + production_without_trailer + docs_issued_without_trailer
    paid_without_contract = [
        order for order in scoped_order_query().filter(
            CustomerOrder.documents_issued == False,
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        ).order_by(CustomerOrder.created_at.desc()).all()
        if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount and not SalesContract.query.filter_by(order_id=order.id).first()
    ]
    contract_without_docs = (
        scoped_order_query()
        .join(SalesContract, SalesContract.order_id == CustomerOrder.id)
        .filter(
            CustomerOrder.documents_issued == False,
            CustomerOrder.status != 'cancelled',
        )
        .order_by(CustomerOrder.created_at.desc())
        .limit(10)
        .all()
    )
    sold_wrong_warehouse = [
        order for order in sold_not_shipped_orders
        if order.trailer and order.warehouse_id and order.trailer.warehouse_id != order.warehouse_id and order.trailer.lifecycle_status != 'in_transit'
    ][:10]
    stale_produced_no_vin = [
        unit for unit in produced_no_vin_units
        if unit.produced_at and unit.produced_at.date() < today
    ][:10]
    ready_not_sent = [
        unit for unit in ready_with_vin_units
        if unit.target_warehouse_id and production_warehouse and unit.target_warehouse_id != production_warehouse.id
    ][:10]

    problem_cards = [
        {'title': 'Заявки без ответа', 'count': unprocessed_leads, 'kind': 'lead'},
        {'title': 'Заказы без прицепа', 'count': len(orders_without_trailer), 'kind': 'order'},
        {'title': 'Оплачено, но нет договора', 'count': len(paid_without_contract), 'kind': 'contract'},
        {'title': 'Договор есть, документы не выданы', 'count': len(contract_without_docs), 'kind': 'docs'},
        {'title': 'Документы выданы, не отгружено', 'count': len(sold_not_shipped_orders), 'kind': 'shipment'},
        {'title': 'Продано, не на складе выдачи', 'count': len(sold_wrong_warehouse), 'kind': 'warehouse'},
        {'title': 'Перемещения просрочены', 'count': len(overdue_movements), 'kind': 'movement'},
        {'title': 'Производство просрочено', 'count': len(overdue_production), 'kind': 'production'},
        {'title': 'Выпущено без VIN со вчера', 'count': len(stale_produced_no_vin), 'kind': 'vin'},
        {'title': 'Готово с VIN, не отправлено', 'count': len(ready_not_sent), 'kind': 'ready'},
    ]

    items_query = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).outerjoin(ProductCategory, ProductCategory.id == Item.product_category_id)
    if category_filter and category_filter != 'all':
        items_query = items_query.filter(ProductCategory.code == category_filter)
    items = items_query.order_by(Item.article, Item.name).all()
    inventory_rows = []
    for item in items:
        row = {
            'item': item,
            'warehouses': {},
            'stock_total': 0,
            'in_production': 0,
            'in_production_customer': 0,
            'in_production_stock': 0,
            'produced_no_vin': 0,
            'ready_at_production': 0,
            'in_transit': 0,
            'in_transit_customer': 0,
            'in_transit_stock': 0,
            'reserved': 0,
            'sold': 0,
        }
        for warehouse in warehouses:
            if warehouse_id and warehouse.id != warehouse_id:
                row['warehouses'][warehouse.id] = 0
                continue
            count = len([
                trailer for trailer in Trailer.query.filter_by(item_id=item.id, warehouse_id=warehouse.id, status='IN_STOCK').all()
                if _trailer_available_for_sale(trailer)
            ])
            row['warehouses'][warehouse.id] = count
            row['stock_total'] += count
        item_active_lines = [line for line in active_lines if line.item_id == item.id]
        for line in item_active_lines:
            qty_left = max((line.quantity or 0) - (line.produced_qty or 0), 0)
            row['in_production'] += qty_left
            if _is_stock_replenishment_need(line.supply_need):
                row['in_production_stock'] += qty_left
            else:
                row['in_production_customer'] += qty_left
        row['produced_no_vin'] = len([unit for unit in produced_no_vin_units if unit.item_id == item.id])
        row['ready_at_production'] = len([unit for unit in ready_with_vin_units if unit.item_id == item.id])
        item_movements = [movement for movement in in_transit_movements if movement.trailer and movement.trailer.item_id == item.id]
        row['in_transit'] = len(item_movements)
        row['in_transit_customer'] = len([movement for movement in item_movements if movement.order_id])
        row['in_transit_stock'] = len([movement for movement in item_movements if not movement.order_id])
        row['reserved'] = scoped_trailer_query().filter_by(item_id=item.id, status='RESERVED').count()
        row['sold'] = scoped_trailer_query().filter_by(item_id=item.id, status='SOLD').filter(or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped')).count()
        row_total = row['stock_total'] + row['in_production'] + row['produced_no_vin'] + row['ready_at_production'] + row['in_transit'] + row['reserved'] + row['sold']
        if status_filter != 'all':
            status_metric = {
                'free': 'stock_total',
                'reserved': 'reserved',
                'sold_not_shipped': 'sold',
                'in_transit': 'in_transit',
                'in_production': 'in_production',
                'produced_no_vin': 'produced_no_vin',
                'ready_to_send': 'ready_at_production',
            }.get(status_filter)
            row_total = row.get(status_metric, row_total) if status_metric else row_total
        if show_zero or row_total > 0:
            inventory_rows.append(row)

    payment_pending = OrderPayment.query.filter_by(status='PENDING').order_by(OrderPayment.created_at.desc()).all()
    return render_template(
        'director_dashboard.html',
        warehouses=warehouses,
        warehouse_id=warehouse_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        show_zero=show_zero,
        category_filter=category_filter,
        categories=ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order, ProductCategory.name).all(),
        inventory_rows=inventory_rows,
        payment_pending=payment_pending,
        sales_today=sales_today,
        sales_week=sales_week,
        sales_period=sales_period,
        revenue_today=sum(float(order.price or 0) for order in sales_today),
        revenue_week=sum(float(order.price or 0) for order in sales_week),
        revenue_period=sum(float(order.price or 0) for order in sales_period),
        active_orders=active_orders,
        free_count=free_count,
        reserved_count=reserved_count,
        sold_not_shipped_orders=sold_not_shipped_orders,
        sold_not_shipped_amount=sold_not_shipped_amount,
        in_transit_movements=in_transit_movements,
        shipped_period_count=shipped_period_count,
        production_ordered=production_ordered,
        production_left=production_left,
        production_customer_left=production_customer_left,
        production_stock_left=production_stock_left,
        produced_no_vin_units=produced_no_vin_units,
        ready_with_vin_units=ready_with_vin_units,
        overdue_production=overdue_production,
        overdue_movements=overdue_movements,
        problem_cards=problem_cards,
        orders_without_trailer=orders_without_trailer,
        paid_without_contract=paid_without_contract[:10],
        contract_without_docs=contract_without_docs,
        sold_wrong_warehouse=sold_wrong_warehouse,
        stale_produced_no_vin=stale_produced_no_vin,
        ready_not_sent=ready_not_sent,
        later_orders=later_orders,
        production_without_trailer=production_without_trailer,
        docs_issued_without_trailer=docs_issued_without_trailer,
        vin_reserved_without_trailer=vin_reserved_without_trailer,
        paid_without_vin=paid_without_vin[:10],
        vin_assigned_not_confirmed=vin_assigned_not_confirmed,
    )


@main_bp.route('/director/reports')
@main_bp.route('/director/reports/<section>')
@main_bp.route('/reports')
@main_bp.route('/reports/<section>')
@role_required('director', 'manager')
def director_report(section='sales'):
    sections = {
        'sales': 'Продажи',
        'dynamics': 'Динамика',
        'branches': 'Филиалы',
        'types': 'Типы прицепов',
        'turnover': 'Оборачиваемость',
        'stock': 'Склад',
        'production': 'Производство',
        'movements': 'Перемещения',
        'problems': 'Проблемные заказы',
        'finance': 'Финансовая сводка',
    }
    if section not in sections:
        abort(404)

    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    managers = User.query.filter(User.role == 'manager').order_by(User.full_name, User.username).all()
    warehouse_id = request.args.get('warehouse_id', type=int)
    manager_id = request.args.get('manager_id', type=int)
    status_filter = (request.args.get('status') or 'all').strip()
    search = (request.args.get('q') or '').strip()
    report_direction = (request.args.get('direction') or 'all').strip()
    period = request.args.get('period', 'month')
    today = date.today()

    if period == 'day':
        date_from = today
        date_to = today
    elif period == 'week':
        date_from = today - timedelta(days=today.weekday())
        date_to = today
    elif period == 'year':
        date_from = today.replace(month=1, day=1)
        date_to = today
    elif period == 'custom':
        try:
            date_from = datetime.strptime(request.args.get('date_from') or '', '%Y-%m-%d').date()
        except ValueError:
            date_from = today.replace(day=1)
        try:
            date_to = datetime.strptime(request.args.get('date_to') or '', '%Y-%m-%d').date()
        except ValueError:
            date_to = today
    else:
        period = 'month'
        date_from = today.replace(day=1)
        date_to = today

    period_start = datetime.combine(date_from, time.min)
    period_end = datetime.combine(date_to, time.max)

    def order_scope(query):
        if current_user.is_manager:
            query = query.filter(or_(
                CustomerOrder.assigned_user_id == current_user.id,
                CustomerOrder.created_by_user_id == current_user.id,
            ))
        if warehouse_id:
            query = query.filter(CustomerOrder.warehouse_id == warehouse_id)
        if manager_id:
            query = query.filter(CustomerOrder.assigned_user_id == manager_id)
        return query

    def record_direction(record: dict) -> str:
        lines = record.get('lines') or []
        article = None
        if lines:
            article = next(
                (
                    line.article_snapshot or (line.item.article if line.item else None)
                    for line in lines
                    if line.article_snapshot or (line.item and line.item.article)
                ),
                None,
            )
        if not article and record.get('item'):
            article = record['item'].article
        group_code = _article_group_code(article)
        vin = record.get('trailer').vin if record.get('trailer') else None
        if not vin or not group_code:
            return 'other'
        if group_code == '002':
            return 'light'
        return 'cargo'

    def contract_report_records():
        contracts = (
            SalesContract.query
            .outerjoin(CustomerOrder, CustomerOrder.id == SalesContract.order_id)
            .outerjoin(Trailer, Trailer.id == SalesContract.trailer_id)
            .outerjoin(Customer, Customer.id == SalesContract.customer_id)
            .order_by(SalesContract.contract_date.desc().nullslast(), SalesContract.id.desc())
            .all()
        )
        result = []
        search_lower = search.lower()
        for contract in contracts:
            order = contract.order
            if order and order.status == 'cancelled':
                continue
            sale_dt = None
            if order and order.documents_issued_at:
                sale_dt = order.documents_issued_at
            elif contract.contract_date:
                sale_dt = datetime.combine(contract.contract_date, time.min)
            elif contract.created_at:
                sale_dt = contract.created_at
            if not sale_dt or sale_dt < period_start or sale_dt > period_end:
                continue

            trailer = contract.trailer or (order.trailer if order else None)
            warehouse = (
                contract.warehouse if contract.warehouse
                else order.warehouse if order and order.warehouse
                else trailer.warehouse if trailer else None
            )
            manager = (
                contract.assigned_user if contract.assigned_user
                else order.assigned_user if order and order.assigned_user
                else None
            )
            if current_user.is_manager:
                allowed_by_order = order and (
                    order.assigned_user_id == current_user.id
                    or getattr(order, 'created_by_user_id', None) == current_user.id
                )
                allowed_by_trailer = False
                if not (allowed_by_order or allowed_by_trailer):
                    continue
            if warehouse_id and (not warehouse or warehouse.id != warehouse_id):
                continue
            if manager_id and (not manager or manager.id != manager_id):
                continue

            lines = contract.lines.order_by(SalesContractLine.line_no.asc(), SalesContractLine.id.asc()).all()
            item = trailer.item if trailer else (order.item if order else None)
            vin_text = get_order_effective_vin(order) if order else (trailer.vin if trailer else '')
            article_text = ' '.join(
                filter(None, [
                    item.article if item else None,
                    *(line.article_snapshot or (line.item.article if line.item else '') for line in lines),
                ])
            )
            haystack = ' '.join(
                str(value or '')
                for value in (
                    contract.contract_number,
                    contract.customer.name if contract.customer else '',
                    vin_text,
                    article_text,
                    warehouse.name if warehouse else '',
                )
            ).lower()
            if search_lower and search_lower not in haystack:
                continue

            quantity = sum(line.quantity or 0 for line in lines)
            if quantity <= 0:
                quantity = order.quantity if order and order.quantity else 1
            revenue = float(contract.price if contract.price is not None else (order.price if order else 0) or 0)
            record = {
                'date': sale_dt,
                'contract': contract,
                'order': order,
                'trailer': trailer,
                'warehouse': warehouse,
                'manager': manager,
                'item': item,
                'lines': lines,
                'quantity': quantity,
                'revenue': revenue,
            }
            record['direction'] = record_direction(record)
            if report_direction != 'all' and record['direction'] != report_direction:
                continue
            result.append(record)
        return result

    cards = []
    rows = []
    problem_rows = []
    analytics_rows = []
    anomaly_rows = []

    if section in ('sales', 'finance', 'dynamics', 'branches', 'types'):
        rows = contract_report_records()
        anomaly_rows = [
            row for row in rows
            if row['revenue'] >= 3000000 or row['direction'] == 'other'
        ][:10]
        sold_quantity = sum(row['quantity'] or 0 for row in rows)
        revenue = sum(row['revenue'] or 0 for row in rows)
        cards = [
            {'title': 'Выручка', 'value': money(revenue), 'caption': 'юридические продажи за период'},
            {'title': 'Продано', 'value': sold_quantity, 'caption': 'шт. по договорам'},
            {'title': 'Средний чек', 'value': money(revenue / len(rows) if rows else 0), 'caption': 'по закрытым продажам'},
            {'title': 'Резервы', 'value': order_scope(CustomerOrder.query).filter(CustomerOrder.status.in_(['reserved', 'waiting_payment', 'prepaid', 'ready_to_ship'])).count(), 'caption': 'активные заказы'},
        ]
        if section in ('sales', 'dynamics'):
            buckets = defaultdict(lambda: {'orders': 0, 'quantity': 0, 'revenue': 0.0})
            for row in rows:
                key = row['date'].strftime('%Y-%m') if row.get('date') else 'без даты'
                buckets[key]['orders'] += 1
                buckets[key]['quantity'] += row['quantity'] or 0
                buckets[key]['revenue'] += row['revenue'] or 0
            analytics_rows = [
                {
                    'label': key,
                    'orders': value['orders'],
                    'quantity': value['quantity'],
                    'revenue': value['revenue'],
                    'average': value['revenue'] / value['orders'] if value['orders'] else 0,
                }
                for key, value in sorted(buckets.items())
            ]
        elif section == 'branches':
            buckets = defaultdict(lambda: {'orders': 0, 'quantity': 0, 'revenue': 0.0})
            for row in rows:
                key = row['warehouse'].name if row.get('warehouse') else 'Без филиала'
                buckets[key]['orders'] += 1
                buckets[key]['quantity'] += row['quantity'] or 0
                buckets[key]['revenue'] += row['revenue'] or 0
            analytics_rows = sorted(
                ({'label': key, **value, 'average': value['revenue'] / value['orders'] if value['orders'] else 0} for key, value in buckets.items()),
                key=lambda row: row['revenue'],
                reverse=True,
            )
        elif section == 'types':
            buckets = defaultdict(lambda: {'orders': 0, 'quantity': 0, 'revenue': 0.0})
            for row in rows:
                lines = row.get('lines') or []
                if lines:
                    line_count = len(lines)
                    for line in lines:
                        label = _article_group_code(line.article_snapshot or (line.item.article if line.item else None)) or 'Без типа'
                        buckets[label]['orders'] += 1
                        buckets[label]['quantity'] += line.quantity or 1
                        buckets[label]['revenue'] += float(line.total_price if line.total_price is not None else ((row['revenue'] or 0) / line_count if line_count else 0))
                else:
                    item = row.get('item')
                    label = _article_group_code(item.article if item else None) or 'Без типа'
                    buckets[label]['orders'] += 1
                    buckets[label]['quantity'] += row['quantity'] or 0
                    buckets[label]['revenue'] += row['revenue'] or 0
            analytics_rows = sorted(
                ({'label': key, **value, 'average': value['revenue'] / value['orders'] if value['orders'] else 0} for key, value in buckets.items()),
                key=lambda row: row['quantity'],
                reverse=True,
            )
        if section == 'finance':
            rows = (
                OrderPayment.query
                .join(CustomerOrder, CustomerOrder.id == OrderPayment.order_id)
                .filter(OrderPayment.paid_at >= period_start, OrderPayment.paid_at <= period_end)
            )
            if warehouse_id:
                rows = rows.filter(CustomerOrder.warehouse_id == warehouse_id)
            if manager_id:
                rows = rows.filter(CustomerOrder.assigned_user_id == manager_id)
            if status_filter != 'all':
                rows = rows.filter(OrderPayment.status == status_filter)
            rows = rows.order_by(OrderPayment.paid_at.desc().nullslast(), OrderPayment.id.desc()).limit(300).all()
            confirmed_sum = sum(float(payment.amount or 0) for payment in rows if payment.status == 'CONFIRMED')
            cards = [
                {'title': 'Подтверждено', 'value': money(confirmed_sum), 'caption': 'оплаты за период'},
                {'title': 'Оплат всего', 'value': len(rows), 'caption': 'операций'},
                {'title': 'На проверке', 'value': len([p for p in rows if p.status == 'PENDING']), 'caption': 'PENDING'},
                {'title': 'Отменено', 'value': len([p for p in rows if p.status == 'CANCELED']), 'caption': 'CANCELED'},
            ]

    elif section in ('stock', 'turnover'):
        query = Trailer.query.join(Item, Item.id == Trailer.item_id)
        if current_user.is_manager and current_user.warehouse_id:
            query = query.filter(Trailer.warehouse_id == current_user.warehouse_id)
        if warehouse_id:
            query = query.filter(Trailer.warehouse_id == warehouse_id)
        if search:
            like = f'%{search}%'
            query = query.filter(or_(Trailer.vin.ilike(like), Item.article.ilike(like), Item.name.ilike(like)))
        trailers = query.order_by(Trailer.id.desc()).limit(500).all()
        if status_filter == 'free':
            rows = [trailer for trailer in trailers if _trailer_available_for_sale(trailer)]
        elif status_filter == 'reserved':
            rows = [trailer for trailer in trailers if trailer.status == 'RESERVED']
        elif status_filter == 'sold_not_shipped':
            rows = [trailer for trailer in trailers if trailer.status == 'SOLD' and not _trailer_is_customer_shipped(trailer)]
        elif status_filter == 'in_transit':
            rows = [trailer for trailer in trailers if trailer.status == 'IN_TRANSIT' or trailer.lifecycle_status == 'in_transit']
        elif status_filter == 'shipped':
            rows = [trailer for trailer in trailers if _trailer_is_customer_shipped(trailer)]
        else:
            rows = trailers
        cards = [
            {'title': 'Свободно', 'value': len([t for t in trailers if _trailer_available_for_sale(t)]), 'caption': 'доступно к продаже'},
            {'title': 'В резерве', 'value': len([t for t in trailers if t.status == 'RESERVED']), 'caption': 'занято заказами'},
            {'title': 'Продано, не отгружено', 'value': len([t for t in trailers if t.status == 'SOLD' and not _trailer_is_customer_shipped(t)]), 'caption': 'контроль выдачи'},
            {'title': 'В пути', 'value': len([t for t in trailers if t.status == 'IN_TRANSIT' or t.lifecycle_status == 'in_transit']), 'caption': 'логистика'},
        ]
        if section == 'turnover':
            sold_records = contract_report_records()
            period_days = max((date_to - date_from).days + 1, 1)
            stock_buckets = defaultdict(lambda: {'stock': 0, 'sold': 0, 'revenue': 0.0})
            for trailer in trailers:
                label = _article_group_code(trailer.item.article if trailer.item else None) or 'Без типа'
                stock_buckets[label]['stock'] += 1
            for record in sold_records:
                lines = record.get('lines') or []
                if lines:
                    line_count = len(lines)
                    for line in lines:
                        label = _article_group_code(line.article_snapshot or (line.item.article if line.item else None)) or 'Без типа'
                        stock_buckets[label]['sold'] += line.quantity or 1
                        stock_buckets[label]['revenue'] += float(line.total_price if line.total_price is not None else ((record['revenue'] or 0) / line_count if line_count else 0))
                else:
                    item = record.get('item')
                    label = _article_group_code(item.article if item else None) or 'Без типа'
                    stock_buckets[label]['sold'] += record['quantity'] or 0
                    stock_buckets[label]['revenue'] += record['revenue'] or 0
            analytics_rows = []
            for label, value in stock_buckets.items():
                avg_stock = value['stock']
                sold_qty = value['sold']
                turnover = sold_qty / avg_stock if avg_stock else 0
                days_on_stock = round(period_days / turnover, 1) if turnover else None
                analytics_rows.append({
                    'label': label,
                    'stock': avg_stock,
                    'quantity': sold_qty,
                    'revenue': value['revenue'],
                    'turnover': turnover,
                    'days_on_stock': days_on_stock,
                })
            analytics_rows.sort(key=lambda row: row['turnover'], reverse=True)
            rows = trailers
            cards = [
                {'title': 'Остаток', 'value': len(trailers), 'caption': 'прицепов в выборке'},
                {'title': 'Продано', 'value': sum(row['quantity'] for row in analytics_rows), 'caption': 'за период'},
                {'title': 'Оборачиваемость', 'value': round((sum(row['quantity'] for row in analytics_rows) / len(trailers)) if trailers else 0, 2), 'caption': 'продано / остаток'},
                {'title': 'Период', 'value': period_days, 'caption': 'дней'},
            ]

    elif section == 'production':
        query = ProductionRequestLine.query.join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        if current_user.is_manager and current_user.warehouse_id:
            query = query.filter(ProductionRequest.target_warehouse_id == current_user.warehouse_id)
        if warehouse_id:
            query = query.filter(ProductionRequest.target_warehouse_id == warehouse_id)
        if status_filter != 'all':
            query = query.filter(ProductionRequestLine.status == status_filter)
        rows = query.order_by(ProductionRequest.created_at.desc(), ProductionRequestLine.id.desc()).limit(300).all()
        active_rows = [line for line in rows if (line.status or '').lower() not in ('ready', 'closed', 'cancelled', 'canceled')]
        produced_no_vin = ProducedUnit.query.filter_by(status='produced_no_vin')
        if warehouse_id:
            produced_no_vin = produced_no_vin.filter(ProducedUnit.target_warehouse_id == warehouse_id)
        cards = [
            {'title': 'Заказано', 'value': sum(line.quantity or 0 for line in active_rows), 'caption': 'активное производство'},
            {'title': 'Осталось выпустить', 'value': sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in active_rows), 'caption': 'по активным строкам'},
            {'title': 'Выпущено без VIN', 'value': produced_no_vin.count(), 'caption': 'ждёт логиста'},
            {'title': 'Просрочено', 'value': len([line for line in active_rows if line.supply_need and line.supply_need.required_by and line.supply_need.required_by < today]), 'caption': 'по сроку'},
        ]

    elif section == 'movements':
        query = StockMovement.query.filter(StockMovement.created_at >= period_start, StockMovement.created_at <= period_end)
        if current_user.is_manager and current_user.warehouse_id:
            query = query.filter(or_(StockMovement.from_warehouse_id == current_user.warehouse_id, StockMovement.to_warehouse_id == current_user.warehouse_id))
        if warehouse_id:
            query = query.filter(or_(StockMovement.from_warehouse_id == warehouse_id, StockMovement.to_warehouse_id == warehouse_id))
        if status_filter != 'all':
            query = query.filter(StockMovement.status == status_filter)
        if search:
            like = f'%{search}%'
            query = query.join(Trailer, Trailer.id == StockMovement.trailer_id, isouter=True).join(Item, Item.id == Trailer.item_id, isouter=True).filter(
                or_(Trailer.vin.ilike(like), Item.article.ilike(like), Item.name.ilike(like), StockMovement.batch_key.ilike(like))
            )
        rows = query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(500).all()
        cards = [
            {'title': 'Создано', 'value': len(rows), 'caption': 'строк перемещения'},
            {'title': 'В пути', 'value': len([m for m in rows if m.status in ('sent', 'in_transit')]), 'caption': 'не принято'},
            {'title': 'Принято', 'value': len([m for m in rows if m.status == 'arrived']), 'caption': 'закрыто складом'},
            {'title': 'Просрочено', 'value': len([m for m in rows if m.status in ('sent', 'in_transit') and m.arrival_date and m.arrival_date < today]), 'caption': 'по ожидаемой дате'},
        ]

    else:
        later_orders = order_scope(CustomerOrder.query).filter(
            CustomerOrder.trailer_id.is_(None),
            CustomerOrder.fulfillment_source == 'later',
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        ).order_by(CustomerOrder.created_at.desc()).all()
        production_without_trailer = order_scope(CustomerOrder.query).filter(
            CustomerOrder.trailer_id.is_(None),
            CustomerOrder.fulfillment_source == 'production',
            CustomerOrder.documents_issued == False,
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        ).order_by(CustomerOrder.created_at.desc()).all()
        docs_issued_without_trailer = [
            order for order in order_scope(CustomerOrder.query).filter(
                CustomerOrder.trailer_id.is_(None),
                CustomerOrder.documents_issued == True,
                CustomerOrder.status != 'cancelled',
            ).order_by(CustomerOrder.documents_issued_at.desc().nullslast()).all()
            if get_order_effective_vin(order)
        ]
        vin_reserved_without_trailer = VinRegistry.query.join(CustomerOrder, CustomerOrder.id == VinRegistry.customer_order_id).filter(
            VinRegistry.status == 'reserved',
            VinRegistry.trailer_id.is_(None),
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        )
        if warehouse_id:
            vin_reserved_without_trailer = vin_reserved_without_trailer.filter(CustomerOrder.warehouse_id == warehouse_id)
        if manager_id:
            vin_reserved_without_trailer = vin_reserved_without_trailer.filter(CustomerOrder.assigned_user_id == manager_id)
        vin_reserved_without_trailer = vin_reserved_without_trailer.order_by(VinRegistry.reserved_at.desc().nullslast()).all()
        paid_without_vin = [
            order for order in order_scope(CustomerOrder.query).filter(
                CustomerOrder.documents_issued == False,
                CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
            ).order_by(CustomerOrder.created_at.desc()).all()
            if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount and not get_order_effective_vin(order)
        ]
        vin_assigned_not_confirmed = VinRegistry.query.filter(
            VinRegistry.status == 'assigned',
            VinRegistry.confirmed_at.is_(None),
        ).order_by(VinRegistry.assigned_at.desc().nullslast()).all()
        if warehouse_id:
            vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if _vin_registry_order(row) and _vin_registry_order(row).warehouse_id == warehouse_id]
        if manager_id:
            vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if _vin_registry_order(row) and _vin_registry_order(row).assigned_user_id == manager_id]
        paid_without_contract = [
            order for order in order_scope(CustomerOrder.query).filter(
                CustomerOrder.documents_issued == False,
                CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
            ).order_by(CustomerOrder.created_at.desc()).all()
            if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount and not SalesContract.query.filter_by(order_id=order.id).first()
        ]
        contract_without_docs = (
            order_scope(CustomerOrder.query)
            .join(SalesContract, SalesContract.order_id == CustomerOrder.id)
            .filter(CustomerOrder.documents_issued == False, CustomerOrder.status != 'cancelled')
            .order_by(CustomerOrder.created_at.desc())
            .all()
        )
        sold_not_shipped = order_scope(CustomerOrder.query).filter(
            CustomerOrder.documents_issued == True,
            CustomerOrder.is_shipped == False,
            CustomerOrder.status != 'cancelled',
        ).order_by(CustomerOrder.documents_issued_at.desc().nullslast()).all()
        overdue_movements = StockMovement.query.filter(
            StockMovement.status.in_(['sent', 'in_transit']),
            StockMovement.arrival_date.isnot(None),
            StockMovement.arrival_date < today,
        )
        if warehouse_id:
            overdue_movements = overdue_movements.filter(or_(StockMovement.from_warehouse_id == warehouse_id, StockMovement.to_warehouse_id == warehouse_id))
        overdue_movements = overdue_movements.order_by(StockMovement.arrival_date.asc()).all()
        overdue_production = (
            ProductionRequestLine.query
            .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
            .join(SupplyNeed, SupplyNeed.id == ProductionRequestLine.supply_need_id)
            .filter(
                ProductionRequestLine.status.notin_(['ready', 'closed', 'cancelled', 'canceled']),
                SupplyNeed.required_by.isnot(None),
                SupplyNeed.required_by < today,
            )
        )
        if warehouse_id:
            overdue_production = overdue_production.filter(ProductionRequest.target_warehouse_id == warehouse_id)
        overdue_production = overdue_production.order_by(SupplyNeed.required_by.asc()).all()
        problem_rows = [
            {'type': 'Подобрать позже', 'items': later_orders},
            {'type': 'В производстве без Trailer', 'items': production_without_trailer},
            {'type': 'Документы выданы, Trailer не привязан', 'items': docs_issued_without_trailer},
            {'type': 'VIN зарезервирован, Trailer не привязан', 'items': vin_reserved_without_trailer},
            {'type': 'Оплачено, VIN не зарезервирован', 'items': paid_without_vin},
            {'type': 'VIN назначен, не подтверждён', 'items': vin_assigned_not_confirmed},
            {'type': 'Оплачено, но нет договора', 'items': paid_without_contract},
            {'type': 'Договор есть, документы не выданы', 'items': contract_without_docs},
            {'type': 'Документы выданы, не отгружено', 'items': sold_not_shipped},
            {'type': 'Перемещение просрочено', 'items': overdue_movements},
            {'type': 'Производство просрочено', 'items': overdue_production},
        ]
        cards = [
            {'title': 'Без договора', 'value': len(paid_without_contract), 'caption': 'полная оплата есть'},
            {'title': 'Без документов', 'value': len(contract_without_docs), 'caption': 'договор создан'},
            {'title': 'Не отгружено', 'value': len(sold_not_shipped), 'caption': 'юридически продано'},
            {'title': 'Просрочки', 'value': len(overdue_movements) + len(overdue_production), 'caption': 'логистика + производство'},
        ]

    chart_data = {'labels': [], 'revenue': [], 'quantity': [], 'average': [], 'stock': [], 'turnover': []}
    if section == 'sales':
        chart_data['labels'] = [row['label'] for row in analytics_rows]
        chart_data['revenue'] = [round(row['revenue'], 2) for row in analytics_rows]
        chart_data['quantity'] = [row['quantity'] for row in analytics_rows]
        chart_data['average'] = [round(row['average'], 2) for row in analytics_rows]
    elif section in ('dynamics', 'branches', 'types'):
        chart_data['labels'] = [row['label'] for row in analytics_rows]
        chart_data['revenue'] = [round(row['revenue'], 2) for row in analytics_rows]
        chart_data['quantity'] = [row['quantity'] for row in analytics_rows]
        chart_data['average'] = [round(row['average'], 2) for row in analytics_rows]
    elif section == 'turnover':
        chart_data['labels'] = [row['label'] for row in analytics_rows]
        chart_data['stock'] = [row['stock'] for row in analytics_rows]
        chart_data['quantity'] = [row['quantity'] for row in analytics_rows]
        chart_data['revenue'] = [round(row['revenue'], 2) for row in analytics_rows]
        chart_data['turnover'] = [round(row['turnover'], 2) for row in analytics_rows]

    return render_template(
        'director_report.html',
        section=section,
        sections=sections,
        title=sections[section],
        warehouses=warehouses,
        managers=managers,
        warehouse_id=warehouse_id,
        manager_id=manager_id,
        status_filter=status_filter,
        report_direction=report_direction,
        search=search,
        period=period,
        date_from=date_from,
        date_to=date_to,
        cards=cards,
        rows=rows,
        problem_rows=problem_rows,
        analytics_rows=analytics_rows,
        chart_data=chart_data,
        anomaly_rows=anomaly_rows,
    )


def _payload_text(payload: dict) -> str:
    for key in ('text', 'message', 'comment', 'body'):
        value = payload.get(key)
        if value:
            return str(value)
    return ''


def _create_lead_from_payload(channel: str, payload: dict):
    channel = (channel or payload.get('channel') or 'other').lower()
    name = payload.get('name') or payload.get('customer_name') or payload.get('username') or 'Новый контакт'
    phone = payload.get('phone') or payload.get('contact')
    username = payload.get('username') or payload.get('messenger_username')
    chat_id = payload.get('chat_id') or payload.get('external_chat_id') or payload.get('from')
    external_lead_id = payload.get('lead_id') or payload.get('external_lead_id')
    external_message_id = payload.get('message_id') or payload.get('external_message_id')
    text = _payload_text(payload)

    phone = str(phone).strip() if phone else None
    chat_id = str(chat_id).strip() if chat_id else None
    external_message_id = str(external_message_id).strip() if external_message_id else None

    if external_message_id:
        existing_message = LeadMessage.query.filter_by(
            channel=channel,
            external_message_id=external_message_id,
        ).first()
        if existing_message:
            current_app.logger.info('duplicate %s webhook message_id=%s lead_id=%s', channel, external_message_id, existing_message.lead_id)
            return existing_message.lead, existing_message, False

    lead = Lead.query.filter_by(channel=channel, external_chat_id=chat_id).first() if chat_id else None
    if not lead and phone:
        lead = Lead.query.filter_by(phone=phone).order_by(Lead.last_message_at.desc().nullslast(), Lead.id.desc()).first()

    if not lead:
        lead = Lead(
            channel=channel,
            source_channel=channel.upper(),
            source_name=payload.get('source_name') or channel,
            source_platform=payload.get('source_platform') or channel,
            external_chat_id=chat_id,
            external_lead_id=str(external_lead_id) if external_lead_id else None,
            source_payload=json.dumps(payload, ensure_ascii=False),
            customer_name=str(name),
            phone=phone,
            messenger_username=username,
            text=text or None,
            customer_city=payload.get('city') or payload.get('customer_city'),
            interest_text=payload.get('interest_text') or payload.get('interest') or text or None,
            status='NEW',
            conversation_status='new',
            unread_count=0,
        )
        db.session.add(lead)
        db.session.flush()
    else:
        if name and lead.customer_name == 'Новый контакт':
            lead.customer_name = str(name)
        if phone and not lead.phone:
            lead.phone = phone
        if username and not lead.messenger_username:
            lead.messenger_username = username
        if chat_id and not lead.external_chat_id:
            lead.external_chat_id = chat_id
        if external_lead_id and not lead.external_lead_id:
            lead.external_lead_id = str(external_lead_id)
        if payload.get('city') and not lead.customer_city:
            lead.customer_city = payload.get('city')
        if text and not lead.interest_text:
            lead.interest_text = text
        lead.source_payload = json.dumps(payload, ensure_ascii=False)

    message = LeadMessage(
        lead_id=lead.id,
        direction='IN',
        sender_type='client',
        channel=channel,
        external_message_id=external_message_id,
        sender_name=str(name) if name else None,
        sender_contact=str(phone or chat_id) if (phone or chat_id) else None,
        text=text or None,
        payload=json.dumps(payload, ensure_ascii=False),
        payload_json=json.dumps(payload, ensure_ascii=False),
        is_read=False,
    )
    db.session.add(message)
    now = datetime.utcnow()
    lead.last_message_at = now
    lead.last_message_text = text or lead.last_message_text
    lead.unread_count = (lead.unread_count or 0) + 1
    if lead.conversation_status == 'closed':
        lead.conversation_status = 'new'
    elif lead.assigned_user_id:
        lead.conversation_status = 'manager_needed'
    else:
        lead.conversation_status = 'new'
    # TODO: здесь будет вызов ИИ-менеджера для автоответа или передачи менеджеру.
    db.session.commit()
    current_app.logger.info('incoming %s webhook lead_id=%s payload=%s', channel, lead.id, payload)
    return lead, message, True


def _webhook_response(channel: str):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        current_app.logger.warning('invalid %s webhook payload: %r', channel, payload)
        return jsonify({'ok': False, 'error': 'json object expected'}), 400
    lead, message, created = _create_lead_from_payload(channel, payload)
    return jsonify({'ok': True, 'lead_id': lead.id, 'message_id': message.id, 'created': created})


@main_bp.route('/webhooks/site', methods=['POST'])
@csrf.exempt
def webhook_site():
    return _webhook_response('website')


@main_bp.route('/webhooks/telegram', methods=['POST'])
@csrf.exempt
def webhook_telegram():
    return _webhook_response('telegram')


@main_bp.route('/webhooks/instagram', methods=['POST'])
@csrf.exempt
def webhook_instagram():
    return _webhook_response('instagram')


@main_bp.route('/webhooks/whatsapp', methods=['POST'])
@csrf.exempt
def webhook_whatsapp():
    return _webhook_response('whatsapp')
