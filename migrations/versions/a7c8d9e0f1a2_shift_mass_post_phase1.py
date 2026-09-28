"""Shift mass post: direction mode, materials, shortage, produced unit seq.

Revision ID: a7c8d9e0f1a2
Revises: f1a2b3c4d5e6
Create Date: 2026-09-28 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a7c8d9e0f1a2'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _insp().get_table_names()


def _columns(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {c['name'] for c in _insp().get_columns(table)}


def _indexes(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {i['name'] for i in _insp().get_indexes(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name in _columns(table):
        return
    op.add_column(table, column)


def upgrade():
    if 'shopfloor_posting_mode' not in _columns('warehouse'):
        op.add_column(
            'warehouse',
            sa.Column(
                'shopfloor_posting_mode',
                sa.String(length=30),
                nullable=False,
                server_default='legacy_plus_one',
            ),
        )
        op.create_index('ix_warehouse_shopfloor_posting_mode', 'warehouse', ['shopfloor_posting_mode'])

    _add_column('production_shift', sa.Column('hours_fact', sa.Numeric(10, 2), nullable=True))
    _add_column('production_shift', sa.Column('direction_warehouse_id', sa.Integer(), nullable=True))
    _add_column('production_shift', sa.Column('posted_at', sa.DateTime(), nullable=True))
    _add_column('production_shift', sa.Column('posted_by_user_id', sa.Integer(), nullable=True))
    _add_column(
        'production_shift',
        sa.Column('senior_shortage_confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    if 'ix_production_shift_direction_warehouse_id' not in _indexes('production_shift'):
        op.create_index('ix_production_shift_direction_warehouse_id', 'production_shift', ['direction_warehouse_id'])
    _add_column('production_shift', sa.Column('direction_area_id', sa.Integer(), nullable=True))
    if 'ix_production_shift_direction_area_id' not in _indexes('production_shift'):
        op.create_index('ix_production_shift_direction_area_id', 'production_shift', ['direction_area_id'])
    if 'ix_production_shift_posted_at' not in _indexes('production_shift'):
        op.create_index('ix_production_shift_posted_at', 'production_shift', ['posted_at'])

    _add_column('warehouse_storage_area', sa.Column('product_category', sa.String(length=40), nullable=True))
    _add_column(
        'warehouse_storage_area',
        sa.Column(
            'shopfloor_posting_mode',
            sa.String(length=30),
            nullable=False,
            server_default='legacy_plus_one',
        ),
    )
    if 'ix_warehouse_storage_area_product_category' not in _indexes('warehouse_storage_area'):
        op.create_index('ix_warehouse_storage_area_product_category', 'warehouse_storage_area', ['product_category'])
    if 'ix_warehouse_storage_area_shopfloor_posting_mode' not in _indexes('warehouse_storage_area'):
        op.create_index(
            'ix_warehouse_storage_area_shopfloor_posting_mode',
            'warehouse_storage_area',
            ['shopfloor_posting_mode'],
        )
    # Пустые зоны направления на производственном складе. Остатки не переносим.
    if _has_table('warehouse') and _has_table('warehouse_storage_area'):
        conn = op.get_bind()
        for code, name, category, sort_order in (
            ('DIR_LIGHT', 'Легковые ТМЦ', 'light_trailer', 210),
            ('DIR_CARGO', 'Грузовые ТМЦ', 'cargo_trailer', 220),
        ):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO warehouse_storage_area
                        (warehouse_id, code, name, area_type, is_active, sort_order,
                         product_category, shopfloor_posting_mode, created_at, updated_at)
                    SELECT id, :code, :name, 'direction_stock', 1, :sort_order,
                           :category, 'legacy_plus_one', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    FROM warehouse
                    WHERE is_production = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM warehouse_storage_area a
                          WHERE a.warehouse_id = warehouse.id AND a.code = :code
                      )
                    """
                ),
                {'code': code, 'name': name, 'category': category, 'sort_order': sort_order},
            )

    _add_column(
        'production_shift_output',
        sa.Column('replenishment_request_line_id', sa.Integer(), nullable=True),
    )
    if 'ix_production_shift_output_replenishment_request_line_id' not in _indexes('production_shift_output'):
        op.create_index(
            'ix_production_shift_output_replenishment_request_line_id',
            'production_shift_output',
            ['replenishment_request_line_id'],
        )

    if not _has_table('production_shift_material'):
        op.create_table(
            'production_shift_material',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('shift_id', sa.Integer(), nullable=False),
            sa.Column('item_id', sa.Integer(), nullable=False),
            sa.Column('qty_fact', sa.Numeric(12, 3), nullable=False, server_default='0'),
            sa.Column('qty_issued', sa.Numeric(12, 3), nullable=False, server_default='0'),
            sa.Column('qty_shortage', sa.Numeric(12, 3), nullable=False, server_default='0'),
            sa.Column('unit', sa.String(length=20), nullable=False, server_default='шт'),
            sa.Column('shortage_status', sa.String(length=30), nullable=False, server_default='none'),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='draft'),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['item_id'], ['item.id']),
            sa.ForeignKeyConstraint(['shift_id'], ['production_shift.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_production_shift_material_shift_id', 'production_shift_material', ['shift_id'])
        op.create_index('ix_production_shift_material_item_id', 'production_shift_material', ['item_id'])
        op.create_index('ix_production_shift_material_shortage_status', 'production_shift_material', ['shortage_status'])
        op.create_index('ix_production_shift_material_status', 'production_shift_material', ['status'])

    _add_column('produced_unit', sa.Column('shift_output_id', sa.Integer(), nullable=True))
    _add_column('produced_unit', sa.Column('unit_seq', sa.Integer(), nullable=True))
    if 'ix_produced_unit_shift_output_id' not in _indexes('produced_unit'):
        op.create_index('ix_produced_unit_shift_output_id', 'produced_unit', ['shift_output_id'])
    if 'uq_produced_unit_shift_output_seq' not in _indexes('produced_unit'):
        op.create_index(
            'uq_produced_unit_shift_output_seq',
            'produced_unit',
            ['shift_output_id', 'unit_seq'],
            unique=True,
        )

    _add_column('inventory_operation', sa.Column('shift_id', sa.Integer(), nullable=True))
    _add_column('inventory_operation', sa.Column('shift_output_id', sa.Integer(), nullable=True))
    _add_column('inventory_operation', sa.Column('shift_material_id', sa.Integer(), nullable=True))
    if 'ix_inventory_operation_shift_id' not in _indexes('inventory_operation'):
        op.create_index('ix_inventory_operation_shift_id', 'inventory_operation', ['shift_id'])
    if 'ix_inventory_operation_shift_output_id' not in _indexes('inventory_operation'):
        op.create_index('ix_inventory_operation_shift_output_id', 'inventory_operation', ['shift_output_id'])
    if 'ix_inventory_operation_shift_material_id' not in _indexes('inventory_operation'):
        op.create_index('ix_inventory_operation_shift_material_id', 'inventory_operation', ['shift_material_id'])

    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        if 'uq_inv_op_posted_issue_shift_material' not in _indexes('inventory_operation'):
            op.execute(
                "CREATE UNIQUE INDEX uq_inv_op_posted_issue_shift_material "
                "ON inventory_operation (shift_material_id) "
                "WHERE operation_type = 'production_issue' AND status = 'posted' AND shift_material_id IS NOT NULL"
            )
        if 'uq_inv_op_posted_shortage_shift_material' not in _indexes('inventory_operation'):
            op.execute(
                "CREATE UNIQUE INDEX uq_inv_op_posted_shortage_shift_material "
                "ON inventory_operation (shift_material_id) "
                "WHERE operation_type = 'production_shortage' AND status = 'posted' AND shift_material_id IS NOT NULL"
            )
        if 'uq_inv_op_posted_receipt_shift_output' not in _indexes('inventory_operation'):
            op.execute(
                "CREATE UNIQUE INDEX uq_inv_op_posted_receipt_shift_output "
                "ON inventory_operation (shift_output_id) "
                "WHERE operation_type = 'production_output_receipt' AND status = 'posted' AND shift_output_id IS NOT NULL"
            )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        op.execute('DROP INDEX IF EXISTS uq_inv_op_posted_receipt_shift_output')
        op.execute('DROP INDEX IF EXISTS uq_inv_op_posted_shortage_shift_material')
        op.execute('DROP INDEX IF EXISTS uq_inv_op_posted_issue_shift_material')
    if 'ix_inventory_operation_shift_material_id' in _indexes('inventory_operation'):
        op.drop_index('ix_inventory_operation_shift_material_id', table_name='inventory_operation')
    if 'ix_inventory_operation_shift_output_id' in _indexes('inventory_operation'):
        op.drop_index('ix_inventory_operation_shift_output_id', table_name='inventory_operation')
    if 'ix_inventory_operation_shift_id' in _indexes('inventory_operation'):
        op.drop_index('ix_inventory_operation_shift_id', table_name='inventory_operation')
    for col in ('shift_material_id', 'shift_output_id', 'shift_id'):
        if col in _columns('inventory_operation'):
            op.drop_column('inventory_operation', col)
    if 'uq_produced_unit_shift_output_seq' in _indexes('produced_unit'):
        op.drop_index('uq_produced_unit_shift_output_seq', table_name='produced_unit')
    if 'ix_produced_unit_shift_output_id' in _indexes('produced_unit'):
        op.drop_index('ix_produced_unit_shift_output_id', table_name='produced_unit')
    for col in ('unit_seq', 'shift_output_id'):
        if col in _columns('produced_unit'):
            op.drop_column('produced_unit', col)
    if _has_table('production_shift_material'):
        op.drop_table('production_shift_material')
    if 'ix_production_shift_output_replenishment_request_line_id' in _indexes('production_shift_output'):
        op.drop_index(
            'ix_production_shift_output_replenishment_request_line_id',
            table_name='production_shift_output',
        )
    if 'replenishment_request_line_id' in _columns('production_shift_output'):
        op.drop_column('production_shift_output', 'replenishment_request_line_id')
    if 'ix_production_shift_posted_at' in _indexes('production_shift'):
        op.drop_index('ix_production_shift_posted_at', table_name='production_shift')
    if 'ix_production_shift_direction_area_id' in _indexes('production_shift'):
        op.drop_index('ix_production_shift_direction_area_id', table_name='production_shift')
    if 'ix_production_shift_direction_warehouse_id' in _indexes('production_shift'):
        op.drop_index('ix_production_shift_direction_warehouse_id', table_name='production_shift')
    for col in (
        'senior_shortage_confirmed',
        'posted_by_user_id',
        'posted_at',
        'direction_area_id',
        'direction_warehouse_id',
        'hours_fact',
    ):
        if col in _columns('production_shift'):
            op.drop_column('production_shift', col)
    if 'ix_warehouse_storage_area_shopfloor_posting_mode' in _indexes('warehouse_storage_area'):
        op.drop_index('ix_warehouse_storage_area_shopfloor_posting_mode', table_name='warehouse_storage_area')
    if 'ix_warehouse_storage_area_product_category' in _indexes('warehouse_storage_area'):
        op.drop_index('ix_warehouse_storage_area_product_category', table_name='warehouse_storage_area')
    for col in ('shopfloor_posting_mode', 'product_category'):
        if col in _columns('warehouse_storage_area'):
            op.drop_column('warehouse_storage_area', col)
    if 'ix_warehouse_shopfloor_posting_mode' in _indexes('warehouse'):
        op.drop_index('ix_warehouse_shopfloor_posting_mode', table_name='warehouse')
    if 'shopfloor_posting_mode' in _columns('warehouse'):
        op.drop_column('warehouse', 'shopfloor_posting_mode')
