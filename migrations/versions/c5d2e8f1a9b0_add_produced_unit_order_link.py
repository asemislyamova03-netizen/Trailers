"""add produced unit order link

Revision ID: c5d2e8f1a9b0
Revises: a4d7e9b2c601
Create Date: 2026-05-01 20:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d2e8f1a9b0'
down_revision = 'a4d7e9b2c601'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('produced_unit', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_produced_unit_order_id'), ['order_id'], unique=False)
        batch_op.create_foreign_key('fk_produced_unit_order_id_customer_order', 'customer_order', ['order_id'], ['id'])


def downgrade():
    with op.batch_alter_table('produced_unit', schema=None) as batch_op:
        batch_op.drop_constraint('fk_produced_unit_order_id_customer_order', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_produced_unit_order_id'))
        batch_op.drop_column('order_id')
