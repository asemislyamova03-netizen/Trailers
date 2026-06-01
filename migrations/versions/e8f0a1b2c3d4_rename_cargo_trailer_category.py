"""Rename product category cargo_vehicle to cargo_trailer.

Revision ID: e8f0a1b2c3d4
Revises: d3e5f7a9b1c2
Create Date: 2026-06-01 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e8f0a1b2c3d4'
down_revision = 'd3e5f7a9b1c2'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE product_category
        SET code = 'cargo_trailer',
            name = 'Грузовые прицепы'
        WHERE code = 'cargo_vehicle'
        """
    )
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'production_workshop' in insp.get_table_names():
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых прицепов',
                product_category_scope = 'cargo_trailer'
            WHERE product_category_scope = 'cargo_vehicle'
               OR code = 'CARGO_VEHICLES'
            """
        )


def downgrade():
    op.execute(
        """
        UPDATE product_category
        SET code = 'cargo_vehicle',
            name = 'Грузовые автомобили'
        WHERE code = 'cargo_trailer'
        """
    )
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'production_workshop' in insp.get_table_names():
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых автомобилей',
                product_category_scope = 'cargo_vehicle'
            WHERE product_category_scope = 'cargo_trailer'
               OR code = 'CARGO_VEHICLES'
            """
        )
