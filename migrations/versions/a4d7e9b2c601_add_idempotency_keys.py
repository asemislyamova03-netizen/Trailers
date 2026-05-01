"""add idempotency keys

Revision ID: a4d7e9b2c601
Revises: f2c9a8b1d704
Create Date: 2026-05-01 17:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4d7e9b2c601'
down_revision = 'f2c9a8b1d704'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'idempotency_key',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(length=255), nullable=False),
        sa.Column('form_token', sa.String(length=64), nullable=False),
        sa.Column('object_type', sa.String(length=80), nullable=True),
        sa.Column('object_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'endpoint', 'form_token', name='uq_idempotency_user_endpoint_token'),
    )
    op.create_index(op.f('ix_idempotency_key_created_at'), 'idempotency_key', ['created_at'], unique=False)
    op.create_index(op.f('ix_idempotency_key_user_id'), 'idempotency_key', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_idempotency_key_user_id'), table_name='idempotency_key')
    op.drop_index(op.f('ix_idempotency_key_created_at'), table_name='idempotency_key')
    op.drop_table('idempotency_key')
