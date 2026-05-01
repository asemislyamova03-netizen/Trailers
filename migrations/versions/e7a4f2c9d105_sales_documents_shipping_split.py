"""sales documents shipping split

Revision ID: e7a4f2c9d105
Revises: d4f6b8a2c913
Create Date: 2026-05-01 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e7a4f2c9d105'
down_revision = 'd4f6b8a2c913'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('planned_ship_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('planned_ship_comment', sa.Text(), nullable=True))

    op.create_index(
        'uq_sales_contract_order',
        'sales_contract',
        ['order_id'],
        unique=True,
    )


def downgrade():
    op.drop_index('uq_sales_contract_order', table_name='sales_contract')

    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.drop_column('planned_ship_comment')
        batch_op.drop_column('planned_ship_date')
