"""order lead vin final workflow on d1

Revision ID: e5b8c7d9a012
Revises: d1f2a3b4c5d6
Create Date: 2026-05-05 17:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'e5b8c7d9a012'
down_revision = 'd1f2a3b4c5d6'
branch_labels = None
depends_on = None


SNAPSHOT_COLUMNS = [
    sa.Column('article_snapshot', sa.String(length=80), nullable=True),
    sa.Column('product_name_snapshot', sa.String(length=255), nullable=True),
    sa.Column('config_snapshot_json', sa.Text(), nullable=True),
    sa.Column('calculated_price', sa.Numeric(12, 2), nullable=True),
    sa.Column('price_breakdown_json', sa.Text(), nullable=True),
    sa.Column('overall_dimensions_text', sa.String(length=80), nullable=True),
    sa.Column('inner_dimensions_text', sa.String(length=80), nullable=True),
    sa.Column('otss_number', sa.String(length=120), nullable=True),
    sa.Column('otss_type', sa.String(length=20), nullable=True),
    sa.Column('otss_modification', sa.String(length=20), nullable=True),
    sa.Column('vin_modification_code', sa.String(length=20), nullable=True),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table_name):
    return {column['name'] for column in _inspector().get_columns(table_name)}


def _indexes(table_name):
    return {index['name'] for index in _inspector().get_indexes(table_name)}


def _table_exists(table_name):
    return table_name in _inspector().get_table_names()


def _add_columns_if_missing(table_name, columns):
    existing = _columns(table_name)
    missing = [column for column in columns if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        for column in missing:
            batch_op.add_column(column.copy())


def upgrade():
    op.execute('DROP TABLE IF EXISTS _alembic_tmp_customer_order')
    op.execute('DROP TABLE IF EXISTS _alembic_tmp_vin_registry')
    _add_columns_if_missing('lead', SNAPSHOT_COLUMNS)

    order_cols = _columns('customer_order')
    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        batch_op.alter_column('item_id', existing_type=sa.Integer(), nullable=True)
        if 'reserved_vin_registry_id' not in order_cols:
            batch_op.add_column(sa.Column('reserved_vin_registry_id', sa.Integer(), nullable=True))
            batch_op.create_index('ix_customer_order_reserved_vin_registry_id', ['reserved_vin_registry_id'], unique=False)
            batch_op.create_foreign_key('fk_customer_order_reserved_vin_registry_id_vin_registry', 'vin_registry', ['reserved_vin_registry_id'], ['id'])

    vin_indexes = _indexes('vin_registry')
    for index_name in ('ix_vin_registry_production_request_line_id', 'ix_vin_registry_produced_unit_id'):
        if index_name in vin_indexes:
            op.drop_index(index_name, table_name='vin_registry')

    vin_cols = _columns('vin_registry')
    with op.batch_alter_table('vin_registry', schema=None) as batch_op:
        if 'vin_full' in vin_cols:
            batch_op.alter_column('vin_full', existing_type=sa.String(length=50), nullable=True)
        if 'vin_modification_code' in vin_cols:
            batch_op.alter_column('vin_modification_code', existing_type=sa.String(length=20), nullable=True)
        if 'year_code' in vin_cols:
            batch_op.alter_column('year_code', existing_type=sa.String(length=1), nullable=True)
        if 'docs_issued_order_id' not in vin_cols:
            batch_op.add_column(sa.Column('docs_issued_order_id', sa.Integer(), nullable=True))
            batch_op.create_index('ix_vin_registry_docs_issued_order_id', ['docs_issued_order_id'], unique=False)
            batch_op.create_foreign_key('fk_vin_registry_docs_issued_order_id_customer_order', 'customer_order', ['docs_issued_order_id'], ['id'])
        if 'production_request_line_id' in vin_cols:
            batch_op.drop_column('production_request_line_id')
        if 'produced_unit_id' in vin_cols:
            batch_op.drop_column('produced_unit_id')

    op.execute("""
        UPDATE vin_registry
        SET docs_issued_order_id = customer_order_id
        WHERE docs_issued_order_id IS NULL
          AND customer_order_id IS NOT NULL
          AND (status = 'docs_issued' OR docs_issued_at IS NOT NULL)
    """)
    op.execute("""
        UPDATE vin_registry
        SET status = CASE
            WHEN trailer_id IS NOT NULL AND confirmed_at IS NOT NULL THEN 'confirmed'
            WHEN trailer_id IS NOT NULL THEN 'assigned'
            ELSE 'reserved'
        END
        WHERE status = 'docs_issued'
    """)

    if not _table_exists('vin_registry_event'):
        op.create_table(
            'vin_registry_event',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('vin_registry_id', sa.Integer(), nullable=False),
            sa.Column('event_type', sa.String(length=40), nullable=False),
            sa.Column('old_status', sa.String(length=30), nullable=True),
            sa.Column('new_status', sa.String(length=30), nullable=True),
            sa.Column('customer_order_id', sa.Integer(), nullable=True),
            sa.Column('trailer_id', sa.Integer(), nullable=True),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['customer_order_id'], ['customer_order.id']),
            sa.ForeignKeyConstraint(['trailer_id'], ['trailer.id']),
            sa.ForeignKeyConstraint(['user_id'], ['user.id']),
            sa.ForeignKeyConstraint(['vin_registry_id'], ['vin_registry.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_vin_registry_event_vin_registry_id', 'vin_registry_event', ['vin_registry_id'], unique=False)
        op.create_index('ix_vin_registry_event_event_type', 'vin_registry_event', ['event_type'], unique=False)
        op.create_index('ix_vin_registry_event_customer_order_id', 'vin_registry_event', ['customer_order_id'], unique=False)
        op.create_index('ix_vin_registry_event_trailer_id', 'vin_registry_event', ['trailer_id'], unique=False)
        op.create_index('ix_vin_registry_event_user_id', 'vin_registry_event', ['user_id'], unique=False)
        op.create_index('ix_vin_registry_event_created_at', 'vin_registry_event', ['created_at'], unique=False)


def downgrade():
    if _table_exists('vin_registry_event'):
        op.drop_table('vin_registry_event')
    with op.batch_alter_table('vin_registry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('produced_unit_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('production_request_line_id', sa.Integer(), nullable=True))
        if 'docs_issued_order_id' in _columns('vin_registry'):
            batch_op.drop_constraint('fk_vin_registry_docs_issued_order_id_customer_order', type_='foreignkey')
            batch_op.drop_index('ix_vin_registry_docs_issued_order_id')
            batch_op.drop_column('docs_issued_order_id')
        batch_op.alter_column('year_code', existing_type=sa.String(length=1), nullable=False)
        batch_op.alter_column('vin_modification_code', existing_type=sa.String(length=20), nullable=False)
        batch_op.alter_column('vin_full', existing_type=sa.String(length=50), nullable=False)
    with op.batch_alter_table('customer_order', schema=None) as batch_op:
        if 'reserved_vin_registry_id' in _columns('customer_order'):
            batch_op.drop_constraint('fk_customer_order_reserved_vin_registry_id_vin_registry', type_='foreignkey')
            batch_op.drop_index('ix_customer_order_reserved_vin_registry_id')
            batch_op.drop_column('reserved_vin_registry_id')
        batch_op.alter_column('item_id', existing_type=sa.Integer(), nullable=False)

