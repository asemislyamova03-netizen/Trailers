"""product categories and inventory foundation

Revision ID: b1c2d3e4f5a6
Revises: a2b3c4d5e6f7
Create Date: 2026-05-29 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(table_name):
    return table_name in _insp().get_table_names()


def _columns(table_name):
    if not _has_table(table_name):
        return set()
    return {column['name'] for column in _insp().get_columns(table_name)}


def _indexes(table_name):
    if not _has_table(table_name):
        return set()
    return {index['name'] for index in _insp().get_indexes(table_name)}


def _add_column(table_name, column):
    if column.name in _columns(table_name):
        return
    with op.batch_alter_table(table_name) as batch:
        batch.add_column(column)


def _create_index(name, table_name, columns, unique=False):
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns, unique=unique)


def _seed_categories():
    categories = [
        ('light_trailer', 'Легковые прицепы', 'finished_vehicle', 1, 1, 1, 10),
        ('cargo_vehicle', 'Грузовые автомобили', 'finished_vehicle', 1, 1, 1, 20),
        ('component', 'Комплектующие', 'component', 0, 1, 0, 30),
        ('semi_finished', 'Полуфабрикаты', 'semi_finished', 0, 0, 0, 40),
        ('hardware', 'Метизы', 'component', 0, 0, 0, 50),
        ('raw_material', 'Сырьё / металлопрокат', 'raw_material', 0, 0, 0, 60),
        ('tent', 'Тенты', 'component', 0, 1, 0, 70),
        ('frame', 'Каркасы', 'component', 0, 1, 0, 80),
        ('tent_frame', 'Тент-каркасы', 'component', 0, 1, 0, 90),
    ]
    for code, name, kind, vin_required, sellable, finished, sort_order in categories:
        op.execute(sa.text("""
            INSERT INTO product_category
                (code, name, kind, is_vin_required, is_sellable, is_finished_vehicle, sort_order, is_active, created_at, updated_at)
            SELECT
                :code, :name, :kind, :vin_required, :sellable, :finished, :sort_order, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (SELECT 1 FROM product_category WHERE code = :code)
        """).bindparams(
            code=code,
            name=name,
            kind=kind,
            vin_required=vin_required,
            sellable=sellable,
            finished=finished,
            sort_order=sort_order,
        ))


def _seed_storage_areas():
    if not _has_table('warehouse_storage_area') or not _has_table('warehouse'):
        return
    areas = [
        ('FINISHED_GOODS', 'Готовая продукция', 'finished_goods', 10),
        ('COMPONENTS', 'Комплектующие', 'components', 20),
        ('SEMI_FINISHED', 'Полуфабрикаты', 'semi_finished', 30),
        ('HARDWARE', 'Метизы', 'hardware', 40),
        ('RAW_MATERIALS', 'Сырьё / металлопрокат', 'raw_materials', 50),
    ]
    conn = op.get_bind()
    warehouse_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM warehouse")).fetchall()]
    for warehouse_id in warehouse_ids:
        for code, name, area_type, sort_order in areas:
            op.execute(sa.text("""
                INSERT INTO warehouse_storage_area
                    (warehouse_id, code, name, area_type, is_active, sort_order, created_at, updated_at)
                SELECT :warehouse_id, :code, :name, :area_type, 1, :sort_order, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE NOT EXISTS (
                    SELECT 1 FROM warehouse_storage_area
                    WHERE warehouse_id = :warehouse_id AND code = :code
                )
            """).bindparams(
                warehouse_id=warehouse_id,
                code=code,
                name=name,
                area_type=area_type,
                sort_order=sort_order,
            ))


def upgrade():
    if not _has_table('product_category'):
        op.create_table(
            'product_category',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=40), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('kind', sa.String(length=40), nullable=False, server_default='goods'),
            sa.Column('is_vin_required', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('is_sellable', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('is_finished_vehicle', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.UniqueConstraint('code', name='uq_product_category_code'),
        )
    _create_index('ix_product_category_code', 'product_category', ['code'], unique=False)
    _create_index('ix_product_category_is_active', 'product_category', ['is_active'], unique=False)
    _create_index('ix_product_category_kind', 'product_category', ['kind'], unique=False)
    _seed_categories()

    if _has_table('warehouse'):
        _add_column('warehouse', sa.Column('is_sales_point', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _add_column('warehouse', sa.Column('can_sell', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _add_column('warehouse', sa.Column('can_ship_to_customer', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _add_column('warehouse', sa.Column('primary_product_category', sa.String(length=40), nullable=True))
        _add_column('warehouse', sa.Column('product_category_scope', sa.String(length=80), nullable=True))
        _create_index('ix_warehouse_is_sales_point', 'warehouse', ['is_sales_point'])
        _create_index('ix_warehouse_can_sell', 'warehouse', ['can_sell'])
        _create_index('ix_warehouse_can_ship_to_customer', 'warehouse', ['can_ship_to_customer'])
        _create_index('ix_warehouse_primary_product_category', 'warehouse', ['primary_product_category'])
        _create_index('ix_warehouse_product_category_scope', 'warehouse', ['product_category_scope'])
        op.execute(sa.text("""
            UPDATE warehouse
            SET can_sell = CASE WHEN COALESCE(is_production, 0) = 1 OR warehouse_kind IN ('assembly', 'raw_materials') THEN 0 ELSE 1 END,
                is_sales_point = CASE WHEN COALESCE(is_production, 0) = 1 OR warehouse_kind IN ('assembly', 'raw_materials') THEN 0 ELSE 1 END,
                can_ship_to_customer = CASE WHEN COALESCE(is_production, 0) = 1 OR warehouse_kind IN ('assembly', 'raw_materials') THEN 0 ELSE 1 END
        """))

    if _has_table('trailer_product_group'):
        _add_column('trailer_product_group', sa.Column('product_category_id', sa.Integer(), nullable=True))
        _create_index('ix_trailer_product_group_product_category_id', 'trailer_product_group', ['product_category_id'])
        op.execute(sa.text("""
            UPDATE trailer_product_group
            SET product_category_id = (
                SELECT id FROM product_category
                WHERE code = CASE
                    WHEN vehicle_category = 'O4' OR code LIKE '%G%' THEN 'cargo_vehicle'
                    ELSE 'light_trailer'
                END
            )
            WHERE product_category_id IS NULL
        """))

    if _has_table('item'):
        _add_column('item', sa.Column('product_category_id', sa.Integer(), nullable=True))
        _add_column('item', sa.Column('group_id', sa.Integer(), nullable=True))
        _add_column('item', sa.Column('max_mass_kg', sa.Integer(), nullable=True))
        _add_column('item', sa.Column('is_sellable', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _add_column('item', sa.Column('requires_vin', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        _add_column('item', sa.Column('is_realization_line', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        _add_column('item', sa.Column('is_internal_bom_item', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        _create_index('ix_item_product_category_id', 'item', ['product_category_id'])
        _create_index('ix_item_group_id', 'item', ['group_id'])
        _create_index('ix_item_is_sellable', 'item', ['is_sellable'])
        _create_index('ix_item_requires_vin', 'item', ['requires_vin'])
        _create_index('ix_item_is_realization_line', 'item', ['is_realization_line'])
        _create_index('ix_item_is_internal_bom_item', 'item', ['is_internal_bom_item'])
        op.execute(sa.text("UPDATE item SET item_type = 'COMPONENT' WHERE item_type = 'PART'"))
        op.execute(sa.text("""
            UPDATE item
            SET product_category_id = (
                SELECT id FROM product_category
                WHERE code = CASE
                    WHEN item_type = 'COMPONENT' THEN 'component'
                    WHEN article LIKE '%G-%' OR article LIKE '%G%' THEN 'cargo_vehicle'
                    ELSE 'light_trailer'
                END
            )
            WHERE product_category_id IS NULL
        """))
        op.execute(sa.text("UPDATE item SET requires_vin = CASE WHEN item_type = 'TRAILER' THEN 1 ELSE 0 END WHERE requires_vin IS NULL OR requires_vin = 0"))
        op.execute(sa.text("UPDATE item SET is_realization_line = CASE WHEN item_type = 'TRAILER' THEN 1 ELSE is_realization_line END"))
        op.execute(sa.text("UPDATE item SET is_sellable = 1 WHERE is_sellable IS NULL"))

    if _has_table('customer_order'):
        _add_column('customer_order', sa.Column('realization_status', sa.String(length=30), nullable=False, server_default='not_started'))
        _add_column('customer_order', sa.Column('realized_at', sa.DateTime(), nullable=True))
        _create_index('ix_customer_order_realization_status', 'customer_order', ['realization_status'])

    if _has_table('customer_order_line'):
        _add_column('customer_order_line', sa.Column('trailer_id', sa.Integer(), nullable=True))
        _add_column('customer_order_line', sa.Column('assembly_status', sa.String(length=30), nullable=False, server_default='not_required'))
        _add_column('customer_order_line', sa.Column('assembly_operation_id', sa.Integer(), nullable=True))
        _add_column('customer_order_line', sa.Column('include_in_vehicle_contract', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        _add_column('customer_order_line', sa.Column('include_in_realization', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _create_index('ix_customer_order_line_trailer_id', 'customer_order_line', ['trailer_id'])
        _create_index('ix_customer_order_line_assembly_status', 'customer_order_line', ['assembly_status'])
        _create_index('ix_customer_order_line_assembly_operation_id', 'customer_order_line', ['assembly_operation_id'])
        _create_index('ix_customer_order_line_include_in_vehicle_contract', 'customer_order_line', ['include_in_vehicle_contract'])
        _create_index('ix_customer_order_line_include_in_realization', 'customer_order_line', ['include_in_realization'])
        op.execute(sa.text("""
            UPDATE customer_order_line
            SET include_in_vehicle_contract = CASE WHEN line_type = 'TRAILER' THEN 1 ELSE 0 END,
                include_in_realization = 1
        """))

    if _has_table('item_bill_of_materials_line'):
        _add_column('item_bill_of_materials_line', sa.Column('component_category', sa.String(length=40), nullable=True))
        _add_column('item_bill_of_materials_line', sa.Column('is_required', sa.Boolean(), nullable=False, server_default=sa.text('1')))
        _add_column('item_bill_of_materials_line', sa.Column('allow_substitute', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        _add_column('item_bill_of_materials_line', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))
        _create_index('ix_item_bill_of_materials_line_component_category', 'item_bill_of_materials_line', ['component_category'])

    if _has_table('production_request_line'):
        _add_column('production_request_line', sa.Column('assembly_warehouse_id', sa.Integer(), nullable=True))
        _create_index('ix_production_request_line_assembly_warehouse_id', 'production_request_line', ['assembly_warehouse_id'])

    if not _has_table('inventory_balance'):
        op.create_table(
            'inventory_balance',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=False),
            sa.Column('storage_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('quantity', sa.Numeric(14, 3), nullable=False, server_default='0'),
            sa.Column('reserved_quantity', sa.Numeric(14, 3), nullable=False, server_default='0'),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.UniqueConstraint('warehouse_id', 'storage_area_id', 'item_id', name='uq_inventory_balance_place_item'),
        )
    _create_index('ix_inventory_balance_warehouse_id', 'inventory_balance', ['warehouse_id'])
    _create_index('ix_inventory_balance_storage_area_id', 'inventory_balance', ['storage_area_id'])
    _create_index('ix_inventory_balance_item_id', 'inventory_balance', ['item_id'])

    if not _has_table('inventory_operation'):
        op.create_table(
            'inventory_operation',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operation_type', sa.String(length=40), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='posted'),
            sa.Column('source_warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=True),
            sa.Column('source_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('target_warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=True),
            sa.Column('target_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('production_request_line_id', sa.Integer(), sa.ForeignKey('production_request_line.id'), nullable=True),
            sa.Column('produced_unit_id', sa.Integer(), sa.ForeignKey('produced_unit.id'), nullable=True),
            sa.Column('stock_movement_id', sa.Integer(), sa.ForeignKey('stock_movement.id'), nullable=True),
            sa.Column('customer_order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=True),
            sa.Column('customer_order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('posted_at', sa.DateTime(), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['operation_type', 'status', 'source_warehouse_id', 'target_warehouse_id', 'production_request_line_id', 'produced_unit_id', 'stock_movement_id', 'customer_order_id', 'customer_order_line_id', 'workshop_id', 'created_by_user_id', 'created_at']:
        _create_index(f'ix_inventory_operation_{col}', 'inventory_operation', [col])

    if not _has_table('inventory_operation_line'):
        op.create_table(
            'inventory_operation_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operation_id', sa.Integer(), sa.ForeignKey('inventory_operation.id'), nullable=False),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('quantity', sa.Numeric(14, 3), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('direction', sa.String(length=10), nullable=False, server_default='out'),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    _create_index('ix_inventory_operation_line_operation_id', 'inventory_operation_line', ['operation_id'])
    _create_index('ix_inventory_operation_line_item_id', 'inventory_operation_line', ['item_id'])
    _create_index('ix_inventory_operation_line_direction', 'inventory_operation_line', ['direction'])

    if not _has_table('inventory_transaction'):
        op.create_table(
            'inventory_transaction',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('transaction_type', sa.String(length=40), nullable=False),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=False),
            sa.Column('storage_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('qty', sa.Numeric(14, 3), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('related_order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=True),
            sa.Column('related_order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('related_production_request_line_id', sa.Integer(), sa.ForeignKey('production_request_line.id'), nullable=True),
            sa.Column('related_workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=True),
            sa.Column('related_stock_movement_id', sa.Integer(), sa.ForeignKey('stock_movement.id'), nullable=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['created_at', 'transaction_type', 'warehouse_id', 'storage_area_id', 'item_id', 'related_order_id', 'related_order_line_id', 'related_production_request_line_id', 'related_workshop_id', 'related_stock_movement_id', 'user_id']:
        _create_index(f'ix_inventory_transaction_{col}', 'inventory_transaction', [col])

    if not _has_table('trailer_assembly_operation'):
        op.create_table(
            'trailer_assembly_operation',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('trailer_id', sa.Integer(), sa.ForeignKey('trailer.id'), nullable=False),
            sa.Column('order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=True),
            sa.Column('order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=False),
            sa.Column('operation_type', sa.String(length=30), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='draft'),
            sa.Column('from_item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('to_item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('posted_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('posted_at', sa.DateTime(), nullable=True),
            sa.Column('cancelled_at', sa.DateTime(), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['trailer_id', 'order_id', 'order_line_id', 'warehouse_id', 'operation_type', 'status', 'from_item_id', 'to_item_id', 'created_by_user_id', 'posted_by_user_id']:
        _create_index(f'ix_trailer_assembly_operation_{col}', 'trailer_assembly_operation', [col])

    if not _has_table('trailer_assembly_operation_line'):
        op.create_table(
            'trailer_assembly_operation_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operation_id', sa.Integer(), sa.ForeignKey('trailer_assembly_operation.id'), nullable=False),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('quantity', sa.Numeric(12, 3), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('direction', sa.String(length=20), nullable=False),
            sa.Column('storage_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('inventory_operation_line_id', sa.Integer(), sa.ForeignKey('inventory_operation_line.id'), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['operation_id', 'item_id', 'direction', 'storage_area_id', 'inventory_operation_line_id']:
        _create_index(f'ix_trailer_assembly_operation_line_{col}', 'trailer_assembly_operation_line', [col])

    if not _has_table('sales_realization'):
        op.create_table(
            'sales_realization',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('number', sa.String(length=50), nullable=True, unique=True),
            sa.Column('realization_date', sa.Date(), nullable=False),
            sa.Column('order_id', sa.Integer(), sa.ForeignKey('customer_order.id'), nullable=True),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customer.id'), nullable=False),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=True),
            sa.Column('assigned_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='draft'),
            sa.Column('total_amount', sa.Numeric(12, 2), nullable=False, server_default='0'),
            sa.Column('currency', sa.String(length=10), nullable=False, server_default='KZT'),
            sa.Column('posted_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('posted_at', sa.DateTime(), nullable=True),
            sa.Column('cancelled_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('cancelled_at', sa.DateTime(), nullable=True),
            sa.Column('cancel_reason', sa.Text(), nullable=True),
            sa.Column('one_c_export_status', sa.String(length=30), nullable=False, server_default='not_exported'),
            sa.Column('one_c_exported_at', sa.DateTime(), nullable=True),
            sa.Column('one_c_document_ref', sa.String(length=120), nullable=True),
            sa.Column('one_c_export_payload_json', sa.Text(), nullable=True),
            sa.Column('one_c_export_error', sa.Text(), nullable=True),
            sa.Column('esf_status', sa.String(length=30), nullable=False, server_default='not_started'),
            sa.Column('esf_ref', sa.String(length=120), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        )
    for col in ['number', 'realization_date', 'order_id', 'customer_id', 'warehouse_id', 'assigned_user_id', 'status', 'posted_by_user_id', 'cancelled_by_user_id', 'one_c_export_status', 'one_c_document_ref', 'esf_status', 'esf_ref', 'created_by_user_id']:
        _create_index(f'ix_sales_realization_{col}', 'sales_realization', [col])

    if not _has_table('sales_realization_line'):
        op.create_table(
            'sales_realization_line',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('realization_id', sa.Integer(), sa.ForeignKey('sales_realization.id'), nullable=False),
            sa.Column('line_no', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('line_type', sa.String(length=30), nullable=False),
            sa.Column('order_line_id', sa.Integer(), sa.ForeignKey('customer_order_line.id'), nullable=True),
            sa.Column('trailer_id', sa.Integer(), sa.ForeignKey('trailer.id'), nullable=True),
            sa.Column('vin_registry_id', sa.Integer(), sa.ForeignKey('vin_registry.id'), nullable=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouse.id'), nullable=True),
            sa.Column('storage_area_id', sa.Integer(), sa.ForeignKey('warehouse_storage_area.id'), nullable=True),
            sa.Column('quantity', sa.Numeric(12, 3), nullable=False, server_default='1'),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('unit_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('total_price', sa.Numeric(12, 2), nullable=True),
            sa.Column('inventory_effect', sa.String(length=30), nullable=False, server_default='none'),
            sa.Column('article_snapshot', sa.String(length=80), nullable=True),
            sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
            sa.Column('vin_full', sa.String(length=50), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['realization_id', 'line_type', 'order_line_id', 'trailer_id', 'vin_registry_id', 'item_id', 'warehouse_id', 'storage_area_id', 'inventory_effect', 'article_snapshot', 'vin_full']:
        _create_index(f'ix_sales_realization_line_{col}', 'sales_realization_line', [col])

    if not _has_table('item_production_route'):
        op.create_table(
            'item_production_route',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=False),
            sa.Column('version', sa.String(length=40), nullable=False, server_default='default'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.UniqueConstraint('item_id', 'version', name='uq_item_production_route_version'),
        )
    _create_index('ix_item_production_route_item_id', 'item_production_route', ['item_id'])
    _create_index('ix_item_production_route_is_active', 'item_production_route', ['is_active'])

    if not _has_table('item_production_route_step'):
        op.create_table(
            'item_production_route_step',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('route_id', sa.Integer(), sa.ForeignKey('item_production_route.id'), nullable=False),
            sa.Column('step_no', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('sequence_no', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('workshop_id', sa.Integer(), sa.ForeignKey('production_workshop.id'), nullable=False),
            sa.Column('operation_name', sa.String(length=120), nullable=True),
            sa.Column('input_item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('output_item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
            sa.Column('input_area_type', sa.String(length=40), nullable=True),
            sa.Column('output_area_type', sa.String(length=40), nullable=True),
            sa.Column('planned_qty', sa.Numeric(12, 3), nullable=True),
            sa.Column('planned_duration_minutes', sa.Integer(), nullable=True),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('is_required', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('comment', sa.Text(), nullable=True),
        )
    for col in ['route_id', 'step_no', 'sequence_no', 'workshop_id', 'input_item_id', 'output_item_id', 'input_area_type', 'output_area_type']:
        _create_index(f'ix_item_production_route_step_{col}', 'item_production_route_step', [col])

    _seed_storage_areas()


def downgrade():
    for table_name in [
        'sales_realization_line',
        'sales_realization',
        'trailer_assembly_operation_line',
        'trailer_assembly_operation',
        'item_production_route_step',
        'item_production_route',
        'inventory_transaction',
        'inventory_operation_line',
        'inventory_operation',
        'inventory_balance',
    ]:
        if _has_table(table_name):
            op.drop_table(table_name)
