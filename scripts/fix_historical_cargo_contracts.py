import argparse
from decimal import Decimal

from app import create_app
from extensions import db
from models import SalesContract, User, Warehouse


TARGETS = [
    {'contract_number': '1991', 'contract_date': '2026-02-06', 'price': Decimal('18000000')},
    {'contract_number': '2026', 'contract_date': '2026-03-03', 'price': Decimal('18800000')},
    {'contract_number': '2136', 'contract_date': '2026-04-29', 'price': Decimal('14000000')},
    {'contract_number': '2164', 'contract_date': '2026-05-13', 'price': Decimal('8500000')},
]


def _find_one(model, query_text, label):
    text = (query_text or '').strip().lower()
    rows = model.query.all()
    matches = []
    for row in rows:
        haystack = ' '.join(
            str(value or '')
            for value in (
                getattr(row, 'name', None),
                getattr(row, 'full_name', None),
                getattr(row, 'username', None),
                getattr(row, 'code', None),
            )
        ).lower()
        if text in haystack:
            matches.append(row)
    if len(matches) != 1:
        print(f'Найдено {len(matches)} вариантов для {label!r} по запросу {query_text!r}:')
        for row in matches:
            print(f'  id={row.id} name={getattr(row, "name", None) or getattr(row, "full_name", None) or getattr(row, "username", None)}')
        raise SystemExit(f'Уточните --{label}-query.')
    return matches[0]


def _contract_matches(contract, target):
    if (contract.contract_number or '').strip() == target['contract_number']:
        return True
    if not contract.contract_date:
        return False
    if contract.contract_date.isoformat() != target['contract_date']:
        return False
    return Decimal(str(contract.price or 0)).quantize(Decimal('1')) == target['price']


def main():
    parser = argparse.ArgumentParser(description='Set cargo warehouse and manager on selected historical contracts.')
    parser.add_argument('--warehouse-query', default='груз', help='Substring for cargo trailers warehouse.')
    parser.add_argument('--user-query', default='Кальман Игор', help='Substring for responsible manager user.')
    parser.add_argument('--apply', action='store_true', help='Apply changes. Without this flag only prints dry-run.')
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        warehouse = _find_one(Warehouse, args.warehouse_query, 'warehouse')
        user = _find_one(User, args.user_query, 'user')

        contracts = SalesContract.query.order_by(SalesContract.contract_date, SalesContract.id).all()
        target_contracts = [
            contract for contract in contracts
            if any(_contract_matches(contract, target) for target in TARGETS)
        ]
        print(f'Склад для отчетов: id={warehouse.id} {warehouse.name}')
        print(f'Ответственный для отчетов: id={user.id} {user.full_name or user.username}')
        print(f'Найдено договоров: {len(target_contracts)}')

        for contract in target_contracts:
            print(
                f'contract id={contract.id} №{contract.contract_number} date={contract.contract_date} '
                f'price={contract.price} order_id={contract.order_id} '
                f'trailer={contract.trailer.vin if contract.trailer else None}'
            )
            print(
                f'  before: contract.warehouse_id={contract.warehouse_id}, contract.assigned_user_id={contract.assigned_user_id}, '
                f'report warehouse={contract.warehouse.name if contract.warehouse else None}, '
                f'report manager={contract.assigned_user.full_name if contract.assigned_user else None}'
            )
            if args.apply:
                contract.warehouse_id = warehouse.id
                contract.assigned_user_id = user.id

        if args.apply:
            db.session.commit()
            print('Изменения сохранены.')
        else:
            db.session.rollback()
            print('DRY RUN: изменения не сохранены. Для применения запустите с --apply.')


if __name__ == '__main__':
    main()
