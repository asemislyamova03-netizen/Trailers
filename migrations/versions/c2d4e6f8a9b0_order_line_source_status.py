"""order line source and fulfillment status

Revision ID: c2d4e6f8a9b0
Revises: b1c2d3e4f5a6
Create Date: 2026-05-29 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'c2d4e6f8a9b0'
down_revision = 'b1c2d3e4f5a6'
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


def _create_index(name, table_name, columns):
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns)


def upgrade():
    if not _has_table('customer_order_line'):
        return

    _add_column('customer_order_line', sa.Column('source_type', sa.String(length=30), nullable=False, server_default='none'))
    _add_column('customer_order_line', sa.Column('fulfillment_status', sa.String(length=40), nullable=False, server_default='draft'))
    _create_index('ix_customer_order_line_source_type', 'customer_order_line', ['source_type'])
    _create_index('ix_customer_order_line_fulfillment_status', 'customer_order_line', ['fulfillment_status'])

    op.execute(sa.text("""
        UPDATE customer_order_line
        SET source_type = CASE
            WHEN lower(coalesce(fulfillment_source, '')) = 'production' THEN 'production'
            WHEN lower(coalesce(fulfillment_source, '')) IN ('stock', 'other_warehouse', 'transit') THEN 'stock'
            WHEN lower(coalesce(fulfillment_source, '')) = 'assembly' THEN 'assembly'
            WHEN lower(coalesce(fulfillment_source, '')) = 'external' THEN 'external'
            WHEN upper(coalesce(line_type, '')) = 'COMPONENT' THEN 'component'
            ELSE 'none'
        END
        WHERE source_type = 'none' OR source_type IS NULL
    """))
    op.execute(sa.text("""
        UPDATE customer_order_line
        SET fulfillment_status = CASE
            WHEN lower(coalesce(status, '')) IN ('shipped', 'done') THEN 'shipped'
            WHEN lower(coalesce(status, '')) IN ('cancelled', 'canceled') THEN 'cancelled'
            WHEN lower(coalesce(status, '')) IN ('realized') THEN 'realized'
            WHEN lower(coalesce(status, '')) IN ('reserved', 'stock_reserved') THEN 'stock_reserved'
            WHEN lower(coalesce(status, '')) IN ('waiting_transfer', 'in_transit', 'arrived') THEN 'movement_required'
            WHEN lower(coalesce(status, '')) IN ('sold_not_shipped', 'ready_to_ship') THEN 'ready_for_documents'
            WHEN lower(coalesce(status, '')) IN ('waiting_production', 'new') AND source_type = 'production' THEN 'production_requested'
            WHEN lower(coalesce(status, '')) IN ('in_production') THEN 'production_started'
            WHEN lower(coalesce(status, '')) IN ('produced_waiting_vin', 'produced_no_vin') THEN 'produced_waiting_vin'
            WHEN lower(coalesce(status, '')) IN ('vin_assigned') THEN 'vin_assigned'
            WHEN lower(coalesce(status, '')) IN ('vin_confirmed', 'confirmed') THEN 'vin_confirmed'
            ELSE 'draft'
        END
        WHERE fulfillment_status = 'draft' OR fulfillment_status IS NULL
    """))


def downgrade():
    if not _has_table('customer_order_line'):
        return
    with op.batch_alter_table('customer_order_line') as batch:
        if 'ix_customer_order_line_fulfillment_status' in _indexes('customer_order_line'):
            batch.drop_index('ix_customer_order_line_fulfillment_status')
        if 'ix_customer_order_line_source_type' in _indexes('customer_order_line'):
            batch.drop_index('ix_customer_order_line_source_type')
        cols = _columns('customer_order_line')
        if 'fulfillment_status' in cols:
            batch.drop_column('fulfillment_status')
        if 'source_type' in cols:
            batch.drop_column('source_type')
