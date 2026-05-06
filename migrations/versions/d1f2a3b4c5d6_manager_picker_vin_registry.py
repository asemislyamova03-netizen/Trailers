"""manager picker snapshots and vin registry

Revision ID: d1f2a3b4c5d6
Revises: c6e4b2a9d813
Create Date: 2026-05-04
"""

from alembic import op
import sqlalchemy as sa


revision = 'd1f2a3b4c5d6'
down_revision = 'c6e4b2a9d813'
branch_labels = None
depends_on = None


snapshot_columns = [
    sa.Column('article_snapshot', sa.String(length=80), nullable=True),
    sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
    sa.Column('config_snapshot_json', sa.Text(), nullable=True),
    sa.Column('calculated_price', sa.Numeric(12, 2), nullable=True),
    sa.Column('price_breakdown_json', sa.Text(), nullable=True),
    sa.Column('overall_dimensions_text', sa.String(length=255), nullable=True),
    sa.Column('inner_dimensions_text', sa.String(length=255), nullable=True),
    sa.Column('otts_number', sa.String(length=120), nullable=True),
    sa.Column('otts_type', sa.String(length=20), nullable=True),
    sa.Column('otts_modification', sa.String(length=50), nullable=True),
    sa.Column('vin_modification_code', sa.String(length=20), nullable=True),
]


def _add_columns(table_name, columns):
    with op.batch_alter_table(table_name) as batch_op:
        for column in columns:
            batch_op.add_column(column.copy())


def _drop_columns(table_name, names):
    with op.batch_alter_table(table_name) as batch_op:
        for name in names:
            batch_op.drop_column(name)


def upgrade():
    _add_columns('item', [
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('body_size_id', sa.Integer(), nullable=True),
        sa.Column('body_execution_id', sa.Integer(), nullable=True),
        sa.Column('board_height_id', sa.Integer(), nullable=True),
        sa.Column('wheel_option_id', sa.Integer(), nullable=True),
        sa.Column('hub_option_id', sa.Integer(), nullable=True),
        sa.Column('support_wheel_option_id', sa.Integer(), nullable=True),
        sa.Column('tent_option_id', sa.Integer(), nullable=True),
        sa.Column('special_options_json', sa.Text(), nullable=True),
        sa.Column('vin_modification_code', sa.String(length=20), nullable=True),
    ])
    _add_columns('customer_order', [sa.Column('source_warehouse_id', sa.Integer(), nullable=True)] + snapshot_columns)
    _add_columns('supply_need', snapshot_columns + [
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
    ])
    _add_columns('production_request_line', snapshot_columns + [
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
    ])
    op.create_table(
        'vin_registry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vin_full', sa.String(length=50), nullable=False),
        sa.Column('prefix', sa.String(length=10), nullable=False),
        sa.Column('vin_modification_code', sa.String(length=20), nullable=False),
        sa.Column('year_code', sa.String(length=1), nullable=False),
        sa.Column('serial7', sa.String(length=7), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('customer_order_id', sa.Integer(), nullable=True),
        sa.Column('supply_need_id', sa.Integer(), nullable=True),
        sa.Column('production_request_line_id', sa.Integer(), nullable=True),
        sa.Column('produced_unit_id', sa.Integer(), nullable=True),
        sa.Column('trailer_id', sa.Integer(), nullable=True),
        sa.Column('reserved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('assigned_by_user_id', sa.Integer(), nullable=True),
        sa.Column('confirmed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('void_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reserved_at', sa.DateTime(), nullable=True),
        sa.Column('assigned_at', sa.DateTime(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('docs_issued_at', sa.DateTime(), nullable=True),
        sa.Column('void_at', sa.DateTime(), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vin_full'),
        sa.UniqueConstraint('serial7'),
    )
    for table_name, column_name in (
        ('item', 'group_id'), ('item', 'body_size_id'), ('item', 'body_execution_id'), ('item', 'board_height_id'),
        ('item', 'wheel_option_id'), ('item', 'hub_option_id'), ('item', 'support_wheel_option_id'), ('item', 'tent_option_id'),
        ('item', 'vin_modification_code'), ('customer_order', 'source_warehouse_id'), ('customer_order', 'vin_modification_code'),
        ('supply_need', 'vin_modification_code'), ('production_request_line', 'vin_modification_code'),
        ('vin_registry', 'vin_full'), ('vin_registry', 'serial7'), ('vin_registry', 'status'),
        ('vin_registry', 'vin_modification_code'), ('vin_registry', 'year_code'),
    ):
        op.create_index(f'ix_{table_name}_{column_name}', table_name, [column_name])


def downgrade():
    op.drop_table('vin_registry')
    _drop_columns('production_request_line', [column.name for column in snapshot_columns] + ['cancelled_at', 'cancelled_by_user_id', 'cancel_reason'])
    _drop_columns('supply_need', [column.name for column in snapshot_columns] + ['cancelled_at', 'cancelled_by_user_id', 'cancel_reason'])
    _drop_columns('customer_order', ['source_warehouse_id'] + [column.name for column in snapshot_columns])
    _drop_columns('item', [
        'group_id', 'body_size_id', 'body_execution_id', 'board_height_id', 'wheel_option_id',
        'hub_option_id', 'support_wheel_option_id', 'tent_option_id', 'special_options_json',
        'vin_modification_code',
    ])
