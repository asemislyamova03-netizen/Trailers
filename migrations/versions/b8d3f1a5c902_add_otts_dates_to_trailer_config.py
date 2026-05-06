"""add otts dates to trailer config

Revision ID: b8d3f1a5c902
Revises: a9c4e2f6b701
Create Date: 2026-05-04 14:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b8d3f1a5c902'
down_revision = 'a9c4e2f6b701'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trailer_otss_modification_matrix', schema=None) as batch_op:
        batch_op.add_column(sa.Column('otts_valid_from', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('otts_valid_to', sa.Date(), nullable=True))
        batch_op.create_index('ix_trailer_otss_modification_matrix_otts_valid_from', ['otts_valid_from'], unique=False)
        batch_op.create_index('ix_trailer_otss_modification_matrix_otts_valid_to', ['otts_valid_to'], unique=False)


def downgrade():
    with op.batch_alter_table('trailer_otss_modification_matrix', schema=None) as batch_op:
        batch_op.drop_index('ix_trailer_otss_modification_matrix_otts_valid_to')
        batch_op.drop_index('ix_trailer_otss_modification_matrix_otts_valid_from')
        batch_op.drop_column('otts_valid_to')
        batch_op.drop_column('otts_valid_from')
