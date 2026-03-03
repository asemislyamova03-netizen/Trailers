"""add company fields to customer

Revision ID: 168cb25d618d
Revises: c1d03f92e3c6
Create Date: 2026-03-03 13:16:59.982103

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '168cb25d618d'
down_revision = 'c1d03f92e3c6'
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(r[1] == column_name for r in rows)


def upgrade():
    # на всякий случай: убираем мусорную таблицу, если она осталась после падения
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_sales_contract")

    # добавляем только если нет (у тебя уже есть — станет no-op)
    if not _has_column("customer", "bank_account"):
        op.add_column("customer", sa.Column("bank_account", sa.String(34), nullable=True))
    if not _has_column("customer", "bank_name"):
        op.add_column("customer", sa.Column("bank_name", sa.String(255), nullable=True))
    if not _has_column("customer", "bank_bic"):
        op.add_column("customer", sa.Column("bank_bic", sa.String(20), nullable=True))
    if not _has_column("customer", "director_position"):
        op.add_column("customer", sa.Column("director_position", sa.String(100), nullable=True))
    if not _has_column("customer", "director_fio"):
        op.add_column("customer", sa.Column("director_fio", sa.String(255), nullable=True))


def downgrade():
    # можно оставить пустым (для SQLite откат сложнее), но если хочешь:
    pass