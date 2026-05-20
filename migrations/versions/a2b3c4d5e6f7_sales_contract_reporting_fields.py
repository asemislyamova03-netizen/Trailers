"""sales contract reporting fields

Revision ID: a2b3c4d5e6f7
Revises: f9a1b2c3d4e5
Create Date: 2026-05-20 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a2b3c4d5e6f7'
down_revision = 'f9a1b2c3d4e5'
branch_labels = None
depends_on = None


def _columns(table_name):
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name):
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade():
    columns = _columns('sales_contract')
    with op.batch_alter_table('sales_contract') as batch:
        if 'warehouse_id' not in columns:
            batch.add_column(sa.Column('warehouse_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(
                'fk_sales_contract_warehouse_id_warehouse',
                'warehouse',
                ['warehouse_id'],
                ['id'],
            )
        if 'assigned_user_id' not in columns:
            batch.add_column(sa.Column('assigned_user_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(
                'fk_sales_contract_assigned_user_id_user',
                'user',
                ['assigned_user_id'],
                ['id'],
            )

    indexes = _indexes('sales_contract')
    if 'ix_sales_contract_warehouse_id' not in indexes:
        op.create_index('ix_sales_contract_warehouse_id', 'sales_contract', ['warehouse_id'], unique=False)
    if 'ix_sales_contract_assigned_user_id' not in indexes:
        op.create_index('ix_sales_contract_assigned_user_id', 'sales_contract', ['assigned_user_id'], unique=False)


def downgrade():
    indexes = _indexes('sales_contract')
    if 'ix_sales_contract_assigned_user_id' in indexes:
        op.drop_index('ix_sales_contract_assigned_user_id', table_name='sales_contract')
    if 'ix_sales_contract_warehouse_id' in indexes:
        op.drop_index('ix_sales_contract_warehouse_id', table_name='sales_contract')

    columns = _columns('sales_contract')
    with op.batch_alter_table('sales_contract') as batch:
        if 'assigned_user_id' in columns:
            batch.drop_column('assigned_user_id')
        if 'warehouse_id' in columns:
            batch.drop_column('warehouse_id')
