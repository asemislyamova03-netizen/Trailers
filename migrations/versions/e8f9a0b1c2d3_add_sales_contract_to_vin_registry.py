"""add sales contract link to vin registry

Revision ID: e8f9a0b1c2d3
Revises: e7d8c9b0a1f2
Create Date: 2026-05-05 18:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e8f9a0b1c2d3'
down_revision = 'e7d8c9b0a1f2'
branch_labels = None
depends_on = None


def _columns(table_name):
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name):
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade():
    vin_columns = _columns('vin_registry')
    if 'sales_contract_id' not in vin_columns:
        with op.batch_alter_table('vin_registry') as batch:
            batch.add_column(sa.Column('sales_contract_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(
                'fk_vin_registry_sales_contract_id_sales_contract',
                'sales_contract',
                ['sales_contract_id'],
                ['id'],
            )
    vin_indexes = _indexes('vin_registry')
    if 'ix_vin_registry_sales_contract_id' not in vin_indexes:
        op.create_index('ix_vin_registry_sales_contract_id', 'vin_registry', ['sales_contract_id'], unique=False)

    event_columns = _columns('vin_registry_event')
    if 'sales_contract_id' not in event_columns:
        with op.batch_alter_table('vin_registry_event') as batch:
            batch.add_column(sa.Column('sales_contract_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(
                'fk_vin_registry_event_sales_contract_id_sales_contract',
                'sales_contract',
                ['sales_contract_id'],
                ['id'],
            )
    event_indexes = _indexes('vin_registry_event')
    if 'ix_vin_registry_event_sales_contract_id' not in event_indexes:
        op.create_index('ix_vin_registry_event_sales_contract_id', 'vin_registry_event', ['sales_contract_id'], unique=False)


def downgrade():
    event_indexes = _indexes('vin_registry_event')
    if 'ix_vin_registry_event_sales_contract_id' in event_indexes:
        op.drop_index('ix_vin_registry_event_sales_contract_id', table_name='vin_registry_event')
    if 'sales_contract_id' in _columns('vin_registry_event'):
        with op.batch_alter_table('vin_registry_event') as batch:
            batch.drop_column('sales_contract_id')

    vin_indexes = _indexes('vin_registry')
    if 'ix_vin_registry_sales_contract_id' in vin_indexes:
        op.drop_index('ix_vin_registry_sales_contract_id', table_name='vin_registry')
    if 'sales_contract_id' in _columns('vin_registry'):
        with op.batch_alter_table('vin_registry') as batch:
            batch.drop_column('sales_contract_id')
