"""add crm orders production movements

Revision ID: 6f2b7d9c1a04
Revises: 05dbd814346a
Create Date: 2026-04-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '6f2b7d9c1a04'
down_revision = '05dbd814346a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trailer') as batch_op:
        batch_op.add_column(sa.Column('lifecycle_status', sa.String(length=30), nullable=True))
        batch_op.create_index(batch_op.f('ix_trailer_lifecycle_status'), ['lifecycle_status'], unique=False)

    op.create_table(
        'lead',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('priority', sa.String(length=20), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('source_channel', sa.String(length=30), nullable=False),
        sa.Column('source_name', sa.String(length=100), nullable=True),
        sa.Column('source_platform', sa.String(length=50), nullable=True),
        sa.Column('source_account', sa.String(length=100), nullable=True),
        sa.Column('external_chat_id', sa.String(length=120), nullable=True),
        sa.Column('external_lead_id', sa.String(length=120), nullable=True),
        sa.Column('source_payload', sa.Text(), nullable=True),
        sa.Column('customer_name', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('messenger_username', sa.String(length=100), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('desired_item_id', sa.Integer(), nullable=True),
        sa.Column('desired_model', sa.String(length=255), nullable=True),
        sa.Column('desired_specs', sa.Text(), nullable=True),
        sa.Column('warehouse_id', sa.Integer(), nullable=True),
        sa.Column('assigned_user_id', sa.Integer(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['assigned_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['desired_item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['warehouse_id'], ['warehouse.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('assigned_user_id', 'channel', 'created_at', 'customer_id', 'desired_item_id', 'external_chat_id', 'external_lead_id', 'phone', 'source_channel', 'status', 'warehouse_id'):
        op.create_index(op.f(f'ix_lead_{col}'), 'lead', [col], unique=False)

    op.create_table(
        'customer_order',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_number', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('fulfillment_source', sa.String(length=30), nullable=True),
        sa.Column('lead_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('trailer_id', sa.Integer(), nullable=True),
        sa.Column('warehouse_id', sa.Integer(), nullable=True),
        sa.Column('assigned_user_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price', sa.Numeric(12, 2), nullable=True),
        sa.Column('prepayment_percent', sa.Numeric(5, 2), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('expected_date', sa.Date(), nullable=True),
        sa.Column('documents_issued', sa.Boolean(), nullable=False),
        sa.Column('is_shipped', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['assigned_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['lead_id'], ['lead.id']),
        sa.ForeignKeyConstraint(['trailer_id'], ['trailer.id']),
        sa.ForeignKeyConstraint(['warehouse_id'], ['warehouse.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_number'),
    )
    for col in ('assigned_user_id', 'created_at', 'customer_id', 'item_id', 'lead_id', 'order_number', 'status', 'trailer_id', 'warehouse_id'):
        op.create_index(op.f(f'ix_customer_order_{col}'), 'customer_order', [col], unique=False)

    with op.batch_alter_table('sales_contract') as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_sales_contract_order_id_customer_order', 'customer_order', ['order_id'], ['id'])
        batch_op.create_index(batch_op.f('ix_sales_contract_order_id'), ['order_id'], unique=False)

    op.create_table(
        'lead_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lead_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('external_message_id', sa.String(length=120), nullable=True),
        sa.Column('sender_name', sa.String(length=255), nullable=True),
        sa.Column('sender_contact', sa.String(length=255), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['lead_id'], ['lead.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('created_at', 'external_message_id', 'lead_id'):
        op.create_index(op.f(f'ix_lead_message_{col}'), 'lead_message', [col], unique=False)

    op.create_table(
        'order_payment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('stage', sa.String(length=20), nullable=False),
        sa.Column('method', sa.String(length=30), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('transaction_ref', sa.String(length=120), nullable=True),
        sa.Column('payment_link', sa.String(length=255), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['order_id'], ['customer_order.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('created_at', 'order_id', 'status'):
        op.create_index(op.f(f'ix_order_payment_{col}'), 'order_payment', [col], unique=False)

    op.create_table(
        'reservation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('source_type', sa.String(length=30), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('trailer_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['order_id'], ['customer_order.id']),
        sa.ForeignKeyConstraint(['trailer_id'], ['trailer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('created_at', 'item_id', 'order_id', 'status', 'trailer_id'):
        op.create_index(op.f(f'ix_reservation_{col}'), 'reservation', [col], unique=False)

    op.create_table(
        'supply_need',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('need_type', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('warehouse_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('required_by', sa.Date(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['order_id'], ['customer_order.id']),
        sa.ForeignKeyConstraint(['warehouse_id'], ['warehouse.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('created_at', 'item_id', 'order_id', 'status', 'warehouse_id'):
        op.create_index(op.f(f'ix_supply_need_{col}'), 'supply_need', [col], unique=False)

    op.create_table(
        'production_request',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_number', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('target_warehouse_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['target_warehouse_id'], ['warehouse.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_number'),
    )
    for col in ('created_at', 'request_number', 'status', 'target_warehouse_id'):
        op.create_index(op.f(f'ix_production_request_{col}'), 'production_request', [col], unique=False)

    op.create_table(
        'production_request_line',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('production_request_id', sa.Integer(), nullable=False),
        sa.Column('supply_need_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['production_request_id'], ['production_request.id']),
        sa.ForeignKeyConstraint(['supply_need_id'], ['supply_need.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('item_id', 'production_request_id', 'status', 'supply_need_id'):
        op.create_index(op.f(f'ix_production_request_line_{col}'), 'production_request_line', [col], unique=False)

    op.create_table(
        'stock_movement',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('departure_date', sa.Date(), nullable=True),
        sa.Column('arrival_date', sa.Date(), nullable=True),
        sa.Column('moved_at', sa.DateTime(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.Column('movement_type', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('trailer_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('from_warehouse_id', sa.Integer(), nullable=True),
        sa.Column('to_warehouse_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['from_warehouse_id'], ['warehouse.id']),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['order_id'], ['customer_order.id']),
        sa.ForeignKeyConstraint(['to_warehouse_id'], ['warehouse.id']),
        sa.ForeignKeyConstraint(['trailer_id'], ['trailer.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for col in ('created_at', 'from_warehouse_id', 'item_id', 'order_id', 'status', 'to_warehouse_id', 'trailer_id'):
        op.create_index(op.f(f'ix_stock_movement_{col}'), 'stock_movement', [col], unique=False)


def downgrade():
    op.drop_table('stock_movement')
    op.drop_table('production_request_line')
    op.drop_table('production_request')
    op.drop_table('supply_need')
    op.drop_table('reservation')
    op.drop_table('order_payment')
    op.drop_table('lead_message')
    with op.batch_alter_table('sales_contract') as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_contract_order_id'))
        batch_op.drop_constraint('fk_sales_contract_order_id_customer_order', type_='foreignkey')
        batch_op.drop_column('order_id')
    op.drop_table('customer_order')
    op.drop_table('lead')
    with op.batch_alter_table('trailer') as batch_op:
        batch_op.drop_index(batch_op.f('ix_trailer_lifecycle_status'))
        batch_op.drop_column('lifecycle_status')
