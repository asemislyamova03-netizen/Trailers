"""order lines and production foundation

Revision ID: f9a1b2c3d4e5
Revises: e8f9a0b1c2d3
Create Date: 2026-05-18 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f9a1b2c3d4e5'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def _has_table(table_name):
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name):
    if not _has_table(table_name):
        return set()
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name):
    if not _has_table(table_name):
        return set()
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _add_column(table_name, column):
    if column.name not in _columns(table_name):
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)


def _create_index(table_name, index_name, columns):
    if index_name not in _indexes(table_name):
        op.create_index(index_name, table_name, columns, unique=False)


def upgrade():
    bind = op.get_bind()

    _add_column('warehouse', sa.Column('warehouse_kind', sa.String(length=30), nullable=False, server_default='finished_goods'))
    _create_index('warehouse', 'ix_warehouse_warehouse_kind', ['warehouse_kind'])
    bind.execute(sa.text("UPDATE warehouse SET warehouse_kind = CASE WHEN is_production = 1 THEN 'production' ELSE 'finished_goods' END WHERE warehouse_kind IS NULL OR warehouse_kind = 'finished_goods'"))

    if not _has_table('warehouse_storage_area'):
        op.create_table(
            'warehouse_storage_area',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=False),
            sa.Column('code', sa.String(length=40), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('area_type', sa.String(length=40), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('warehouse_id', 'code', name='uq_warehouse_storage_area_code'),
        )
    _create_index('warehouse_storage_area', 'ix_warehouse_storage_area_warehouse_id', ['warehouse_id'])
    _create_index('warehouse_storage_area', 'ix_warehouse_storage_area_code', ['code'])
    _create_index('warehouse_storage_area', 'ix_warehouse_storage_area_area_type', ['area_type'])
    _create_index('warehouse_storage_area', 'ix_warehouse_storage_area_is_active', ['is_active'])

    if not _has_table('production_workshop'):
        op.create_table(
            'production_workshop',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=40), nullable=False, unique=True),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('workshop_type', sa.String(length=40), nullable=False, server_default='trailer'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    _create_index('production_workshop', 'ix_production_workshop_code', ['code'])
    _create_index('production_workshop', 'ix_production_workshop_workshop_type', ['workshop_type'])
    _create_index('production_workshop', 'ix_production_workshop_is_active', ['is_active'])

    for code, name, workshop_type, sort_order in [
        ('LIGHT_TRAILERS', 'Легковые прицепы', 'trailer_light', 10),
        ('CARGO_TRAILERS', 'Грузовые прицепы', 'trailer_cargo', 20),
        ('LASER', 'Лазерная резка', 'semi_finished', 30),
        ('BENDING', 'Листогиб', 'semi_finished', 40),
        ('WELDING', 'Сварочный блок', 'welding', 50),
        ('TENTS', 'Тенты', 'tent', 60),
        ('FRAMES', 'Каркасы', 'frame', 70),
        ('TENT_FRAMES', 'Тент-каркасы', 'tent_frame', 80),
        ('ELECTRIC', 'Автоэлектрика', 'electric', 90),
        ('CONSTRUCTION', 'Стройка', 'construction', 100),
        ('CARGO_VEHICLES', 'Цех грузовых автомобилей', 'cargo_vehicle', 110),
        ('OTHER_TASKS', 'Прочие поручения', 'other', 120),
    ]:
        bind.execute(sa.text("""
            INSERT INTO production_workshop (code, name, workshop_type, is_active, sort_order, created_at, updated_at)
            SELECT :code, :name, :workshop_type, 1, :sort_order, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (SELECT 1 FROM production_workshop WHERE code = :code)
        """), {'code': code, 'name': name, 'workshop_type': workshop_type, 'sort_order': sort_order})

    bind.execute(sa.text("""
        INSERT INTO warehouse_storage_area (warehouse_id, code, name, area_type, is_active, sort_order, created_at, updated_at)
        SELECT id, 'COMPONENTS', 'Склад комплектующих', 'components', 1, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM warehouse
        WHERE NOT EXISTS (
            SELECT 1 FROM warehouse_storage_area a WHERE a.warehouse_id = warehouse.id AND a.code = 'COMPONENTS'
        )
    """))
    bind.execute(sa.text("""
        INSERT INTO warehouse_storage_area (warehouse_id, code, name, area_type, is_active, sort_order, created_at, updated_at)
        SELECT id, 'SEMI_FINISHED', 'Склад полуфабрикатов', 'semi_finished', 1, 20, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM warehouse
        WHERE is_production = 1 AND NOT EXISTS (
            SELECT 1 FROM warehouse_storage_area a WHERE a.warehouse_id = warehouse.id AND a.code = 'SEMI_FINISHED'
        )
    """))

    if not _has_table('customer_order_line'):
        op.create_table(
            'customer_order_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=False),
            sa.Column('line_no', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('line_type', sa.String(length=30), nullable=False, server_default='TRAILER'),
            sa.Column('fulfillment_source', sa.String(length=30), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('production_workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('unit_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('total_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='NEW'),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('article_snapshot', sa.String(length=80), nullable=True),
            sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
            sa.Column('config_snapshot_json', sa.Text(), nullable=True),
            sa.Column('calculated_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('price_breakdown_json', sa.Text(), nullable=True),
            sa.Column('overall_dimensions_text', sa.String(length=80), nullable=True),
            sa.Column('inner_dimensions_text', sa.String(length=80), nullable=True),
            sa.Column('otss_number', sa.String(length=120), nullable=True),
            sa.Column('otss_type', sa.String(length=20), nullable=True),
            sa.Column('otss_modification', sa.String(length=20), nullable=True),
            sa.Column('vin_modification_code', sa.String(length=20), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('order_id', 'line_no', name='uq_customer_order_line_no'),
        )
    for name, cols in [
        ('ix_customer_order_line_order_id', ['order_id']),
        ('ix_customer_order_line_line_type', ['line_type']),
        ('ix_customer_order_line_fulfillment_source', ['fulfillment_source']),
        ('ix_customer_order_line_item_id', ['item_id']),
        ('ix_customer_order_line_production_workshop_id', ['production_workshop_id']),
        ('ix_customer_order_line_status', ['status']),
        ('ix_customer_order_line_article_snapshot', ['article_snapshot']),
        ('ix_customer_order_line_vin_modification_code', ['vin_modification_code']),
        ('ix_customer_order_line_created_at', ['created_at']),
    ]:
        _create_index('customer_order_line', name, cols)

    bind.execute(sa.text("""
        INSERT INTO customer_order_line (
            order_id, line_no, line_type, fulfillment_source, item_id, quantity,
            unit_price, total_price, status, article_snapshot, product_name_snapshot,
            config_snapshot_json, calculated_price, price_breakdown_json,
            overall_dimensions_text, inner_dimensions_text, otss_number, otss_type,
            otss_modification, vin_modification_code, created_at, updated_at
        )
        SELECT
            o.id, 1, 'TRAILER', o.fulfillment_source, o.item_id, COALESCE(o.quantity, 1),
            CASE WHEN COALESCE(o.quantity, 1) > 0 THEN o.price / COALESCE(o.quantity, 1) ELSE o.price END,
            o.price, o.status, o.article_snapshot, o.product_name_snapshot,
            o.config_snapshot_json, o.calculated_price, o.price_breakdown_json,
            o.overall_dimensions_text, o.inner_dimensions_text, o.otss_number, o.otss_type,
            o.otss_modification, o.vin_modification_code, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM customer_order o
        WHERE NOT EXISTS (SELECT 1 FROM customer_order_line l WHERE l.order_id = o.id AND l.line_no = 1)
    """))

    if not _has_table('contract_template'):
        op.create_table(
            'contract_template',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=60), nullable=False, unique=True),
            sa.Column('name', sa.String(length=160), nullable=False),
            sa.Column('template_type', sa.String(length=40), nullable=False, server_default='sale'),
            sa.Column('product_group_code', sa.String(length=20), nullable=True),
            sa.Column('otss_number', sa.String(length=120), nullable=True),
            sa.Column('otss_type', sa.String(length=20), nullable=True),
            sa.Column('otss_modification', sa.String(length=20), nullable=True),
            sa.Column('body_execution_code', sa.String(length=40), nullable=True),
            sa.Column('customer_type', sa.String(length=20), nullable=True),
            sa.Column('content_path', sa.String(length=500), nullable=True),
            sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_contract_template_code', ['code']),
        ('ix_contract_template_template_type', ['template_type']),
        ('ix_contract_template_product_group_code', ['product_group_code']),
        ('ix_contract_template_otss_number', ['otss_number']),
        ('ix_contract_template_otss_type', ['otss_type']),
        ('ix_contract_template_otss_modification', ['otss_modification']),
        ('ix_contract_template_body_execution_code', ['body_execution_code']),
        ('ix_contract_template_customer_type', ['customer_type']),
        ('ix_contract_template_is_default', ['is_default']),
        ('ix_contract_template_is_active', ['is_active']),
    ]:
        _create_index('contract_template', name, cols)

    if not _has_table('sales_contract_line'):
        op.create_table(
            'sales_contract_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('sales_contract_id', sa.Integer(), sa.ForeignKey('sales_contract.id'), nullable=False),
            sa.Column('order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('trailer_id', sa.Integer(), sa.ForeignKey('trailer.id'), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('vin_registry_id', sa.Integer(), sa.ForeignKey('vin_registry.id'), nullable=True),
            sa.Column('line_no', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('unit_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('total_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('article_snapshot', sa.String(length=80), nullable=True),
            sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
            sa.Column('otss_number', sa.String(length=120), nullable=True),
            sa.Column('otss_type', sa.String(length=20), nullable=True),
            sa.Column('otss_modification', sa.String(length=20), nullable=True),
            sa.Column('vin_modification_code', sa.String(length=20), nullable=True),
            sa.Column('vin_full', sa.String(length=50), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('sales_contract_id', 'line_no', name='uq_sales_contract_line_no'),
        )
    for name, cols in [
        ('ix_sales_contract_line_sales_contract_id', ['sales_contract_id']),
        ('ix_sales_contract_line_order_line_id', ['order_line_id']),
        ('ix_sales_contract_line_trailer_id', ['trailer_id']),
        ('ix_sales_contract_line_item_id', ['item_id']),
        ('ix_sales_contract_line_vin_registry_id', ['vin_registry_id']),
        ('ix_sales_contract_line_article_snapshot', ['article_snapshot']),
        ('ix_sales_contract_line_vin_modification_code', ['vin_modification_code']),
        ('ix_sales_contract_line_vin_full', ['vin_full']),
    ]:
        _create_index('sales_contract_line', name, cols)

    if not _has_table('payment_schedule_stage'):
        op.create_table(
            'payment_schedule_stage',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=True),
            sa.Column('sales_contract_id', sa.Integer(), sa.ForeignKey('sales_contract.id'), nullable=True),
            sa.Column('stage_code', sa.String(length=40), nullable=False, server_default='PREPAYMENT'),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('due_date', sa.Date(), nullable=True),
            sa.Column('percent', sa.Numeric(5, 2), nullable=True),
            sa.Column('amount', sa.Numeric(12, 2), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='planned'),
            sa.Column('kaspi_payment_id', sa.String(length=120), nullable=True),
            sa.Column('kaspi_payment_url', sa.String(length=500), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_payment_schedule_stage_order_id', ['order_id']),
        ('ix_payment_schedule_stage_sales_contract_id', ['sales_contract_id']),
        ('ix_payment_schedule_stage_stage_code', ['stage_code']),
        ('ix_payment_schedule_stage_due_date', ['due_date']),
        ('ix_payment_schedule_stage_status', ['status']),
        ('ix_payment_schedule_stage_kaspi_payment_id', ['kaspi_payment_id']),
        ('ix_payment_schedule_stage_created_at', ['created_at']),
    ]:
        _create_index('payment_schedule_stage', name, cols)

    if not _has_table('production_employee'):
        op.create_table(
            'production_employee',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('full_name', sa.String(length=160), nullable=False),
            sa.Column('employee_code', sa.String(length=40), nullable=True, unique=True),
            sa.Column('phone', sa.String(length=50), nullable=True),
            sa.Column('default_workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('primary_role', sa.String(length=60), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_production_employee_user_id', ['user_id']),
        ('ix_production_employee_full_name', ['full_name']),
        ('ix_production_employee_employee_code', ['employee_code']),
        ('ix_production_employee_default_workshop_id', ['default_workshop_id']),
        ('ix_production_employee_primary_role', ['primary_role']),
        ('ix_production_employee_is_active', ['is_active']),
    ]:
        _create_index('production_employee', name, cols)

    if not _has_table('production_shift'):
        op.create_table(
            'production_shift',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('employee_id', sa.Integer(), sa.ForeignKey('production_employee.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('work_area', sa.String(length=60), nullable=False, server_default='production'),
            sa.Column('planned_date', sa.Date(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='open'),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_production_shift_employee_id', ['employee_id']),
        ('ix_production_shift_user_id', ['user_id']),
        ('ix_production_shift_workshop_id', ['workshop_id']),
        ('ix_production_shift_work_area', ['work_area']),
        ('ix_production_shift_planned_date', ['planned_date']),
        ('ix_production_shift_started_at', ['started_at']),
        ('ix_production_shift_ended_at', ['ended_at']),
        ('ix_production_shift_status', ['status']),
    ]:
        _create_index('production_shift', name, cols)

    if not _has_table('production_shift_output'):
        op.create_table(
            'production_shift_output',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('shift_id', sa.Integer(), sa.ForeignKey('production_shift.id'), nullable=False),
            sa.Column('employee_id', sa.Integer(), sa.ForeignKey('production_employee.id'), nullable=False),
            sa.Column('workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('production_request_line_id', sa.Integer(), sa.ForeignKey('production_request_line.id'), nullable=True),
            sa.Column('output_type', sa.String(length=60), nullable=False, server_default='other'),
            sa.Column('quantity', sa.Numeric(12, 3), nullable=False, server_default='0'),
            sa.Column('defect_quantity', sa.Numeric(12, 3), nullable=False, server_default='0'),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='accepted'),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_production_shift_output_shift_id', ['shift_id']),
        ('ix_production_shift_output_employee_id', ['employee_id']),
        ('ix_production_shift_output_workshop_id', ['workshop_id']),
        ('ix_production_shift_output_item_id', ['item_id']),
        ('ix_production_shift_output_order_line_id', ['order_line_id']),
        ('ix_production_shift_output_production_request_line_id', ['production_request_line_id']),
        ('ix_production_shift_output_output_type', ['output_type']),
        ('ix_production_shift_output_status', ['status']),
        ('ix_production_shift_output_created_at', ['created_at']),
    ]:
        _create_index('production_shift_output', name, cols)

    if not _has_table('item_bill_of_materials'):
        op.create_table(
            'item_bill_of_materials',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('version', sa.String(length=40), nullable=False, server_default='default'),
            sa.Column('name', sa.String(length=160), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('item_id', 'version', name='uq_item_bom_version'),
        )
    _create_index('item_bill_of_materials', 'ix_item_bill_of_materials_item_id', ['item_id'])
    _create_index('item_bill_of_materials', 'ix_item_bill_of_materials_is_active', ['is_active'])

    if not _has_table('item_bill_of_materials_line'):
        op.create_table(
            'item_bill_of_materials_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('bom_id', sa.Integer(), sa.ForeignKey('item_bill_of_materials.id'), nullable=False),
            sa.Column('component_item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('quantity_per_unit', sa.Numeric(12, 3), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('source_area_type', sa.String(length=40), nullable=False, server_default='components'),
            sa.Column('workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for name, cols in [
        ('ix_item_bill_of_materials_line_bom_id', ['bom_id']),
        ('ix_item_bill_of_materials_line_component_item_id', ['component_item_id']),
        ('ix_item_bill_of_materials_line_source_area_type', ['source_area_type']),
        ('ix_item_bill_of_materials_line_workshop_id', ['workshop_id']),
        ('ix_item_bill_of_materials_line_is_active', ['is_active']),
    ]:
        _create_index('item_bill_of_materials_line', name, cols)

    for table_name, columns in [
        ('order_payment', [sa.Column('payment_stage_id', sa.Integer(), nullable=True)]),
        ('reservation', [sa.Column('order_line_id', sa.Integer(), nullable=True)]),
        ('supply_need', [sa.Column('order_line_id', sa.Integer(), nullable=True), sa.Column('production_workshop_id', sa.Integer(), nullable=True)]),
        ('production_request_line', [sa.Column('order_line_id', sa.Integer(), nullable=True), sa.Column('production_workshop_id', sa.Integer(), nullable=True)]),
        ('produced_unit', [sa.Column('order_line_id', sa.Integer(), nullable=True), sa.Column('production_workshop_id', sa.Integer(), nullable=True)]),
        ('stock_movement', [sa.Column('order_line_id', sa.Integer(), nullable=True)]),
        ('vin_registry', [sa.Column('order_line_id', sa.Integer(), nullable=True)]),
        ('vin_registry_event', [sa.Column('order_line_id', sa.Integer(), nullable=True)]),
    ]:
        for column in columns:
            _add_column(table_name, column)
            _create_index(table_name, f'ix_{table_name}_{column.name}', [column.name])

    bind.execute(sa.text("UPDATE reservation SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = reservation.order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND order_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE supply_need SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = supply_need.order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND order_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE production_request_line SET order_line_id = (SELECT order_line_id FROM supply_need n WHERE n.id = production_request_line.supply_need_id) WHERE order_line_id IS NULL AND supply_need_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE produced_unit SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = produced_unit.order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND order_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE stock_movement SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = stock_movement.order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND order_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE vin_registry SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = vin_registry.customer_order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND customer_order_id IS NOT NULL"))
    bind.execute(sa.text("UPDATE vin_registry_event SET order_line_id = (SELECT id FROM customer_order_line l WHERE l.order_id = vin_registry_event.customer_order_id AND l.line_no = 1) WHERE order_line_id IS NULL AND customer_order_id IS NOT NULL"))


def downgrade():
    for table_name, column_names in [
        ('vin_registry_event', ['order_line_id']),
        ('vin_registry', ['order_line_id']),
        ('stock_movement', ['order_line_id']),
        ('produced_unit', ['production_workshop_id', 'order_line_id']),
        ('production_request_line', ['production_workshop_id', 'order_line_id']),
        ('supply_need', ['production_workshop_id', 'order_line_id']),
        ('reservation', ['order_line_id']),
        ('order_payment', ['payment_stage_id']),
    ]:
        for column_name in column_names:
            index_name = f'ix_{table_name}_{column_name}'
            if index_name in _indexes(table_name):
                op.drop_index(index_name, table_name=table_name)
            if column_name in _columns(table_name):
                with op.batch_alter_table(table_name) as batch:
                    batch.drop_column(column_name)

    for table_name in [
        'item_bill_of_materials_line',
        'item_bill_of_materials',
        'production_shift_output',
        'production_shift',
        'production_employee',
        'payment_schedule_stage',
        'sales_contract_line',
        'contract_template',
        'customer_order_line',
        'production_workshop',
        'warehouse_storage_area',
    ]:
        if _has_table(table_name):
            op.drop_table(table_name)

    if 'ix_warehouse_warehouse_kind' in _indexes('warehouse'):
        op.drop_index('ix_warehouse_warehouse_kind', table_name='warehouse')
    if 'warehouse_kind' in _columns('warehouse'):
        with op.batch_alter_table('warehouse') as batch:
            batch.drop_column('warehouse_kind')
