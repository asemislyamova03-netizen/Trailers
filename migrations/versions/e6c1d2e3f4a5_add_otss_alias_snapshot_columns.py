"""add otss snapshot columns after legacy otts migration

Revision ID: e6c1d2e3f4a5
Revises: e5b8c7d9a012
Create Date: 2026-05-05 17:08:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e6c1d2e3f4a5'
down_revision = 'e5b8c7d9a012'
branch_labels = None
depends_on = None


OTSS_COLUMNS = [
    sa.Column('otss_number', sa.String(length=120), nullable=True),
    sa.Column('otss_type', sa.String(length=20), nullable=True),
    sa.Column('otss_modification', sa.String(length=20), nullable=True),
]


def _columns(table_name):
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_missing(table_name):
    existing = _columns(table_name)
    missing = [column for column in OTSS_COLUMNS if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        for column in missing:
            batch_op.add_column(column.copy())


def upgrade():
    for table_name in ('customer_order', 'supply_need', 'production_request_line'):
        _add_missing(table_name)


def downgrade():
    for table_name in ('production_request_line', 'supply_need', 'customer_order'):
        existing = _columns(table_name)
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            for column in reversed(OTSS_COLUMNS):
                if column.name in existing:
                    batch_op.drop_column(column.name)
