from decimal import Decimal

from models import (
    TrailerAllowedOption,
    TrailerBoardHeight,
    TrailerBoardPriceMatrix,
    TrailerBodyExecution,
    TrailerBodySize,
    TrailerDimensionMatrix,
    TrailerHubOption,
    TrailerHubPriceMatrix,
    TrailerOtssModificationMatrix,
    TrailerPlatformPriceMatrix,
    TrailerProductGroup,
    TrailerSpecialOptionPriceMatrix,
    TrailerSpecialOption,
    TrailerSupportWheelOption,
    TrailerSupportWheelPriceMatrix,
    TrailerTentOption,
    TrailerTentPriceMatrix,
    TrailerWheelOption,
    TrailerWheelPriceMatrix,
)


OPTION_MODELS = {
    'body_size': TrailerBodySize,
    'board_height': TrailerBoardHeight,
    'wheel': TrailerWheelOption,
    'hub': TrailerHubOption,
    'support_wheel': TrailerSupportWheelOption,
    'tent': TrailerTentOption,
    'body_execution': TrailerBodyExecution,
    'special': TrailerSpecialOption,
}


def _by_code(model, code):
    if not code:
        return None
    return model.query.filter_by(code=code).first()


def _amount(value):
    return int(value or Decimal('0'))


def _lower_first(text):
    if not text:
        return ''
    return text[:1].lower() + text[1:]


def normalize_config(config):
    return {
        'group_code': config.get('group_code') or '',
        'body_size_code': config.get('body_size_code') or '',
        'board_height_code': config.get('board_height_code') or '',
        'wheel_code': config.get('wheel_code') or '',
        'hub_code': config.get('hub_code') or '',
        'support_wheel_code': config.get('support_wheel_code') or '',
        'tent_code': config.get('tent_code') or '',
        'body_execution_code': config.get('body_execution_code') or '',
        'special_options': [code for code in config.get('special_options', []) if code],
    }


def load_config_objects(config):
    config = normalize_config(config)
    objects = {
        'group': _by_code(TrailerProductGroup, config['group_code']),
        'body_size': _by_code(TrailerBodySize, config['body_size_code']),
        'board_height': _by_code(TrailerBoardHeight, config['board_height_code']),
        'wheel': _by_code(TrailerWheelOption, config['wheel_code']),
        'hub': _by_code(TrailerHubOption, config['hub_code']),
        'support_wheel': _by_code(TrailerSupportWheelOption, config['support_wheel_code']),
        'tent': _by_code(TrailerTentOption, config['tent_code']),
        'body_execution': _by_code(TrailerBodyExecution, config['body_execution_code']),
        'special_options': [
            option for option in (
                _by_code(TrailerSpecialOption, code) for code in config['special_options']
            ) if option is not None
        ],
    }
    return config, objects


def _is_allowed(group, option_type, option):
    if not group or not option:
        return False
    row = TrailerAllowedOption.query.filter_by(
        group_id=group.id,
        option_type=option_type,
        option_id=option.id,
        is_allowed=True,
    ).first()
    return row is not None


def _has_allowed_options(group, option_type):
    if not group:
        return False
    return TrailerAllowedOption.query.filter_by(
        group_id=group.id,
        option_type=option_type,
        is_allowed=True,
    ).first() is not None


def validate_trailer_config(config):
    config, objects = load_config_objects(config)
    errors = []
    group = objects['group']

    required = [
        ('group', 'Не выбрана группа'),
        ('body_size', 'Не выбран размер кузова'),
        ('board_height', 'Не выбрана высота борта'),
        ('body_execution', 'Не выбран тип кузова'),
    ]
    if _has_allowed_options(group, 'support_wheel'):
        required.append(('support_wheel', 'Не выбрано опорное колесо'))
    if _has_allowed_options(group, 'tent'):
        required.append(('tent', 'Не выбран тент'))
    for key, message in required:
        if not objects[key]:
            errors.append(message)

    if bool(config['wheel_code']) == bool(config['hub_code']):
        errors.append('Нужно выбрать либо колесо, либо ступицу')

    for option_type, key in (
        ('body_size', 'body_size'),
        ('board_height', 'board_height'),
        ('wheel', 'wheel'),
        ('hub', 'hub'),
        ('support_wheel', 'support_wheel'),
        ('tent', 'tent'),
        ('body_execution', 'body_execution'),
    ):
        option = objects[key]
        if option and group and not _is_allowed(group, option_type, option):
            errors.append(f'Параметр {option.code} запрещён для группы {group.code}')

    for special in objects['special_options']:
        if group and not _is_allowed(group, 'special', special):
            errors.append(f'Параметр {special.code} запрещён для группы {group.code}')
        if group and not (group.code or '').endswith('G') and special.code in ('SINGLE', 'DOUBLE'):
            errors.append('SINGLE / DOUBLE доступны только для грузовых групп G')

    special_by_type = {}
    for special in objects['special_options']:
        special_by_type.setdefault(special.option_type, []).append(special)
    for option_type, options in special_by_type.items():
        if len(options) > 1:
            if option_type == 'tire_layout':
                errors.append('Нельзя одновременно выбрать односкатный и двухскатный вариант')
            else:
                names = ', '.join(option.name for option in options)
                errors.append(f'Можно выбрать только один спец. параметр типа {option_type}: {names}')

    execution = objects['body_execution']
    board = objects['board_height']
    if execution and board:
        if execution.code == 'BOARD' and board.code not in ('E30', 'E50'):
            errors.append('Для BOARD высота борта должна быть E30 или E50')
        elif execution.code == 'PLATFORM' and board.code != 'E0':
            errors.append('Для PLATFORM высота борта должна быть E0')
        elif execution.code in ('VAN', 'TRAL', 'TRADE', 'LIVING', 'G') and board.code in ('E30', 'E50'):
            errors.append('Для выбранного типа кузова высота борта не применяется.')

    if _by_code(TrailerHubOption, 'R14'):
        errors.append('R14 не должен существовать как отдельная ступица')

    return errors


def build_trailer_article(config):
    config, objects = load_config_objects(config)
    group = objects['group']
    body_size = objects['body_size']
    board = objects['board_height']
    wheel_or_hub = objects['wheel'] or objects['hub']
    support = objects['support_wheel']
    tent = objects['tent']

    left = ''.join([
        body_size.article_part if body_size else config['body_size_code'],
        board.article_part if board else config['board_height_code'],
        wheel_or_hub.article_part if wheel_or_hub else '',
    ])
    right = ''.join([
        support.article_part if support and support.article_part else '',
        tent.article_part if tent and tent.article_part else '',
        ''.join(s.article_part or s.code for s in objects['special_options']),
    ])
    parts = []
    if group:
        parts.append(group.code)
    if left:
        parts.append(left)
    article = '-'.join(parts)
    if right:
        article = f'{article}-{right}' if article else right
    return article


def build_trailer_name(config):
    config, objects = load_config_objects(config)
    group = objects['group']
    body_size = objects['body_size']
    board = objects['board_height']
    wheel = objects['wheel']
    hub = objects['hub']
    support = objects['support_wheel']
    tent = objects['tent']
    execution = objects['body_execution']

    if group and execution and execution.code == 'PLATFORM':
        title = group.name_prefix.replace('Прицеп ', 'Прицеп-платформа ', 1)
    elif group:
        title = group.name_prefix
    else:
        title = 'Прицеп'

    parts = [title]
    if body_size:
        parts.append(body_size.name)
    if board and not board.is_no_board:
        parts.append(_lower_first(board.name))
    if wheel:
        parts.append(_lower_first(wheel.name))
    if hub:
        parts.append(_lower_first(hub.name))
    if support and support.code == 'OK':
        parts.append('с опорным колесом')
    if tent and not tent.is_no_tent:
        parts.append(_lower_first(tent.name))
    for special in objects['special_options']:
        parts.append(_lower_first(special.name))

    return parts[0] + (' ' + parts[1] if len(parts) > 1 else '') + (', ' + ', '.join(parts[2:]) if len(parts) > 2 else '')


def _latest_price(query):
    return query.filter_by(is_active=True).order_by(query.column_descriptions[0]['entity'].valid_from.desc().nullslast()).first()


def calculate_trailer_price(config):
    config, objects = load_config_objects(config)
    group = objects['group']
    body_size = objects['body_size']
    board = objects['board_height']
    wheel = objects['wheel']
    hub = objects['hub']
    support = objects['support_wheel']
    tent = objects['tent']
    missing = []
    breakdown = []

    def add_missing(message):
        if message not in missing:
            missing.append(message)

    if group and body_size:
        row = _latest_price(TrailerPlatformPriceMatrix.query.filter_by(group_id=group.id, body_size_id=body_size.id))
        if row:
            breakdown.append({'type': 'platform', 'name': f'Платформа {group.code} {body_size.name}', 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена платформы {group.code} для кузова {body_size.code}')

    if body_size and board:
        row = _latest_price(TrailerBoardPriceMatrix.query.filter_by(body_size_id=body_size.id, board_height_id=board.id))
        if row:
            breakdown.append({'type': 'board', 'name': f'{board.name} для кузова {body_size.name}', 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена борта {board.code} для кузова {body_size.code}')

    if group and wheel:
        row = _latest_price(TrailerWheelPriceMatrix.query.filter_by(group_id=group.id, wheel_option_id=wheel.id))
        if row:
            breakdown.append({'type': 'wheel', 'name': wheel.name, 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена колёс {wheel.code} для группы {group.code}')

    if group and hub:
        row = _latest_price(TrailerHubPriceMatrix.query.filter_by(group_id=group.id, hub_option_id=hub.id))
        if row:
            breakdown.append({'type': 'hub', 'name': hub.name, 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена ступицы {hub.code} для группы {group.code}')

    if support:
        row = _latest_price(TrailerSupportWheelPriceMatrix.query.filter_by(support_wheel_option_id=support.id))
        if row:
            breakdown.append({'type': 'support_wheel', 'name': support.name, 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена опорного колеса {support.code}')

    if body_size and tent:
        row = _latest_price(TrailerTentPriceMatrix.query.filter_by(body_size_id=body_size.id, tent_option_id=tent.id))
        if row:
            breakdown.append({'type': 'tent', 'name': f'{tent.name} для кузова {body_size.name}', 'amount': _amount(row.price)})
        else:
            add_missing(f'Не найдена цена тента {tent.code} для кузова {body_size.code}')

    for special in objects['special_options']:
        if group:
            row = _latest_price(TrailerSpecialOptionPriceMatrix.query.filter_by(group_id=group.id, special_option_id=special.id))
            if row:
                breakdown.append({'type': 'special', 'name': special.name, 'amount': _amount(row.price)})
            else:
                add_missing(f'Не найдена цена спец. параметра {special.code} для группы {group.code}')

    return {
        'total_price': sum(item['amount'] for item in breakdown),
        'price_breakdown': breakdown,
        'missing_components': missing,
        'is_complete': not missing,
    }


def get_trailer_dimensions(config):
    config, objects = load_config_objects(config)
    body_size = objects['body_size']
    board = objects['board_height']
    if not body_size or not board:
        return None
    row = TrailerDimensionMatrix.query.filter_by(
        body_size_id=body_size.id,
        board_height_id=board.id,
        is_active=True,
    ).first()
    if not row:
        return None
    return {
        'overall_length_mm': row.overall_length_mm,
        'overall_width_mm': row.overall_width_mm,
        'overall_height_mm': row.overall_height_mm,
        'inner_length_mm': row.inner_length_mm,
        'inner_width_mm': row.inner_width_mm,
        'inner_height_mm': row.inner_height_mm,
        'overall_dimensions_text': row.overall_dimensions_text,
        'inner_dimensions_text': row.inner_dimensions_text,
    }


def resolve_otss_modification(config):
    config, objects = load_config_objects(config)
    group = objects['group']
    execution = objects['body_execution']
    board = objects['board_height']
    special_ids = [special.id for special in objects['special_options']]

    if not group or not execution:
        return {'error': 'Не найдена ОТТС-модификация для выбранной конфигурации'}

    query = TrailerOtssModificationMatrix.query.filter_by(
        group_id=group.id,
        body_execution_id=execution.id,
        is_active=True,
    )

    rows = query.order_by(TrailerOtssModificationMatrix.sort_order).all()
    if board:
        board_rows = [row for row in rows if row.board_height_id == board.id]
        if board_rows:
            rows = board_rows
        else:
            rows = [row for row in rows if row.board_height_id is None]
    else:
        rows = [row for row in rows if row.board_height_id is None]

    for row in rows:
        if row.special_option_id and row.special_option_id not in special_ids:
            continue
        if not row.special_option_id and special_ids and (group.code or '').endswith('G'):
            continue
        return {
            'otss_number': row.otss_number or group.otss_number,
            'otss_type': row.otss_type or group.otss_type,
            'otss_modification': row.otss_modification,
            'vin_modification_code': row.vin_modification_code,
            'otts_valid_from': row.otts_valid_from or group.otts_valid_from,
            'otts_valid_to': row.otts_valid_to or group.otts_valid_to,
            'description': row.description,
        }

    return {'error': 'Не найдена ОТТС-модификация для выбранной конфигурации'}


def build_trailer_configuration_result(config):
    config, objects = load_config_objects(config)
    errors = validate_trailer_config(config)
    price = calculate_trailer_price(config)
    dimensions = get_trailer_dimensions(config)
    otss = resolve_otss_modification(config)
    warnings = []
    if dimensions is None:
        warnings.append('Не найдена строка габаритов')
    if otss.get('error'):
        errors.append(otss['error'])

    return {
        'config': config,
        'option_ids': {
            'group_id': objects['group'].id if objects['group'] else None,
            'body_size_id': objects['body_size'].id if objects['body_size'] else None,
            'body_execution_id': objects['body_execution'].id if objects['body_execution'] else None,
            'board_height_id': objects['board_height'].id if objects['board_height'] else None,
            'wheel_option_id': objects['wheel'].id if objects['wheel'] else None,
            'hub_option_id': objects['hub'].id if objects['hub'] else None,
            'support_wheel_option_id': objects['support_wheel'].id if objects['support_wheel'] else None,
            'tent_option_id': objects['tent'].id if objects['tent'] else None,
            'special_option_ids': [special.id for special in objects['special_options']],
        },
        'article': build_trailer_article(config),
        'name': build_trailer_name(config),
        'price': price,
        'dimensions': dimensions,
        'otss': otss,
        'errors': errors,
        'warnings': warnings,
    }
