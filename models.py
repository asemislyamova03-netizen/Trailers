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
    warehouse = db.relationship('Warehouse')

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

    def __repr__(self):
        return f'<User id={self.id} username={self.username!r} role={self.role}>'



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


class Customer(db.Model):
    """
    Клиенты (покупатели):
      - физлица и юрлица в одной таблице.
    """
    __tablename__ = 'customer'

    id = db.Column(db.Integer, primary_key=True)

    # Тип клиента
    customer_type = db.Column(CustomerTypeEnum, nullable=False, index=True)

    # Основное наименование:
    #   - для физлица: ФИО
    #   - для юрлица: Название организации
    name = db.Column(db.String(255), nullable=False)

    # Для юрлица можно указать контактное лицо
    contact_person = db.Column(db.String(255), nullable=True)

    # ИИН или БИН
    iin_bin = db.Column(db.String(20), nullable=True, index=True)

    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(100), nullable=True)
    address = db.Column(db.String(255), nullable=True)

    # --- НОВОЕ: документ ---
    # тип документа: удостоверение, вид на жительство, паспорт иностранца
    doc_type = db.Column(db.String(20))        # 'ID', 'RESIDENT', 'FOREIGN_PASSPORT'
    doc_number = db.Column(db.String(50))      # номер документа
    doc_issue_date = db.Column(db.Date)        # дата выдачи
    doc_issuer = db.Column(db.String(255))     # кем выдан

    # Активен / нет
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

    price = db.Column(Numeric(12, 2), nullable=True)
    payment_method = db.Column(db.String(50), nullable=True)
    source = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='sales_contracts')
    trailer  = db.relationship('Trailer', backref='sales_contracts')

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
