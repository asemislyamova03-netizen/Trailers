"""add lead conversation fields

Revision ID: d4f6b8a2c913
Revises: b3e2a7c9d401
Create Date: 2026-04-30 15:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'd4f6b8a2c913'
down_revision = 'b3e2a7c9d401'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conversation_status', sa.String(length=30), server_default='new', nullable=False))
        batch_op.add_column(sa.Column('customer_city', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('interest_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('last_message_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_message_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('unread_count', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('ai_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('ai_summary', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_lead_conversation_status'), ['conversation_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_last_message_at'), ['last_message_at'], unique=False)

    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.alter_column('conversation_status', server_default=None)
        batch_op.alter_column('unread_count', server_default=None)
        batch_op.alter_column('ai_enabled', server_default=None)

    with op.batch_alter_table('lead_message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sender_type', sa.String(length=20), server_default='client', nullable=False))
        batch_op.add_column(sa.Column('is_read', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_lead_message_user_id_user', 'user', ['user_id'], ['id'])
        batch_op.create_index(batch_op.f('ix_lead_message_is_read'), ['is_read'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_message_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('lead_message', schema=None) as batch_op:
        batch_op.alter_column('sender_type', server_default=None)
        batch_op.alter_column('is_read', server_default=None)


def downgrade():
    with op.batch_alter_table('lead_message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lead_message_user_id'))
        batch_op.drop_index(batch_op.f('ix_lead_message_is_read'))
        batch_op.drop_constraint('fk_lead_message_user_id_user', type_='foreignkey')
        batch_op.drop_column('user_id')
        batch_op.drop_column('is_read')
        batch_op.drop_column('sender_type')

    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lead_last_message_at'))
        batch_op.drop_index(batch_op.f('ix_lead_conversation_status'))
        batch_op.drop_column('ai_summary')
        batch_op.drop_column('ai_enabled')
        batch_op.drop_column('unread_count')
        batch_op.drop_column('last_message_text')
        batch_op.drop_column('last_message_at')
        batch_op.drop_column('interest_text')
        batch_op.drop_column('customer_city')
        batch_op.drop_column('conversation_status')
