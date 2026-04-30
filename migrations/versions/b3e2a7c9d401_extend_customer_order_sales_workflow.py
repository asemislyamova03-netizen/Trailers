"""extend customer order sales workflow

Revision ID: b3e2a7c9d401
Revises: 9f4a1c2b7e30
Create Date: 2026-04-30 13:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'b3e2a7c9d401'
down_revision = '9f4a1c2b7e30'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manager_comment', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('document_status', sa.String(length=30), server_default='not_started', nullable=False))
        batch_op.add_column(sa.Column('documents_issued_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('shipped_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancel_reason', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_customer_order_document_status'), ['document_status'], unique=False)

    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.alter_column('document_status', server_default=None)

    op.create_table(
        'order_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('old_value', sa.String(length=255), nullable=True),
        sa.Column('new_value', sa.String(length=255), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['customer_order.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_order_event_created_at'), 'order_event', ['created_at'], unique=False)
    op.create_index(op.f('ix_order_event_event_type'), 'order_event', ['event_type'], unique=False)
    op.create_index(op.f('ix_order_event_order_id'), 'order_event', ['order_id'], unique=False)
    op.create_index(op.f('ix_order_event_user_id'), 'order_event', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_order_event_user_id'), table_name='order_event')
    op.drop_index(op.f('ix_order_event_order_id'), table_name='order_event')
    op.drop_index(op.f('ix_order_event_event_type'), table_name='order_event')
    op.drop_index(op.f('ix_order_event_created_at'), table_name='order_event')
    op.drop_table('order_event')

    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_order_document_status'))
        batch_op.drop_column('cancel_reason')
        batch_op.drop_column('cancelled_at')
        batch_op.drop_column('shipped_at')
        batch_op.drop_column('documents_issued_at')
        batch_op.drop_column('document_status')
        batch_op.drop_column('manager_comment')
