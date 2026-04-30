"""roles production logistics workspaces

Revision ID: 8b1f4a9d2c70
Revises: 6f2b7d9c1a04
Create Date: 2026-04-29 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '8b1f4a9d2c70'
down_revision = '6f2b7d9c1a04'
branch_labels = None
depends_on = None


def _columns(table_name):
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade():
    line_cols = _columns('production_request_line')
    with op.batch_alter_table('production_request_line') as batch_op:
        if 'produced_qty' not in line_cols:
            batch_op.add_column(sa.Column('produced_qty', sa.Integer(), nullable=False, server_default='0'))
        if 'started_at' not in line_cols:
            batch_op.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        if 'completed_at' not in line_cols:
            batch_op.add_column(sa.Column('completed_at', sa.DateTime(), nullable=True))
        if 'produced_at' not in line_cols:
            batch_op.add_column(sa.Column('produced_at', sa.DateTime(), nullable=True))
        if 'production_comment' not in line_cols:
            batch_op.add_column(sa.Column('production_comment', sa.Text(), nullable=True))

    if 'produced_unit' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            'produced_unit',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('production_request_line_id', sa.Integer(), nullable=False),
            sa.Column('item_id', sa.Integer(), nullable=False),
            sa.Column('target_warehouse_id', sa.Integer(), nullable=True),
            sa.Column('trailer_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('produced_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['item_id'], ['item.id']),
            sa.ForeignKeyConstraint(['production_request_line_id'], ['production_request_line.id']),
            sa.ForeignKeyConstraint(['target_warehouse_id'], ['warehouse.id']),
            sa.ForeignKeyConstraint(['trailer_id'], ['trailer.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        for col in ('created_at', 'item_id', 'production_request_line_id', 'status', 'target_warehouse_id', 'trailer_id'):
            op.create_index(op.f(f'ix_produced_unit_{col}'), 'produced_unit', [col], unique=False)


def downgrade():
    if 'produced_unit' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('produced_unit')
    line_cols = _columns('production_request_line')
    with op.batch_alter_table('production_request_line') as batch_op:
        for column in ('production_comment', 'produced_at', 'completed_at', 'started_at', 'produced_qty'):
            if column in line_cols:
                batch_op.drop_column(column)
