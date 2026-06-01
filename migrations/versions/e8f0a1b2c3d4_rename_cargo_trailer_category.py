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


def _workshop_columns() -> set[str]:
    bind = op.get_bind()
    if 'production_workshop' not in sa.inspect(bind).get_table_names():
        return set()
    return {column['name'] for column in sa.inspect(bind).get_columns('production_workshop')}


def upgrade():
    op.execute(
        """
        UPDATE product_category
        SET code = 'cargo_trailer',
            name = 'Грузовые прицепы'
        WHERE code = 'cargo_vehicle'
        """
    )
    columns = _workshop_columns()
    if not columns:
        return
    if 'workshop_type' in columns:
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых прицепов',
                workshop_type = 'cargo_trailer'
            WHERE code = 'CARGO_VEHICLES'
               OR workshop_type = 'cargo_vehicle'
            """
        )
    elif 'product_category_scope' in columns:
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых прицепов',
                product_category_scope = 'cargo_trailer'
            WHERE product_category_scope = 'cargo_vehicle'
               OR code = 'CARGO_VEHICLES'
            """
        )
    else:
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых прицепов'
            WHERE code = 'CARGO_VEHICLES'
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
    columns = _workshop_columns()
    if not columns:
        return
    if 'workshop_type' in columns:
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых автомобилей',
                workshop_type = 'cargo_vehicle'
            WHERE code = 'CARGO_VEHICLES'
               OR workshop_type = 'cargo_trailer'
            """
        )
    elif 'product_category_scope' in columns:
        op.execute(
            """
            UPDATE production_workshop
            SET name = 'Цех грузовых автомобилей',
                product_category_scope = 'cargo_vehicle'
            WHERE product_category_scope = 'cargo_trailer'
               OR code = 'CARGO_VEHICLES'
            """
        )
