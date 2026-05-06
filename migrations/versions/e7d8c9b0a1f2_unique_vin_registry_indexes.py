"""unique vin registry indexes with duplicate guard

Revision ID: e7d8c9b0a1f2
Revises: e6c1d2e3f4a5
Create Date: 2026-05-05 17:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e7d8c9b0a1f2'
down_revision = 'e6c1d2e3f4a5'
branch_labels = None
depends_on = None


def _index_names():
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('vin_registry')}


def upgrade():
    bind = op.get_bind()
    serial_dupes = bind.execute(sa.text("""
        SELECT serial7, COUNT(*) AS qty
        FROM vin_registry
        GROUP BY serial7
        HAVING COUNT(*) > 1
    """)).fetchall()
    if serial_dupes:
        report = ', '.join(f"{row[0]}: {row[1]}" for row in serial_dupes[:20])
        raise RuntimeError(f'VIN registry has duplicate serial7 values. Resolve duplicates before migration: {report}')

    vin_dupes = bind.execute(sa.text("""
        SELECT vin_full, COUNT(*) AS qty
        FROM vin_registry
        WHERE vin_full IS NOT NULL
        GROUP BY vin_full
        HAVING COUNT(*) > 1
    """)).fetchall()
    if vin_dupes:
        report = ', '.join(f"{row[0]}: {row[1]}" for row in vin_dupes[:20])
        raise RuntimeError(f'VIN registry has duplicate vin_full values. Resolve duplicates before migration: {report}')

    indexes = _index_names()
    if 'ux_vin_registry_serial7' not in indexes:
        op.create_index('ux_vin_registry_serial7', 'vin_registry', ['serial7'], unique=True)
    if 'ux_vin_registry_vin_full' not in indexes:
        op.create_index('ux_vin_registry_vin_full', 'vin_registry', ['vin_full'], unique=True)


def downgrade():
    indexes = _index_names()
    if 'ux_vin_registry_vin_full' in indexes:
        op.drop_index('ux_vin_registry_vin_full', table_name='vin_registry')
    if 'ux_vin_registry_serial7' in indexes:
        op.drop_index('ux_vin_registry_serial7', table_name='vin_registry')
