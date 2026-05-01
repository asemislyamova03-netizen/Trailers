"""add stock movement batch key

Revision ID: f2c9a8b1d704
Revises: e7a4f2c9d105
Create Date: 2026-05-01 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'f2c9a8b1d704'
down_revision = 'e7a4f2c9d105'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('stock_movement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('batch_key', sa.String(length=50), nullable=True))
        batch_op.create_index('ix_stock_movement_batch_key', ['batch_key'], unique=False)


def downgrade():
    with op.batch_alter_table('stock_movement', schema=None) as batch_op:
        batch_op.drop_index('ix_stock_movement_batch_key')
        batch_op.drop_column('batch_key')
