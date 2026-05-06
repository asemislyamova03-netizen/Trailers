# app.py
from flask import Flask, redirect, url_for
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFProtect
import os

# Берём ОДИН общий db/migrate из extensions
from extensions import db, migrate
from dotenv import load_dotenv

login_manager = LoginManager()
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)
    basedir = os.path.abspath(os.path.dirname(__file__))

    # Настройки базы
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'trailers.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'dev-secret-key-change-me'

    # --- SIGEX settings (добавь) ---
    app.config['SIGEX_BASE_URL'] = os.getenv('SIGEX_BASE_URL', 'https://sigex.kz:10443')
    app.config['SIGEX_MTLS_CRT'] = os.getenv('SIGEX_MTLS_CRT')
    app.config['SIGEX_MTLS_KEY'] = os.getenv('SIGEX_MTLS_KEY')
    # ------------------------------
    csrf.init_app(app)


    # Инициализация расширений
    db.init_app(app)
    migrate.init_app(app, db)

    # Импорты внутри, чтобы избежать циклов
    from models import User
    from views import main_bp

    # --- Flask-Login ---
    login_manager.init_app(app)
    login_manager.login_view = 'main.login'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # current_user во всех шаблонах (base.html и др.)
    @app.context_processor
    def inject_current_user():
        return dict(current_user=current_user)

    @app.cli.command('seed_trailer_catalog')
    def seed_trailer_catalog_command():
        from trailer_catalog_seed import seed_trailer_catalog

        created = seed_trailer_catalog()
        print(f'Trailer catalog seed completed. Created {created} new records.')

    @app.cli.command('backfill_vin_registry')
    def backfill_vin_registry_command():
        import click
        from datetime import datetime, time
        from sqlalchemy import func
        from models import Trailer, CustomerOrder, SalesContract, SupplyNeed, VinRegistry, VinRegistryEvent

        def parse_vin(vin_full):
            vin = (vin_full or '').strip().upper()
            if len(vin) != 17 or not vin.startswith('MX4'):
                return None
            serial7 = vin[-7:]
            if not serial7.isdigit():
                return None
            return {
                'vin_full': vin,
                'prefix': vin[:3],
                'vin_modification_code': vin[3:9],
                'year_code': vin[9:10],
                'serial7': serial7,
            }

        def newest_contract_for_trailer(trailer_id):
            return (
                SalesContract.query
                .filter(SalesContract.trailer_id == trailer_id)
                .order_by(
                    SalesContract.contract_date.is_(None),
                    SalesContract.contract_date.desc(),
                    SalesContract.created_at.is_(None),
                    SalesContract.created_at.desc(),
                    SalesContract.id.desc(),
                )
                .first()
            )

        def newest_order_for_trailer(trailer_id, contract=None):
            if contract and contract.order_id:
                order = CustomerOrder.query.get(contract.order_id)
                if order:
                    return order
            return (
                CustomerOrder.query
                .filter(CustomerOrder.trailer_id == trailer_id)
                .order_by(CustomerOrder.documents_issued.desc(), CustomerOrder.created_at.desc(), CustomerOrder.id.desc())
                .first()
            )

        def latest_supply_need_for_order(order):
            if not order:
                return None
            return (
                SupplyNeed.query
                .filter(SupplyNeed.order_id == order.id)
                .order_by(SupplyNeed.created_at.desc(), SupplyNeed.id.desc())
                .first()
            )

        def as_datetime(value):
            if not value:
                return None
            if isinstance(value, datetime):
                return value
            return datetime.combine(value, time.min)

        def contract_label(contract):
            if not contract:
                return 'нет договора'
            number = contract.contract_number or contract.id
            date_text = contract.contract_date.isoformat() if contract.contract_date else 'без даты'
            return f'договор {number} от {date_text} (id={contract.id})'

        registry_dupes = (
            db.session.query(VinRegistry.serial7, func.count(VinRegistry.id))
            .group_by(VinRegistry.serial7)
            .having(func.count(VinRegistry.id) > 1)
            .all()
        )
        if registry_dupes:
            report = ', '.join(f'{serial}: {qty}' for serial, qty in registry_dupes[:20])
            raise click.ClickException(f'В VinRegistry найдены дубли serial7. Исправьте их перед backfill: {report}')

        rows_by_serial = {}
        structure_errors = []
        trailers = Trailer.query.filter(Trailer.vin.isnot(None), Trailer.vin != '').all()
        for trailer in trailers:
            parsed = parse_vin(trailer.vin)
            if not parsed:
                structure_errors.append((trailer, trailer.vin))
                continue
            contract = newest_contract_for_trailer(trailer.id)
            order = newest_order_for_trailer(trailer.id, contract)
            rows_by_serial.setdefault(parsed['serial7'], []).append({
                'trailer': trailer,
                'parsed': parsed,
                'contract': contract,
                'order': order,
            })

        conflict_serials = {serial: items for serial, items in rows_by_serial.items() if len(items) > 1}
        conflict_serial_set = set(conflict_serials)

        created = 0
        updated = 0
        skipped_existing = 0
        runtime_conflicts = []

        for serial7, items in rows_by_serial.items():
            if serial7 in conflict_serial_set:
                continue
            item = items[0]
            trailer = item['trailer']
            parsed = item['parsed']
            contract = item['contract']
            order = item['order']
            supply_need = latest_supply_need_for_order(order)

            row = VinRegistry.query.filter_by(serial7=serial7).first()
            if row and row.vin_full and row.vin_full != parsed['vin_full']:
                runtime_conflicts.append({
                    'serial7': serial7,
                    'existing_vin': row.vin_full,
                    'existing_trailer_id': row.trailer_id,
                    'new_vin': parsed['vin_full'],
                    'new_trailer_id': trailer.id,
                    'contract': contract,
                })
                continue

            old_status = row.status if row else None
            before = None
            if row:
                before = {
                    'vin_full': row.vin_full,
                    'prefix': row.prefix,
                    'vin_modification_code': row.vin_modification_code,
                    'year_code': row.year_code,
                    'serial7': row.serial7,
                    'status': row.status,
                    'customer_order_id': row.customer_order_id,
                    'supply_need_id': row.supply_need_id,
                    'trailer_id': row.trailer_id,
                    'sales_contract_id': row.sales_contract_id,
                    'docs_issued_order_id': row.docs_issued_order_id,
                    'docs_issued_at': row.docs_issued_at,
                    'source': row.source,
                    'comment': row.comment,
                }
            else:
                row = VinRegistry(serial7=serial7, created_at=datetime.utcnow())
                db.session.add(row)

            row.vin_full = parsed['vin_full']
            row.prefix = parsed['prefix']
            row.vin_modification_code = parsed['vin_modification_code']
            row.year_code = parsed['year_code']
            row.status = 'confirmed'
            row.trailer_id = trailer.id
            row.sales_contract_id = contract.id if contract else None
            row.customer_order_id = order.id if order else None
            row.supply_need_id = supply_need.id if supply_need else row.supply_need_id

            if order and order.documents_issued:
                row.docs_issued_order_id = order.id
                if order.documents_issued_at:
                    row.docs_issued_at = order.documents_issued_at
            elif contract and not order:
                row.docs_issued_at = as_datetime(contract.contract_date)

            row.source = 'backfill_existing_sales'
            if contract and not order:
                row.comment = 'Backfill из существующего договора'
            elif not row.comment:
                row.comment = 'Backfill существующей продажи с VIN'

            after = {
                'vin_full': row.vin_full,
                'prefix': row.prefix,
                'vin_modification_code': row.vin_modification_code,
                'year_code': row.year_code,
                'serial7': row.serial7,
                'status': row.status,
                'customer_order_id': row.customer_order_id,
                'supply_need_id': row.supply_need_id,
                'trailer_id': row.trailer_id,
                'sales_contract_id': row.sales_contract_id,
                'docs_issued_order_id': row.docs_issued_order_id,
                'docs_issued_at': row.docs_issued_at,
                'source': row.source,
                'comment': row.comment,
            }

            if before is None:
                created += 1
                event_type = 'created'
            elif before != after:
                updated += 1
                event_type = 'comment_added'
            else:
                skipped_existing += 1
                continue

            db.session.flush()
            db.session.add(VinRegistryEvent(
                vin_registry_id=row.id,
                event_type=event_type,
                old_status=old_status,
                new_status=row.status,
                customer_order_id=row.customer_order_id,
                sales_contract_id=row.sales_contract_id,
                trailer_id=row.trailer_id,
                comment='backfill_existing_sales',
                created_at=datetime.utcnow(),
            ))

        db.session.commit()

        if structure_errors:
            click.echo('Некорректные VIN у Trailer:')
            for trailer, vin in structure_errors[:50]:
                click.echo(f'  Trailer #{trailer.id}: {vin}')
            if len(structure_errors) > 50:
                click.echo(f'  ... ещё {len(structure_errors) - 50}')

        if conflict_serials:
            click.echo('В Trailer.vin найдены дубли serial7. Конфликтующие VIN пропущены:')
            for serial7, items in conflict_serials.items():
                click.echo(f'  serial7 {serial7}')
                for item in items:
                    trailer = item['trailer']
                    contract = item['contract']
                    click.echo(f'    VIN {item["parsed"]["vin_full"]}, Trailer #{trailer.id}, {contract_label(contract)}')

        if runtime_conflicts:
            click.echo('В VinRegistry найдены конфликтующие VIN для существующих serial7. Эти записи пропущены:')
            for conflict in runtime_conflicts:
                click.echo(
                    f'  serial7 {conflict["serial7"]}: '
                    f'{conflict["existing_vin"]} / Trailer #{conflict["existing_trailer_id"]} и '
                    f'{conflict["new_vin"]} / Trailer #{conflict["new_trailer_id"]}, {contract_label(conflict["contract"])}'
                )

        click.echo(f'Добавлено VIN: {created}')
        click.echo(f'Обновлено VIN: {updated}')
        click.echo(f'Пропущено уже существующих: {skipped_existing}')
        click.echo(f'Конфликты serial7: {len(conflict_serials) + len(runtime_conflicts)}')
        click.echo(f'Ошибки структуры VIN: {len(structure_errors)}')


    app.register_blueprint(main_bp)

    @app.route('/')
    def index():
        from flask import redirect, url_for
        from flask_login import current_user

        if current_user.is_authenticated and getattr(current_user, 'is_manager', False):
            return redirect(url_for('main.manager_workspace'))

        return redirect(url_for('main.trailers_list'))


    return app
