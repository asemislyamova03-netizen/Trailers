"""Единицы измерения для складского учёта ТМЦ."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# code, подпись в UI, знаков после запятой в остатках
INVENTORY_UNIT_SPECS: list[tuple[str, str, int]] = [
    ('шт', 'Штуки (шт)', 0),
    ('компл', 'Комплект (компл)', 0),
    ('упак', 'Упаковка (упак)', 0),
    ('кг', 'Килограммы (кг)', 3),
    ('т', 'Тонны (т)', 3),
    ('м', 'Метры (м)', 3),
    ('м²', 'Квадратные метры (м²)', 3),
    ('м³', 'Кубические метры (м³)', 3),
    ('л', 'Литры (л)', 3),
]

INVENTORY_UNIT_CODES: frozenset[str] = frozenset(code for code, _, _ in INVENTORY_UNIT_SPECS)

_UNIT_ALIASES: dict[str, str] = {
    'штука': 'шт',
    'штуки': 'шт',
    'шт.': 'шт',
    'pcs': 'шт',
    'pc': 'шт',
    'комплект': 'компл',
    'компл.': 'компл',
    'уп': 'упак',
    'уп.': 'упак',
    'килограмм': 'кг',
    'килограммы': 'кг',
    'kg': 'кг',
    'тонна': 'т',
    'тонны': 'т',
    't': 'т',
    'метр': 'м',
    'метры': 'м',
    'пог.м': 'м',
    'пог. м': 'м',
    'погонный метр': 'м',
    'm': 'м',
    'кв.м': 'м²',
    'кв. м': 'м²',
    'квм': 'м²',
    'm2': 'м²',
    'м2': 'м²',
    'куб.м': 'м³',
    'куб. м': 'м³',
    'm3': 'м³',
    'м3': 'м³',
    'литр': 'л',
    'литры': 'л',
    'l': 'л',
}

_DECIMALS: dict[str, int] = {code: decimals for code, _, decimals in INVENTORY_UNIT_SPECS}
_LABELS: dict[str, str] = {code: label for code, label, _ in INVENTORY_UNIT_SPECS}


def inventory_unit_choices(include_empty: bool = False) -> list[tuple[str, str]]:
    choices = [(code, label) for code, label, _ in INVENTORY_UNIT_SPECS]
    if include_empty:
        return [('', '— не выбрано —')] + choices
    return choices


def normalize_unit(value: str | None, *, default: str = 'шт') -> str:
    raw = (value or '').strip().lower().replace(' ', '')
    if not raw:
        return default
    if raw in INVENTORY_UNIT_CODES:
        return raw
    alias = _UNIT_ALIASES.get(raw) or _UNIT_ALIASES.get((value or '').strip().lower())
    if alias:
        return alias
    compact = (value or '').strip()
    if compact in INVENTORY_UNIT_CODES:
        return compact
    return default if default in INVENTORY_UNIT_CODES else 'шт'


def unit_decimal_places(unit: str | None) -> int:
    return _DECIMALS.get(normalize_unit(unit), 3)


def quantity_input_step(unit: str | None) -> str:
    places = unit_decimal_places(unit)
    if places <= 0:
        return '1'
    return f'0.{"0" * (places - 1)}1'


def format_inventory_quantity(value, unit: str | None = None) -> str:
    unit_code = normalize_unit(unit)
    places = unit_decimal_places(unit_code)
    try:
        number = Decimal(str(value or 0))
    except Exception:
        return f'0 {unit_code}'
    if places <= 0:
        quantized = number.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        text = f'{int(quantized)}'
    else:
        quant = Decimal('1').scaleb(-places)
        quantized = number.quantize(quant, rounding=ROUND_HALF_UP)
        text = f'{quantized:f}'.rstrip('0').rstrip('.')
    return f'{text} {unit_code}'


def units_match(left: str | None, right: str | None) -> bool:
    return normalize_unit(left) == normalize_unit(right)


def unit_label(unit: str | None) -> str:
    code = normalize_unit(unit)
    return _LABELS.get(code, code)
