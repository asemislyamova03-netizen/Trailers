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
    warehouse_kind = db.Column(db.String(30), nullable=False, default='finished_goods', index=True)
    is_sales_point = db.Column(db.Boolean, nullable=False, default=True, index=True)
    can_sell = db.Column(db.Boolean, nullable=False, default=True, index=True)
    can_ship_to_customer = db.Column(db.Boolean, nullable=False, default=True, index=True)
    primary_product_category = db.Column(db.String(40), nullable=True, index=True)
    product_category_scope = db.Column(db.String(80), nullable=True, index=True)

    trailers = db.relationship('Trailer', back_populates='warehouse')

    # Пользователи, привязанные к этому складу (кладовщики / менеджеры)
    users = db.relationship('User', back_populates='warehouse')

    def __repr__(self) -> str:
        return f'<Warehouse id={self.id} name={self.name!r}>'


class WarehouseStorageArea(db.Model):
    __tablename__ = 'warehouse_storage_area'

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False, index=True)
    code = db.Column(db.String(40), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    area_type = db.Column(db.String(40), nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    warehouse = db.relationship('Warehouse', backref='storage_areas')

    __table_args__ = (
        db.UniqueConstraint('warehouse_id', 'code', name='uq_warehouse_storage_area_code'),
    )


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
        return self.role in ('admin', 'director', 'manager', 'production')

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

class ProductCategory(db.Model):
    __tablename__ = 'product_category'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(40), nullable=False, default='goods', index=True)
    is_vin_required = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_sellable = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_finished_vehicle = db.Column(db.Boolean, nullable=False, default=False, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f'<ProductCategory {self.code}>'


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
    product_category_id = db.Column(db.Integer, db.ForeignKey('product_category.id'), nullable=True, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=True, index=True)
    max_mass_kg = db.Column(db.Integer, nullable=True)
    is_sellable = db.Column(db.Boolean, nullable=False, default=True, index=True)
    requires_vin = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_realization_line = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_internal_bom_item = db.Column(db.Boolean, nullable=False, default=False, index=True)

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
    product_category = db.relationship('ProductCategory', foreign_keys=[product_category_id], backref='items')
    product_group = db.relationship('TrailerProductGroup', foreign_keys=[group_id], backref='items')

    def __repr__(self) -> str:
        return f'<Item id={self.id} type={self.item_type} article={self.article!r}>'


class ItemBillOfMaterials(db.Model):
    __tablename__ = 'item_bill_of_materials'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    version = db.Column(db.String(40), nullable=False, default='default')
    name = db.Column(db.String(160), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = db.relationship('Item', foreign_keys=[item_id], backref='bom_headers')

    __table_args__ = (
        db.UniqueConstraint('item_id', 'version', name='uq_item_bom_version'),
    )


class ItemBillOfMaterialsLine(db.Model):
    __tablename__ = 'item_bill_of_materials_line'

    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(db.Integer, db.ForeignKey('item_bill_of_materials.id'), nullable=False, index=True)
    component_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    quantity_per_unit = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    source_area_type = db.Column(db.String(40), nullable=False, default='components', index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    component_category = db.Column(db.String(40), nullable=True, index=True)
    is_required = db.Column(db.Boolean, nullable=False, default=True)
    allow_substitute = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bom = db.relationship('ItemBillOfMaterials', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    component_item = db.relationship('Item', foreign_keys=[component_item_id], backref='bom_component_lines')
    workshop = db.relationship('ProductionWorkshop', backref='bom_lines')


class InventoryBalance(db.Model):
    __tablename__ = 'inventory_balance'

    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False, index=True)
    storage_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    quantity = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    reserved_quantity = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    warehouse = db.relationship('Warehouse', backref='inventory_balances')
    storage_area = db.relationship('WarehouseStorageArea', backref='inventory_balances')
    item = db.relationship('Item', backref='inventory_balances')

    __table_args__ = (
        db.UniqueConstraint('warehouse_id', 'storage_area_id', 'item_id', name='uq_inventory_balance_place_item'),
    )


class InventoryOperation(db.Model):
    __tablename__ = 'inventory_operation'

    id = db.Column(db.Integer, primary_key=True)
    operation_type = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='posted', index=True)
    source_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    source_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    target_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    target_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    production_request_line_id = db.Column(db.Integer, db.ForeignKey('production_request_line.id'), nullable=True, index=True)
    produced_unit_id = db.Column(db.Integer, db.ForeignKey('produced_unit.id'), nullable=True, index=True)
    stock_movement_id = db.Column(db.Integer, db.ForeignKey('stock_movement.id'), nullable=True, index=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    customer_order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    posted_at = db.Column(db.DateTime, nullable=True)
    comment = db.Column(db.Text, nullable=True)

    source_warehouse = db.relationship('Warehouse', foreign_keys=[source_warehouse_id], backref='source_inventory_operations')
    target_warehouse = db.relationship('Warehouse', foreign_keys=[target_warehouse_id], backref='target_inventory_operations')
    source_area = db.relationship('WarehouseStorageArea', foreign_keys=[source_area_id])
    target_area = db.relationship('WarehouseStorageArea', foreign_keys=[target_area_id])
    production_request_line = db.relationship('ProductionRequestLine', backref='inventory_operations')
    produced_unit = db.relationship('ProducedUnit', backref='inventory_operations')
    stock_movement = db.relationship('StockMovement', backref='inventory_operations')
    customer_order = db.relationship('CustomerOrder', backref='inventory_operations')
    customer_order_line = db.relationship('CustomerOrderLine', backref='inventory_operations')
    workshop = db.relationship('ProductionWorkshop', backref='inventory_operations')
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id])


class InventoryOperationLine(db.Model):
    __tablename__ = 'inventory_operation_line'

    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey('inventory_operation.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    quantity = db.Column(db.Numeric(14, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    direction = db.Column(db.String(10), nullable=False, default='out', index=True)
    comment = db.Column(db.Text, nullable=True)

    operation = db.relationship('InventoryOperation', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    item = db.relationship('Item', backref='inventory_operation_lines')


class InventoryTransaction(db.Model):
    __tablename__ = 'inventory_transaction'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    transaction_type = db.Column(db.String(40), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False, index=True)
    storage_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    qty = db.Column(db.Numeric(14, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    related_order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    related_order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    related_production_request_line_id = db.Column(db.Integer, db.ForeignKey('production_request_line.id'), nullable=True, index=True)
    related_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    related_stock_movement_id = db.Column(db.Integer, db.ForeignKey('stock_movement.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    comment = db.Column(db.Text, nullable=True)

    warehouse = db.relationship('Warehouse', backref='inventory_transactions')
    storage_area = db.relationship('WarehouseStorageArea', backref='inventory_transactions')
    item = db.relationship('Item', backref='inventory_transactions')
    related_order = db.relationship('CustomerOrder', foreign_keys=[related_order_id])
    related_order_line = db.relationship('CustomerOrderLine', foreign_keys=[related_order_line_id])
    related_production_request_line = db.relationship('ProductionRequestLine', foreign_keys=[related_production_request_line_id])
    related_workshop = db.relationship('ProductionWorkshop', foreign_keys=[related_workshop_id])
    related_stock_movement = db.relationship('StockMovement', foreign_keys=[related_stock_movement_id])
    user = db.relationship('User', foreign_keys=[user_id])


class TrailerAssemblyOperation(db.Model):
    __tablename__ = 'trailer_assembly_operation'

    id = db.Column(db.Integer, primary_key=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False, index=True)
    operation_type = db.Column(db.String(30), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='draft', index=True)
    from_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    to_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    posted_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    posted_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    comment = db.Column(db.Text, nullable=True)

    trailer = db.relationship('Trailer', backref='assembly_operations')
    order = db.relationship('CustomerOrder', backref='assembly_operations')
    order_line = db.relationship('CustomerOrderLine', foreign_keys=[order_line_id], backref='assembly_operations')
    warehouse = db.relationship('Warehouse', backref='trailer_assembly_operations')
    from_item = db.relationship('Item', foreign_keys=[from_item_id])
    to_item = db.relationship('Item', foreign_keys=[to_item_id])
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id])
    posted_by_user = db.relationship('User', foreign_keys=[posted_by_user_id])


class TrailerAssemblyOperationLine(db.Model):
    __tablename__ = 'trailer_assembly_operation_line'

    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey('trailer_assembly_operation.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    quantity = db.Column(db.Numeric(12, 3), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    direction = db.Column(db.String(20), nullable=False, index=True)
    storage_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    inventory_operation_line_id = db.Column(db.Integer, db.ForeignKey('inventory_operation_line.id'), nullable=True, index=True)
    comment = db.Column(db.Text, nullable=True)

    operation = db.relationship('TrailerAssemblyOperation', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    item = db.relationship('Item', backref='trailer_assembly_lines')
    storage_area = db.relationship('WarehouseStorageArea', backref='trailer_assembly_lines')
    inventory_operation_line = db.relationship('InventoryOperationLine', backref='trailer_assembly_lines')


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
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    price = db.Column(Numeric(12, 2), nullable=True)
    payment_method = db.Column(db.String(50), nullable=True)
    source = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    customer = db.relationship('Customer', backref='sales_contracts')
    trailer  = db.relationship('Trailer', backref='sales_contracts')
    order = db.relationship('CustomerOrder', backref=db.backref('sales_contracts', lazy='dynamic'))
    warehouse = db.relationship('Warehouse', foreign_keys=[warehouse_id], backref='sales_contracts')
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id], backref='sales_contracts')

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


class SalesContractLine(db.Model):
    __tablename__ = 'sales_contract_line'

    id = db.Column(db.Integer, primary_key=True)
    sales_contract_id = db.Column(db.Integer, db.ForeignKey('sales_contract.id'), nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    vin_registry_id = db.Column(db.Integer, db.ForeignKey('vin_registry.id'), nullable=True, index=True)

    line_no = db.Column(db.Integer, nullable=False, default=1)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=True)
    total_price = db.Column(db.Numeric(12, 2), nullable=True)

    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)
    vin_full = db.Column(db.String(50), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    sales_contract = db.relationship('SalesContract', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    order_line = db.relationship('CustomerOrderLine', backref='contract_lines')
    trailer = db.relationship('Trailer', backref='contract_lines')
    item = db.relationship('Item', backref='contract_lines')
    vin_registry = db.relationship('VinRegistry', foreign_keys=[vin_registry_id], backref='contract_lines')

    __table_args__ = (
        db.UniqueConstraint('sales_contract_id', 'line_no', name='uq_sales_contract_line_no'),
    )


class SalesRealization(db.Model):
    __tablename__ = 'sales_realization'

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(50), nullable=True, unique=True, index=True)
    realization_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default='draft', index=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    posted_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    posted_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancel_reason = db.Column(db.Text, nullable=True)
    one_c_export_status = db.Column(db.String(30), nullable=False, default='not_exported', index=True)
    one_c_exported_at = db.Column(db.DateTime, nullable=True)
    one_c_document_ref = db.Column(db.String(120), nullable=True, index=True)
    one_c_export_payload_json = db.Column(db.Text, nullable=True)
    one_c_export_error = db.Column(db.Text, nullable=True)
    esf_status = db.Column(db.String(30), nullable=False, default='not_started', index=True)
    esf_ref = db.Column(db.String(120), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = db.relationship('CustomerOrder', backref='sales_realizations')
    customer = db.relationship('Customer', backref='sales_realizations')
    warehouse = db.relationship('Warehouse', backref='sales_realizations')
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id], backref='assigned_sales_realizations')
    posted_by_user = db.relationship('User', foreign_keys=[posted_by_user_id])
    cancelled_by_user = db.relationship('User', foreign_keys=[cancelled_by_user_id])
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id])


class SalesRealizationLine(db.Model):
    __tablename__ = 'sales_realization_line'

    id = db.Column(db.Integer, primary_key=True)
    realization_id = db.Column(db.Integer, db.ForeignKey('sales_realization.id'), nullable=False, index=True)
    line_no = db.Column(db.Integer, nullable=False, default=1)
    line_type = db.Column(db.String(30), nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    vin_registry_id = db.Column(db.Integer, db.ForeignKey('vin_registry.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    storage_area_id = db.Column(db.Integer, db.ForeignKey('warehouse_storage_area.id'), nullable=True, index=True)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    unit_price = db.Column(db.Numeric(12, 2), nullable=True)
    total_price = db.Column(db.Numeric(12, 2), nullable=True)
    inventory_effect = db.Column(db.String(30), nullable=False, default='none', index=True)
    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    vin_full = db.Column(db.String(50), nullable=True, index=True)
    comment = db.Column(db.Text, nullable=True)

    realization = db.relationship('SalesRealization', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    order_line = db.relationship('CustomerOrderLine', backref='realization_lines')
    trailer = db.relationship('Trailer', backref='realization_lines')
    vin_registry = db.relationship('VinRegistry', foreign_keys=[vin_registry_id], backref='realization_lines')
    item = db.relationship('Item', backref='realization_lines')
    warehouse = db.relationship('Warehouse', backref='realization_lines')
    storage_area = db.relationship('WarehouseStorageArea', backref='realization_lines')


class ContractTemplate(db.Model):
    __tablename__ = 'contract_template'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(60), nullable=False, unique=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    template_type = db.Column(db.String(40), nullable=False, default='sale', index=True)
    product_group_code = db.Column(db.String(20), nullable=True, index=True)
    otss_number = db.Column(db.String(120), nullable=True, index=True)
    otss_type = db.Column(db.String(20), nullable=True, index=True)
    otss_modification = db.Column(db.String(20), nullable=True, index=True)
    body_execution_code = db.Column(db.String(30), nullable=True, index=True)
    customer_type = db.Column(db.String(20), nullable=True, index=True)
    content_path = db.Column(db.String(500), nullable=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    
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
    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    config_snapshot_json = db.Column(db.Text, nullable=True)
    calculated_price = db.Column(db.Numeric(12, 2), nullable=True)
    price_breakdown_json = db.Column(db.Text, nullable=True)
    overall_dimensions_text = db.Column(db.String(80), nullable=True)
    inner_dimensions_text = db.Column(db.String(80), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)

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
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    source_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
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
    realization_status = db.Column(db.String(30), nullable=False, default='not_started', index=True)
    realized_at = db.Column(db.DateTime, nullable=True)
    planned_ship_date = db.Column(db.Date, nullable=True)
    planned_ship_comment = db.Column(db.Text, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancel_reason = db.Column(db.Text, nullable=True)
    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    config_snapshot_json = db.Column(db.Text, nullable=True)
    calculated_price = db.Column(db.Numeric(12, 2), nullable=True)
    price_breakdown_json = db.Column(db.Text, nullable=True)
    overall_dimensions_text = db.Column(db.String(80), nullable=True)
    inner_dimensions_text = db.Column(db.String(80), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)
    reserved_vin_registry_id = db.Column(db.Integer, db.ForeignKey('vin_registry.id'), nullable=True, index=True)

    lead = db.relationship('Lead', backref='orders')
    customer = db.relationship('Customer', backref='orders')
    item = db.relationship('Item', backref='orders')
    trailer = db.relationship('Trailer', backref='orders')
    warehouse = db.relationship('Warehouse', foreign_keys=[warehouse_id], backref='orders')
    source_warehouse = db.relationship('Warehouse', foreign_keys=[source_warehouse_id])
    assigned_user = db.relationship('User', foreign_keys=[assigned_user_id])
    reserved_vin_registry = db.relationship('VinRegistry', foreign_keys=[reserved_vin_registry_id], post_update=True)

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


class ProductionWorkshop(db.Model):
    __tablename__ = 'production_workshop'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    workshop_type = db.Column(db.String(40), nullable=False, default='trailer', index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ProductionWorkshop id={self.id} code={self.code!r}>'


class ProductionEmployee(db.Model):
    __tablename__ = 'production_employee'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    full_name = db.Column(db.String(160), nullable=False, index=True)
    employee_code = db.Column(db.String(40), nullable=True, unique=True, index=True)
    phone = db.Column(db.String(50), nullable=True)
    default_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    primary_role = db.Column(db.String(60), nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref='production_employee_profile')
    default_workshop = db.relationship('ProductionWorkshop', foreign_keys=[default_workshop_id], backref='default_employees')


class ProductionShift(db.Model):
    __tablename__ = 'production_shift'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('production_employee.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    work_area = db.Column(db.String(60), nullable=False, default='production', index=True)
    planned_date = db.Column(db.Date, nullable=True, index=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    ended_at = db.Column(db.DateTime, nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default='open', index=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    employee = db.relationship('ProductionEmployee', backref='shifts')
    user = db.relationship('User', backref='production_shifts')
    workshop = db.relationship('ProductionWorkshop', backref='shifts')


class ProductionShiftOutput(db.Model):
    __tablename__ = 'production_shift_output'

    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(db.Integer, db.ForeignKey('production_shift.id'), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('production_employee.id'), nullable=False, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    production_request_line_id = db.Column(db.Integer, db.ForeignKey('production_request_line.id'), nullable=True, index=True)
    output_type = db.Column(db.String(60), nullable=False, default='other', index=True)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    defect_quantity = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    status = db.Column(db.String(30), nullable=False, default='accepted', index=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    shift = db.relationship('ProductionShift', backref=db.backref('outputs', lazy='dynamic', cascade='all, delete-orphan'))
    employee = db.relationship('ProductionEmployee', backref='outputs')
    workshop = db.relationship('ProductionWorkshop', backref='outputs')
    item = db.relationship('Item', backref='production_outputs')
    order_line = db.relationship('CustomerOrderLine', backref='production_outputs')
    production_request_line = db.relationship('ProductionRequestLine', backref='shift_outputs')


class CustomerOrderLine(db.Model):
    __tablename__ = 'customer_order_line'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=False, index=True)
    line_no = db.Column(db.Integer, nullable=False, default=1)
    line_type = db.Column(db.String(30), nullable=False, default='TRAILER', index=True)
    fulfillment_source = db.Column(db.String(30), nullable=True, index=True)

    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    production_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    assembly_status = db.Column(db.String(30), nullable=False, default='not_required', index=True)
    assembly_operation_id = db.Column(db.Integer, db.ForeignKey('trailer_assembly_operation.id'), nullable=True, index=True)
    include_in_vehicle_contract = db.Column(db.Boolean, nullable=False, default=False, index=True)
    include_in_realization = db.Column(db.Boolean, nullable=False, default=True, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=True)
    total_price = db.Column(db.Numeric(12, 2), nullable=True)
    status = db.Column(db.String(30), nullable=False, default='NEW', index=True)
    note = db.Column(db.Text, nullable=True)

    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    config_snapshot_json = db.Column(db.Text, nullable=True)
    calculated_price = db.Column(db.Numeric(12, 2), nullable=True)
    price_breakdown_json = db.Column(db.Text, nullable=True)
    overall_dimensions_text = db.Column(db.String(80), nullable=True)
    inner_dimensions_text = db.Column(db.String(80), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = db.relationship('CustomerOrder', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    item = db.relationship('Item', backref='order_lines')
    trailer = db.relationship('Trailer', foreign_keys=[trailer_id], backref='order_lines')
    production_workshop = db.relationship('ProductionWorkshop', backref='order_lines')
    assembly_operation = db.relationship('TrailerAssemblyOperation', foreign_keys=[assembly_operation_id], post_update=True)

    __table_args__ = (
        db.UniqueConstraint('order_id', 'line_no', name='uq_customer_order_line_no'),
    )

    def __repr__(self):
        return f'<CustomerOrderLine id={self.id} order_id={self.order_id} line_no={self.line_no}>'


class OrderPayment(db.Model):
    __tablename__ = 'order_payment'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=False, index=True)
    payment_stage_id = db.Column(db.Integer, db.ForeignKey('payment_schedule_stage.id'), nullable=True, index=True)
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
    payment_stage = db.relationship('PaymentScheduleStage', foreign_keys=[payment_stage_id], backref='payments')


class PaymentScheduleStage(db.Model):
    __tablename__ = 'payment_schedule_stage'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    sales_contract_id = db.Column(db.Integer, db.ForeignKey('sales_contract.id'), nullable=True, index=True)
    stage_code = db.Column(db.String(40), nullable=False, default='PREPAYMENT', index=True)
    name = db.Column(db.String(120), nullable=False)
    due_date = db.Column(db.Date, nullable=True, index=True)
    percent = db.Column(db.Numeric(5, 2), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    status = db.Column(db.String(30), nullable=False, default='planned', index=True)
    kaspi_payment_id = db.Column(db.String(120), nullable=True, index=True)
    kaspi_payment_url = db.Column(db.String(500), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = db.relationship('CustomerOrder', backref=db.backref('payment_schedule', lazy='dynamic', cascade='all, delete-orphan'))
    sales_contract = db.relationship('SalesContract', backref=db.backref('payment_schedule', lazy='dynamic', cascade='all, delete-orphan'))


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
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref=db.backref('reservations', lazy='dynamic', cascade='all, delete-orphan'))
    order_line = db.relationship('CustomerOrderLine', backref='reservations')
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
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    production_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    required_by = db.Column(db.Date, nullable=True)
    note = db.Column(db.Text, nullable=True)
    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    config_snapshot_json = db.Column(db.Text, nullable=True)
    calculated_price = db.Column(db.Numeric(12, 2), nullable=True)
    price_breakdown_json = db.Column(db.Text, nullable=True)
    overall_dimensions_text = db.Column(db.String(80), nullable=True)
    inner_dimensions_text = db.Column(db.String(80), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    cancel_reason = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref=db.backref('supply_needs', lazy='dynamic', cascade='all, delete-orphan'))
    order_line = db.relationship('CustomerOrderLine', backref='supply_needs')
    item = db.relationship('Item', backref='supply_needs')
    warehouse = db.relationship('Warehouse', backref='supply_needs')
    production_workshop = db.relationship('ProductionWorkshop', backref='supply_needs')
    cancelled_by_user = db.relationship('User', foreign_keys=[cancelled_by_user_id])


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
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    production_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    assembly_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    produced_qty = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    produced_at = db.Column(db.DateTime, nullable=True)
    production_comment = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default='PLANNED', index=True)
    note = db.Column(db.Text, nullable=True)
    article_snapshot = db.Column(db.String(80), nullable=True, index=True)
    product_name_snapshot = db.Column(db.String(255), nullable=True)
    config_snapshot_json = db.Column(db.Text, nullable=True)
    calculated_price = db.Column(db.Numeric(12, 2), nullable=True)
    price_breakdown_json = db.Column(db.Text, nullable=True)
    overall_dimensions_text = db.Column(db.String(80), nullable=True)
    inner_dimensions_text = db.Column(db.String(80), nullable=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True)
    otss_modification = db.Column(db.String(20), nullable=True)
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    cancel_reason = db.Column(db.Text, nullable=True)

    production_request = db.relationship('ProductionRequest', backref=db.backref('lines', lazy='dynamic', cascade='all, delete-orphan'))
    supply_need = db.relationship('SupplyNeed', backref='production_lines')
    order_line = db.relationship('CustomerOrderLine', backref='production_lines')
    item = db.relationship('Item', backref='production_lines')
    production_workshop = db.relationship('ProductionWorkshop', backref='production_lines')
    assembly_warehouse = db.relationship('Warehouse', foreign_keys=[assembly_warehouse_id], backref='assembly_production_lines')
    cancelled_by_user = db.relationship('User', foreign_keys=[cancelled_by_user_id])


class ProducedUnit(db.Model):
    __tablename__ = 'produced_unit'

    id = db.Column(db.Integer, primary_key=True)
    production_request_line_id = db.Column(db.Integer, db.ForeignKey('production_request_line.id'), nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    production_workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=True, index=True)
    target_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    produced_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=False, default='produced_no_vin', index=True)
    note = db.Column(db.Text, nullable=True)

    production_request_line = db.relationship('ProductionRequestLine', backref=db.backref('produced_units', lazy='dynamic', cascade='all, delete-orphan'))
    order_line = db.relationship('CustomerOrderLine', backref='produced_units')
    item = db.relationship('Item', backref='produced_units')
    production_workshop = db.relationship('ProductionWorkshop', backref='produced_units')
    target_warehouse = db.relationship('Warehouse', backref='produced_units')
    order = db.relationship('CustomerOrder', backref='produced_units')
    trailer = db.relationship('Trailer', backref='produced_unit', uselist=False)


class ItemProductionRoute(db.Model):
    __tablename__ = 'item_production_route'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False, index=True)
    version = db.Column(db.String(40), nullable=False, default='default')
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = db.relationship('Item', backref='production_routes')

    __table_args__ = (
        db.UniqueConstraint('item_id', 'version', name='uq_item_production_route_version'),
    )


class ItemProductionRouteStep(db.Model):
    __tablename__ = 'item_production_route_step'

    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey('item_production_route.id'), nullable=False, index=True)
    step_no = db.Column(db.Integer, nullable=False, default=1, index=True)
    sequence_no = db.Column(db.Integer, nullable=False, default=1, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('production_workshop.id'), nullable=False, index=True)
    operation_name = db.Column(db.String(120), nullable=True)
    input_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    output_item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    input_area_type = db.Column(db.String(40), nullable=True, index=True)
    output_area_type = db.Column(db.String(40), nullable=True, index=True)
    planned_qty = db.Column(db.Numeric(12, 3), nullable=True)
    planned_duration_minutes = db.Column(db.Integer, nullable=True)
    unit = db.Column(db.String(20), nullable=False, default='шт')
    is_required = db.Column(db.Boolean, nullable=False, default=True)
    comment = db.Column(db.Text, nullable=True)

    route = db.relationship('ItemProductionRoute', backref=db.backref('steps', lazy='dynamic', cascade='all, delete-orphan'))
    workshop = db.relationship('ProductionWorkshop', backref='production_route_steps')
    input_item = db.relationship('Item', foreign_keys=[input_item_id])
    output_item = db.relationship('Item', foreign_keys=[output_item_id])


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
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=True, index=True)
    qty = db.Column(db.Integer, nullable=False, default=1)

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)

    order = db.relationship('CustomerOrder', backref='movements')
    order_line = db.relationship('CustomerOrderLine', backref='movements')
    trailer = db.relationship('Trailer', backref='movements')
    item = db.relationship('Item', backref='movements')
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id], backref='out_movements')
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id], backref='in_movements')


class VinRegistry(db.Model):
    __tablename__ = 'vin_registry'

    id = db.Column(db.Integer, primary_key=True)
    vin_full = db.Column(db.String(50), nullable=True, unique=True, index=True)
    prefix = db.Column(db.String(10), nullable=False, default='MX4')
    vin_modification_code = db.Column(db.String(20), nullable=True, index=True)
    year_code = db.Column(db.String(1), nullable=True, index=True)
    serial7 = db.Column(db.String(7), nullable=False, unique=True, index=True)
    status = db.Column(db.String(30), nullable=False, default='free', index=True)

    customer_order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    supply_need_id = db.Column(db.Integer, db.ForeignKey('supply_need.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    sales_contract_id = db.Column(db.Integer, db.ForeignKey('sales_contract.id'), nullable=True, index=True)
    docs_issued_order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)

    reserved_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    assigned_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    confirmed_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    void_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    reserved_at = db.Column(db.DateTime, nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    docs_issued_at = db.Column(db.DateTime, nullable=True)
    void_at = db.Column(db.DateTime, nullable=True)

    source = db.Column(db.String(30), nullable=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer_order = db.relationship('CustomerOrder', foreign_keys=[customer_order_id], backref='vin_registry_rows')
    order_line = db.relationship('CustomerOrderLine', foreign_keys=[order_line_id], backref='vin_registry_rows')
    docs_issued_order = db.relationship('CustomerOrder', foreign_keys=[docs_issued_order_id])
    supply_need = db.relationship('SupplyNeed', foreign_keys=[supply_need_id], backref='vin_registry_rows')
    trailer = db.relationship('Trailer', foreign_keys=[trailer_id], backref='vin_registry_rows')
    sales_contract = db.relationship('SalesContract', foreign_keys=[sales_contract_id], backref='vin_registry_rows')
    reserved_by_user = db.relationship('User', foreign_keys=[reserved_by_user_id])
    assigned_by_user = db.relationship('User', foreign_keys=[assigned_by_user_id])
    confirmed_by_user = db.relationship('User', foreign_keys=[confirmed_by_user_id])
    void_by_user = db.relationship('User', foreign_keys=[void_by_user_id])


class VinRegistryEvent(db.Model):
    __tablename__ = 'vin_registry_event'

    id = db.Column(db.Integer, primary_key=True)
    vin_registry_id = db.Column(db.Integer, db.ForeignKey('vin_registry.id'), nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    old_status = db.Column(db.String(30), nullable=True)
    new_status = db.Column(db.String(30), nullable=True)
    customer_order_id = db.Column(db.Integer, db.ForeignKey('customer_order.id'), nullable=True, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey('customer_order_line.id'), nullable=True, index=True)
    sales_contract_id = db.Column(db.Integer, db.ForeignKey('sales_contract.id'), nullable=True, index=True)
    trailer_id = db.Column(db.Integer, db.ForeignKey('trailer.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    vin_registry = db.relationship('VinRegistry', backref=db.backref('events', lazy='dynamic', cascade='all, delete-orphan'))
    customer_order = db.relationship('CustomerOrder', foreign_keys=[customer_order_id])
    order_line = db.relationship('CustomerOrderLine', foreign_keys=[order_line_id])
    sales_contract = db.relationship('SalesContract', foreign_keys=[sales_contract_id])
    trailer = db.relationship('Trailer', foreign_keys=[trailer_id])
    user = db.relationship('User', foreign_keys=[user_id])


# ---------- СПРАВОЧНИКИ И МАТРИЦЫ КОНФИГУРАТОРА ПРИЦЕПОВ ----------

class TrailerProductGroup(db.Model):
    __tablename__ = 'trailer_product_group'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    name_prefix = db.Column(db.String(160), nullable=False)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=True, index=True)
    otts_valid_from = db.Column(db.Date, nullable=True, index=True)
    otts_valid_to = db.Column(db.Date, nullable=True, index=True)
    vehicle_category = db.Column(db.String(20), nullable=True)
    product_category_id = db.Column(db.Integer, db.ForeignKey('product_category.id'), nullable=True, index=True)
    axle_count = db.Column(db.Integer, nullable=True)
    wheel_count = db.Column(db.Integer, nullable=True)
    max_mass_kg = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    product_category = db.relationship('ProductCategory', foreign_keys=[product_category_id], backref='product_groups')


class TrailerBodySize(db.Model):
    __tablename__ = 'trailer_body_size'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    length_mm = db.Column(db.Integer, nullable=False)
    width_mm = db.Column(db.Integer, nullable=False)
    article_part = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerBoardHeight(db.Model):
    __tablename__ = 'trailer_board_height'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    height_mm = db.Column(db.Integer, nullable=False, default=0)
    article_part = db.Column(db.String(20), nullable=False)
    is_no_board = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerWheelOption(db.Model):
    __tablename__ = 'trailer_wheel_option'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    wheel_size = db.Column(db.String(40), nullable=False)
    article_part = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerHubOption(db.Model):
    __tablename__ = 'trailer_hub_option'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    for_wheel_size = db.Column(db.String(60), nullable=False)
    article_part = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerSupportWheelOption(db.Model):
    __tablename__ = 'trailer_support_wheel_option'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    article_part = db.Column(db.String(20), nullable=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerTentOption(db.Model):
    __tablename__ = 'trailer_tent_option'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    height_mm = db.Column(db.Integer, nullable=False, default=0)
    article_part = db.Column(db.String(20), nullable=True)
    is_no_tent = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerBodyExecution(db.Model):
    __tablename__ = 'trailer_body_execution'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    name_for_title = db.Column(db.String(120), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerSpecialOption(db.Model):
    __tablename__ = 'trailer_special_option'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    option_type = db.Column(db.String(40), nullable=False, index=True)
    article_part = db.Column(db.String(40), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrailerAllowedOption(db.Model):
    __tablename__ = 'trailer_allowed_option'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    option_type = db.Column(db.String(40), nullable=False, index=True)
    option_id = db.Column(db.Integer, nullable=False, index=True)
    is_allowed = db.Column(db.Boolean, nullable=False, default=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup', backref='allowed_options')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'option_type', 'option_id', name='uq_trailer_allowed_option'),
    )


class TrailerPlatformPriceMatrix(db.Model):
    __tablename__ = 'trailer_platform_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    body_size_id = db.Column(db.Integer, db.ForeignKey('trailer_body_size.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup')
    body_size = db.relationship('TrailerBodySize')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'body_size_id', 'valid_from', name='uq_trailer_platform_price'),
    )


class TrailerBoardPriceMatrix(db.Model):
    __tablename__ = 'trailer_board_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    body_size_id = db.Column(db.Integer, db.ForeignKey('trailer_body_size.id'), nullable=False, index=True)
    board_height_id = db.Column(db.Integer, db.ForeignKey('trailer_board_height.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    body_size = db.relationship('TrailerBodySize')
    board_height = db.relationship('TrailerBoardHeight')

    __table_args__ = (
        db.UniqueConstraint('body_size_id', 'board_height_id', 'valid_from', name='uq_trailer_board_price'),
    )


class TrailerTentPriceMatrix(db.Model):
    __tablename__ = 'trailer_tent_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    body_size_id = db.Column(db.Integer, db.ForeignKey('trailer_body_size.id'), nullable=False, index=True)
    tent_option_id = db.Column(db.Integer, db.ForeignKey('trailer_tent_option.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    body_size = db.relationship('TrailerBodySize')
    tent_option = db.relationship('TrailerTentOption')

    __table_args__ = (
        db.UniqueConstraint('body_size_id', 'tent_option_id', 'valid_from', name='uq_trailer_tent_price'),
    )


class TrailerWheelPriceMatrix(db.Model):
    __tablename__ = 'trailer_wheel_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    wheel_option_id = db.Column(db.Integer, db.ForeignKey('trailer_wheel_option.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup')
    wheel_option = db.relationship('TrailerWheelOption')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'wheel_option_id', 'valid_from', name='uq_trailer_wheel_price'),
    )


class TrailerHubPriceMatrix(db.Model):
    __tablename__ = 'trailer_hub_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    hub_option_id = db.Column(db.Integer, db.ForeignKey('trailer_hub_option.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup')
    hub_option = db.relationship('TrailerHubOption')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'hub_option_id', 'valid_from', name='uq_trailer_hub_price'),
    )


class TrailerSupportWheelPriceMatrix(db.Model):
    __tablename__ = 'trailer_support_wheel_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    support_wheel_option_id = db.Column(db.Integer, db.ForeignKey('trailer_support_wheel_option.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    support_wheel_option = db.relationship('TrailerSupportWheelOption')

    __table_args__ = (
        db.UniqueConstraint('support_wheel_option_id', 'valid_from', name='uq_trailer_support_wheel_price'),
    )


class TrailerSpecialOptionPriceMatrix(db.Model):
    __tablename__ = 'trailer_special_option_price_matrix'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    special_option_id = db.Column(db.Integer, db.ForeignKey('trailer_special_option.id'), nullable=False, index=True)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='KZT')
    valid_from = db.Column(db.Date, nullable=True, index=True)
    valid_to = db.Column(db.Date, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup')
    special_option = db.relationship('TrailerSpecialOption')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'special_option_id', 'valid_from', name='uq_trailer_special_option_price'),
    )


class TrailerDimensionMatrix(db.Model):
    __tablename__ = 'trailer_dimension_matrix'

    id = db.Column(db.Integer, primary_key=True)
    body_size_id = db.Column(db.Integer, db.ForeignKey('trailer_body_size.id'), nullable=False, index=True)
    board_height_id = db.Column(db.Integer, db.ForeignKey('trailer_board_height.id'), nullable=False, index=True)
    overall_length_mm = db.Column(db.Integer, nullable=False)
    overall_width_mm = db.Column(db.Integer, nullable=False)
    overall_height_mm = db.Column(db.Integer, nullable=False)
    inner_length_mm = db.Column(db.Integer, nullable=False)
    inner_width_mm = db.Column(db.Integer, nullable=False)
    inner_height_mm = db.Column(db.Integer, nullable=False)
    overall_dimensions_text = db.Column(db.String(80), nullable=False)
    inner_dimensions_text = db.Column(db.String(80), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    body_size = db.relationship('TrailerBodySize')
    board_height = db.relationship('TrailerBoardHeight')

    __table_args__ = (
        db.UniqueConstraint('body_size_id', 'board_height_id', name='uq_trailer_dimension'),
    )


class TrailerOtssModificationMatrix(db.Model):
    __tablename__ = 'trailer_otss_modification_matrix'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('trailer_product_group.id'), nullable=False, index=True)
    body_execution_id = db.Column(db.Integer, db.ForeignKey('trailer_body_execution.id'), nullable=False, index=True)
    board_height_id = db.Column(db.Integer, db.ForeignKey('trailer_board_height.id'), nullable=True, index=True)
    special_option_id = db.Column(db.Integer, db.ForeignKey('trailer_special_option.id'), nullable=True, index=True)
    otss_number = db.Column(db.String(120), nullable=True)
    otss_type = db.Column(db.String(20), nullable=False, index=True)
    otss_modification = db.Column(db.String(20), nullable=False)
    vin_modification_code = db.Column(db.String(20), nullable=False)
    otts_valid_from = db.Column(db.Date, nullable=True, index=True)
    otts_valid_to = db.Column(db.Date, nullable=True, index=True)
    description = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = db.relationship('TrailerProductGroup')
    body_execution = db.relationship('TrailerBodyExecution')
    board_height = db.relationship('TrailerBoardHeight')
    special_option = db.relationship('TrailerSpecialOption')
