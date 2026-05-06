"""add special option prices and group otts dates

Revision ID: c6e4b2a9d813
Revises: b8d3f1a5c902
Create Date: 2026-05-04 15:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'c6e4b2a9d813'
down_revision = 'b8d3f1a5c902'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trailer_product_group', schema=None) as batch_op:
        batch_op.add_column(sa.Column('otts_valid_from', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('otts_valid_to', sa.Date(), nullable=True))
        batch_op.create_index('ix_trailer_product_group_otts_valid_from', ['otts_valid_from'], unique=False)
        batch_op.create_index('ix_trailer_product_group_otts_valid_to', ['otts_valid_to'], unique=False)

    op.create_table(
        'trailer_special_option_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('special_option_id', sa.Integer(), nullable=False),
        sa.Column('price', sa.Numeric(12, 2), nullable=False),
        sa.Column('cost_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('currency', sa.String(10), nullable=False),
        sa.Column('valid_from', sa.Date(), nullable=True),
        sa.Column('valid_to', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.ForeignKeyConstraint(['special_option_id'], ['trailer_special_option.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'special_option_id', 'valid_from', name='uq_trailer_special_option_price'),
    )
    op.create_index('ix_trailer_special_option_price_matrix_group_id', 'trailer_special_option_price_matrix', ['group_id'])
    op.create_index('ix_trailer_special_option_price_matrix_special_option_id', 'trailer_special_option_price_matrix', ['special_option_id'])
    op.create_index('ix_trailer_special_option_price_matrix_is_active', 'trailer_special_option_price_matrix', ['is_active'])
    op.create_index('ix_trailer_special_option_price_matrix_valid_from', 'trailer_special_option_price_matrix', ['valid_from'])
    op.create_index('ix_trailer_special_option_price_matrix_valid_to', 'trailer_special_option_price_matrix', ['valid_to'])


def downgrade():
    op.drop_table('trailer_special_option_price_matrix')
    with op.batch_alter_table('trailer_product_group', schema=None) as batch_op:
        batch_op.drop_index('ix_trailer_product_group_otts_valid_to')
        batch_op.drop_index('ix_trailer_product_group_otts_valid_from')
        batch_op.drop_column('otts_valid_to')
        batch_op.drop_column('otts_valid_from')
