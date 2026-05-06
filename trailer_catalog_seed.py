from datetime import date

from extensions import db
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


OTSS_INFO = {
    '002': dict(otss_number='ТС KZ E-KZ.0317.00251.П1', otss_type='01', otts_valid_from=date(2026, 2, 28), otts_valid_to=date(2029, 2, 27)),
    '004': dict(otss_number='ТС KZ E-KZ.0317.00219.П1', otss_type='02', otts_valid_from=date(2025, 6, 17), otts_valid_to=date(2028, 6, 16)),
    '004K': dict(otss_number='ТС KZ E-KZ.0317.00214.П1', otss_type='03', otts_valid_from=date(2025, 6, 17), otts_valid_to=date(2028, 6, 16)),
    '004G': dict(otss_number='ТС KZ E-KZ.0317.00437', otss_type='05', otts_valid_from=date(2025, 2, 26), otts_valid_to=date(2028, 2, 25)),
}

GROUPS = [
    dict(code='002', name='Легковой одноосный', name_prefix='Прицеп легковой одноосный', vehicle_category='O1', axle_count=1, wheel_count=2, max_mass_kg=750, comment='до 750 кг', sort_order=10, **OTSS_INFO['002']),
    dict(code='004', name='Легковой двухосный', name_prefix='Прицеп легковой двухосный', vehicle_category='O1', axle_count=2, wheel_count=4, max_mass_kg=750, comment='до 750 кг', sort_order=20, **OTSS_INFO['004']),
    dict(code='004K', name='Коммерческий двухосный', name_prefix='Прицеп коммерческий', vehicle_category='O2', axle_count=2, wheel_count=4, max_mass_kg=3500, comment='до 3,5 т', sort_order=30, **OTSS_INFO['004K']),
    dict(code='004G', name='Грузовой', name_prefix='Прицеп грузовой', vehicle_category='O4', axle_count=2, wheel_count=4, comment='грузовой', sort_order=40, **OTSS_INFO['004G']),
]

BODY_SIZES = [
    ('2013', '2,0 × 1,25 м', 2000, 1250),
    ('2513', '2,5 × 1,25 м', 2500, 1250),
    ('2515', '2,5 × 1,5 м', 2500, 1500),
    ('3015', '3,0 × 1,5 м', 3000, 1500),
    ('3515', '3,5 × 1,5 м', 3500, 1500),
    ('4015', '4,0 × 1,5 м', 4000, 1500),
]

BOARD_HEIGHTS = [
    ('E0', 'Без борта', 0, 'E0', True),
    ('E30', 'Борт 30 см', 300, 'E30', False),
    ('E50', 'Борт 50 см', 500, 'E50', False),
]

WHEELS = [
    ('Q13', 'Колёса R13', 'R13', 'Q13'),
    ('Q14', 'Колёса R14', 'R14', 'Q14'),
    ('Q15', 'Колёса R15', 'R15', 'Q15'),
]

HUBS = [
    ('R13', 'Ступица под R13/R14', 'R13/R14', 'R13'),
    ('R15', 'Ступица под R15/R16', 'R15/R16', 'R15'),
]

SUPPORT_WHEELS = [
    ('NOOK', 'Без опорного колеса', '', True),
    ('OK', 'С опорным колесом', 'OK', False),
]

TENTS = [
    ('NONE', 'Без тента', 0, '', True),
    ('30', 'Тент-каркас 300 мм', 300, '30', False),
    ('60', 'Тент-каркас 600 мм', 600, '60', False),
    ('90', 'Тент-каркас 900 мм', 900, '90', False),
    ('120', 'Тент-каркас 1200 мм', 1200, '120', False),
    ('150', 'Тент-каркас 1500 мм', 1500, '150', False),
]

EXECUTIONS = [
    ('BOARD', 'Бортовой', 'бортовой'),
    ('PLATFORM', 'Платформа без бортов', 'платформа'),
    ('VAN', 'Фургон', 'фургон'),
    ('TRAL', 'Трал', 'трал'),
    ('TRADE', 'Торговый фургон', 'торговый фургон'),
    ('LIVING', 'Жилой фургон', 'жилой фургон'),
    ('G', 'Грузовой', 'грузовой'),
]

SPECIALS = [
    ('SINGLE', 'Односкатный', 'tire_layout', 'SINGLE'),
    ('DOUBLE', 'Двухскатный', 'tire_layout', 'DOUBLE'),
]

PLATFORM_PRICES = [
    ('002', '2013', 260000), ('002', '2513', 300000), ('002', '2515', 360000),
    ('002', '3015', 410000), ('002', '3515', 460000), ('002', '4015', 510000),
    ('004', '2515', 490000), ('004', '3015', 530000), ('004', '3515', 580000), ('004', '4015', 680000),
]

BOARD_PRICES = [
    ('2013', 'E0', 0), ('2013', 'E30', 20000), ('2013', 'E50', 40000),
    ('2513', 'E0', 0), ('2513', 'E30', 40000), ('2513', 'E50', 50000),
    ('2515', 'E0', 0), ('2515', 'E30', 40000), ('2515', 'E50', 50000),
    ('3015', 'E0', 0), ('3015', 'E30', 50000), ('3015', 'E50', 60000),
    ('3515', 'E0', 0), ('3515', 'E30', 60000), ('3515', 'E50', 70000),
    ('4015', 'E0', 0), ('4015', 'E30', 70000), ('4015', 'E50', 80000),
]

TENT_PRICES = [
    ('2013', 'NONE', 0), ('2013', '30', 30000), ('2013', '60', 60000), ('2013', '90', 80000),
    ('2513', 'NONE', 0), ('2513', '30', 35000), ('2513', '60', 70000), ('2513', '90', 90000),
    ('2515', 'NONE', 0), ('2515', '90', 100000), ('2515', '120', 110000),
    ('3015', 'NONE', 0), ('3015', '90', 110000), ('3015', '120', 140000),
    ('3515', 'NONE', 0), ('3515', '120', 130000), ('3515', '150', 140000),
    ('4015', 'NONE', 0), ('4015', '120', 150000), ('4015', '150', 160000),
]

WHEEL_PRICES = [('002', 'Q13', 30000), ('002', 'Q14', 30000), ('002', 'Q15', 50000)]
HUB_PRICES = [('002', 'R13', 0), ('002', 'R15', 30000)]
SUPPORT_WHEEL_PRICES = [('NOOK', 0), ('OK', 10000)]
SPECIAL_OPTION_PRICES = [('004G', 'SINGLE', 0), ('004G', 'DOUBLE', 0)]

ALLOWED_DEFAULTS = {
    '002': {
        'body_size': {'2013', '2513', '2515', '3015', '3515', '4015'},
        'board_height': {'E0', 'E30', 'E50'},
        'wheel': {'Q13', 'Q14', 'Q15'},
        'hub': {'R13', 'R15'},
        'support_wheel': {'NOOK', 'OK'},
        'tent': {'NONE', '30', '60', '90', '120', '150'},
        'body_execution': {'BOARD', 'PLATFORM', 'VAN'},
        'special': set(),
    },
    '004': {
        'body_size': {'2515', '3015', '3515', '4015'},
        'board_height': {'E0', 'E30', 'E50'},
        'wheel': {'Q13', 'Q14', 'Q15'},
        'hub': {'R13', 'R15'},
        'support_wheel': {'NOOK', 'OK'},
        'tent': {'NONE', '30', '60', '90', '120', '150'},
        'body_execution': {'BOARD', 'PLATFORM'},
        'special': set(),
    },
    '004K': {
        'body_execution': {'BOARD', 'TRAL', 'TRADE', 'LIVING'},
        'special': set(),
    },
    '004G': {
        'body_execution': {'G'},
        'special': {'SINGLE', 'DOUBLE'},
    },
}

DIMENSIONS = [
    ('2013', 'E0', '4000x1700x650', '2000x1250x0'),
    ('2013', 'E30', '3150x1700x900', '2000x1250x300'),
    ('2013', 'E50', '3150x1700x1070', '2000x1250x500'),
    ('2513', 'E0', '4000x1700x650', '2500x1250x0'),
    ('2513', 'E30', '3650x1700x900', '2500x1250x300'),
    ('2513', 'E50', '3650x1700x1070', '2500x1250x500'),
    ('2515', 'E0', '4000x1950x650', '2500x1500x0'),
    ('2515', 'E30', '3650x1950x900', '2500x1500x300'),
    ('2515', 'E50', '3650x1950x1070', '2500x1500x500'),
    ('3015', 'E0', '4150x1950x650', '3000x1500x0'),
    ('3015', 'E30', '3800x1950x900', '3000x1500x300'),
    ('3015', 'E50', '3800x1950x1200', '3000x1500x500'),
    ('3515', 'E0', '4650x1950x650', '3500x1500x0'),
    ('3515', 'E30', '3800x1950x900', '3500x1500x300'),
    ('3515', 'E50', '3800x1950x1200', '3500x1500x500'),
    ('4015', 'E0', '5150x1950x650', '4000x1500x0'),
    ('4015', 'E30', '3800x1950x900', '4000x1500x300'),
    ('4015', 'E50', '3800x1950x1200', '4000x1500x500'),
]

OTSS_ROWS = [
    ('002', 'VAN', None, None, '01', '001', '000001', 'Кузов-фургон'),
    ('002', 'BOARD', 'E30', None, '01', '002', '000002', 'Бортовая платформа'),
    ('002', 'BOARD', 'E50', None, '01', '002', '000002', 'Бортовая платформа'),
    ('002', 'PLATFORM', 'E0', None, '01', '003', '000003', 'Платформа без бортов'),
    ('004', 'BOARD', 'E30', None, '02', '004', '000004', 'Бортовая платформа'),
    ('004', 'BOARD', 'E50', None, '02', '004', '000004', 'Бортовая платформа'),
    ('004', 'PLATFORM', 'E0', None, '02', '005', '000005', 'Платформа без бортов'),
    ('004K', 'BOARD', 'E30', None, '03', '006', '000006', 'Бортовая платформа'),
    ('004K', 'BOARD', 'E50', None, '03', '006', '000006', 'Бортовая платформа'),
    ('004K', 'TRAL', None, None, '03', '007', '000007', 'Трал'),
    ('004K', 'TRADE', None, None, '03', '008', '000008', 'Торговый фургон'),
    ('004K', 'LIVING', None, None, '03', '009', '000009', 'Жилой фургон'),
    ('004G', 'G', None, 'SINGLE', '05', '012', '000012', 'Грузовой односкатный'),
    ('004G', 'G', None, 'DOUBLE', '05', '013', '000013', 'Грузовой двухскатный'),
]


def _get_by_code(model, code):
    return model.query.filter_by(code=code).one()


def _upsert_code(model, code, **values):
    obj = model.query.filter_by(code=code).first()
    if obj is None:
        obj = model(code=code, **values)
        db.session.add(obj)
        return obj, True
    for key, value in values.items():
        setattr(obj, key, value)
    return obj, False


def _upsert_matrix(model, filters, values):
    obj = model.query.filter_by(**filters).first()
    if obj is None:
        obj = model(**filters, **values)
        db.session.add(obj)
        return obj, True
    for key, value in values.items():
        setattr(obj, key, value)
    return obj, False


def seed_trailer_catalog():
    created = 0

    for row in GROUPS:
        values = dict(row)
        code = values.pop('code')
        _, was_created = _upsert_code(TrailerProductGroup, code, **values)
        created += int(was_created)

    for idx, (code, name, length, width) in enumerate(BODY_SIZES, start=1):
        _, was_created = _upsert_code(TrailerBodySize, code, name=name, length_mm=length, width_mm=width, article_part=code, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, height, article_part, is_no_board) in enumerate(BOARD_HEIGHTS, start=1):
        _, was_created = _upsert_code(TrailerBoardHeight, code, name=name, height_mm=height, article_part=article_part, is_no_board=is_no_board, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, wheel_size, article_part) in enumerate(WHEELS, start=1):
        _, was_created = _upsert_code(TrailerWheelOption, code, name=name, wheel_size=wheel_size, article_part=article_part, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, for_wheel_size, article_part) in enumerate(HUBS, start=1):
        _, was_created = _upsert_code(TrailerHubOption, code, name=name, for_wheel_size=for_wheel_size, article_part=article_part, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, article_part, is_default) in enumerate(SUPPORT_WHEELS, start=1):
        _, was_created = _upsert_code(TrailerSupportWheelOption, code, name=name, article_part=article_part, is_default=is_default, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, height, article_part, is_no_tent) in enumerate(TENTS, start=1):
        _, was_created = _upsert_code(TrailerTentOption, code, name=name, height_mm=height, article_part=article_part, is_no_tent=is_no_tent, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, name_for_title) in enumerate(EXECUTIONS, start=1):
        _, was_created = _upsert_code(TrailerBodyExecution, code, name=name, name_for_title=name_for_title, sort_order=idx * 10)
        created += int(was_created)

    for idx, (code, name, option_type, article_part) in enumerate(SPECIALS, start=1):
        _, was_created = _upsert_code(TrailerSpecialOption, code, name=name, option_type=option_type, article_part=article_part, sort_order=idx * 10)
        created += int(was_created)

    db.session.flush()

    groups = {g.code: g for g in TrailerProductGroup.query.all()}
    sizes = {s.code: s for s in TrailerBodySize.query.all()}
    boards = {b.code: b for b in TrailerBoardHeight.query.all()}
    wheels = {w.code: w for w in TrailerWheelOption.query.all()}
    hubs = {h.code: h for h in TrailerHubOption.query.all()}
    supports = {s.code: s for s in TrailerSupportWheelOption.query.all()}
    tents = {t.code: t for t in TrailerTentOption.query.all()}
    executions = {e.code: e for e in TrailerBodyExecution.query.all()}
    specials = {s.code: s for s in TrailerSpecialOption.query.all()}

    for group in groups.values():
        for option_type, collection in (
            ('body_size', sizes.values()), ('board_height', boards.values()), ('wheel', wheels.values()),
            ('hub', hubs.values()), ('support_wheel', supports.values()), ('tent', tents.values()),
            ('body_execution', executions.values()), ('special', specials.values()),
        ):
            for option in collection:
                allowed_codes = ALLOWED_DEFAULTS.get(group.code, {}).get(option_type, set())
                allowed = option.code in allowed_codes
                _, was_created = _upsert_matrix(
                    TrailerAllowedOption,
                    dict(group_id=group.id, option_type=option_type, option_id=option.id),
                    dict(is_allowed=allowed, is_default=False, sort_order=getattr(option, 'sort_order', 0), comment='seed default'),
                )
                created += int(was_created)

    for group_code, size_code, price in PLATFORM_PRICES:
        _, was_created = _upsert_matrix(TrailerPlatformPriceMatrix, dict(group_id=groups[group_code].id, body_size_id=sizes[size_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for size_code, board_code, price in BOARD_PRICES:
        _, was_created = _upsert_matrix(TrailerBoardPriceMatrix, dict(body_size_id=sizes[size_code].id, board_height_id=boards[board_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for size_code, tent_code, price in TENT_PRICES:
        _, was_created = _upsert_matrix(TrailerTentPriceMatrix, dict(body_size_id=sizes[size_code].id, tent_option_id=tents[tent_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for group_code, wheel_code, price in WHEEL_PRICES:
        _, was_created = _upsert_matrix(TrailerWheelPriceMatrix, dict(group_id=groups[group_code].id, wheel_option_id=wheels[wheel_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for group_code, hub_code, price in HUB_PRICES:
        _, was_created = _upsert_matrix(TrailerHubPriceMatrix, dict(group_id=groups[group_code].id, hub_option_id=hubs[hub_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for support_code, price in SUPPORT_WHEEL_PRICES:
        _, was_created = _upsert_matrix(TrailerSupportWheelPriceMatrix, dict(support_wheel_option_id=supports[support_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for group_code, special_code, price in SPECIAL_OPTION_PRICES:
        _, was_created = _upsert_matrix(TrailerSpecialOptionPriceMatrix, dict(group_id=groups[group_code].id, special_option_id=specials[special_code].id, valid_from=None), dict(price=price, currency='KZT', is_active=True))
        created += int(was_created)

    for size_code, board_code, overall, inner in DIMENSIONS:
        overall_values = [int(part) for part in overall.split('x')]
        inner_values = [int(part) for part in inner.split('x')]
        _, was_created = _upsert_matrix(
            TrailerDimensionMatrix,
            dict(body_size_id=sizes[size_code].id, board_height_id=boards[board_code].id),
            dict(
                overall_length_mm=overall_values[0], overall_width_mm=overall_values[1], overall_height_mm=overall_values[2],
                inner_length_mm=inner_values[0], inner_width_mm=inner_values[1], inner_height_mm=inner_values[2],
                overall_dimensions_text=overall.replace('x', '×'), inner_dimensions_text=inner.replace('x', '×'), is_active=True,
            ),
        )
        created += int(was_created)

    for idx, (group_code, execution_code, board_code, special_code, otss_type, otss_mod, vin_code, description) in enumerate(OTSS_ROWS, start=1):
        filters = dict(
            group_id=groups[group_code].id,
            body_execution_id=executions[execution_code].id,
            board_height_id=boards[board_code].id if board_code else None,
            special_option_id=specials[special_code].id if special_code else None,
        )
        _, was_created = _upsert_matrix(
            TrailerOtssModificationMatrix,
            filters,
            dict(
                otss_number=OTSS_INFO[group_code]['otss_number'],
                otss_type=otss_type,
                otss_modification=otss_mod,
                vin_modification_code=vin_code,
                otts_valid_from=OTSS_INFO[group_code]['otts_valid_from'],
                otts_valid_to=OTSS_INFO[group_code]['otts_valid_to'],
                description=description,
                sort_order=idx * 10,
                is_active=True,
            ),
        )
        created += int(was_created)

    db.session.commit()
    return created
