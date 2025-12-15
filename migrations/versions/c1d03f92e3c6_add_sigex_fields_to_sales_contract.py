"""add sigex fields to sales_contract

Revision ID: c1d03f92e3c6
Revises: eb0efc471291
Create Date: 2025-12-15 13:13:21.712740
"""
from alembic import op
import sqlalchemy as sa

revision = 'c1d03f92e3c6'
down_revision = 'eb0efc471291'
branch_labels = None
depends_on = None


def upgrade():
    # --- sales_contract: добавляем поля SIGEX ---
    with op.batch_alter_table('sales_contract', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sigex_document_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('sigex_operation_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('sigex_expire_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('sigex_last_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('sigex_last_sign_id', sa.Integer(), nullable=True))

        batch_op.create_index(
            batch_op.f('ix_sales_contract_sigex_document_id'),
            ['sigex_document_id'],
            unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_sales_contract_sigex_operation_id'),
            ['sigex_operation_id'],
            unique=False
        )

    # --- trailer: FK otts_id -> otts.id (ОБЯЗАТЕЛЬНО с именем!) ---
    with op.batch_alter_table('trailer', schema=None) as batch_op:
        batch_op.create_foreign_key(
            'fk_trailer_otts_id',   # имя constraint
            'otts',                 # таблица-родитель
            ['otts_id'],            # локальная колонка
            ['id']                  # удаленная колонка
        )


def downgrade():
    # --- trailer: удаляем FK по имени ---
    with op.batch_alter_table('trailer', schema=None) as batch_op:
        batch_op.drop_constraint('fk_trailer_otts_id', type_='foreignkey')

    # --- sales_contract: откатываем SIGEX поля ---
    with op.batch_alter_table('sales_contract', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_contract_sigex_operation_id'))
        batch_op.drop_index(batch_op.f('ix_sales_contract_sigex_document_id'))

        batch_op.drop_column('sigex_last_sign_id')
        batch_op.drop_column('sigex_last_status')
        batch_op.drop_column('sigex_expire_at')
        batch_op.drop_column('sigex_operation_id')
        batch_op.drop_column('sigex_document_id')
