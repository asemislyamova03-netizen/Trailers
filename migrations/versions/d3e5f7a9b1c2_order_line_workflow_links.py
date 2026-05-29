"""order line workflow links

Revision ID: d3e5f7a9b1c2
Revises: c2d4e6f8a9b0
Create Date: 2026-05-29 16:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'd3e5f7a9b1c2'
down_revision = 'c2d4e6f8a9b0'
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

    for column in [
        sa.Column('vin_registry_id', sa.Integer(), nullable=True),
        sa.Column('reservation_id', sa.Integer(), nullable=True),
        sa.Column('supply_need_id', sa.Integer(), nullable=True),
        sa.Column('production_request_line_id', sa.Integer(), nullable=True),
        sa.Column('stock_movement_id', sa.Integer(), nullable=True),
        sa.Column('realization_status', sa.String(length=30), nullable=False, server_default='not_started'),
        sa.Column('shipment_status', sa.String(length=30), nullable=False, server_default='not_started'),
    ]:
        _add_column('customer_order_line', column)

    for name, cols in [
        ('ix_customer_order_line_vin_registry_id', ['vin_registry_id']),
        ('ix_customer_order_line_reservation_id', ['reservation_id']),
        ('ix_customer_order_line_supply_need_id', ['supply_need_id']),
        ('ix_customer_order_line_production_request_line_id', ['production_request_line_id']),
        ('ix_customer_order_line_stock_movement_id', ['stock_movement_id']),
        ('ix_customer_order_line_realization_status', ['realization_status']),
        ('ix_customer_order_line_shipment_status', ['shipment_status']),
    ]:
        _create_index(name, 'customer_order_line', cols)

    bind = op.get_bind()
    if _has_table('reservation'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET reservation_id = (
                SELECT r.id FROM reservation r
                WHERE r.order_line_id = customer_order_line.id
                  AND r.status = 'ACTIVE'
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT 1
            )
            WHERE reservation_id IS NULL
        """))
    if _has_table('supply_need'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET supply_need_id = (
                SELECT n.id FROM supply_need n
                WHERE n.order_line_id = customer_order_line.id
                  AND lower(coalesce(n.status, '')) NOT IN ('cancelled', 'canceled', 'done', 'closed', 'void')
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT 1
            )
            WHERE supply_need_id IS NULL
        """))
    if _has_table('production_request_line'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET production_request_line_id = (
                SELECT l.id FROM production_request_line l
                WHERE l.order_line_id = customer_order_line.id
                  AND lower(coalesce(l.status, '')) NOT IN ('ready', 'closed', 'cancelled', 'canceled')
                ORDER BY l.id DESC
                LIMIT 1
            )
            WHERE production_request_line_id IS NULL
        """))
    if _has_table('stock_movement'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET stock_movement_id = (
                SELECT m.id FROM stock_movement m
                WHERE m.order_line_id = customer_order_line.id
                  AND lower(coalesce(m.status, '')) IN ('draft', 'sent', 'in_transit')
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
            )
            WHERE stock_movement_id IS NULL
        """))
    if _has_table('vin_registry'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET vin_registry_id = (
                SELECT v.id FROM vin_registry v
                WHERE v.order_line_id = customer_order_line.id
                  AND v.status IN ('reserved', 'assigned', 'confirmed')
                ORDER BY v.confirmed_at DESC, v.assigned_at DESC, v.reserved_at DESC, v.id DESC
                LIMIT 1
            )
            WHERE vin_registry_id IS NULL
        """))
    if _has_table('sales_realization_line') and _has_table('sales_realization'):
        bind.execute(sa.text("""
            UPDATE customer_order_line
            SET realization_status = CASE
                WHEN EXISTS (
                    SELECT 1 FROM sales_realization_line rl
                    JOIN sales_realization r ON r.id = rl.realization_id
                    WHERE rl.order_line_id = customer_order_line.id
                      AND r.status = 'posted'
                ) THEN 'realized'
                WHEN EXISTS (
                    SELECT 1 FROM sales_realization_line rl
                    WHERE rl.order_line_id = customer_order_line.id
                ) THEN 'draft'
                ELSE 'not_started'
            END
        """))
    bind.execute(sa.text("""
        UPDATE customer_order_line
        SET shipment_status = CASE
            WHEN lower(coalesce(status, '')) = 'shipped' THEN 'shipped'
            WHEN stock_movement_id IS NOT NULL THEN 'in_transit'
            ELSE 'not_started'
        END
    """))


def downgrade():
    if not _has_table('customer_order_line'):
        return
    with op.batch_alter_table('customer_order_line') as batch:
        indexes = _indexes('customer_order_line')
        for name in [
            'ix_customer_order_line_shipment_status',
            'ix_customer_order_line_realization_status',
            'ix_customer_order_line_stock_movement_id',
            'ix_customer_order_line_production_request_line_id',
            'ix_customer_order_line_supply_need_id',
            'ix_customer_order_line_reservation_id',
            'ix_customer_order_line_vin_registry_id',
        ]:
            if name in indexes:
                batch.drop_index(name)
        cols = _columns('customer_order_line')
        for name in [
            'shipment_status',
            'realization_status',
            'stock_movement_id',
            'production_request_line_id',
            'supply_need_id',
            'reservation_id',
            'vin_registry_id',
        ]:
            if name in cols:
                batch.drop_column(name)
