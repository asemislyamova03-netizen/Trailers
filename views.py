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
from models import Trailer, Item, Warehouse, Customer, SalesContract, User, OTTS, Lead, LeadMessage, CustomerOrder, OrderPayment, OrderEvent, Reservation, SupplyNeed, ProductionRequest, ProductionRequestLine, ProducedUnit, StockMovement, IdempotencyKey, VinRegistry, VinRegistryEvent
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
    AssignVinForm, SendTrailerForm, StockReplenishmentForm
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

from sigex_client import sigex_post_json, sigex_get_json, sigex_post_octet
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


def _production_warehouses():
    return (
        Warehouse.query
        .filter_by(is_active=True, is_production=True)
        .order_by(Warehouse.name)
        .all()
    )


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
    return current_user.is_manager and current_user.warehouse_id == movement.to_warehouse_id


def can_ship_order(order: CustomerOrder) -> bool:
    if current_user.is_admin:
        return True
    return current_user.is_manager and current_user.warehouse_id and order.warehouse_id == current_user.warehouse_id


def can_access_order(order: CustomerOrder) -> bool:
    if current_user.is_admin or current_user.is_director:
        return True
    if current_user.is_manager:
        return (
            current_user.warehouse_id and order.warehouse_id == current_user.warehouse_id
        ) or order.assigned_user_id == current_user.id
    if current_user.is_viewer:
        return True
    return False


def can_manage_order(order: CustomerOrder) -> bool:
    if current_user.is_admin:
        return True
    return current_user.is_manager and (
        (current_user.warehouse_id and order.warehouse_id == current_user.warehouse_id)
        or order.assigned_user_id == current_user.id
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
        return bool(current_user.warehouse_id and contract.trailer and contract.trailer.warehouse_id == current_user.warehouse_id)
    return False


def can_manage_contract(contract: SalesContract) -> bool:
    if current_user.is_admin:
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
    return {
        'unit': unit,
        'line': line,
        'need': need,
        'order': order,
        'need_type': _need_type_key(need),
        'target_warehouse': unit.target_warehouse if unit else None,
        'vin_registry': vin_row,
        'vin_to_apply': vin_row.vin_full if vin_row else None,
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


def _trailer_available_for_sale(trailer: Trailer, exclude_order_id: int | None = None) -> bool:
    if not trailer or trailer.status != 'IN_STOCK' or _trailer_is_customer_shipped(trailer):
        return False
    return _active_reservation_for_trailer(trailer.id, exclude_order_id=exclude_order_id) is None


def _active_movement_for_trailer(trailer_id: int, exclude_movement_id: int | None = None):
    q = StockMovement.query.filter(
        StockMovement.trailer_id == trailer_id,
        StockMovement.status.in_(['sent', 'in_transit']),
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
        return redirect(url_for('main.logistics_workspace'))
    return redirect(fallback_url or request.referrer or url_for('main.role_home'))


@main_bp.app_template_filter('status_label')
def status_label(value):
    labels = {'draft': 'Черновик', 'new': 'Новая', 'waiting_payment': 'Ждём оплату', 'prepaid': 'Предоплата', 'confirmed': 'Подтверждён', 'waiting_production': 'Ожидает производства', 'in_production': 'В производстве', 'produced_waiting_vin': 'Выпущен, ждёт VIN', 'waiting_transfer': 'Ждёт отправки', 'in_transit': 'В пути', 'arrived': 'Прибыл', 'ready_to_ship': 'Готов к выдаче', 'sold_not_shipped': 'Продан, не отгружен', 'customer_shipped': 'Физически отгружен клиенту', 'shipped': 'Отгружен', 'done': 'Завершён', 'cancelled': 'Отменён', 'canceled': 'Отменён', 'produced_no_vin': 'Выпущен без VIN', 'vin_assigned': 'VIN присвоен', 'planned': 'Запланирована', 'partial_ready': 'Частично выпущена', 'ready': 'Готово', 'closed': 'Закрыта', 'sent': 'Отправлено', 'in_progress': 'В работе', 'approved': 'Утверждена', 'ready_production_warehouse': 'Готов на складе выпуска', 'stock': 'Из наличия', 'other_warehouse': 'С другого склада', 'production': 'Под производство', 'transit': 'В пути', 'free': 'Свободен', 'reserved': 'Зарезервирован', 'assigned': 'Назначен', 'void': 'Аннулирован', 'not_started': 'Не начаты', 'invoice_sent': 'Счёт отправлен', 'contract_ready': 'Договор готов', 'documents_ready': 'Документы готовы', 'documents_issued': 'Документы выданы', 'unpaid': 'Не оплачено', 'partial': 'Частичная оплата', 'paid': 'Оплачено', 'order_created': 'Заказ создан', 'order_status_changed': 'Статус заказа изменён', 'payment_added': 'Оплата добавлена', 'payment_cancelled': 'Оплата отменена', 'trailer_reserved': 'Прицеп зарезервирован', 'trailer_assigned': 'Прицеп назначен', 'production_need_created': 'Создана потребность', 'production_started': 'Производство начато', 'produced_without_vin': 'Выпущено без VIN', 'transfer_requested': 'Запрошено перемещение', 'transfer_started': 'Перемещение начато', 'trailer_received': 'Прицеп принят', 'reservation_cancelled': 'Резерв отменён', 'uploaded': 'Загружен', 'assigned': 'Назначен', 'voided': 'Аннулирован', 'comment_added': 'Комментарий добавлен'}
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
        'ready_production_warehouse': 'Готов на складе выпуска',
        'in_stock': 'В наличии',
        'reserved': 'В резерве',
        'sold': 'Продан',
        'decommissioned': 'Списан',
    })
    normalized = str(value or '').lower()
    return labels.get(normalized, value or '')


@main_bp.app_template_filter('status_badge_class')
def status_badge_class(value):
    value = (value or '').lower()
    if value in ('draft', 'new', 'planned', 'manual', 'website', 'phone', 'other', 'customer_order'):
        return 'secondary'
    if value in ('in_progress', 'in_production', 'sent', 'in_transit', 'manager_handling', 'telegram'):
        return 'primary'
    if value in ('waiting_payment', 'waiting_production', 'waiting_transfer', 'produced_waiting_vin', 'produced_no_vin', 'invoice_sent', 'partial', 'not_started', 'manager_needed', 'waiting_client'):
        return 'warning'
    if value in ('prepaid', 'partial_ready', 'ai_handling', 'whatsapp', 'instagram', 'stock_replenishment', 'warehouse_stock'):
        return 'info'
    if value in ('ready', 'arrived', 'ready_to_ship', 'sold_not_shipped', 'customer_shipped', 'done', 'vin_assigned', 'confirmed', 'paid', 'documents_ready', 'documents_issued', 'contract_ready', 'order_created', 'in_stock', 'sold'):
        return 'success'
    if value in ('cancelled', 'canceled', 'closed', 'spam', 'decommissioned'):
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
    if current_user.is_production:
        return redirect(url_for('main.production_workspace'))
    if current_user.is_logistics:
        return redirect(url_for('main.logistics_workspace'))
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


@main_bp.route('/workspace')
@main_bp.route('/manager/workspace')
@login_required
def manager_workspace():
    if not (current_user.is_manager or current_user.is_admin or current_user.is_director):
        return redirect(url_for('main.role_home'))

    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    warehouse_id = current_user.warehouse_id
    if current_user.can_view_all:
        warehouse_id = request.args.get('warehouse_id', type=int) or warehouse_id
        if not warehouse_id and warehouses:
            warehouse_id = warehouses[0].id

    if not warehouse_id:
        flash('За пользователем не закреплён склад. Обратитесь к администратору.', 'warning')
        return redirect(url_for('main.trailers_list'))

    active_tab = (request.args.get('tab') or 'stock').strip() or 'stock'

    stock_trailers = [
        trailer for trailer in (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .filter(
            Trailer.warehouse_id == warehouse_id,
            Trailer.status == 'IN_STOCK',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Item.article, Trailer.vin)
        .all()
        )
        if _trailer_available_for_sale(trailer)
    ]
    reserved_trailers = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .filter(Trailer.warehouse_id == warehouse_id, Trailer.status == 'RESERVED')
        .order_by(Item.article, Trailer.vin)
        .all()
    )
    free_trailers = stock_trailers + reserved_trailers

    grouped_trailers = defaultdict(list)
    for t in free_trailers:
        article = t.item.article if t.item and t.item.article else 'Без артикула'
        grouped_trailers[article].append(t)

    new_leads = Lead.query.filter(or_(Lead.warehouse_id == warehouse_id, Lead.warehouse_id.is_(None)), Lead.status.in_(['NEW', 'IN_PROGRESS'])).order_by(Lead.created_at.desc()).limit(20).all()
    conversation_query = (
        _conversation_visible_query()
        .filter(or_(Lead.warehouse_id == warehouse_id, Lead.warehouse_id.is_(None), Lead.assigned_user_id == current_user.id))
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
    active_orders = CustomerOrder.query.filter(CustomerOrder.warehouse_id == warehouse_id, CustomerOrder.status.notin_(['done', 'cancelled', 'shipped'])).order_by(CustomerOrder.created_at.desc()).limit(30).all()
    inbound_movements = StockMovement.query.filter(StockMovement.to_warehouse_id == warehouse_id, StockMovement.status.in_(['sent', 'in_transit'])).order_by(StockMovement.departure_date.desc().nullslast(), StockMovement.id.desc()).all()
    outgoing_movements = StockMovement.query.filter(StockMovement.from_warehouse_id == warehouse_id, StockMovement.status.in_(['sent', 'in_transit'])).order_by(StockMovement.departure_date.desc().nullslast(), StockMovement.id.desc()).all()
    production_for_warehouse = (
        ProductionRequestLine.query
        .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        .filter(
            ProductionRequest.target_warehouse_id == warehouse_id,
            ProductionRequestLine.status.in_(['planned', 'PLANNED', 'in_production', 'partial_ready']),
        )
        .order_by(ProductionRequest.created_at.desc(), ProductionRequestLine.id.desc())
        .all()
    )
    production_qty_left = sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in production_for_warehouse)
    produced_units_for_warehouse = (
        ProducedUnit.query
        .filter(
            ProducedUnit.target_warehouse_id == warehouse_id,
            ProducedUnit.status == 'produced_no_vin',
        )
        .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
        .all()
    )
    production_warehouse = _default_production_warehouse()
    ready_at_production_units = []
    if production_warehouse:
        ready_at_production_units = (
            ProducedUnit.query
            .join(Trailer, Trailer.id == ProducedUnit.trailer_id)
            .filter(
                ProducedUnit.target_warehouse_id == warehouse_id,
                ProducedUnit.status == 'vin_assigned',
                Trailer.warehouse_id == production_warehouse.id,
                Trailer.status.in_(['IN_STOCK', 'RESERVED']),
                ProducedUnit.target_warehouse_id != production_warehouse.id,
            )
            .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
            .all()
        )
    ready_at_production_rows = [_produced_unit_context(unit) for unit in ready_at_production_units]
    ready_to_ship_orders = CustomerOrder.query.filter(
        CustomerOrder.warehouse_id == warehouse_id,
        CustomerOrder.status.in_(['ready_to_ship', 'arrived']),
    ).order_by(CustomerOrder.created_at.desc()).all()
    ready_to_ship_extra = CustomerOrder.query.filter(
        CustomerOrder.warehouse_id == warehouse_id,
        CustomerOrder.is_shipped == False,
        CustomerOrder.trailer_id.isnot(None),
        CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
    ).order_by(CustomerOrder.created_at.desc()).all()
    ready_to_ship_orders = list({order.id: order for order in ready_to_ship_orders + ready_to_ship_extra}.values())
    planned_ship_orders = (
        CustomerOrder.query
        .filter(
            CustomerOrder.warehouse_id == warehouse_id,
            CustomerOrder.documents_issued == True,
            CustomerOrder.is_shipped == False,
            CustomerOrder.status != 'cancelled',
        )
        .order_by(CustomerOrder.planned_ship_date.asc().nullslast(), CustomerOrder.documents_issued_at.desc().nullslast())
        .all()
    )

    paid_not_shipped_orders = [
        order for order in active_orders
        if order.confirmed_paid_amount > 0 and not order.is_shipped
    ]
    paid_docs_not_issued_orders = [
        order for order in CustomerOrder.query.filter(
            CustomerOrder.warehouse_id == warehouse_id,
            CustomerOrder.documents_issued == False,
            CustomerOrder.status.notin_(['done', 'cancelled', 'shipped']),
        ).order_by(CustomerOrder.created_at.desc()).all()
        if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount
    ]
    today_start = datetime.combine(date.today(), time.min)
    month_start = datetime(date.today().year, date.today().month, 1)
    sales_scope_orders = (
        CustomerOrder.query
        .join(SalesContract, SalesContract.order_id == CustomerOrder.id)
        .join(Trailer, Trailer.id == CustomerOrder.trailer_id)
        .filter(
            CustomerOrder.warehouse_id == warehouse_id,
            CustomerOrder.documents_issued == True,
            CustomerOrder.documents_issued_at.isnot(None),
            CustomerOrder.status != 'cancelled',
            Trailer.status == 'SOLD',
        )
        .order_by(CustomerOrder.documents_issued_at.desc())
        .all()
    )
    sales_scope_orders = [
        order for order in sales_scope_orders
        if order.total_amount > 0 and order.confirmed_paid_amount >= order.total_amount
    ]
    sold_today_orders = [
        order for order in sales_scope_orders
        if order.documents_issued_at and order.documents_issued_at >= today_start
    ]
    sold_month_orders = [
        order for order in sales_scope_orders
        if order.documents_issued_at and order.documents_issued_at >= month_start
    ]
    payments = (
        OrderPayment.query
        .join(CustomerOrder, CustomerOrder.id == OrderPayment.order_id)
        .filter(CustomerOrder.warehouse_id == warehouse_id)
        .order_by(OrderPayment.paid_at.desc().nullslast(), OrderPayment.created_at.desc())
        .limit(20)
        .all()
    )
    other_stock = [
        trailer for trailer in (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .join(Warehouse, Warehouse.id == Trailer.warehouse_id)
        .filter(
            Trailer.warehouse_id != warehouse_id,
            Trailer.status == 'IN_STOCK',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Warehouse.name, Item.article, Trailer.vin)
        .all()
        )
        if _trailer_available_for_sale(trailer)
    ]
    sold_not_shipped_trailers = []
    for order in planned_ship_orders:
        if order.trailer and order.trailer.id not in {trailer.id for trailer in sold_not_shipped_trailers}:
            sold_not_shipped_trailers.append(order.trailer)
    inbound_trailers = []
    for movement in inbound_movements:
        if movement.trailer and movement.trailer.id not in {trailer.id for trailer in inbound_trailers}:
            inbound_trailers.append(movement.trailer)
    shipped_trailers = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .filter(
            Trailer.warehouse_id == warehouse_id,
            Trailer.status == 'SOLD',
            Trailer.lifecycle_status == 'customer_shipped',
        )
        .order_by(Trailer.created_at.desc(), Trailer.id.desc())
        .limit(50)
        .all()
    )
    warehouse_stock_rows = []
    for trailer in stock_trailers:
        warehouse_stock_rows.append({'group': 'available', 'group_label': 'Свободен', 'trailer': trailer, 'order': None})
    for trailer in reserved_trailers:
        order = CustomerOrder.query.filter_by(trailer_id=trailer.id, is_shipped=False).filter(CustomerOrder.status != 'cancelled').order_by(CustomerOrder.created_at.desc()).first()
        warehouse_stock_rows.append({'group': 'reserved', 'group_label': 'Резерв', 'trailer': trailer, 'order': order})
    for trailer in sold_not_shipped_trailers:
        order = CustomerOrder.query.filter_by(trailer_id=trailer.id, documents_issued=True, is_shipped=False).order_by(CustomerOrder.documents_issued_at.desc()).first()
        warehouse_stock_rows.append({'group': 'sold_not_shipped', 'group_label': 'Продан, не отгружен', 'trailer': trailer, 'order': order})
    for trailer in inbound_trailers:
        movement = next((m for m in inbound_movements if m.trailer_id == trailer.id), None)
        warehouse_stock_rows.append({'group': 'in_transit', 'group_label': 'В пути', 'trailer': trailer, 'order': movement.order if movement else None})
    for unit in produced_units_for_warehouse:
        need = unit.production_request_line.supply_need if unit.production_request_line else None
        warehouse_stock_rows.append({
            'group': 'produced_no_vin',
            'group_label': 'Выпущен без VIN',
            'trailer': None,
            'unit': unit,
            'order': need.order if need and need.order else None,
        })
    for row in ready_at_production_rows:
        warehouse_stock_rows.append({
            'group': 'ready_production',
            'group_label': 'Готов на складе выпуска',
            'trailer': row['unit'].trailer if row.get('unit') and row['unit'].trailer else None,
            'unit': row.get('unit'),
            'order': row.get('order'),
        })
    for trailer in shipped_trailers:
        order = CustomerOrder.query.filter_by(trailer_id=trailer.id, is_shipped=True).order_by(CustomerOrder.shipped_at.desc().nullslast()).first()
        warehouse_stock_rows.append({'group': 'shipped', 'group_label': 'Отгружен', 'trailer': trailer, 'order': order})

    return render_template(
        'manager_workspace.html',
        warehouse_id=warehouse_id,
        warehouses=warehouses,
        grouped_trailers=grouped_trailers,
        free_trailers=free_trailers,
        stock_trailers=stock_trailers,
        reserved_trailers=reserved_trailers,
        new_leads=new_leads,
        recent_conversations=recent_conversations,
        new_conversations_count=new_conversations_count,
        unread_messages_count=unread_messages_count,
        manager_needed_count=manager_needed_count,
        active_orders=active_orders,
        inbound_movements=inbound_movements,
        outgoing_movements=outgoing_movements,
        ready_to_ship_orders=ready_to_ship_orders,
        planned_ship_orders=planned_ship_orders,
        production_for_warehouse=production_for_warehouse,
        production_qty_left=production_qty_left,
        produced_units_for_warehouse=produced_units_for_warehouse,
        ready_at_production_rows=ready_at_production_rows,
        production_warehouse=production_warehouse,
        paid_not_shipped_orders=paid_not_shipped_orders,
        paid_docs_not_issued_orders=paid_docs_not_issued_orders,
        sold_today_orders=sold_today_orders,
        sold_today_revenue=sum(float(order.price or 0) for order in sold_today_orders),
        sold_month_orders=sold_month_orders,
        sold_month_revenue=sum(float(order.price or 0) for order in sold_month_orders),
        warehouse_stock_rows=warehouse_stock_rows,
        payments=payments,
        other_stock=other_stock,
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
    status_filter = request.args.get('status', 'all')
    warehouse_id = request.args.get('warehouse_id', type=int)

    query = Trailer.query.join(Item).join(Warehouse)

    # менеджер видит только свой склад
    if getattr(current_user, 'is_manager', False) and current_user.warehouse_id:
        query = query.filter(Trailer.warehouse_id == current_user.warehouse_id)

    if vin_filter:
        query = query.filter(Trailer.vin.ilike(f'%{vin_filter}%'))

    if article_filter:
        query = query.filter(Item.article.ilike(f'%{article_filter}%'))

    if status_filter and status_filter != 'all':
        query = query.filter(Trailer.status == status_filter)

    if warehouse_id:
        query = query.filter(Trailer.warehouse_id == warehouse_id)

    trailers = query.order_by(Trailer.id.desc()).all()
    warehouses = Warehouse.query.order_by(Warehouse.name).all()

    return render_template(
        'trailers_list.html',
        trailers=trailers,
        warehouses=warehouses,
        vin_filter=vin_filter,
        article_filter=article_filter,
        status_filter=status_filter,
        warehouse_filter=warehouse_id,
    )



@main_bp.route('/trailers/new', methods=['GET', 'POST'])
@login_required
def trailer_create():
    if not (current_user.is_admin or current_user.is_logistics):
        abort(403)
    form = TrailerCreateForm()
    _fill_trailer_form_choices(form)

    if form.validate_on_submit():
        item, error = _find_item_for_form(form)
        if error:
            flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Новый прицеп')

        trailer = Trailer(
            vin=form.vin.data.strip(),
            item_id=item.id,
            warehouse_id=form.warehouse_id.data,
            manufacture_date=form.manufacture_date.data,
            status=form.status.data,
        )
        db.session.add(trailer)
        db.session.commit()
        flash('Прицеп создан', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Новый прицеп')


@main_bp.route('/trailers/<int:trailer_id>/edit', methods=['GET', 'POST'])
@login_required
def trailer_edit(trailer_id):
    if not (current_user.is_admin or current_user.is_logistics):
        abort(403)
    trailer = Trailer.query.get_or_404(trailer_id)

    form = TrailerCreateForm(
        vin=trailer.vin,
        warehouse_id=trailer.warehouse_id,
        manufacture_date=trailer.manufacture_date,
        status=trailer.status,
    )
    _fill_trailer_form_choices(form)

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



    if form.validate_on_submit():
        item, error = _find_item_for_form(form)
        if error:
            flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Редактирование прицепа')

        trailer.vin = form.vin.data.strip()
        trailer.warehouse_id = form.warehouse_id.data
        trailer.manufacture_date = form.manufacture_date.data
        trailer.status = form.status.data
        trailer.item_id = item.id

        db.session.commit()
        flash('Прицеп обновлён', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Редактирование прицепа')


@main_bp.route('/trailers/<int:trailer_id>/delete')
@login_required
def trailer_delete(trailer_id):
    if not (current_user.is_admin or current_user.is_logistics):
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


@main_bp.route('/items/new', methods=['GET', 'POST'])
@login_required
def item_create():
    """Создание новой позиции номенклатуры."""
    form = ItemForm()

    if form.validate_on_submit():
        item_type = form.item_type.data
        article = form.article.data.strip() if form.article.data else None

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

    if request.method == 'GET':
        form.item_type.data = item.item_type
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
        'fields': ['code', 'name', 'name_prefix', 'otss_number', 'otss_type', 'otts_valid_from', 'otts_valid_to', 'vehicle_category', 'axle_count', 'wheel_count', 'max_mass_kg', 'is_active', 'sort_order', 'comment'],
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
    'otss_type': 'Тип ОТТС', 'vehicle_category': 'Категория', 'axle_count': 'Оси', 'wheel_count': 'Колёса',
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
    selected_body_size = TrailerBodySize.query.filter_by(code=config['body_size_code']).first()
    option_warnings = []

    def allowed_items(option_type, model):
        base_query = model.query.filter_by(is_active=True)
        if not selected_group:
            return base_query.order_by(model.sort_order).all()
        allowed_ids = [
            row.option_id for row in TrailerAllowedOption.query.filter_by(
                group_id=selected_group.id,
                option_type=option_type,
                is_allowed=True,
            ).all()
        ]
        if not allowed_ids:
            return []
        return base_query.filter(model.id.in_(allowed_ids)).order_by(model.sort_order).all()

    def matrix_filtered_items(option_type, model, price_model, price_field, warning):
        allowed = allowed_items(option_type, model)
        if not selected_group:
            return allowed
        price_ids = [
            getattr(row, price_field) for row in price_model.query.filter_by(
                group_id=selected_group.id,
                is_active=True,
            ).all()
        ]
        if not price_ids:
            option_warnings.append(warning)
            return []
        return [item for item in allowed if item.id in set(price_ids)]

    board_heights = allowed_items('board_height', TrailerBoardHeight)
    if selected_body_size:
        board_price_ids = {
            row.board_height_id for row in TrailerBoardPriceMatrix.query.filter_by(
                body_size_id=selected_body_size.id,
                is_active=True,
            ).all()
        }
        board_heights = [item for item in board_heights if item.id in board_price_ids]
        if not board_heights:
            option_warnings.append(f'Для кузова {selected_body_size.code} не заведены цены бортов.')

    tents = allowed_items('tent', TrailerTentOption)
    if selected_body_size:
        tent_price_ids = {
            row.tent_option_id for row in TrailerTentPriceMatrix.query.filter_by(
                body_size_id=selected_body_size.id,
                is_active=True,
            ).all()
        }
        tents = [item for item in tents if item.id in tent_price_ids]
        if not tents:
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
    body_sizes = allowed_items('body_size', TrailerBodySize)
    support_wheels = allowed_items('support_wheel', TrailerSupportWheelOption)
    executions = allowed_items('body_execution', TrailerBodyExecution)
    specials = allowed_items('special', TrailerSpecialOption)

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
@role_required('director', 'manager', 'logistics')
def manager_trailer_picker():
    from trailer_configurator import build_trailer_configuration_result

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

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        warehouse_id = request.form.get('warehouse_id', type=int) or (current_user.warehouse_id if current_user.is_manager else None)
        customer_id = request.form.get('customer_id', type=int)
        if action in ('stock_order', 'production_order') and not customer_id:
            flash('Выберите клиента для создания заказа.', 'danger')
            return redirect(url_for('main.manager_trailer_picker', **config))

        if action == 'stock_order':
            trailer = Trailer.query.get(request.form.get('trailer_id', type=int))
            if not trailer or not _trailer_available_for_sale(trailer):
                flash('Выбранный прицеп уже недоступен для продажи.', 'danger')
                return redirect(url_for('main.manager_trailer_picker', **config))
            idem_key, duplicate = _reserve_idempotency_key()
            if duplicate:
                return _duplicate_redirect(idem_key, url_for('main.orders_list'))
            order = CustomerOrder(
                order_number=_next_number('ORD', CustomerOrder, 'order_number'),
                customer_id=customer_id,
                item_id=trailer.item_id,
                trailer_id=trailer.id,
                warehouse_id=warehouse_id or trailer.warehouse_id,
                source_warehouse_id=trailer.warehouse_id,
                assigned_user_id=current_user.id if current_user.is_manager else None,
                quantity=1,
                price=trailer.item.base_price if trailer.item else None,
                prepayment_percent=30,
                status='waiting_payment',
                fulfillment_source='stock',
            )
            db.session.add(order)
            db.session.flush()
            trailer.status = 'RESERVED'
            trailer.lifecycle_status = 'reserved'
            db.session.add(Reservation(order_id=order.id, trailer_id=trailer.id, item_id=trailer.item_id, source_type='STOCK' if trailer.warehouse_id == order.warehouse_id else 'TRANSFER', status='ACTIVE', priority=10, note='Резерв из подбора прицепа'))
            add_order_event(order, 'order_created', new_value=order.status, comment='Создан из подбора прицепа')
            add_order_event(order, 'trailer_reserved', new_value=trailer.vin, comment='Резерв из подбора прицепа')
            _finish_idempotency(idem_key, 'CustomerOrder', order.id)
            db.session.commit()
            flash('Заказ из наличия создан, прицеп зарезервирован.', 'success')
            return redirect(url_for('main.order_detail', order_id=order.id))

        if action == 'production_order':
            if result.get('errors'):
                flash('Исправьте ошибки конфигурации перед созданием заказа.', 'danger')
                return redirect(url_for('main.manager_trailer_picker', **config))
            item = get_or_create_configured_item(result)
            idem_key, duplicate = _reserve_idempotency_key()
            if duplicate:
                return _duplicate_redirect(idem_key, url_for('main.orders_list'))
            order = CustomerOrder(
                order_number=_next_number('ORD', CustomerOrder, 'order_number'),
                customer_id=customer_id,
                item_id=item.id,
                warehouse_id=warehouse_id,
                assigned_user_id=current_user.id if current_user.is_manager else None,
                quantity=1,
                price=snapshot.get('calculated_price'),
                prepayment_percent=30,
                status='waiting_production',
                fulfillment_source='production',
            )
            _apply_snapshot(order, snapshot)
            db.session.add(order)
            db.session.flush()
            need = SupplyNeed(order_id=order.id, item_id=item.id, warehouse_id=order.warehouse_id, quantity=1, status='NEW', need_type='CUSTOMER_ORDER', priority=10, note='Потребность создана из подбора прицепа')
            _apply_snapshot(need, snapshot)
            db.session.add(need)
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
        config=config,
        result=result,
        inventory=inventory,
        config_options=_trailer_config_form_context(),
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

def _build_contract_context(contract_id: int) -> dict:
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_access_contract(contract)
    customer = contract.customer
    trailer = contract.trailer
    effective_vin = get_order_effective_vin(contract.order) if contract.order else (trailer.vin if trailer else '')
    item = trailer.item if trailer else (contract.order.item if contract.order else None)

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
    )
    if current_user.is_manager:
        query = query.filter(or_(
            CustomerOrder.assigned_user_id == current_user.id,
            CustomerOrder.warehouse_id == current_user.warehouse_id,
            Trailer.warehouse_id == current_user.warehouse_id,
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

    if request.method == 'GET':
        form.contract_date.data = contract.contract_date
        form.contract_number.data = contract.contract_number or ''
        if contract.customer_id:
            form.customer_id.data = contract.customer_id
        if contract.trailer_id:
            form.trailer_id.data = contract.trailer_id
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
        contract.price = form.price.data
        contract.payment_method = _norm_str(form.payment_method.data)
        if current_user.is_admin:
            contract.is_paid = bool(form.is_paid.data)
            contract.is_shipped = bool(form.is_shipped.data)

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
    return render_template('contract_print.html', **ctx)


@main_bp.route('/contracts/<int:contract_id>/pdf')
@login_required
def contract_pdf(contract_id):
    ctx = _build_contract_context(contract_id)
    if HTML is not None:
        from flask import make_response, request
        html = render_template('contract_print.html', **ctx)
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
    html = render_template('contract_print.html', **ctx)

    if HTML is None:
        raise RuntimeError("WeasyPrint (HTML) is not available. Install/configure WeasyPrint on server.")

    pdf = HTML(string=html, base_url=request.host_url).write_pdf()
    return pdf

def _sigex_title_for_contract(contract) -> str:
    num = contract.contract_number or contract.id
    return f"Договор_{num}.pdf"

@main_bp.route('/contracts/<int:contract_id>/sign')
@login_required
def contract_sign(contract_id):
    """
    Страница подписи:
      1) менеджер подписывает ЭЦП организации (через NCALayer на своём ПК)
      2) показываем QR для клиента
      3) после done — даём кнопку "Открыть карточку (DDC)"
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
    if HTML is None:
        return jsonify({"ok": False, "error": "WeasyPrint is not available. HTML print/PDF fallback is available, SIGEX PDF is disabled."}), 503

    # если уже есть documentId — просто вернём
    if contract.sigex_document_id:
        return jsonify({"documentId": contract.sigex_document_id})

    # предрегистрация без подписи возможна только при mTLS :contentReference[oaicite:3]{index=3}
    title = _sigex_title_for_contract(contract)

    payload = {
        "title": title,
        "description": f"Договор №{contract.contract_number or contract.id}",
        "settings": {
            "private": False,
            "signaturesLimit": 2,
            "switchToPrivateAfterLimitReached": True,
            # чтобы QR-подпись работала: документ должен быть в tempStorage или архиве :contentReference[oaicite:4]{index=4}
            "tempStorageAfterRegistration": 86400000,  # 24 часа
        },
    }

    reg = sigex_post_json("/api", payload)
    document_id = reg["documentId"]

    # завершить регистрацию нужно передачей тела документа :contentReference[oaicite:5]{index=5}
    pdf_bytes = _contract_pdf_bytes(contract_id)
    sigex_post_octet(f"/api/{document_id}/data", pdf_bytes)

    contract.sigex_document_id = document_id
    db.session.commit()

    return jsonify({"documentId": document_id})

@main_bp.route('/contracts/<int:contract_id>/sigex/add_org_signature', methods=['POST'])
@login_required
def contract_sigex_add_org_signature(contract_id):
    """
    Сюда фронт пришлёт CMS подпись (base64) от NCALayer.
    """
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)

    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    data = request.get_json(silent=True) or {}
    signature = data.get("signature")
    sign_type = data.get("signType", "cms")

    if not signature:
        abort(400, "signature is required")

    # добавление подписи к документу :contentReference[oaicite:6]{index=6}
    res = sigex_post_json(f"/api/{contract.sigex_document_id}", {
        "signType": sign_type,
        "signature": signature,
    })

    contract.sigex_last_sign_id = res.get("signId")
    contract.sigex_last_status = "org_signed"
    db.session.commit()

    return jsonify({"ok": True, "signId": res.get("signId")})

@main_bp.route('/contracts/<int:contract_id>/sigex/start_qr', methods=['POST'])
@login_required
def contract_sigex_start_qr(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_manage_contract(contract)
    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    payload = {
        "description": f"Подпишите договор №{contract.contract_number or contract.id}",
        "meta": [
            {"name": "Номер договора", "value": str(contract.contract_number or contract.id)},
            {"name": "Сумма", "value": str(contract.price or "")},
        ],
    }

    # инициировать процедуру QR-подписи :contentReference[oaicite:7]{index=7}
    res = sigex_post_json(f"/api/{contract.sigex_document_id}/egovQr", payload)

    contract.sigex_operation_id = res["operationId"]
    contract.sigex_last_status = "qr_started"
    db.session.commit()

    return jsonify(res)

@main_bp.route('/contracts/<int:contract_id>/sigex/qr_status')
@login_required
def contract_sigex_qr_status(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    _ensure_can_access_contract(contract)
    if not (contract.sigex_document_id and contract.sigex_operation_id):
        abort(400, "No active operation")

    # получить статус процедуры :contentReference[oaicite:8]{index=8}
    res = sigex_get_json(f"/api/{contract.sigex_document_id}/egovOperation/{contract.sigex_operation_id}")

    status = res.get("status")
    contract.sigex_last_status = status
    if status == "done":
        contract.sigex_last_sign_id = res.get("signId")
    db.session.commit()

    return jsonify(res)

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

    # buildDDC :contentReference[oaicite:9]{index=9}
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

    res = sigex_post_json(f"/api/{contract.sigex_document_id}/buildDDC", payload={}, params=params)

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


def _order_trailer_options(current_order_id: int | None = None):
    trailers = (
        Trailer.query
        .filter(
            Trailer.status == 'IN_STOCK',
            or_(Trailer.lifecycle_status.is_(None), Trailer.lifecycle_status != 'customer_shipped'),
        )
        .order_by(Trailer.vin)
        .all()
    )
    result = []
    for trailer in trailers:
        if not _trailer_available_for_sale(trailer, exclude_order_id=current_order_id):
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
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in User.query.order_by(User.full_name, User.username).all()]
    if form.fulfillment_source.data in ('other_warehouse', 'transit'):
        form.fulfillment_source.data = 'stock' if form.fulfillment_source.data == 'other_warehouse' else 'later'

    _apply_item_search(form, 'item_search', 'item_id')
    _apply_customer_search(form)
    form.trailer_id.choices = [(0, '— подобрать позже / под заказ —')] + [
        (option['id'], f'{option["vin"]} — {option["article"]} — {option["warehouse"]} — {option["status"]}')
        for option in _order_trailer_options(current_order_id)
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


def _trailer_config_form_context():
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
    if current_user.is_admin or current_user.is_director or current_user.is_logistics:
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
    existing = Item.query.filter_by(article=article).first()
    if existing:
        return existing
    config = config_result.get('config') or {}
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
        body_length_mm=body_size.length_mm if body_size else None,
        body_width_mm=body_size.width_mm if body_size else None,
        board_height_mm=board.height_mm if board else None,
        axle_count=(TrailerProductGroup.query.filter_by(code=config.get('group_code')).first().axle_count if TrailerProductGroup.query.filter_by(code=config.get('group_code')).first() else None),
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


def _render_order_form(form: CustomerOrderForm, title: str):
    return render_template(
        'order_form.html',
        form=form,
        title=title,
        customer_options=_customer_options(limit=50),
        item_options=_trailer_item_options(),
        trailer_options=_order_trailer_options(getattr(form, 'order_id', None)),
        availability=_order_future_availability(form.item_id.data or None, form.warehouse_id.data or None),
        config_options=_trailer_config_form_context(),
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


def _fill_stock_movement_form_choices(form: StockMovementForm, current_movement_id: int | None = None) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.from_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in warehouses]
    form.to_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in warehouses]
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
    ]
    _set_stock_movement_trailer_search_label(form)


def _fill_stock_movement_batch_form_choices(form: StockMovementBatchForm) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.from_warehouse_id.choices = [(w.id, w.name) for w in warehouses]
    form.to_warehouse_id.choices = [(w.id, w.name) for w in warehouses]


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
            return redirect(url_for('main.manager_workspace', tab=request.args.get('return_tab') or 'leads'))
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
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    managers = User.query.filter(User.role == 'manager').order_by(User.full_name, User.username).all()
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
    status = request.args.get('status', '').strip()
    q = request.args.get('q', '').strip()

    query = CustomerOrder.query.join(Customer, Customer.id == CustomerOrder.customer_id).outerjoin(Item, Item.id == CustomerOrder.item_id)
    if not current_user.can_view_all and current_user.warehouse_id:
        query = query.filter(or_(CustomerOrder.warehouse_id == current_user.warehouse_id, CustomerOrder.warehouse_id.is_(None)))

    if status:
        query = query.filter(CustomerOrder.status == status)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(CustomerOrder.order_number.ilike(like), Customer.name.ilike(like), Item.article.ilike(like), Item.name.ilike(like)))

    orders = query.order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc()).all()
    return render_template('orders_list.html', orders=orders, status=status, q=q)


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
    return jsonify({'ok': True, 'result': _config_preview_payload(result)})


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
@role_required('director', 'logistics')
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
            if _request_has_trailer_config(request.form):
                configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
                if config_errors:
                    for error in config_errors:
                        flash(error, 'danger')
                    return _render_order_form(form, 'Новый заказ')
                form.item_id.data = configured_item.id
            elif lead_id_prefill:
                lead = Lead.query.get(lead_id_prefill)
                if lead and lead.article_snapshot and lead.desired_item_id:
                    configured_item = lead.desired_item
                    configured_snapshot = _lead_snapshot(lead)
                    form.item_id.data = lead.desired_item_id
            if not configured_item:
                flash('Для заказа в производство соберите прицеп через конфигуратор.', 'danger')
                return _render_order_form(form, 'Новый заказ')
        elif fulfillment_source == 'stock':
            if not selected_trailer:
                flash('Для продажи из наличия выберите конкретный прицеп из таблицы.', 'danger')
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
            lead_id=form.lead_id.data or None,
            customer_id=form.customer_id.data,
            item_id=form.item_id.data or None,
            trailer_id=form.trailer_id.data or None,
            warehouse_id=form.warehouse_id.data or None,
            source_warehouse_id=selected_trailer.warehouse_id if selected_trailer else None,
            assigned_user_id=form.assigned_user_id.data or None,
            quantity=form.quantity.data or 1,
            price=form.price.data or (configured_snapshot.get('calculated_price') if configured_snapshot else None),
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
        if selected_trailer and order.source_warehouse_id and order.warehouse_id and order.source_warehouse_id != order.warehouse_id:
            order.status = 'waiting_transfer'
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
                need = SupplyNeed(
                    order_id=order.id,
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
@role_required('manager', 'director', 'logistics')
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
@role_required('manager', 'director', 'logistics')
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
        configured_snapshot = None
        if request.form.get('item_source') == 'config':
            configured_item, configured_snapshot, config_errors = _configured_item_from_request(request.form)
            if config_errors:
                for error in config_errors:
                    flash(error, 'danger')
                return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())
            form.item_id.data = configured_item.id
        elif not form.item_id.data:
            flash('Выберите номенклатуру или соберите прицеп через конфигуратор.', 'danger')
            return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())
        idem_key, duplicate = _reserve_idempotency_key()
        if duplicate:
            return _duplicate_redirect(idem_key, url_for('main.stock_replenishment_list'))
        need = SupplyNeed(
            need_type='STOCK_REPLENISHMENT',
            order_id=None,
            item_id=form.item_id.data,
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
        if current_user.is_manager:
            return redirect(url_for('main.manager_workspace', tab=request.args.get('return_tab') or 'production'))
        return redirect(url_for('main.stock_replenishment_list'))

    return render_template('stock_replenishment_form.html', form=form, title='Заказать на склад', config_options=_trailer_config_form_context())


@main_bp.route('/supply-needs/<int:need_id>/cancel', methods=['POST'])
@login_required
def supply_need_cancel(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
    if current_user.is_admin or current_user.is_director or current_user.is_logistics:
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
    if not (current_user.is_admin or current_user.is_director or current_user.is_logistics):
        abort(403)
    reason = (request.form.get('cancel_reason') or request.form.get('reason') or '').strip()
    if not reason:
        flash('Укажите причину отмены ошибочного производства.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if not need.production_lines:
        flash('По этой потребности нет производственных строк.', 'warning')
        return redirect(request.referrer or url_for('main.supply_needs_list'))
    if _supply_need_has_produced_output(need):
        flash('Нельзя очистить производство: по заявке уже есть выпуск или готовая строка.', 'danger')
        return redirect(request.referrer or url_for('main.supply_needs_list'))

    old_status = need.status
    cleanup_reason = f'Ошибочное производство отменено логистикой. Причина: {reason}'
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
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager or current_user.is_logistics):
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
    if current_user.is_manager and lead.warehouse_id and lead.warehouse_id != current_user.warehouse_id:
        abort(403)
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
    order_contract = SalesContract.query.filter_by(order_id=order.id).first()
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
        order_contract=order_contract,
        order_vin_row=order_vin_row,
        future_production_lines=future_production_lines,
        future_produced_unit_rows=future_produced_unit_rows,
        future_ready_trailer_rows=future_ready_trailer_rows,
        future_inbound_movements=future_inbound_movements,
        can_manage=can_manage_order(order),
    )


@main_bp.route('/orders/<int:order_id>/edit', methods=['GET', 'POST'])
@login_required
def order_edit(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    form = CustomerOrderForm(order_id=order.id, obj=order)
    _fill_order_form_choices(form, item_id_prefill=order.item_id, current_order_id=order.id)

    if request.method == 'GET':
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

    if form.validate_on_submit():
        old_status = order.status
        new_trailer_id = form.trailer_id.data or None
        if new_trailer_id and _active_reservation_for_trailer(new_trailer_id, exclude_order_id=order.id):
            flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        selected_trailer = Trailer.query.get(new_trailer_id) if new_trailer_id else None
        if selected_trailer and selected_trailer.item_id != form.item_id.data:
            flash('Выбранный VIN не соответствует выбранной модели.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and selected_trailer.status == 'SOLD':
            flash('Проданный прицеп нельзя выбрать для новой продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and (form.fulfillment_source.data or 'stock') != 'other_warehouse' and form.warehouse_id.data and selected_trailer.warehouse_id != form.warehouse_id.data:
            flash('Для продажи из наличия выбранный VIN должен находиться на складе продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')
        if selected_trailer and form.fulfillment_source.data == 'other_warehouse' and form.warehouse_id.data and selected_trailer.warehouse_id == form.warehouse_id.data:
            flash('Для сценария с другого склада выберите VIN не со склада продажи.', 'danger')
            return _render_order_form(form, 'Редактирование заказа')

        old_trailer_id = order.trailer_id
        order.order_number = (form.order_number.data or '').strip() or order.order_number
        order.lead_id = form.lead_id.data or None
        order.customer_id = form.customer_id.data
        order.item_id = form.item_id.data
        order.trailer_id = new_trailer_id
        order.warehouse_id = form.warehouse_id.data or None
        order.assigned_user_id = form.assigned_user_id.data or None
        order.quantity = form.quantity.data or 1
        order.price = form.price.data
        order.prepayment_percent = form.prepayment_percent.data
        order.fulfillment_source = (form.fulfillment_source.data or '').strip() or None
        order.expected_date = form.expected_date.data
        order.planned_ship_date = form.planned_ship_date.data
        order.planned_ship_comment = (form.planned_ship_comment.data or '').strip() or None
        order.note = (form.note.data or '').strip() or None
        order.manager_comment = (form.manager_comment.data or '').strip() or None

        if old_trailer_id and old_trailer_id != new_trailer_id:
            old_reservations = Reservation.query.filter_by(order_id=order.id, trailer_id=old_trailer_id, status='ACTIVE').all()
            for reservation in old_reservations:
                reservation.status = 'CANCELED'
            old_trailer = Trailer.query.get(old_trailer_id)
            if old_trailer and old_trailer.status == 'RESERVED':
                old_trailer.status = 'IN_STOCK'

        if new_trailer_id:
            trailer = Trailer.query.get(new_trailer_id)
            if trailer:
                trailer.status = 'RESERVED'
                order.fulfillment_source = order.fulfillment_source or 'stock'
                if not order.warehouse_id:
                    order.warehouse_id = trailer.warehouse_id
                active = Reservation.query.filter_by(order_id=order.id, trailer_id=trailer.id, status='ACTIVE').first()
                if not active:
                    db.session.add(Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, source_type='STOCK', status='ACTIVE', priority=10))
        elif order.fulfillment_source == 'production' and order.supply_needs.count() == 0:
            can_request, message = order_can_request_production(order)
            if not can_request:
                flash(message, 'warning')
                return _render_order_form(form, 'Редактирование заказа')
            db.session.add(SupplyNeed(order_id=order.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity, status='NEW', priority=10, need_type='CUSTOMER_ORDER', required_by=order.expected_date))

        _refresh_order_status(order)
        if old_status != order.status:
            add_order_event(order, 'order_status_changed', old_value=old_status, new_value=order.status)
        if old_trailer_id != new_trailer_id and new_trailer_id:
            add_order_event(order, 'trailer_reserved', new_value=new_trailer_id)
        db.session.commit()
        flash('Заказ обновлен', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    return _render_order_form(form, 'Редактирование заказа')


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
    if order.trailer_id and SalesContract.query.filter_by(trailer_id=order.trailer_id).first():
        flash('На этот прицеп уже существует договор.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
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
    order.trailer_id = trailer.id
    order.fulfillment_source = 'stock'
    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    reservation = Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, status='ACTIVE', source_type='STOCK', priority=10)
    db.session.add(reservation)
    db.session.flush()
    _finish_idempotency(idem_key, 'Reservation', reservation.id)
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
    if trailer.item_id != order.item_id:
        flash('Выбранный VIN не соответствует модели заказа.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if trailer.status != 'IN_STOCK' or trailer.warehouse_id == order.warehouse_id:
        flash('Для запроса перемещения нужен свободный прицеп на другом складе.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if not _trailer_available_for_sale(trailer, exclude_order_id=order.id):
        flash('Этот прицеп уже зарезервирован.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.order_detail', order_id=order.id))

    order.trailer_id = trailer.id
    order.fulfillment_source = 'other_warehouse'
    order.status = 'waiting_transfer'
    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    reservation = Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, status='ACTIVE', source_type='TRANSFER', priority=10)
    db.session.add(reservation)
    db.session.flush()
    _finish_idempotency(idem_key, 'Reservation', reservation.id)
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
    need = SupplyNeed(order_id=order.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity or 1, required_by=order.expected_date, status='NEW', need_type='CUSTOMER_ORDER', priority=10, note='Потребность создана из карточки заказа')
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
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager or current_user.is_logistics):
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
    if not (current_user.is_admin or current_user.is_director or current_user.is_manager or current_user.is_logistics):
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

@main_bp.route('/logistics/vin-registry', methods=['GET', 'POST'])
@role_required('logistics', 'director')
def vin_registry_list():
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
    query = VinRegistry.query
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
        query = query.outerjoin(CustomerOrder, CustomerOrder.id == VinRegistry.customer_order_id).outerjoin(Customer, Customer.id == CustomerOrder.customer_id).filter(or_(
            VinRegistry.vin_full.ilike(like),
            VinRegistry.serial7.ilike(like),
            CustomerOrder.order_number.ilike(like),
            Customer.name.ilike(like),
        ))
    rows = query.order_by(VinRegistry.created_at.desc(), VinRegistry.id.desc()).limit(300).all()
    return render_template('vin_registry_list.html', rows=rows, filters={'status': status, 'year': year, 'modification': modification, 'serial7': serial, 'docs': docs, 'q': q})


@main_bp.route('/logistics/vin-registry/<int:vin_id>')
@role_required('logistics', 'director')
def vin_registry_detail(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    orders = CustomerOrder.query.filter(
        CustomerOrder.status.notin_(['cancelled', 'canceled', 'closed', 'done', 'shipped']),
        CustomerOrder.documents_issued == False,
        CustomerOrder.is_shipped == False,
    ).order_by(CustomerOrder.created_at.desc()).limit(100).all()
    trailers = Trailer.query.order_by(Trailer.created_at.desc()).limit(150).all()
    return render_template('vin_registry_detail.html', row=row, orders=orders, trailers=trailers, events=row.events.order_by(VinRegistryEvent.created_at.desc()).all())


@main_bp.route('/logistics/vin-registry/<int:vin_id>/reserve', methods=['POST'])
@role_required('logistics', 'director')
def vin_registry_reserve(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status != 'free':
        flash('Резервировать можно только свободный VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    order = CustomerOrder.query.get(request.form.get('customer_order_id', type=int))
    if not order or order.is_shipped or order.documents_issued or order.status in ('cancelled', 'canceled', 'closed', 'done') or _active_vin_registry_for_order(order.id):
        flash('Этот заказ нельзя связать с VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    modification = _order_vin_modification_code(order)
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
    active_need = _active_supply_need_for_order(order.id)
    row.supply_need_id = active_need.id if active_need else None
    row.reserved_by_user_id = current_user.id
    row.reserved_at = datetime.utcnow()
    row.comment = (request.form.get('comment') or '').strip() or row.comment
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
    if trailer.vin and trailer.vin != row.vin_full and not (current_user.is_admin or current_user.is_director):
        flash('У прицепа уже есть другой VIN. Заменить его может только администратор или директор.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if trailer.status == 'SOLD' and not (row.customer_order and row.customer_order.trailer_id == trailer.id):
        flash('Проданный прицеп нельзя привязать к этому VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    if row.customer_order and row.customer_order.item_id and trailer.item_id != row.customer_order.item_id:
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
@role_required('logistics', 'director')
def vin_registry_confirm(vin_id):
    row = VinRegistry.query.get_or_404(vin_id)
    if row.status != 'assigned':
        flash('Подтвердить нанесение можно только после назначения VIN.', 'danger')
        return redirect(url_for('main.vin_registry_detail', vin_id=row.id))
    old_status = row.status
    row.status = 'confirmed'
    row.confirmed_by_user_id = current_user.id
    row.confirmed_at = datetime.utcnow()
    _add_vin_event(row, 'confirmed', old_status, row.status, comment=(request.form.get('comment') or '').strip() or None)
    db.session.commit()
    flash('Нанесение VIN подтверждено.', 'success')
    return redirect(url_for('main.vin_registry_detail', vin_id=row.id))


@main_bp.route('/logistics/vin-registry/<int:vin_id>/void', methods=['POST'])
@role_required('logistics', 'director')
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
    effective_vin = get_order_effective_vin(order)
    vin_row = (
        VinRegistry.query
        .filter(
            VinRegistry.customer_order_id == order.id,
            VinRegistry.status.in_(['reserved', 'assigned', 'confirmed']),
        )
        .order_by(VinRegistry.reserved_at.desc().nullslast(), VinRegistry.id.desc())
        .first()
    )
    vin_was_reserved = bool(vin_row and vin_row.status == 'reserved')
    if not effective_vin:
        flash('Нельзя выдать документы: сначала нужен конкретный прицеп/VIN или зарезервированный VIN.', 'danger')
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
    if order.trailer:
        order.trailer.status = 'SOLD'
    if vin_row:
        old_vin_status = vin_row.status
        vin_row.docs_issued_at = datetime.utcnow()
        vin_row.docs_issued_order_id = order.id
        _add_vin_event(vin_row, 'docs_issued', old_vin_status, vin_row.status, comment='Документы выданы по заказу')
    contract.is_paid = True
    contract.is_shipped = False
    comment = 'Юридическая продажа зафиксирована'
    if vin_row:
        comment = 'Юридическая продажа зафиксирована по зарезервированному VIN'
    add_order_event(order, 'documents_issued', old_value=old_document_status, new_value='documents_issued', comment=comment)
    if old_order_status != order.status:
        add_order_event(order, 'order_status_changed', old_value=old_order_status, new_value=order.status)
    _finish_idempotency(idem_key, 'CustomerOrder', order.id)
    db.session.commit()
    if vin_was_reserved and not order.trailer:
        flash('VIN зарезервирован, но ещё не подтверждён производством. Проверьте, что производство нанесёт именно этот VIN.', 'warning')
    flash('Документы выданы. Прицеп юридически продан, но ещё не отгружен клиенту.', 'success')
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
@role_required('manager', 'director', 'logistics')
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
@role_required('manager', 'director', 'logistics')
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
@role_required('admin', 'director', 'logistics')
def supply_need_create_production_request(need_id):
    need = SupplyNeed.query.get_or_404(need_id)
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
@role_required('director', 'logistics')
def production_requests_list():
    status = request.args.get('status', '').strip()
    query = ProductionRequest.query
    if status:
        query = query.filter_by(status=status)
    requests = query.order_by(ProductionRequest.created_at.desc(), ProductionRequest.id.desc()).all()
    return render_template('production_requests_list.html', requests=requests, status=status)


@main_bp.route('/production-requests/new', methods=['GET', 'POST'])
@role_required('director', 'logistics')
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
@role_required('director', 'logistics')
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
@role_required('director', 'logistics')
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
@login_required
def stock_movements_list():
    status = request.args.get('status', '').strip()
    batch_key = request.args.get('batch_key', '').strip()
    query = StockMovement.query
    if status:
        query = query.filter_by(status=status)
    if batch_key:
        query = query.filter_by(batch_key=batch_key)
    movements = query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()
    return render_template('stock_movements_list.html', movements=movements, status=status, batch_key=batch_key)


@main_bp.route('/stock-movements/new', methods=['GET', 'POST'])
@role_required('director', 'logistics')
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
@role_required('director', 'logistics')
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
    )


@main_bp.route('/stock-movements/<int:movement_id>/edit', methods=['GET', 'POST'])
@role_required('director', 'logistics')
def stock_movement_edit(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    form = StockMovementForm(obj=movement)
    if request.method == 'GET':
        form.from_warehouse_id.data = movement.from_warehouse_id or 0
        form.to_warehouse_id.data = movement.to_warehouse_id or 0
        form.trailer_id.data = movement.trailer_id or 0
        form.order_id.data = movement.order_id or 0
    _fill_stock_movement_form_choices(form, current_movement_id=movement.id)
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
@role_required('production', 'director')
def production_workspace():
    active_tab = request.args.get('tab', 'todo')
    today_start = datetime.combine(date.today(), time.min)

    base_query = (
        ProductionRequestLine.query
        .join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
        .outerjoin(SupplyNeed, SupplyNeed.id == ProductionRequestLine.supply_need_id)
    )

    lines = []
    if active_tab == 'in_work':
        lines = base_query.filter(ProductionRequestLine.status.in_(['in_production', 'partial_ready'])).order_by(ProductionRequest.created_at.asc(), ProductionRequestLine.id.asc()).all()
    elif active_tab == 'overdue':
        lines = base_query.filter(
            ~ProductionRequestLine.status.in_(['ready', 'closed', 'cancelled', 'CANCELLED']),
            SupplyNeed.required_by.isnot(None),
            SupplyNeed.required_by < date.today(),
        ).order_by(SupplyNeed.required_by.asc(), ProductionRequestLine.id.asc()).all()
    elif active_tab != 'done_today':
        active_tab = 'todo'
        lines = base_query.filter(ProductionRequestLine.status.in_(['planned', 'PLANNED', 'draft', 'DRAFT', 'waiting_production'])).order_by(ProductionRequest.created_at.asc(), ProductionRequestLine.id.asc()).all()

    today_units = (
        ProducedUnit.query
        .filter(or_(ProducedUnit.created_at >= today_start, ProducedUnit.produced_at >= today_start))
        .order_by(ProducedUnit.created_at.desc(), ProducedUnit.id.desc())
        .all()
    )
    return render_template('production_workspace.html', lines=lines, today_units=today_units, active_tab=active_tab)


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
    produced_unit = ProducedUnit(production_request_line_id=line.id, item_id=line.item_id, target_warehouse_id=line.production_request.target_warehouse_id, produced_at=datetime.utcnow(), status='produced_no_vin')
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
@role_required('logistics')
def logistics_assign_vin(unit_id):
    unit = ProducedUnit.query.get_or_404(unit_id)
    form = AssignVinForm()
    context = _produced_unit_context(unit)
    reserved_vin_row = context.get('vin_registry')
    if reserved_vin_row and (reserved_vin_row.status != 'reserved' or not reserved_vin_row.vin_full):
        reserved_vin_row = None
    if unit.status != 'produced_no_vin':
        flash('По этой единице VIN уже присвоен или она недоступна.', 'warning')
        return redirect(url_for('main.logistics_workspace'))
    if request.method == 'GET':
        form.manufacture_date.data = date.today()
        if reserved_vin_row:
            form.vin.data = reserved_vin_row.vin_full
    if form.validate_on_submit():
        production_warehouse = _default_production_warehouse()
        if not production_warehouse:
            flash('Производственный склад не найден. В справочнике складов отметьте нужный склад как производственный.', 'danger')
            return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row)
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
                return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row)
            vin_registry_row = VinRegistry.query.filter(or_(VinRegistry.vin_full == vin, VinRegistry.serial7 == parsed['serial7'])).first()
            if vin_registry_row and not existing_trailer and vin_registry_row.status not in ('free', 'reserved'):
                form.vin.errors.append('Этот VIN уже занят в реестре.')
                return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row)
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
            return _duplicate_redirect(idem_key, url_for('main.logistics_workspace'))
        if existing_trailer:
            form.vin.errors.append(
                f'VIN уже есть у прицепа Trailer #{existing_trailer.id} на складе. '
                'Нельзя присвоить этот VIN новой выпущенной единице, иначе получится дубль.'
            )
            return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row)
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
        if line and line.supply_need and not vin_registry_row.supply_need_id:
            vin_registry_row.supply_need_id = line.supply_need.id
        _add_vin_event(vin_registry_row, 'assigned', old_vin_status, vin_registry_row.status, comment='VIN привязан к выпущенному прицепу')
        db.session.commit()
        flash('VIN присвоен. Прицеп создан на производственном складе.', 'success')
        return redirect(url_for('main.logistics_workspace'))
    return render_template('assign_vin_form.html', form=form, unit=unit, reserved_vin_row=reserved_vin_row)


@main_bp.route('/logistics/send-trailer', methods=['POST'])
@role_required('logistics')
def logistics_send_trailer():
    form = SendTrailerForm()
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
        return redirect(url_for('main.logistics_workspace'))
    trailer = Trailer.query.get_or_404(form.trailer_id.data)
    to_warehouse = Warehouse.query.get(form.to_warehouse_id.data)
    if not trailer.vin:
        flash('Нельзя отправить прицеп без VIN.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if not trailer.warehouse_id:
        flash('У прицепа не задан текущий склад.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if not to_warehouse:
        flash('Склад назначения не задан.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if trailer.warehouse_id == to_warehouse.id:
        flash('Нельзя отправить прицеп на тот же склад.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if trailer.warehouse_id != production_warehouse.id or trailer.status not in ('IN_STOCK', 'RESERVED', 'SOLD') or (trailer.lifecycle_status or '') in ('in_transit', 'customer_shipped'):
        flash('Этот прицеп нельзя отправить со склада выпуска.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    context = _trailer_production_context(trailer)
    order_id = form.order_id.data or None
    order = CustomerOrder.query.get(order_id) if order_id else None
    if not order and context.get('order'):
        order = context['order']
    if not order and trailer.status == 'SOLD':
        order = _find_order_for_sold_trailer(trailer)
    target_warehouse = order.warehouse if order and order.warehouse_id else context.get('target_warehouse')
    if not target_warehouse or target_warehouse.id == production_warehouse.id:
        flash('Для этого прицепа склад назначения не задан или совпадает со складом выпуска.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if target_warehouse.id != form.to_warehouse_id.data:
        flash(f'Для этого прицепа склад назначения: {target_warehouse.name}.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if _active_movement_for_trailer(trailer.id):
        flash('Этот прицеп уже находится в активном перемещении.', 'warning')
        return redirect(url_for('main.logistics_workspace'))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.logistics_workspace'))
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
    return redirect(url_for('main.logistics_workspace'))


@main_bp.route('/stock-movements/<int:movement_id>/receive', methods=['POST'])
@role_required('manager', 'director')
def stock_movement_receive(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    if not can_receive_movement(movement):
        abort(403)
    if movement.status == 'arrived':
        flash('Перемещение уже принято на склад.', 'warning')
        if current_user.is_manager:
            return redirect(url_for('main.manager_workspace', tab=request.args.get('return_tab') or 'inbound'))
        return redirect(url_for('main.stock_movements_list'))
    idem_key, duplicate = _reserve_idempotency_key()
    if duplicate:
        return _duplicate_redirect(idem_key, url_for('main.manager_workspace') if current_user.is_manager else url_for('main.stock_movements_list'))
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
        return redirect(url_for('main.manager_workspace', tab=request.args.get('return_tab') or 'inbound'))
    return redirect(url_for('main.stock_movements_list'))


@main_bp.route('/orders/<int:order_id>/documents-issued', methods=['POST'])
@role_required('manager', 'director')
def order_documents_issued(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_access_order(order)
    flash('Выдача документов выполняется только через действие в карточке заказа.', 'warning')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/director/dashboard')
@role_required('director')
def director_dashboard():
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    period = request.args.get('period', 'month')
    warehouse_id = request.args.get('warehouse_id', type=int)
    status_filter = (request.args.get('status') or 'all').strip()
    show_zero = request.args.get('show_zero') == '1'
    today = date.today()

    if period == 'day':
        date_from = today
        date_to = today
    elif period == 'week':
        date_from = today - timedelta(days=today.weekday())
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
        vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if row.customer_order and row.customer_order.warehouse_id == warehouse_id]
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

    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
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
@role_required('director')
def director_report(section='sales'):
    sections = {
        'sales': 'Продажи',
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
    period = request.args.get('period', 'month')
    today = date.today()

    if period == 'day':
        date_from = today
        date_to = today
    elif period == 'week':
        date_from = today - timedelta(days=today.weekday())
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
        if warehouse_id:
            query = query.filter(CustomerOrder.warehouse_id == warehouse_id)
        if manager_id:
            query = query.filter(CustomerOrder.assigned_user_id == manager_id)
        return query

    cards = []
    rows = []
    problem_rows = []

    if section in ('sales', 'finance'):
        sales_query = (
            order_scope(CustomerOrder.query)
            .filter(
                CustomerOrder.documents_issued == True,
                CustomerOrder.documents_issued_at >= period_start,
                CustomerOrder.documents_issued_at <= period_end,
                CustomerOrder.status != 'cancelled',
            )
            .order_by(CustomerOrder.documents_issued_at.desc())
        )
        rows = [order for order in sales_query.all() if get_order_effective_vin(order)]
        revenue = sum(float(order.price or 0) for order in rows)
        cards = [
            {'title': 'Выручка', 'value': money(revenue), 'caption': 'юридические продажи за период'},
            {'title': 'Продано', 'value': len(rows), 'caption': 'прицепов'},
            {'title': 'Средний чек', 'value': money(revenue / len(rows) if rows else 0), 'caption': 'по закрытым продажам'},
            {'title': 'Резервы', 'value': order_scope(CustomerOrder.query).filter(CustomerOrder.status.in_(['reserved', 'waiting_payment', 'prepaid', 'ready_to_ship'])).count(), 'caption': 'активные заказы'},
        ]
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

    elif section == 'stock':
        query = Trailer.query.join(Item, Item.id == Trailer.item_id)
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

    elif section == 'production':
        query = ProductionRequestLine.query.join(ProductionRequest, ProductionRequest.id == ProductionRequestLine.production_request_id)
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
            vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if row.customer_order and row.customer_order.warehouse_id == warehouse_id]
        if manager_id:
            vin_assigned_not_confirmed = [row for row in vin_assigned_not_confirmed if row.customer_order and row.customer_order.assigned_user_id == manager_id]
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
        search=search,
        period=period,
        date_from=date_from,
        date_to=date_to,
        cards=cards,
        rows=rows,
        problem_rows=problem_rows,
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
