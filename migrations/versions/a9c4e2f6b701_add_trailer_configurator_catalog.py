"""add trailer configurator catalog

Revision ID: a9c4e2f6b701
Revises: c5d2e8f1a9b0
Create Date: 2026-05-04 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = 'a9c4e2f6b701'
down_revision = 'c5d2e8f1a9b0'
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    ]


def _active_sort_comment():
    return [
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
    ]


def _price_columns():
    return [
        sa.Column('price', sa.Numeric(12, 2), nullable=False),
        sa.Column('cost_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('currency', sa.String(10), nullable=False),
        sa.Column('valid_from', sa.Date(), nullable=True),
        sa.Column('valid_to', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
    ]


def upgrade():
    op.create_table(
        'trailer_product_group',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('name_prefix', sa.String(160), nullable=False),
        sa.Column('otss_number', sa.String(120), nullable=True),
        sa.Column('otss_type', sa.String(20), nullable=True),
        sa.Column('vehicle_category', sa.String(20), nullable=True),
        sa.Column('axle_count', sa.Integer(), nullable=True),
        sa.Column('wheel_count', sa.Integer(), nullable=True),
        sa.Column('max_mass_kg', sa.Integer(), nullable=True),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_product_group_code', 'trailer_product_group', ['code'])
    op.create_index('ix_trailer_product_group_is_active', 'trailer_product_group', ['is_active'])
    op.create_index('ix_trailer_product_group_otss_type', 'trailer_product_group', ['otss_type'])

    op.create_table(
        'trailer_body_size',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('length_mm', sa.Integer(), nullable=False),
        sa.Column('width_mm', sa.Integer(), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_body_size_code', 'trailer_body_size', ['code'])
    op.create_index('ix_trailer_body_size_is_active', 'trailer_body_size', ['is_active'])

    op.create_table(
        'trailer_board_height',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('height_mm', sa.Integer(), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=False),
        sa.Column('is_no_board', sa.Boolean(), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_board_height_code', 'trailer_board_height', ['code'])
    op.create_index('ix_trailer_board_height_is_active', 'trailer_board_height', ['is_active'])

    op.create_table(
        'trailer_wheel_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('wheel_size', sa.String(40), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_wheel_option_code', 'trailer_wheel_option', ['code'])
    op.create_index('ix_trailer_wheel_option_is_active', 'trailer_wheel_option', ['is_active'])

    op.create_table(
        'trailer_hub_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('for_wheel_size', sa.String(60), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_hub_option_code', 'trailer_hub_option', ['code'])
    op.create_index('ix_trailer_hub_option_is_active', 'trailer_hub_option', ['is_active'])

    op.create_table(
        'trailer_support_wheel_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_support_wheel_option_code', 'trailer_support_wheel_option', ['code'])
    op.create_index('ix_trailer_support_wheel_option_is_active', 'trailer_support_wheel_option', ['is_active'])

    op.create_table(
        'trailer_tent_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('height_mm', sa.Integer(), nullable=False),
        sa.Column('article_part', sa.String(20), nullable=True),
        sa.Column('is_no_tent', sa.Boolean(), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_tent_option_code', 'trailer_tent_option', ['code'])
    op.create_index('ix_trailer_tent_option_is_active', 'trailer_tent_option', ['is_active'])

    op.create_table(
        'trailer_body_execution',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(30), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('name_for_title', sa.String(120), nullable=False),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_body_execution_code', 'trailer_body_execution', ['code'])
    op.create_index('ix_trailer_body_execution_is_active', 'trailer_body_execution', ['is_active'])

    op.create_table(
        'trailer_special_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(30), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('option_type', sa.String(40), nullable=False),
        sa.Column('article_part', sa.String(40), nullable=True),
        *_active_sort_comment(),
        *_timestamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_trailer_special_option_code', 'trailer_special_option', ['code'])
    op.create_index('ix_trailer_special_option_is_active', 'trailer_special_option', ['is_active'])
    op.create_index('ix_trailer_special_option_option_type', 'trailer_special_option', ['option_type'])

    op.create_table(
        'trailer_allowed_option',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('option_type', sa.String(40), nullable=False),
        sa.Column('option_id', sa.Integer(), nullable=False),
        sa.Column('is_allowed', sa.Boolean(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'option_type', 'option_id', name='uq_trailer_allowed_option'),
    )
    op.create_index('ix_trailer_allowed_option_group_id', 'trailer_allowed_option', ['group_id'])
    op.create_index('ix_trailer_allowed_option_option_type', 'trailer_allowed_option', ['option_type'])
    op.create_index('ix_trailer_allowed_option_option_id', 'trailer_allowed_option', ['option_id'])

    op.create_table(
        'trailer_platform_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('body_size_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.ForeignKeyConstraint(['body_size_id'], ['trailer_body_size.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'body_size_id', 'valid_from', name='uq_trailer_platform_price'),
    )
    op.create_index('ix_trailer_platform_price_matrix_group_id', 'trailer_platform_price_matrix', ['group_id'])
    op.create_index('ix_trailer_platform_price_matrix_body_size_id', 'trailer_platform_price_matrix', ['body_size_id'])

    op.create_table(
        'trailer_board_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('body_size_id', sa.Integer(), nullable=False),
        sa.Column('board_height_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['body_size_id'], ['trailer_body_size.id']),
        sa.ForeignKeyConstraint(['board_height_id'], ['trailer_board_height.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('body_size_id', 'board_height_id', 'valid_from', name='uq_trailer_board_price'),
    )
    op.create_index('ix_trailer_board_price_matrix_body_size_id', 'trailer_board_price_matrix', ['body_size_id'])
    op.create_index('ix_trailer_board_price_matrix_board_height_id', 'trailer_board_price_matrix', ['board_height_id'])

    op.create_table(
        'trailer_tent_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('body_size_id', sa.Integer(), nullable=False),
        sa.Column('tent_option_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['body_size_id'], ['trailer_body_size.id']),
        sa.ForeignKeyConstraint(['tent_option_id'], ['trailer_tent_option.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('body_size_id', 'tent_option_id', 'valid_from', name='uq_trailer_tent_price'),
    )
    op.create_index('ix_trailer_tent_price_matrix_body_size_id', 'trailer_tent_price_matrix', ['body_size_id'])
    op.create_index('ix_trailer_tent_price_matrix_tent_option_id', 'trailer_tent_price_matrix', ['tent_option_id'])

    op.create_table(
        'trailer_wheel_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('wheel_option_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.ForeignKeyConstraint(['wheel_option_id'], ['trailer_wheel_option.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'wheel_option_id', 'valid_from', name='uq_trailer_wheel_price'),
    )
    op.create_index('ix_trailer_wheel_price_matrix_group_id', 'trailer_wheel_price_matrix', ['group_id'])
    op.create_index('ix_trailer_wheel_price_matrix_wheel_option_id', 'trailer_wheel_price_matrix', ['wheel_option_id'])

    op.create_table(
        'trailer_hub_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('hub_option_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.ForeignKeyConstraint(['hub_option_id'], ['trailer_hub_option.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'hub_option_id', 'valid_from', name='uq_trailer_hub_price'),
    )
    op.create_index('ix_trailer_hub_price_matrix_group_id', 'trailer_hub_price_matrix', ['group_id'])
    op.create_index('ix_trailer_hub_price_matrix_hub_option_id', 'trailer_hub_price_matrix', ['hub_option_id'])

    op.create_table(
        'trailer_support_wheel_price_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('support_wheel_option_id', sa.Integer(), nullable=False),
        *_price_columns(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['support_wheel_option_id'], ['trailer_support_wheel_option.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('support_wheel_option_id', 'valid_from', name='uq_trailer_support_wheel_price'),
    )
    op.create_index('ix_trailer_support_wheel_price_matrix_support_wheel_option_id', 'trailer_support_wheel_price_matrix', ['support_wheel_option_id'])

    op.create_table(
        'trailer_dimension_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('body_size_id', sa.Integer(), nullable=False),
        sa.Column('board_height_id', sa.Integer(), nullable=False),
        sa.Column('overall_length_mm', sa.Integer(), nullable=False),
        sa.Column('overall_width_mm', sa.Integer(), nullable=False),
        sa.Column('overall_height_mm', sa.Integer(), nullable=False),
        sa.Column('inner_length_mm', sa.Integer(), nullable=False),
        sa.Column('inner_width_mm', sa.Integer(), nullable=False),
        sa.Column('inner_height_mm', sa.Integer(), nullable=False),
        sa.Column('overall_dimensions_text', sa.String(80), nullable=False),
        sa.Column('inner_dimensions_text', sa.String(80), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['body_size_id'], ['trailer_body_size.id']),
        sa.ForeignKeyConstraint(['board_height_id'], ['trailer_board_height.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('body_size_id', 'board_height_id', name='uq_trailer_dimension'),
    )
    op.create_index('ix_trailer_dimension_matrix_body_size_id', 'trailer_dimension_matrix', ['body_size_id'])
    op.create_index('ix_trailer_dimension_matrix_board_height_id', 'trailer_dimension_matrix', ['board_height_id'])
    op.create_index('ix_trailer_dimension_matrix_is_active', 'trailer_dimension_matrix', ['is_active'])

    op.create_table(
        'trailer_otss_modification_matrix',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('body_execution_id', sa.Integer(), nullable=False),
        sa.Column('board_height_id', sa.Integer(), nullable=True),
        sa.Column('special_option_id', sa.Integer(), nullable=True),
        sa.Column('otss_number', sa.String(120), nullable=True),
        sa.Column('otss_type', sa.String(20), nullable=False),
        sa.Column('otss_modification', sa.String(20), nullable=False),
        sa.Column('vin_modification_code', sa.String(20), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        *_active_sort_comment(),
        *_timestamps(),
        sa.ForeignKeyConstraint(['group_id'], ['trailer_product_group.id']),
        sa.ForeignKeyConstraint(['body_execution_id'], ['trailer_body_execution.id']),
        sa.ForeignKeyConstraint(['board_height_id'], ['trailer_board_height.id']),
        sa.ForeignKeyConstraint(['special_option_id'], ['trailer_special_option.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_trailer_otss_modification_matrix_group_id', 'trailer_otss_modification_matrix', ['group_id'])
    op.create_index('ix_trailer_otss_modification_matrix_body_execution_id', 'trailer_otss_modification_matrix', ['body_execution_id'])
    op.create_index('ix_trailer_otss_modification_matrix_board_height_id', 'trailer_otss_modification_matrix', ['board_height_id'])
    op.create_index('ix_trailer_otss_modification_matrix_special_option_id', 'trailer_otss_modification_matrix', ['special_option_id'])
    op.create_index('ix_trailer_otss_modification_matrix_otss_type', 'trailer_otss_modification_matrix', ['otss_type'])
    op.create_index('ix_trailer_otss_modification_matrix_is_active', 'trailer_otss_modification_matrix', ['is_active'])

    for table in (
        'trailer_platform_price_matrix', 'trailer_board_price_matrix', 'trailer_tent_price_matrix',
        'trailer_wheel_price_matrix', 'trailer_hub_price_matrix', 'trailer_support_wheel_price_matrix',
    ):
        op.create_index(f'ix_{table}_is_active', table, ['is_active'])
        op.create_index(f'ix_{table}_valid_from', table, ['valid_from'])
        op.create_index(f'ix_{table}_valid_to', table, ['valid_to'])


def downgrade():
    for table in (
        'trailer_otss_modification_matrix', 'trailer_dimension_matrix', 'trailer_support_wheel_price_matrix',
        'trailer_hub_price_matrix', 'trailer_wheel_price_matrix', 'trailer_tent_price_matrix',
        'trailer_board_price_matrix', 'trailer_platform_price_matrix', 'trailer_allowed_option',
        'trailer_special_option', 'trailer_body_execution', 'trailer_tent_option',
        'trailer_support_wheel_option', 'trailer_hub_option', 'trailer_wheel_option',
        'trailer_board_height', 'trailer_body_size', 'trailer_product_group',
    ):
        op.drop_table(table)
