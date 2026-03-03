"""add opf to customer

Revision ID: 05dbd814346a
Revises: 168cb25d618d
Create Date: 2026-03-03 16:06:20.002463

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '05dbd814346a'
down_revision = '168cb25d618d'
branch_labels = None
depends_on = None


def upgrade():
    # Важно: в твоей локальной БД колонка уже добавлена.
    # На чистой БД миграция отработает нормально.
    op.add_column("customer", sa.Column("opf", sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table("customer") as batch_op:
        batch_op.drop_column("opf")