"""Add purchasing entities and receipt plan links.

Revision ID: f1a2b3c4d5e6
Revises: e8f0a1b2c3d4
Create Date: 2026-06-02 13:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'e8f0a1b2c3d4'
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return name in sa.inspect(bind).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return False
    columns = {c['name'] for c in sa.inspect(bind).get_columns(table)}
    return column in columns


def upgrade():
    if not _table_exists('supplier'):
        op.create_table(
            'supplier',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=160), nullable=False),
            sa.Column('contact_name', sa.String(length=120), nullable=True),
            sa.Column('phone', sa.String(length=64), nullable=True),
            sa.Column('email', sa.String(length=120), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('name', name='uq_supplier_name'),
        )
        op.create_index('ix_supplier_name', 'supplier', ['name'], unique=False)
        op.create_index('ix_supplier_is_active', 'supplier', ['is_active'], unique=False)

    if not _table_exists('inventory_receipt_plan'):
        op.create_table(
            'inventory_receipt_plan',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('plan_number', sa.String(length=40), nullable=False),
            sa.Column('supplier_id', sa.Integer(), nullable=True),
            sa.Column('warehouse_id', sa.Integer(), nullable=True),
            sa.Column('storage_area_id', sa.Integer(), nullable=True),
            sa.Column('planned_date', sa.Date(), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='planned'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id']),
            sa.ForeignKeyConstraint(['storage_area_id'], ['warehouse_storage_area.id']),
            sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id']),
            sa.ForeignKeyConstraint(['warehouse_id'], ['warehouse.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('plan_number', name='uq_inventory_receipt_plan_number'),
        )
        op.create_index('ix_inventory_receipt_plan_plan_number', 'inventory_receipt_plan', ['plan_number'], unique=False)
        op.create_index('ix_inventory_receipt_plan_status', 'inventory_receipt_plan', ['status'], unique=False)
        op.create_index('ix_inventory_receipt_plan_planned_date', 'inventory_receipt_plan', ['planned_date'], unique=False)

    if not _table_exists('inventory_receipt_plan_line'):
        op.create_table(
            'inventory_receipt_plan_line',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('plan_id', sa.Integer(), nullable=False),
            sa.Column('item_id', sa.Integer(), nullable=False),
            sa.Column('quantity', sa.Numeric(precision=14, scale=3), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.ForeignKeyConstraint(['item_id'], ['item.id']),
            sa.ForeignKeyConstraint(['plan_id'], ['inventory_receipt_plan.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_inventory_receipt_plan_line_plan_id', 'inventory_receipt_plan_line', ['plan_id'], unique=False)
        op.create_index('ix_inventory_receipt_plan_line_item_id', 'inventory_receipt_plan_line', ['item_id'], unique=False)

    if not _column_exists('item', 'component_category'):
        op.add_column('item', sa.Column('component_category', sa.String(length=40), nullable=True))
        op.create_index('ix_item_component_category', 'item', ['component_category'], unique=False)
    if not _column_exists('item', 'is_controlled'):
        op.add_column('item', sa.Column('is_controlled', sa.Boolean(), nullable=False, server_default=sa.false()))
        op.create_index('ix_item_is_controlled', 'item', ['is_controlled'], unique=False)
    if not _column_exists('item', 'production_stage_default'):
        op.add_column('item', sa.Column('production_stage_default', sa.String(length=40), nullable=True))
        op.create_index('ix_item_production_stage_default', 'item', ['production_stage_default'], unique=False)

    need_supplier = not _column_exists('inventory_operation', 'supplier_id')
    need_plan = not _column_exists('inventory_operation', 'receipt_plan_id')
    if need_supplier or need_plan:
        with op.batch_alter_table('inventory_operation', schema=None) as batch_op:
            if need_supplier:
                batch_op.add_column(sa.Column('supplier_id', sa.Integer(), nullable=True))
                batch_op.create_foreign_key('fk_inventory_operation_supplier_id', 'supplier', ['supplier_id'], ['id'])
                batch_op.create_index('ix_inventory_operation_supplier_id', ['supplier_id'], unique=False)
            if need_plan:
                batch_op.add_column(sa.Column('receipt_plan_id', sa.Integer(), nullable=True))
                batch_op.create_foreign_key('fk_inventory_operation_receipt_plan_id', 'inventory_receipt_plan', ['receipt_plan_id'], ['id'])
                batch_op.create_index('ix_inventory_operation_receipt_plan_id', ['receipt_plan_id'], unique=False)


def downgrade():
    drop_plan = _column_exists('inventory_operation', 'receipt_plan_id')
    drop_supplier = _column_exists('inventory_operation', 'supplier_id')
    if drop_plan or drop_supplier:
        with op.batch_alter_table('inventory_operation', schema=None) as batch_op:
            if drop_plan:
                batch_op.drop_index('ix_inventory_operation_receipt_plan_id')
                batch_op.drop_constraint('fk_inventory_operation_receipt_plan_id', type_='foreignkey')
                batch_op.drop_column('receipt_plan_id')
            if drop_supplier:
                batch_op.drop_index('ix_inventory_operation_supplier_id')
                batch_op.drop_constraint('fk_inventory_operation_supplier_id', type_='foreignkey')
                batch_op.drop_column('supplier_id')

    if _column_exists('item', 'production_stage_default'):
        op.drop_index('ix_item_production_stage_default', table_name='item')
        op.drop_column('item', 'production_stage_default')
    if _column_exists('item', 'is_controlled'):
        op.drop_index('ix_item_is_controlled', table_name='item')
        op.drop_column('item', 'is_controlled')
    if _column_exists('item', 'component_category'):
        op.drop_index('ix_item_component_category', table_name='item')
        op.drop_column('item', 'component_category')

    if _table_exists('inventory_receipt_plan_line'):
        op.drop_index('ix_inventory_receipt_plan_line_item_id', table_name='inventory_receipt_plan_line')
        op.drop_index('ix_inventory_receipt_plan_line_plan_id', table_name='inventory_receipt_plan_line')
        op.drop_table('inventory_receipt_plan_line')
    if _table_exists('inventory_receipt_plan'):
        op.drop_index('ix_inventory_receipt_plan_planned_date', table_name='inventory_receipt_plan')
        op.drop_index('ix_inventory_receipt_plan_status', table_name='inventory_receipt_plan')
        op.drop_index('ix_inventory_receipt_plan_plan_number', table_name='inventory_receipt_plan')
        op.drop_table('inventory_receipt_plan')
    if _table_exists('supplier'):
        op.drop_index('ix_supplier_is_active', table_name='supplier')
        op.drop_index('ix_supplier_name', table_name='supplier')
        op.drop_table('supplier')
