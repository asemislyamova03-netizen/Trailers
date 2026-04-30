# views.py
from functools import wraps
from datetime import datetime, date, time

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, abort, make_response, jsonify, current_app, make_response,
)
from flask_login import (
    login_required, current_user,
    logout_user, login_user
)

from extensions import db
from models import Trailer, Item, Warehouse, Customer, SalesContract, User, OTTS, Lead, LeadMessage, CustomerOrder, OrderPayment, OrderEvent, Reservation, SupplyNeed, ProductionRequest, ProductionRequestLine, ProducedUnit, StockMovement
from forms import (
    TrailerCreateForm, WarehouseForm, ItemForm,
    CustomerForm, SalesContractForm, LoginForm, UserForm, OTTSForm, LeadForm, CustomerOrderForm, OrderPaymentForm,
    SupplyNeedForm, ProductionRequestForm, ProductionRequestLineForm, StockMovementForm,
    AssignVinForm, SendTrailerForm
)
from collections import defaultdict
import sqlalchemy as sa
from sqlalchemy import or_
import json
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
    if current_user.is_production:
        abort(403)


def _ensure_can_access_order(order: CustomerOrder) -> None:
    _block_production_commercial_access()
    if not can_access_order(order):
        abort(403)


def _ensure_can_manage_order(order: CustomerOrder) -> None:
    _block_production_commercial_access()
    if not can_manage_order(order):
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


@main_bp.app_template_filter('status_label')
def status_label(value):
    labels = {'draft': 'Черновик', 'new': 'Новая', 'waiting_payment': 'Ждём оплату', 'prepaid': 'Предоплата', 'confirmed': 'Подтверждён', 'waiting_production': 'Ожидает производства', 'in_production': 'В производстве', 'produced_waiting_vin': 'Выпущен, ждёт VIN', 'waiting_transfer': 'Ждёт отправки', 'in_transit': 'В пути', 'arrived': 'Прибыл', 'ready_to_ship': 'Готов к выдаче', 'shipped': 'Отгружен', 'done': 'Завершён', 'cancelled': 'Отменён', 'canceled': 'Отменён', 'produced_no_vin': 'Выпущен без VIN', 'vin_assigned': 'VIN присвоен', 'planned': 'Запланирована', 'partial_ready': 'Частично выпущена', 'ready': 'Готово', 'closed': 'Закрыта', 'sent': 'Отправлено', 'in_progress': 'В работе', 'approved': 'Утверждена', 'ready_production_warehouse': 'Готов на складе выпуска', 'stock': 'Из наличия', 'other_warehouse': 'С другого склада', 'production': 'Под производство', 'not_started': 'Не начаты', 'invoice_sent': 'Счёт отправлен', 'contract_ready': 'Договор готов', 'documents_ready': 'Документы готовы', 'documents_issued': 'Документы выданы', 'unpaid': 'Не оплачено', 'partial': 'Частичная оплата', 'paid': 'Оплачено', 'order_created': 'Заказ создан', 'order_status_changed': 'Статус заказа изменён', 'payment_added': 'Оплата добавлена', 'payment_cancelled': 'Оплата отменена', 'trailer_reserved': 'Прицеп зарезервирован', 'trailer_assigned': 'Прицеп назначен', 'production_need_created': 'Создана потребность', 'production_started': 'Производство начато', 'produced_without_vin': 'Выпущено без VIN', 'transfer_requested': 'Запрошено перемещение', 'transfer_started': 'Перемещение начато', 'trailer_received': 'Прицеп принят', 'invoice_sent': 'Счёт отправлен', 'contract_ready': 'Договор готов', 'documents_ready': 'Документы готовы', 'comment_added': 'Комментарий добавлен'}
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
    })
    normalized = str(value or '').lower()
    return labels.get(normalized, value or '')


@main_bp.app_template_filter('status_badge_class')
def status_badge_class(value):
    value = (value or '').lower()
    if value in ('draft', 'new', 'planned', 'manual', 'website', 'phone', 'other'):
        return 'secondary'
    if value in ('in_progress', 'in_production', 'sent', 'in_transit', 'manager_handling', 'telegram'):
        return 'primary'
    if value in ('waiting_payment', 'waiting_production', 'waiting_transfer', 'produced_waiting_vin', 'produced_no_vin', 'invoice_sent', 'partial', 'not_started', 'manager_needed', 'waiting_client'):
        return 'warning'
    if value in ('prepaid', 'partial_ready', 'ai_handling', 'whatsapp', 'instagram'):
        return 'info'
    if value in ('ready', 'arrived', 'ready_to_ship', 'done', 'vin_assigned', 'confirmed', 'paid', 'documents_ready', 'documents_issued', 'contract_ready', 'order_created'):
        return 'success'
    if value in ('cancelled', 'canceled', 'closed', 'spam'):
        return 'dark'
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

    stock_trailers = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .filter(
            Trailer.warehouse_id == warehouse_id,
            Trailer.status == 'IN_STOCK',
        )
        .order_by(Item.article, Trailer.vin)
        .all()
    )
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

    paid_not_shipped_orders = [
        order for order in active_orders
        if order.confirmed_paid_amount > 0 and not order.is_shipped
    ]
    payments = (
        OrderPayment.query
        .join(CustomerOrder, CustomerOrder.id == OrderPayment.order_id)
        .filter(CustomerOrder.warehouse_id == warehouse_id)
        .order_by(OrderPayment.paid_at.desc().nullslast(), OrderPayment.created_at.desc())
        .limit(20)
        .all()
    )
    other_stock = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .join(Warehouse, Warehouse.id == Trailer.warehouse_id)
        .filter(Trailer.warehouse_id != warehouse_id, Trailer.status == 'IN_STOCK')
        .order_by(Warehouse.name, Item.article, Trailer.vin)
        .all()
    )

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
        ready_to_ship_orders=ready_to_ship_orders,
        production_for_warehouse=production_for_warehouse,
        paid_not_shipped_orders=paid_not_shipped_orders,
        payments=payments,
        other_stock=other_stock,
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

@main_bp.route('/customers/new', methods=['GET', 'POST'])
@login_required
def customer_create():
    _block_production_commercial_access()
    form = CustomerForm()

    if form.validate_on_submit():
        is_company = (form.customer_type.data == 'COMPANY')
        opf = _norm_str(getattr(form, "opf", None).data if hasattr(form, "opf") else None)
        is_ip = (is_company and (opf or '').upper() == 'ИП')

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
        db.session.commit()
        flash('Клиент создан', 'success')
        return redirect(url_for('main.customers_list'))

    return render_template('customer_form.html', form=form, title='Новый клиент')


@main_bp.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def customer_edit(customer_id):
    _block_production_commercial_access()
    customer = Customer.query.get_or_404(customer_id)
    form = CustomerForm(obj=customer)

    if form.validate_on_submit():
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
        return redirect(url_for('main.customers_list'))

    return render_template('customer_form.html', form=form, title='Редактирование клиента')


@main_bp.route('/customers/<int:customer_id>/delete')
@login_required
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
    customer = contract.customer
    trailer = contract.trailer
    item = trailer.item if trailer else None

    size_external = getattr(item, 'size_external', None)
    size_body     = getattr(item, 'size_body', None)
    axle_count    = getattr(item, 'axle_count', None)
    payload       = getattr(item, 'payload_kg', None)
    full_mass_kg  = getattr(item, 'full_mass_kg', None)

    modification_code = _extract_modification_from_vin(trailer.vin) if trailer else None

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
    )

@main_bp.route('/contracts')
@login_required
def contracts_list():
    _block_production_commercial_access()
    """Список договоров / продаж. Менеджеры видят ВСЕ договоры."""
    warehouse_id = request.args.get('warehouse_id', type=int)

    q = (request.args.get('q') or '').strip()          # общий поиск: номер/клиент
    vin = (request.args.get('vin') or '').strip()      # поиск по VIN/артикулу
    paid = request.args.get('paid', '')                # '', '1', '0'
    shipped = request.args.get('shipped', '')          # '', '1', '0'

    warehouses = Warehouse.query.order_by(Warehouse.name).all()

    query = (
        SalesContract.query
        .outerjoin(Trailer, SalesContract.trailer_id == Trailer.id)
        .outerjoin(Item, Item.id == Trailer.item_id)
        .outerjoin(Customer, Customer.id == SalesContract.customer_id)
        .outerjoin(Warehouse, Warehouse.id == Trailer.warehouse_id)
    )

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
    form = SalesContractForm()

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

    # ----- Прицепы (только не SOLD) -----
    trailers = (
        Trailer.query
        .filter(Trailer.status != 'SOLD')
        .order_by(Trailer.vin)
        .all()
    )
    form.trailer_id.choices = [
        (t.id, f"{t.vin} — {t.item.article if t.item else ''}")
        for t in trailers
    ]

    # Подставляем номер при открытии формы
    if request.method == 'GET' and not (form.contract_number.data or '').strip():
        form.contract_number.data = get_next_contract_number()

    if form.validate_on_submit():
        trailer = Trailer.query.get(form.trailer_id.data)
        if not trailer:
            flash('Прицеп не найден', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        # защита: на прицеп не должно быть договора
        exists = SalesContract.query.filter(SalesContract.trailer_id == trailer.id).first()
        if exists:
            flash('На этот прицеп уже существует договор.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        if trailer.status == 'SOLD':
            flash('Этот прицеп уже продан', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        cn = _norm_str(form.contract_number.data)
        if not cn:
            cn = get_next_contract_number()
            form.contract_number.data = cn

        if not is_contract_number_unique(cn):
            form.contract_number.errors.append('Такой номер договора уже существует. Введите другой.')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        contract = SalesContract(
            contract_number=cn,
            contract_date=form.contract_date.data,
            customer_id=form.customer_id.data,
            trailer_id=trailer.id,
            price=form.price.data,
            payment_method=_norm_str(form.payment_method.data),
            source='manual',
            is_paid=bool(form.is_paid.data),
            is_shipped=bool(form.is_shipped.data),
        )

        trailer.status = 'SOLD'
        db.session.add(contract)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Конфликт сохранения (прицеп уже занят другим договором). Обнови страницу и попробуй снова.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        flash('Договор успешно создан, прицеп помечен как "Продан"', 'success')
        return redirect(url_for('main.contracts_list'))

    return render_template('contract_form.html', form=form, form_title='Новый договор')


@main_bp.route('/contracts/<int:contract_id>/edit', methods=['GET', 'POST'])
@login_required
def contract_edit(contract_id):
    _block_production_commercial_access()
    contract = SalesContract.query.get_or_404(contract_id)
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
                old_trailer.status = 'IN_STOCK'

        # новый прицеп помечаем проданным
        new_trailer.status = 'SOLD'

        contract.contract_date = form.contract_date.data
        contract.customer_id = form.customer_id.data
        contract.trailer_id = new_trailer.id
        contract.price = form.price.data
        contract.payment_method = _norm_str(form.payment_method.data)
        contract.is_paid = bool(form.is_paid.data)
        contract.is_shipped = bool(form.is_shipped.data)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Конфликт сохранения (номер/прицеп). Проверь данные и попробуй снова.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        flash('Договор обновлён', 'success')
        return redirect(url_for('main.contracts_list'))

    return render_template('contract_form.html', form=form, form_title='Редактирование договора')

@main_bp.route('/contracts/<int:contract_id>/delete', methods=['POST'])
@login_required
def contract_delete(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    trailer = contract.trailer

    db.session.delete(contract)
    db.session.flush()

    if trailer:
        other_cnt = SalesContract.query.filter_by(trailer_id=trailer.id).count()
        if other_cnt == 0:
            trailer.status = 'IN_STOCK'

    db.session.commit()
    flash('Договор удалён', 'success')
    return redirect(url_for('main.contracts_list'))


@main_bp.route('/contracts/<int:contract_id>/print')
@login_required
def contract_print(contract_id):
    ctx = _build_contract_context(contract_id)
    return render_template('contract_print.html', **ctx)


@main_bp.route('/contracts/<int:contract_id>/pdf')
@login_required
def contract_pdf(contract_id):
    if HTML is not None:
        from flask import make_response, request
        ctx = _build_contract_context(contract_id)
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
    return render_template("contract_sign.html", contract=contract)

@main_bp.route('/contracts/<int:contract_id>/sigex/pdf_base64')
@login_required
def contract_sigex_pdf_base64(contract_id):
    pdf_bytes = _contract_pdf_bytes(contract_id)
    return jsonify({"pdfBase64": base64.b64encode(pdf_bytes).decode("utf-8")})

@main_bp.route('/contracts/<int:contract_id>/sigex/preregister', methods=['POST'])
@login_required
def contract_sigex_preregister(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)

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

    if order.is_shipped:
        order.status = 'shipped'
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


def _fill_order_form_choices(form: CustomerOrderForm, item_id_prefill: int | None = None, current_order_id: int | None = None) -> None:
    leads = Lead.query.order_by(Lead.created_at.desc(), Lead.id.desc()).all()
    form.lead_id.choices = [(0, '— без заявки —')] + [(l.id, f'#{l.id} {l.customer_name} / {l.channel or l.source_channel}') for l in leads]
    form.customer_id.choices = [(c.id, c.name) for c in Customer.query.filter_by(is_active=True).order_by(Customer.name).all()]
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    form.item_id.choices = [(i.id, f'{i.article or ""} — {i.name}') for i in items]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in User.query.order_by(User.full_name, User.username).all()]

    trailer_query = Trailer.query.filter(Trailer.status.in_(['IN_STOCK', 'RESERVED'])).order_by(Trailer.vin)
    if item_id_prefill:
        trailer_query = trailer_query.filter(Trailer.item_id == item_id_prefill)
    trailers = []
    for trailer in trailer_query.all():
        active = _active_reservation_for_trailer(trailer.id, exclude_order_id=current_order_id)
        if not active:
            trailers.append(trailer)
    form.trailer_id.choices = [(0, '— подобрать позже / под заказ —')] + [
        (t.id, f'{t.vin} — {t.item.article if t.item else ""} — {t.warehouse.name if t.warehouse else ""} — {t.status}')
        for t in trailers
    ]


def _fill_supply_need_form_choices(form: SupplyNeedForm) -> None:
    form.order_id.choices = [(0, '— без заказа / на склад —')] + [
        (o.id, f'{o.order_number} — {o.customer.name if o.customer else ""}') for o in CustomerOrder.query.order_by(CustomerOrder.created_at.desc()).all()
    ]
    form.item_id.choices = [
        (i.id, f'{i.article or ""} — {i.name}') for i in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]


def _fill_production_line_form_choices(form: ProductionRequestLineForm) -> None:
    form.supply_need_id.choices = [(0, '— без потребности —')] + [
        (n.id, f'#{n.id} {n.item.article if n.item else ""} / {n.status}') for n in SupplyNeed.query.order_by(SupplyNeed.priority.asc(), SupplyNeed.created_at.desc()).all()
    ]
    form.item_id.choices = [
        (i.id, f'{i.article or ""} — {i.name}') for i in Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    ]


def _fill_stock_movement_form_choices(form: StockMovementForm) -> None:
    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.from_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in warehouses]
    form.to_warehouse_id.choices = [(0, '— нет —')] + [(w.id, w.name) for w in warehouses]
    form.trailer_id.choices = [(0, '— без VIN —')] + [
        (t.id, f'{t.vin} — {t.item.article if t.item else ""} — {t.status}') for t in Trailer.query.order_by(Trailer.vin).all()
    ]
    form.order_id.choices = [(0, '— без заказа —')] + [
        (o.id, f'{o.order_number} — {o.customer.name if o.customer else ""}') for o in CustomerOrder.query.order_by(CustomerOrder.created_at.desc()).all()
    ]


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

    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    form.desired_item_id.choices = [(0, '— не выбрано —')] + [(i.id, f'{i.article or ""} — {i.name}') for i in items]

    warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in warehouses]

    users = User.query.order_by(User.full_name, User.username).all()
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in users]

    if request.method == 'GET':
        if current_user.warehouse_id:
            form.warehouse_id.data = current_user.warehouse_id
        if current_user.is_manager:
            form.assigned_user_id.data = current_user.id
        form.created_at.data = date.today()

    if form.validate_on_submit():
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
        db.session.commit()
        flash('Заявка создана', 'success')
        if request.args.get('return_to') == 'manager_workspace' or current_user.is_manager:
            return redirect(url_for('main.manager_workspace'))
        return redirect(url_for('main.leads_list'))

    return render_template('lead_form.html', form=form, title='Новая заявка')


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

    query = CustomerOrder.query.join(Customer, Customer.id == CustomerOrder.customer_id).join(Item, Item.id == CustomerOrder.item_id)
    if not current_user.can_view_all and current_user.warehouse_id:
        query = query.filter(or_(CustomerOrder.warehouse_id == current_user.warehouse_id, CustomerOrder.warehouse_id.is_(None)))

    if status:
        query = query.filter(CustomerOrder.status == status)
    if q:
        like = f'%{q}%'
        query = query.filter(or_(CustomerOrder.order_number.ilike(like), Customer.name.ilike(like), Item.article.ilike(like), Item.name.ilike(like)))

    orders = query.order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc()).all()
    return render_template('orders_list.html', orders=orders, status=status, q=q)


@main_bp.route('/orders/new', methods=['GET', 'POST'])
@login_required
def order_create():
    _block_production_commercial_access()
    form = CustomerOrderForm()
    item_id_prefill = request.args.get('item_id', type=int)
    trailer_id_prefill = request.args.get('trailer_id', type=int)
    lead_id_prefill = request.args.get('lead_id', type=int)
    _fill_order_form_choices(form, item_id_prefill=item_id_prefill)

    if request.method == 'GET':
        form.order_number.data = _next_number('ORD', CustomerOrder, 'order_number')
        form.status.data = 'waiting_payment'
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
        if item_id_prefill:
            form.item_id.data = item_id_prefill
        if trailer_id_prefill:
            trailer = Trailer.query.get(trailer_id_prefill)
            if trailer:
                form.trailer_id.data = trailer.id
                form.item_id.data = trailer.item_id
                form.fulfillment_source.data = 'stock' if trailer.warehouse_id == form.warehouse_id.data else 'other_warehouse'

    if form.validate_on_submit():
        order_number = (form.order_number.data or '').strip() or _next_number('ORD', CustomerOrder, 'order_number')
        if CustomerOrder.query.filter_by(order_number=order_number).first():
            flash('Такой номер заказа уже существует', 'danger')
            return render_template('order_form.html', form=form, title='Новый заказ')
        selected_trailer = Trailer.query.get(form.trailer_id.data) if form.trailer_id.data else None
        if selected_trailer and _active_reservation_for_trailer(selected_trailer.id):
            flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
            return render_template('order_form.html', form=form, title='Новый заказ')
        if selected_trailer and selected_trailer.status != 'IN_STOCK':
            flash('Можно резервировать только прицеп в наличии.', 'danger')
            return render_template('order_form.html', form=form, title='Новый заказ')
        if current_user.is_manager and form.warehouse_id.data and current_user.warehouse_id != form.warehouse_id.data:
            abort(403)
        if current_user.is_manager and selected_trailer and (form.fulfillment_source.data or 'stock') == 'stock' and selected_trailer.warehouse_id != current_user.warehouse_id:
            flash('Для сценария из наличия менеджер может выбрать только прицеп своего склада.', 'danger')
            return render_template('order_form.html', form=form, title='Новый заказ')

        order = CustomerOrder(
            order_number=order_number,
            lead_id=form.lead_id.data or None,
            customer_id=form.customer_id.data,
            item_id=form.item_id.data,
            trailer_id=form.trailer_id.data or None,
            warehouse_id=form.warehouse_id.data or None,
            assigned_user_id=form.assigned_user_id.data or None,
            quantity=form.quantity.data or 1,
            price=form.price.data,
            prepayment_percent=form.prepayment_percent.data,
            status=form.status.data,
            fulfillment_source=(form.fulfillment_source.data or '').strip() or None,
            expected_date=form.expected_date.data,
            documents_issued=bool(form.documents_issued.data),
            is_shipped=bool(form.is_shipped.data),
            note=(form.note.data or '').strip() or None,
            manager_comment=(form.manager_comment.data or '').strip() or None,
        )
        db.session.add(order)
        db.session.flush()
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
            source_type = 'TRANSFER' if order.fulfillment_source == 'other_warehouse' or selected_trailer.warehouse_id != order.warehouse_id else 'STOCK'
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
            order.fulfillment_source = 'other_warehouse' if source_type == 'TRANSFER' else (order.fulfillment_source or 'stock')
            if source_type == 'TRANSFER':
                order.status = 'waiting_transfer'
            add_order_event(order, 'trailer_reserved', new_value=selected_trailer.vin, comment='Резерв при создании заказа')
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
                note='Потребность создана автоматически из заказа клиента',
            )
            db.session.add(need)
            order.fulfillment_source = order.fulfillment_source or 'production'
            add_order_event(order, 'production_need_created', new_value='NEW', comment='Потребность создана автоматически из заказа клиента')

        _refresh_order_status(order)
        if selected_trailer and order.fulfillment_source == 'other_warehouse':
            order.status = 'waiting_transfer'
        db.session.commit()
        flash('Заказ создан', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    return render_template('order_form.html', form=form, title='Новый заказ')


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

        # если предоплата подтверждена и нет резерва — создаём/поддерживаем потребность
        if payment.status == 'CONFIRMED' and order.trailer_id is None and order.supply_needs.count() == 0:
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
    old_status = payment.status
    payment.status = 'CANCELED'
    _refresh_order_status(order)
    add_order_event(order, 'payment_cancelled', old_value=old_status, new_value='CANCELED', comment=request.form.get('reason') or None)
    db.session.commit()
    flash('Оплата отменена', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/supply-needs')
@login_required
def supply_needs_list():
    status = request.args.get('status', '').strip()
    query = SupplyNeed.query.join(Item, Item.id == SupplyNeed.item_id)

    if not current_user.is_admin and current_user.warehouse_id:
        query = query.filter(or_(SupplyNeed.warehouse_id == current_user.warehouse_id, SupplyNeed.warehouse_id.is_(None)))
    if status:
        query = query.filter(SupplyNeed.status == status)

    needs = query.order_by(SupplyNeed.priority.asc(), SupplyNeed.created_at.desc()).all()
    return render_template('supply_needs_list.html', needs=needs, status=status)


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
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    form.desired_item_id.choices = [(0, '— не выбрано —')] + [(i.id, f'{i.article or ""} — {i.name}') for i in items]
    form.warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    form.assigned_user_id.choices = [(0, '— не назначен —')] + [(u.id, u.full_name or u.username) for u in User.query.order_by(User.full_name, User.username).all()]

    if request.method == 'GET':
        form.created_at.data = lead.created_at.date() if lead.created_at else date.today()
        form.source_channel.data = lead.channel or (lead.source_channel or 'MANUAL').lower()
        form.source_name.data = lead.source_name or lead.source_account
        form.desired_item_id.data = lead.desired_item_id or 0
        form.warehouse_id.data = lead.warehouse_id or 0
        form.assigned_user_id.data = lead.assigned_user_id or 0

    if form.validate_on_submit():
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
        db.session.commit()
        flash('Заявка обновлена', 'success')
        return redirect(url_for('main.lead_detail', lead_id=lead.id))

    return render_template('lead_form.html', form=form, title='Редактирование заявки')


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

    stock_query = Trailer.query.filter(Trailer.item_id == order.item_id, Trailer.status == 'IN_STOCK').order_by(Trailer.vin)
    if current_user.is_manager:
        stock_query = stock_query.filter(Trailer.warehouse_id == current_user.warehouse_id)
    available_stock_trailers = stock_query.all()
    other_warehouse_trailers = (
        Trailer.query
        .filter(
            Trailer.item_id == order.item_id,
            Trailer.status == 'IN_STOCK',
            Trailer.warehouse_id != order.warehouse_id,
        )
        .order_by(Trailer.vin)
        .all()
    )
    movements = StockMovement.query.filter_by(order_id=order.id).order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()
    events = order.events.order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc()).all()
    payments = order.payments.order_by(OrderPayment.created_at.desc()).all()
    reservations = order.reservations.order_by(Reservation.created_at.desc()).all()
    supply_needs = order.supply_needs.order_by(SupplyNeed.created_at.desc()).all()
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
        form.trailer_id.data = order.trailer_id or 0
        form.warehouse_id.data = order.warehouse_id or 0
        form.assigned_user_id.data = order.assigned_user_id or 0

    if form.validate_on_submit():
        old_status = order.status
        new_trailer_id = form.trailer_id.data or None
        if new_trailer_id and _active_reservation_for_trailer(new_trailer_id, exclude_order_id=order.id):
            flash('Этот прицеп уже зарезервирован под другой активный заказ.', 'danger')
            return render_template('order_form.html', form=form, title='Редактирование заказа')

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
        order.documents_issued = bool(form.documents_issued.data)
        order.is_shipped = bool(form.is_shipped.data)
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
        elif order.supply_needs.count() == 0:
            db.session.add(SupplyNeed(order_id=order.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity, status='NEW', priority=10, need_type='CUSTOMER_ORDER', required_by=order.expected_date))
            order.fulfillment_source = order.fulfillment_source or 'production'

        _refresh_order_status(order)
        if old_status != order.status:
            add_order_event(order, 'order_status_changed', old_value=old_status, new_value=order.status)
        if old_trailer_id != new_trailer_id and new_trailer_id:
            add_order_event(order, 'trailer_reserved', new_value=new_trailer_id)
        db.session.commit()
        flash('Заказ обновлен', 'success')
        return redirect(url_for('main.order_detail', order_id=order.id))

    return render_template('order_form.html', form=form, title='Редактирование заказа')


@main_bp.route('/orders/<int:order_id>/contract/new', methods=['POST'])
@login_required
def order_contract_create(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if not order.trailer_id:
        flash('Для договора нужен конкретный прицеп/VIN. Сначала зарезервируйте прицеп.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    existing = SalesContract.query.filter_by(order_id=order.id).first()
    if existing:
        flash('Договор по этому заказу уже создан.', 'warning')
        return redirect(url_for('main.contracts_list'))
    if SalesContract.query.filter_by(trailer_id=order.trailer_id).first():
        flash('На этот прицеп уже существует договор.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    contract = SalesContract(
        contract_number=get_next_contract_number(),
        contract_date=date.today(),
        customer_id=order.customer_id,
        trailer_id=order.trailer_id,
        order_id=order.id,
        price=order.price,
        payment_method='order',
        source='customer_order',
        is_paid=(order.remaining_amount == 0 and float(order.price or 0) > 0),
        is_shipped=bool(order.is_shipped),
    )
    db.session.add(contract)
    order.document_status = 'contract_ready'
    add_order_event(order, 'contract_ready', new_value=contract.contract_number or contract.id)
    db.session.commit()
    flash('Договор создан из заказа. Прицеп не переведен в SOLD этим действием.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/ship', methods=['POST'])
@login_required
def order_ship(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if not can_ship_order(order) or not order.trailer_id or order.status == 'cancelled' or order.is_shipped:
        abort(403)
    if not order.trailer:
        abort(400)
    if not current_user.is_admin and order.trailer.warehouse_id != order.warehouse_id:
        flash('Прицеп ещё не на складе выдачи.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    order.is_shipped = True
    order.shipped_at = datetime.utcnow()
    order.status = 'shipped'
    if order.trailer:
        order.trailer.status = 'SOLD'
        order.trailer.lifecycle_status = 'sold'
    for reservation in order.reservations.filter_by(status='ACTIVE').all():
        reservation.status = 'CLOSED'
    db.session.add(StockMovement(movement_type='customer_shipment', status='arrived', order_id=order.id, trailer_id=order.trailer_id, from_warehouse_id=order.warehouse_id, departure_date=date.today(), arrival_date=date.today(), received_at=datetime.utcnow(), note=f'Отгрузка клиенту по заказу №{order.order_number}'))
    add_order_event(order, 'shipped', new_value=order.trailer.vin if order.trailer else order.trailer_id)
    db.session.commit()
    flash('Заказ отгружен, прицеп переведен в SOLD.', 'success')
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
    if trailer.status != 'IN_STOCK' or _active_reservation_for_trailer(trailer.id, exclude_order_id=order.id):
        flash('Этот прицеп уже недоступен для резерва.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    old_trailer_id = order.trailer_id
    order.trailer_id = trailer.id
    order.fulfillment_source = 'stock'
    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    db.session.add(Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, status='ACTIVE', source_type='STOCK', priority=10))
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
    if trailer.status != 'IN_STOCK' or trailer.warehouse_id == order.warehouse_id:
        flash('Для запроса перемещения нужен свободный прицеп на другом складе.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
    if _active_reservation_for_trailer(trailer.id, exclude_order_id=order.id):
        flash('Этот прицеп уже зарезервирован.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))

    order.trailer_id = trailer.id
    order.fulfillment_source = 'other_warehouse'
    order.status = 'waiting_transfer'
    trailer.status = 'RESERVED'
    trailer.lifecycle_status = 'reserved'
    db.session.add(Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, status='ACTIVE', source_type='TRANSFER', priority=10))
    add_order_event(order, 'transfer_requested', new_value=trailer.vin, comment='Прицеп зарезервирован на другом складе. Нужна отправка.')
    db.session.commit()
    flash('Прицеп зарезервирован на другом складе. Нужна отправка.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/orders/<int:order_id>/create-production-need', methods=['POST'])
@login_required
def order_create_production_need(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    _ensure_can_manage_order(order)
    if order.trailer_id or order.is_shipped or order.status == 'cancelled':
        abort(400)
    existing = order.supply_needs.filter(SupplyNeed.status.notin_(['CANCELLED', 'CANCELED', 'CLOSED'])).first()
    if existing:
        flash('Потребность по этому заказу уже существует.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))
    need = SupplyNeed(order_id=order.id, item_id=order.item_id, warehouse_id=order.warehouse_id, quantity=order.quantity or 1, required_by=order.expected_date, status='NEW', need_type='CUSTOMER_ORDER', priority=10, note='Потребность создана из карточки заказа')
    db.session.add(need)
    order.fulfillment_source = 'production'
    old_status = order.status
    order.status = 'waiting_production'
    add_order_event(order, 'production_need_created', old_value=old_status, new_value='waiting_production')
    db.session.commit()
    flash('Потребность в производство создана', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


def _mark_order_document(order: CustomerOrder, status: str, event_type: str, message: str):
    _ensure_can_manage_order(order)
    old_status = order.document_status
    order.document_status = status
    if status == 'documents_issued':
        order.documents_issued = True
        order.documents_issued_at = datetime.utcnow()
    add_order_event(order, event_type, old_value=old_status, new_value=status)
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


@main_bp.route('/orders/<int:order_id>/issue-documents', methods=['POST'])
@login_required
def order_issue_documents(order_id):
    return _mark_order_document(CustomerOrder.query.get_or_404(order_id), 'documents_issued', 'documents_issued', 'Документы выданы')


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
    if not current_user.is_admin and (order.confirmed_paid_amount > 0 or order.is_shipped):
        flash('Менеджер может отменить только заказ без подтверждённых оплат и без отгрузки.', 'danger')
        return redirect(url_for('main.order_detail', order_id=order.id))
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
    db.session.commit()
    flash('Заказ отменён', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@main_bp.route('/supply-needs/new', methods=['GET', 'POST'])
@login_required
def supply_need_create():
    form = SupplyNeedForm()
    _fill_supply_need_form_choices(form)
    if form.validate_on_submit():
        need = SupplyNeed(
            need_type=form.need_type.data,
            status=form.status.data,
            order_id=form.order_id.data or None,
            item_id=form.item_id.data,
            warehouse_id=form.warehouse_id.data or None,
            quantity=form.quantity.data,
            priority=form.priority.data,
            required_by=form.required_by.data,
            note=(form.note.data or '').strip() or None,
        )
        db.session.add(need)
        db.session.commit()
        flash('Потребность создана', 'success')
        return redirect(url_for('main.supply_needs_list'))
    return render_template('supply_need_form.html', form=form, title='Новая потребность')


@main_bp.route('/supply-needs/<int:need_id>/edit', methods=['GET', 'POST'])
@login_required
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
        need.order_id = form.order_id.data or None
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
    pr = ProductionRequest(request_number=_next_number('PR', ProductionRequest, 'request_number'), status='approved', target_warehouse_id=need.warehouse_id, note=f'Создана из потребности #{need.id}')
    db.session.add(pr)
    db.session.flush()
    db.session.add(ProductionRequestLine(production_request_id=pr.id, supply_need_id=need.id, item_id=need.item_id, quantity=need.quantity, produced_qty=0, status='planned', note=need.note))
    need.status = 'IN_PRODUCTION'
    if need.order:
        need.order.status = 'in_production'
    db.session.commit()
    flash('Заявка на производство создана из потребности', 'success')
    return redirect(url_for('main.production_request_detail', request_id=pr.id))


@main_bp.route('/production-requests')
@login_required
def production_requests_list():
    status = request.args.get('status', '').strip()
    query = ProductionRequest.query
    if status:
        query = query.filter_by(status=status)
    requests = query.order_by(ProductionRequest.created_at.desc(), ProductionRequest.id.desc()).all()
    return render_template('production_requests_list.html', requests=requests, status=status)


@main_bp.route('/production-requests/new', methods=['GET', 'POST'])
@login_required
def production_request_create():
    form = ProductionRequestForm()
    form.target_warehouse_id.choices = [(0, '— не выбрано —')] + [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all()]
    if request.method == 'GET':
        form.request_number.data = _next_number('PR', ProductionRequest, 'request_number')
        form.status.data = 'draft'
    if form.validate_on_submit():
        pr = ProductionRequest(request_number=(form.request_number.data or '').strip() or _next_number('PR', ProductionRequest, 'request_number'), status=form.status.data, target_warehouse_id=form.target_warehouse_id.data or None, note=(form.note.data or '').strip() or None)
        db.session.add(pr)
        db.session.commit()
        flash('Заявка на производство создана', 'success')
        return redirect(url_for('main.production_request_detail', request_id=pr.id))
    return render_template('production_request_form.html', form=form, title='Новая заявка на производство')


@main_bp.route('/production-requests/<int:request_id>', methods=['GET', 'POST'])
@login_required
def production_request_detail(request_id):
    pr = ProductionRequest.query.get_or_404(request_id)
    form = ProductionRequestLineForm()
    _fill_production_line_form_choices(form)
    if form.validate_on_submit():
        line = ProductionRequestLine(production_request_id=pr.id, supply_need_id=form.supply_need_id.data or None, item_id=form.item_id.data, quantity=form.quantity.data, status=form.status.data, note=(form.note.data or '').strip() or None)
        db.session.add(line)
        if line.supply_need:
            line.supply_need.status = 'IN_PRODUCTION'
        db.session.commit()
        flash('Позиция добавлена', 'success')
        return redirect(url_for('main.production_request_detail', request_id=pr.id))
    return render_template('production_request_detail.html', pr=pr, form=form)


@main_bp.route('/production-requests/<int:request_id>/edit', methods=['GET', 'POST'])
@login_required
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
    query = StockMovement.query
    if status:
        query = query.filter_by(status=status)
    movements = query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()
    return render_template('stock_movements_list.html', movements=movements, status=status)


@main_bp.route('/stock-movements/new', methods=['GET', 'POST'])
@login_required
def stock_movement_create():
    form = StockMovementForm()
    _fill_stock_movement_form_choices(form)
    if form.validate_on_submit():
        movement = StockMovement(
            from_warehouse_id=form.from_warehouse_id.data or None,
            to_warehouse_id=form.to_warehouse_id.data or None,
            trailer_id=form.trailer_id.data or None,
            order_id=form.order_id.data or None,
            movement_type=form.movement_type.data,
            status=form.status.data,
            departure_date=form.departure_date.data,
            arrival_date=form.arrival_date.data,
            note=(form.note.data or '').strip() or None,
        )
        db.session.add(movement)
        _apply_arrived_stock_movement(movement)
        db.session.commit()
        flash('Перемещение создано', 'success')
        return redirect(url_for('main.stock_movements_list'))
    return render_template('stock_movement_form.html', form=form, title='Новое перемещение')


@main_bp.route('/stock-movements/<int:movement_id>/edit', methods=['GET', 'POST'])
@login_required
def stock_movement_edit(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    form = StockMovementForm(obj=movement)
    _fill_stock_movement_form_choices(form)
    if request.method == 'GET':
        form.from_warehouse_id.data = movement.from_warehouse_id or 0
        form.to_warehouse_id.data = movement.to_warehouse_id or 0
        form.trailer_id.data = movement.trailer_id or 0
        form.order_id.data = movement.order_id or 0
    if form.validate_on_submit():
        movement.from_warehouse_id = form.from_warehouse_id.data or None
        movement.to_warehouse_id = form.to_warehouse_id.data or None
        movement.trailer_id = form.trailer_id.data or None
        movement.order_id = form.order_id.data or None
        movement.movement_type = form.movement_type.data
        movement.status = form.status.data
        movement.departure_date = form.departure_date.data
        movement.arrival_date = form.arrival_date.data
        movement.note = (form.note.data or '').strip() or None
        _apply_arrived_stock_movement(movement)
        db.session.commit()
        flash('Перемещение обновлено', 'success')
        return redirect(url_for('main.stock_movements_list'))
    return render_template('stock_movement_form.html', form=form, title='Редактирование перемещения')


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
        movement.order.status = 'ready_to_ship'


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
    line.status = 'in_production'
    line.started_at = line.started_at or datetime.utcnow()
    line.production_request.status = 'in_progress'
    if line.supply_need:
        line.supply_need.status = 'IN_PRODUCTION'
        if line.supply_need.order:
            line.supply_need.order.status = 'in_production'
            add_order_event(line.supply_need.order, 'production_started', new_value=line.production_request.request_number)
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
    ready_trailers = (
        Trailer.query
        .filter(
            Trailer.warehouse_id == production_warehouse.id,
            Trailer.status.in_(['IN_STOCK', 'RESERVED']),
            or_(Trailer.lifecycle_status.is_(None), ~Trailer.lifecycle_status.in_(['in_transit', 'sold'])),
        )
        .order_by(Trailer.created_at.desc())
        .all()
        if production_warehouse else []
    )
    inbound = StockMovement.query.filter(StockMovement.status.in_(['sent', 'in_transit'])).order_by(StockMovement.created_at.desc()).all()
    send_form = SendTrailerForm()
    send_form.trailer_id.choices = [(t.id, f'{t.vin} — {t.item.article if t.item else ""}') for t in ready_trailers]
    send_form.to_warehouse_id.choices = [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all() if not production_warehouse or w.id != production_warehouse.id]
    send_form.order_id.choices = [(0, '— без заказа —')] + [(o.id, o.order_number) for o in CustomerOrder.query.filter(CustomerOrder.status.notin_(['done', 'cancelled'])).order_by(CustomerOrder.created_at.desc()).all()]
    return render_template(
        'logistics_workspace.html',
        production_warehouse=production_warehouse,
        production_warehouses=production_warehouses,
        production_warehouse_warning=production_warehouse_warning,
        production_needs=production_needs,
        production_lines=production_lines,
        produced_units=produced_units,
        ready_trailers=ready_trailers,
        inbound=inbound,
        send_form=send_form,
    )


@main_bp.route('/logistics/produced-units/<int:unit_id>/assign-vin', methods=['GET', 'POST'])
@role_required('logistics')
def logistics_assign_vin(unit_id):
    unit = ProducedUnit.query.get_or_404(unit_id)
    form = AssignVinForm()
    if request.method == 'GET':
        form.manufacture_date.data = date.today()
    if form.validate_on_submit():
        production_warehouse = _default_production_warehouse()
        if not production_warehouse:
            flash('Производственный склад не найден. В справочнике складов отметьте нужный склад как производственный.', 'danger')
            return render_template('assign_vin_form.html', form=form, unit=unit)
        vin = (form.vin.data or '').strip()
        if Trailer.query.filter_by(vin=vin).first():
            form.vin.errors.append('Такой VIN уже существует.')
            return render_template('assign_vin_form.html', form=form, unit=unit)
        line = unit.production_request_line
        order = line.supply_need.order if line and line.supply_need else None
        trailer = Trailer(vin=vin, item_id=unit.item_id, warehouse_id=production_warehouse.id, manufacture_date=form.manufacture_date.data, status='RESERVED' if order else 'IN_STOCK', lifecycle_status='ready_production_warehouse')
        db.session.add(trailer)
        db.session.flush()
        unit.status = 'vin_assigned'
        unit.trailer_id = trailer.id
        if order:
            order.trailer_id = trailer.id
            order.status = 'waiting_transfer' if unit.target_warehouse_id and unit.target_warehouse_id != production_warehouse.id else 'ready_to_ship'
            db.session.add(Reservation(order_id=order.id, trailer_id=trailer.id, item_id=order.item_id, status='ACTIVE', source_type='PRODUCTION', priority=10))
            add_order_event(order, 'vin_assigned', new_value=vin)
            add_order_event(order, 'trailer_assigned', new_value=vin)
        db.session.commit()
        flash('VIN присвоен. Прицеп создан на производственном складе.', 'success')
        return redirect(url_for('main.logistics_workspace'))
    return render_template('assign_vin_form.html', form=form, unit=unit)


@main_bp.route('/logistics/send-trailer', methods=['POST'])
@role_required('logistics')
def logistics_send_trailer():
    form = SendTrailerForm()
    production_warehouse = _default_production_warehouse()
    ready_query = Trailer.query.filter(
        Trailer.warehouse_id == production_warehouse.id,
        Trailer.status.in_(['IN_STOCK', 'RESERVED']),
        or_(Trailer.lifecycle_status.is_(None), ~Trailer.lifecycle_status.in_(['in_transit', 'sold'])),
    ) if production_warehouse else Trailer.query.filter(False)
    form.trailer_id.choices = [(t.id, t.vin) for t in ready_query.order_by(Trailer.vin).all()]
    form.to_warehouse_id.choices = [(w.id, w.name) for w in Warehouse.query.filter_by(is_active=True).order_by(Warehouse.name).all() if not production_warehouse or w.id != production_warehouse.id]
    form.order_id.choices = [(0, '— без заказа —')] + [(o.id, o.order_number) for o in CustomerOrder.query.all()]
    if not form.validate_on_submit() or not production_warehouse:
        flash('Не удалось отправить прицеп. Проверьте производственный склад и данные формы.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    trailer = Trailer.query.get_or_404(form.trailer_id.data)
    if trailer.warehouse_id != production_warehouse.id or trailer.status in ('IN_TRANSIT', 'SOLD') or (trailer.lifecycle_status or '') in ('in_transit', 'sold'):
        flash('Этот прицеп нельзя отправить со склада выпуска.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    if form.to_warehouse_id.data == production_warehouse.id:
        flash('Нельзя отправить прицеп на тот же склад.', 'danger')
        return redirect(url_for('main.logistics_workspace'))
    movement = StockMovement(movement_type='warehouse_transfer', status='in_transit', from_warehouse_id=production_warehouse.id, to_warehouse_id=form.to_warehouse_id.data, trailer_id=trailer.id, order_id=form.order_id.data or None, departure_date=form.departure_date.data or date.today(), arrival_date=form.arrival_date.data, note=(form.note.data or '').strip() or None)
    trailer.status = 'IN_TRANSIT'
    trailer.lifecycle_status = 'in_transit'
    if movement.order:
        movement.order.status = 'in_transit'
        add_order_event(movement.order, 'transfer_started', new_value=trailer.vin, comment=f'Отправка на склад #{form.to_warehouse_id.data}')
    db.session.add(movement)
    db.session.commit()
    flash('Прицеп отправлен на склад.', 'success')
    return redirect(url_for('main.logistics_workspace'))


@main_bp.route('/stock-movements/<int:movement_id>/receive', methods=['POST'])
@role_required('manager', 'director')
def stock_movement_receive(movement_id):
    movement = StockMovement.query.get_or_404(movement_id)
    if not can_receive_movement(movement):
        abort(403)
    movement.status = 'arrived'
    movement.received_at = datetime.utcnow()
    _apply_arrived_stock_movement(movement)
    if movement.trailer:
        movement.trailer.status = 'RESERVED' if _active_reservation_for_trailer(movement.trailer.id) else 'IN_STOCK'
        movement.trailer.lifecycle_status = 'arrived'
    if movement.order:
        movement.order.status = 'ready_to_ship'
        add_order_event(movement.order, 'trailer_received', new_value=movement.trailer.vin if movement.trailer else movement.trailer_id)
    db.session.commit()
    flash('Прицеп принят на склад', 'success')
    if current_user.is_manager:
        return redirect(url_for('main.manager_workspace'))
    return redirect(url_for('main.stock_movements_list'))


@main_bp.route('/orders/<int:order_id>/documents-issued', methods=['POST'])
@role_required('manager', 'director')
def order_documents_issued(order_id):
    order = CustomerOrder.query.get_or_404(order_id)
    return _mark_order_document(order, 'documents_issued', 'documents_issued', 'Выдача документов отмечена')


@main_bp.route('/director/dashboard')
@role_required('director')
def director_dashboard():
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    items = Item.query.filter(Item.item_type == 'TRAILER', Item.is_active == True).order_by(Item.article, Item.name).all()
    rows = []
    for item in items:
        row = {'item': item, 'warehouses': {}, 'in_production': 0, 'produced_no_vin': 0, 'in_transit': 0, 'reserved': 0, 'sold': 0}
        for warehouse in warehouses:
            row['warehouses'][warehouse.id] = Trailer.query.filter_by(item_id=item.id, warehouse_id=warehouse.id, status='IN_STOCK').count()
        active_lines = ProductionRequestLine.query.filter_by(item_id=item.id).filter(ProductionRequestLine.status.in_(['planned', 'PLANNED', 'in_production', 'partial_ready'])).all()
        row['in_production'] = sum(max((line.quantity or 0) - (line.produced_qty or 0), 0) for line in active_lines)
        row['produced_no_vin'] = ProducedUnit.query.filter_by(item_id=item.id, status='produced_no_vin').count()
        row['in_transit'] = Trailer.query.filter_by(item_id=item.id, status='IN_TRANSIT').count()
        row['reserved'] = Trailer.query.filter_by(item_id=item.id, status='RESERVED').count()
        row['sold'] = Trailer.query.filter_by(item_id=item.id, status='SOLD').count()
        rows.append(row)
    month_start = date.today().replace(day=1)
    month_revenue = sum(
        float(payment.amount or 0)
        for payment in OrderPayment.query.filter(
            OrderPayment.status == 'CONFIRMED',
            OrderPayment.paid_at >= datetime.combine(month_start, time.min),
        ).all()
    )
    payment_pending = OrderPayment.query.filter_by(status='PENDING').order_by(OrderPayment.created_at.desc()).all()
    active_orders = CustomerOrder.query.filter(CustomerOrder.status.notin_(['done', 'cancelled'])).all()
    paid_not_shipped = [order for order in active_orders if order.confirmed_paid_amount > 0 and not order.is_shipped]
    unpaid_orders = [order for order in active_orders if order.confirmed_paid_amount == 0]
    overdue_production = (
        ProductionRequestLine.query
        .join(SupplyNeed, SupplyNeed.id == ProductionRequestLine.supply_need_id)
        .filter(
            ~ProductionRequestLine.status.in_(['ready', 'closed', 'cancelled', 'CANCELLED']),
            SupplyNeed.required_by.isnot(None),
            SupplyNeed.required_by < date.today(),
        )
        .all()
    )
    overdue_movements = StockMovement.query.filter(StockMovement.status.in_(['sent', 'in_transit']), StockMovement.arrival_date.isnot(None), StockMovement.arrival_date < date.today()).all()
    return render_template(
        'director_dashboard.html',
        warehouses=warehouses,
        rows=rows,
        payment_pending=payment_pending,
        paid_not_shipped=paid_not_shipped,
        unpaid_orders=unpaid_orders,
        overdue_production=overdue_production,
        overdue_movements=overdue_movements,
        month_revenue=month_revenue,
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
