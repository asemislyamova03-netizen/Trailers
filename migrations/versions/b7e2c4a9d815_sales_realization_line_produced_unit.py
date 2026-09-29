"""nullable FK строки реализации на единицу выпуска, без backfill

Revision ID: b7e2c4a9d815
Revises: a7c8d9e0f1a2
Create Date: 2026-09-29 12:00:00.000000

Существующие строки не обновляются: produced_unit_id остаётся NULL.
Совпадение trailer_id ключом не заполняется.
"""

from alembic import op
import sqlalchemy as sa


revision = 'b7e2c4a9d815'
down_revision = 'a7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sales_realization_line', schema=None) as batch_op:
        batch_op.add_column(sa.Column('produced_unit_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_sales_realization_line_produced_unit_id',
            'produced_unit',
            ['produced_unit_id'],
            ['id'],
        )
        batch_op.create_unique_constraint(
            'uq_sales_realization_line_produced_unit',
            ['produced_unit_id'],
        )


def downgrade():
    with op.batch_alter_table('sales_realization_line', schema=None) as batch_op:
        batch_op.drop_constraint('uq_sales_realization_line_produced_unit', type_='unique')
        batch_op.drop_constraint('fk_sales_realization_line_produced_unit_id', type_='foreignkey')
        batch_op.drop_column('produced_unit_id')
