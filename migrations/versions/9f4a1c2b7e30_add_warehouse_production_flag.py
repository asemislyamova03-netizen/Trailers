"""add warehouse production flag

Revision ID: 9f4a1c2b7e30
Revises: 8b1f4a9d2c70
Create Date: 2026-04-30 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '9f4a1c2b7e30'
down_revision = '8b1f4a9d2c70'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('warehouse', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_production', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.create_index(batch_op.f('ix_warehouse_is_production'), ['is_production'], unique=False)

    with op.batch_alter_table('warehouse', schema=None) as batch_op:
        batch_op.alter_column('is_production', server_default=None)


def downgrade():
    with op.batch_alter_table('warehouse', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warehouse_is_production'))
        batch_op.drop_column('is_production')
