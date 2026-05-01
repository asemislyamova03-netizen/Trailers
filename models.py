# models.py
from datetime import datetime, date
from sqlalchemy import Enum, Numeric
from extensions import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


# ---------- ПЕРЕЧИСЛЕНИЯ (ENUM'ы) ----------

# Тип товара в номенклатуре: прицеп или комплектующее
ItemTypeEnum = Enum(
    'TRAILER',      # прицеп
    'COMPONENT',    # комплектующее (тент, борт, замок и т.д.)
    name='item_type'
)

# Статус прицепа
TrailerStatusEnum = Enum(
    'IN_STOCK',       # в наличии
    'SOLD',           # продан
    'RESERVED',       # в резерве под клиента
    'IN_TRANSIT',     # в пути / в производстве
    'DECOMMISSIONED', # списан / более не используется
    name='trailer_status'
)

# Роли пользователей
UserRoleEnum = Enum(
    'ADMIN',      # админ системы (ты)
    'DIRECTOR',   # директор
    'ACCOUNTANT', # бухгалтер
    'MANAGER',    # менеджер по продажам
    'WAREHOUSE',  # склад (кладовщик)
    'VIEWER',     # только просмотр (на будущее)
    name='user_role'
)

# Тип клиента: физлицо / юрлицо
CustomerTypeEnum = Enum(
    'PERSON',   # физическое лицо
    'COMPANY',  # юридическое лицо
    name='customer_type'
)


# ---------- СПРАВОЧНИК СКЛАДОВ ----------

class Warehouse(db.Model):
    __tablename__ = 'warehouse'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_production = db.Column(db.Boolean, nullable=False, default=False, index=True)

    trailers = db.relationship('Trailer', back_populates='warehouse')

    # Пользователи, привязанные к этому складу (кладовщики / менеджеры)
    users = db.relationship('User', back_populates='warehouse')

    def __repr__(self) -> str:
        return f'<Warehouse id={self.id} name={self.name!r}>'


class User(UserMixin, db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    full_name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(20), nullable=False, default='manager')  # admin / manager

    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    warehouse = db.relationship('Warehouse', back_populates='users')

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    @property
    def is_manager(self) -> bool:
        return self.role == 'manager'

    @property
    def is_director(self) -> bool:
        return self.role == 'director'

    @property
    def is_production(self) -> bool:
        return self.role == 'production'

    @property
    def is_logistics(self) -> bool:
        return self.role == 'logistics'

    @property
    def is_viewer(self) -> bool:
        return self.role == 'viewer'

    @property
    def can_view_all(self) -> bool:
        return self.role in ('admin', 'director')

    @property
    def can_manage_users(self) -> bool:
        return self.is_admin

    @property
    def can_manage_production(self) -> bool:
        return self.role in ('admin', 'director', 'production')

    @property
    def can_manage_logistics(self) -> bool:
        return self.role in ('admin', 'director', 'logistics')

    def __repr__(self):
        return f'<User id={self.id} username={self.username!r} role={self.role}>'


class IdempotencyKey(db.Model):
    __tablename__ = 'idempotency_key'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    endpoint = db.Column(db.String(255), nullable=False)
    form_token = db.Column(db.String(64), nullable=False)
    object_type = db.Column(db.String(80), nullable=True)
    object_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    user = db.relationship('User', backref='idempotency_keys')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'endpoint', 'form_token', name='uq_idempotency_user_endpoint_token'),
    )



# ---------- НОМЕНКЛАТУРА (ПРИЦЕПЫ ПО АРТИКУЛАМ + КОМПЛЕКТУЮЩИЕ) ----------

class Item(db.Model):
    """
    Номенклатура:
      - прицепы по артикулам (item_type='TRAILER')
      - комплектующие (item_type='COMPONENT')
    """
    __tablename__ = 'item'

    id = db.Column(db.Integer, primary_key=True)

    # Тип: прицеп / комплектующее
    item_type = db.Column(ItemTypeEnum, nullable=False, index=True)

    # Артикул:
    #   для прицепов обязателен
    #   для комплектующих — по желанию (можно оставить NULL)
    article = db.Column(db.String(50), nullable=True, index=True)

    # Человекочитаемое наименование
    name = db.Column(db.String(255), nullable=False)

    # --- Характеристики прицепов (берём из "матрицы" и таблицы для артикула) ---

    # Длина кузова (мм)
    body_length_mm = db.Column(db.Integer, nullable=True)
    # Ширина кузова (мм)
    body_width_mm = db.Column(db.Integer, nullable=True)
    # Высота борта (мм)
    board_height_mm = db.Column(db.Integer, nullable=True)

    # Количество осей (1 / 2)
    axle_count = db.Column(db.Integer, nullable=True)

    # Радиус колеса (например, "R13", "R14")
    wheel_radius = db.Column(db.String(10), nullable=True)

    # Наличие тента
    has_tent = db.Column(db.Boolean, nullable=True)
    tent_hight_mm = db.Column(db.Integer, nullable=True)  # высота тента (мм)
    has_jockey_wheel = db.Column(db.Boolean, nullable=True)

    # Тип ступицы (если ведёшь)
    hub_type = db.Column(db.String(50), nullable=True)

    # Внешние габариты (как строка, например "3500 × 1800 × 1400")
    size_external = db.Column(db.String(255), nullable=True)

    # Размеры кузова (как строка)
    size_body = db.Column(db.String(255), nullable=True)

    # Единица измерения (для комплектующих: "шт", "комплект" и т.п.)
    unit = db.Column(db.String(20), nullable=False, default='шт')

    # Базовая цена (можно использовать как "цена по прайсу")
    base_price = db.Column(db.Numeric(12, 2), nullable=True)

    # Активен / скрыт
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # Связь с прицепами (экземплярами)
    trailers = db.relationship('Trailer', back_populates='item')

    def __repr__(self) -> str:
        return f'<Item id={self.id} type={self.item_type} article={self.article!r}>'


# ---------- ПРИЦЕПЫ (КОНКРЕТНЫЕ ЭКЗЕМПЛЯРЫ С VIN) ----------

class Trailer(db.Model):
    """
    Конкретный прицеп:
      - VIN
      - привязка к номенклатуре (какой это артикул/комплектация)
      - склад
      - статус (в наличии / продан / резерв и т.д.)
    """
    __tablename__ = 'trailer'

    id = db.Column(db.Integer, primary_key=True)

    # VIN-код — уникальный
    vin = db.Column(db.String(50), nullable=False, unique=True, index=True)

    # Ссылка на номенклатуру (какая модель/артикул)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)

    # Склад, на котором сейчас числится прицеп
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)

    # Дата производства или дата поступления (как тебе удобнее трактовать)
    manufacture_date = db.Column(db.Date, nullable=True)

    # Когда запись создана в системе
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Статус прицепа:
    #   ВАЖНО: в реальной логике будем менять его автоматически
    #   по наличию договора/продажи с этим VIN.
    status = db.Column(TrailerStatusEnum, nullable=False, default='IN_STOCK')

    # Любые дополнительные заметки
    comment = db.Column(db.Text, nullable=True)
    otts_id = db.Column(db.Integer, db.ForeignKey('otts.id'), nullable=True)
    lifecycle_status = db.Column(db.String(30), nullable=True, default='arrived', index=True)

    # Обратные связи
    item = db.relationship('Item', back_populates='trailers')
    warehouse = db.relationship('Warehouse', back_populates='trailers')

    otts = db.relationship('OTTS', lazy='joined')

    def __repr__(self) -> str:
        return f'<Trailer id={self.id} vin={self.vin!r} status={self.status}>'


# ---------- СПРАВОЧНИК ОТТС (ПОКА ПРОСТО ФИКСИРУЕМ) ----------

class OttsCertificate(db.Model):
    """
    Справочник ОТТС:
      - для какой осности (1/2 оси)
      - номер
      - до какой даты действует
    """
    __tablename__ = 'otts_certificate'

    id = db.Column(db.Integer, primary_key=True)

    # Количество осей, для которых действует ОТТС
    axle_count = db.Column(db.Integer, nullable=False)

    # Номер ОТТС
    number = db.Column(db.String(50), nullable=False)

    # Дата, до которой действует
    valid_to = db.Column(db.Date, nullable=True)

    # Флаг активности
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f'<OttsCertificate id={self.id} axle={self.axle_count} number={self.number!r}>'


# models.py

class Customer(db.Model):
    __tablename__ = 'customer'

    id = db.Column(db.Integer, primary_key=True)
    customer_type = db.Column(CustomerTypeEnum, nullable=False, index=True)
    opf = db.Column(db.String(10), nullable=True)  # 'ТОО', 'ИП', 'КХ', ...
    name = db.Column(db.String(255), nullable=False)
    contact_person = db.Column(db.String(255), nullable=True)
    iin_bin = db.Column(db.String(20), nullable=True, index=True)

    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(100), nullable=True)
    address = db.Column(db.String(255), nullable=True)

    # документ
    doc_type = db.Column(db.String(20))
    doc_number = db.Column(db.String(50))
    doc_issue_date = db.Column(db.Date)
    doc_issuer = db.Column(db.String(255))

    # --- НОВОЕ: реквизиты и руководитель (для юрлица) ---
    bank_account = db.Column(db.String(34), nullable=True)      # IBAN до 34
    bank_name = db.Column(db.String(255), nullable=True)        # наименование банка
    bank_bic = db.Column(db.String(20), nullable=True)          # БИК
    director_position = db.Column(db.String(100), nullable=True)  # должность руководителя
    director_fio = db.Column(db.String(255), nullable=True)       # ФИО руководителя

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f'<Customer id={self.id} type={self.customer_type} name={self.name!r}>'


class SalesContract(db.Model):
    __tablename__ = 'sales_contract'

    id = db.Column(db.Integer, primary_key=True)

    contract_number = db.Column(db.String(50), nullable=True)  # сделаем unique ниже
    contract_date = db.Column(db.Date, nullable=True, index=True)

    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True, index=True)
    trailer_id  = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)

    price = db.Column(Numeric(12, 2), nullable=True)
    payment_method = db.Column(db.String(50), nullable=True)
    source = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='sales_contracts')
    trailer  = db.relationship('Trailer', backref='sales_contracts')
    order = db.relationship('CustomerOrder', backref=db.backref('sales_contracts', lazy='dynamic'))

    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    is_shipped = db.Column(db.Boolean, nullable=False, default=False)

    # --- SIGEX ---
    sigex_document_id = db.Column(db.String(64), nullable=True, index=True)
    sigex_operation_id = db.Column(db.String(64), nullable=True, index=True)
    sigex_expire_at = db.Column(db.DateTime, nullable=True)
    sigex_last_status = db.Column(db.String(32), nullable=True)
    sigex_last_sign_id = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('trailer_id', name='uq_sales_contract_trailer'),
        db.UniqueConstraint('order_id', name='uq_sales_contract_order'),
    )
    
class OTTS(db.Model):
    """
    Справочник ОТТС (одобрение типа ТС).
    Пока используем только основные поля.
    """
    __tablename__ = 'otts'

    id = db.Column(db.Integer, primary_key=True)

    number = db.Column(db.String(100), nullable=False)        # № ОТТС
    date = db.Column(db.Date, nullable=True)                  # дата ОТТС
    modification = db.Column(db.String(10), nullable=False)   # модификация (002, 004 и т.п.)
    name = db.Column(db.String(255), nullable=False)          # наименование (как в ОТТС)
    axle_count = db.Column(db.Integer, nullable=False)        # количество осей
    full_mass_kg = db.Column(db.Integer, nullable=True)      # полная масса (кг)

    is_active = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        db.Index('idx_otts_mod_axles', 'modification', 'axle_count'),
    )

    def __repr__(self):
        return f"<OTTS {self.number} мод.{self.modification} осей={self.axle_count}>"


# ---------- CRM: ЛИДЫ / КАНАЛЫ / ЗАКАЗЫ / ПРОИЗВОДСТВО ----------

class Lead(db.Model):
    __tablename__ = 'lead'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    status = db.Column(db.String(30), nullable=False, default='NEW', index=True)
    conversation_status = db.Column(db.String(30), nullable=False, default='new', index=True)
    priority = db.Column(db.String(20), nullable=False, default='NORMAL')

    channel = db.Column(db.String(30), nullable=False, default='manual', index=True)
    source_channel = db.Column(db.String(30), nullable=False, default='MANUAL', index=True)
    source_name = db.Column(db.String(100), nullable=True)
    source_platform = db.Column(db.String(50), nullable=True)  # instagram, whatsapp, telegram, site, bot, facebook
    source_account = db.Column(db.String(100), nullable=True)  # имя аккаунта / страницы / бота
    external_chat_id = db.Column(db.String(120), nullable=True, index=True)
    external_lead_id = db.Column(db.String(120), nullable=True, index=True)
    source_payload = db.Column(db.Text, nullable=True)

    customer_name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=True, index=True)
    messenger_username = db.Column(db.String(100), nullable=True)
    text = db.Column(db.Text, nullable=True)
    customer_city = db.Column(db.String(100), nullable=True)
    interest_text = db.Column(db.Text, nullable=True)

    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=True, index=True)
    desired_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    desired_model = db.Column(db.String(255), nullable=True)
    desired_specs = db.Column(db.Text, nullable=True)

    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    comment = db.Column(db.Text, nullable=True)
    last_message_at = db.Column(db.DateTime, nullable=True, index=True)
    last_message_text = db.Column(db.Text, nullable=True)
    unread_count = db.Column(db.Integer, nullable=False, default=0)
    ai_enabled = db.Column(db.Boolean, nullable=False, default=False)
    ai_summary = db.Column(db.Text, nullable=True)

    customer = db.relationship('Customer', backref='leads')
    desired_item = db.relationship('Item', foreign_keys=[desired_item_id])
    warehouse = db.relationship('Warehouse', foreign_keys=[warehouse_id])
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id])

    def __repr__(self):
        return f'<Lead id={self.id} status={self.status} channel={self.source_channel}>'


class LeadMessage(db.Model):
    __tablename__ = 'lead_message'

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey('lead.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    direction = db.Column(db.String(10), nullable=False, default='IN')  # IN / OUT
    sender_type = db.Column(db.String(20), nullable=False, default='client')
    channel = db.Column(db.String(30), nullable=False, default='MANUAL')
    external_message_id = db.Column(db.String(120), nullable=True, index=True)

    sender_name = db.Column(db.String(255), nullable=True)
    sender_contact = db.Column(db.String(255), nullable=True)
    text = db.Column(db.Text, nullable=True)
    payload = db.Column(db.Text, nullable=True)
    payload_json = db.Column(db.Text, nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    lead = db.relationship('Lead', backref=db.backref('messages', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id])


class CustomerOrder(db.Model):
    __tablename__ = 'customer_order'

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    status = db.Column(db.String(40), nullable=False, default='DRAFT', index=True)
    fulfillment_source = db.Column(db.String(30), nullable=True)  # STOCK / PRODUCTION / TRANSIT

    lead_id = db.Column(db.Integer, db.ForeignKey('lead.id'), nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    price = db.Column(db.Numeric(12, 2), nullable=True)
    prepayment_percent = db.Column(db.Numeric(5, 2), nullable=True, default=30)
    note = db.Column(db.Text, nullable=True)
    manager_comment = db.Column(db.Text, nullable=True)
    expected_date = db.Column(db.Date, nullable=True)
    document_status = db.Column(db.String(30), nullable=False, default='not_started', index=True)
    documents_issued = db.Column(db.Boolean, nullable=False, default=False)
    documents_issued_at = db.Column(db.DateTime, nullable=True)
    is_shipped = db.Column(db.Boolean, nullable=False, default=False)
    shipped_at = db.Column(db.DateTime, nullable=True)
    planned_ship_date = db.Column(db.Date, nullable=True)
    planned_ship_comment = db.Column(db.Text, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancel_reason = db.Column(db.Text, nullable=True)

    lead = db.relationship('Lead', backref='orders')
    customer = db.relationship('Customer', backref='orders')
    item = db.relationship('Item', backref='orders')
    trailer = db.relationship('Trailer', backref='orders')
    warehouse = db.relationship('Warehouse', backref='orders')
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id])

    @property
    def confirmed_paid_amount(self):
        total = 0
        for p in self.payments:
            if p.status == 'CONFIRMED':
                total += float(p.amount or 0)
        return total

    @property
    def total_amount(self):
        return float(self.price or 0)

    @property
    def remaining_amount(self):
        price = float(self.price or 0)
        return max(price - self.confirmed_paid_amount, 0)

    @property
    def payment_status(self):
        paid = self.confirmed_paid_amount
        total = self.total_amount
        if paid <= 0:
            return 'unpaid'
        if total > 0 and paid >= total:
            return 'paid'
        return 'partial'

    @property
    def payment_percent(self):
        total = self.total_amount
        if total <= 0:
            return 0
        return min(round(self.confirmed_paid_amount / total * 100, 2), 100)

    @property
    def source_lead(self):
        return self.lead

    def __repr__(self):
        return f'<CustomerOrder id={self.id} number={self.order_number} status={self.status}>'


class OrderPayment(db.Model):
    __tablename__ = 'order_payment'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    paid_at = db.Column(db.DateTime, nullable=True)

    stage = db.Column(db.String(20), nullable=False, default='PREPAYMENT')  # PREPAYMENT / FINAL / FULL
    method = db.Column(db.String(30), nullable=False, default='BANK')
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='CONFIRMED', index=True)

    transaction_ref = db.Column(db.String(120), nullable=True)
    payment_link = db.Column(db.String(255), nullable=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref=db.backref('payments', lazy='dynamic', cascade='all, delete-orphan'))


class OrderEvent(db.Model):
    __tablename__ = 'order_event'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    old_value = db.Column(db.String(255), nullable=True)
    new_value = db.Column(db.String(255), nullable=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    order = db.relationship('CustomerOrder', backref=db.backref('events', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id])


class Reservation(db.Model):
    __tablename__ = 'reservation'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    status = db.Column(db.String(20), nullable=False, default='ACTIVE', index=True)
    source_type = db.Column(db.String(30), nullable=False, default='STOCK')  # STOCK / PRODUCTION / TRANSIT
    priority = db.Column(db.Integer, nullable=False, default=100)

    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=False, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref=db.backref('reservations', lazy='dynamic', cascade='all, delete-orphan'))
    trailer = db.relationship('Trailer', backref='reservations')
    item = db.relationship('Item', backref='reservations')


class SupplyNeed(db.Model):
    __tablename__ = 'supply_need'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    need_type = db.Column(db.String(30), nullable=False, default='CUSTOMER_ORDER')
    status = db.Column(db.String(30), nullable=False, default='NEW', index=True)
    priority = db.Column(db.Integer, nullable=False, default=100)

    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    required_by = db.Column(db.Date, nullable=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref=db.backref('supply_needs', lazy='dynamic', cascade='all, delete-orphan'))
    item = db.relationship('Item', backref='supply_needs')
    warehouse = db.relationship('Warehouse', backref='supply_needs')


class ProductionRequest(db.Model):
    __tablename__ = 'production_request'

    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    status = db.Column(db.String(30), nullable=False, default='DRAFT', index=True)

    target_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)

    target_warehouse = db.relationship('Warehouse', backref='production_requests')


class ProductionRequestLine(db.Model):
    __tablename__ = 'production_request_line'

    id = db.Column(db.Integer, primary_key=True)
    production_request_id = db.Column(db.Integer, db.ForeignKey('production_request.id'), nullable=False, index=True)
    supply_need_id = db.Column(db.Integer, db.ForeignKey('supply_need.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    produced_qty = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    produced_at = db.Column(db.DateTime, nullable=True)
    production_comment = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default='PLANNED', index=True)
    note = db.Column(db.Text, nullable=True)

    production_request = db.relationship('ProductionRequest', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    supply_need = db.relationship('SupplyNeed', backref='production_lines')
    item = db.relationship('Item', backref='production_lines')


class ProducedUnit(db.Model):
    __tablename__ = 'produced_unit'

    id = db.Column(db.Integer, primary_key=True)
    production_request_line_id = db.Column(db.Integer, db.ForeignKey('production_request_line.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    target_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    produced_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=False, default='produced_no_vin', index=True)
    note = db.Column(db.Text, nullable=True)

    production_request_line = db.relationship('ProductionRequestLine', backref=db.backref('produced_units', lazy='dynamic', cascade='all, delete-orphan'))
    item = db.relationship('Item', backref='produced_units')
    target_warehouse = db.relationship('Warehouse', backref='produced_units')
    order = db.relationship('CustomerOrder', backref='produced_units')
    trailer = db.relationship('Trailer', backref='produced_unit', uselist=False)


class StockMovement(db.Model):
    __tablename__ = 'stock_movement'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    departure_date = db.Column(db.Date, nullable=True)
    arrival_date = db.Column(db.Date, nullable=True)
    moved_at = db.Column(db.DateTime, nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)

    movement_type = db.Column(db.String(30), nullable=False, default='WAREHOUSE_TRANSFER')
    status = db.Column(db.String(30), nullable=False, default='DRAFT', index=True)
    batch_key = db.Column(db.String(50), nullable=True, index=True)

    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    qty = db.Column(db.Integer, nullable=False, default=1)

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref='movements')
    trailer = db.relationship('Trailer', backref='movements')
    item = db.relationship('Item', backref='movements')
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id], backref='out_movements')
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id], backref='in_movements')
