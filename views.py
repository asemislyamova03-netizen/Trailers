# views.py
from functools import wraps

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, abort, make_response, jsonify, current_app, make_response,
)
from flask_login import (
    login_required, current_user,
    logout_user, login_user
)

from extensions import db
from models import Trailer, Item, Warehouse, Customer, SalesContract, User, OTTS
from forms import (
    TrailerCreateForm, WarehouseForm, ItemForm,
    CustomerForm, SalesContractForm, LoginForm, UserForm, OTTSForm
)
from collections import defaultdict
import sqlalchemy as sa
from sqlalchemy import or_
from wtforms import StringField, SelectField, DateField, BooleanField, DecimalField, IntegerField, TextAreaField, SubmitField

from flask_wtf import FlaskForm
from wtforms.validators import DataRequired, Optional, Length, NumberRange
try:
    from weasyprint import HTML  # опционально, может не загрузиться на Windows
except Exception as e:
    HTML = None
    print("WeasyPrint не доступен:", e)

from sqlalchemy.exc import IntegrityError

from sigex_client import sigex_post_json, sigex_get_json, sigex_post_octet
from pdf_utils import build_contract_pdf_bytes
main_bp = Blueprint('main', __name__)


# ========= ДЕКОРАТОРЫ =========

def admin_required(f):
    """
    Доступ только для админа.
    Сначала логин (через login_required), потом проверка роли.
    """
    @wraps(f)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            flash('Доступ запрещён', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return wrapped


# ========= АУТЕНТИФИКАЦИЯ =========

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    # если уже вошли
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('main.trailers_list'))
        else:
            return redirect(url_for('main.manager_workspace'))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Вы успешно вошли', 'success')

            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)

            # по умолчанию: админ -> прицепы, менеджер -> рабочее место
            if user.is_admin:
                return redirect(url_for('main.trailers_list'))
            else:
                return redirect(url_for('main.manager_workspace'))
        else:
            flash('Неверный логин или пароль', 'danger')

    return render_template('login.html', form=form)



@main_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('main.login'))

def _extract_modification_from_vin(vin: str | None) -> str | None:
    """
    Извлекаем код модификации из VIN:
    - берем 4–9 символы (индексы 3:9),
    - из них пытаемся получить последние 3 цифры (00001 -> 001, 00002 -> 002),
    - если не получилось, возвращаем как есть.
    """
    if not vin:
        return None

    vin = vin.strip()
    if len(vin) < 9:
        return None

    raw = vin[3:9]  # 4..9 знак
    raw = raw.strip()
    if not raw:
        return None

    # пробуем вытащить последние 3 цифры
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 3:
        return digits[-3:]  # '00001' -> '001', '00002' -> '002'

    # запасной вариант: привести к int и форматнуть
    try:
        return f"{int(raw):03d}"
    except ValueError:
        return raw

def _fill_trailer_form_choices(form: TrailerCreateForm) -> None:
    """Заполняем choices для склада и характеристик из таблицы Item."""
    # --- Склады ---
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(w.id, w.name) for w in warehouses]

    # --- Характеристики из номенклатуры (Item) ---
    items = Item.query.filter_by(item_type='TRAILER', is_active=True).all()

    size_body_values = sorted({i.size_body for i in items if i.size_body})
    axle_values = sorted({i.axle_count for i in items if i.axle_count is not None})
    radius_values = sorted({i.wheel_radius for i in items if i.wheel_radius})
    board_values = sorted({i.board_height_mm for i in items if i.board_height_mm is not None})

    form.size_body.choices = [(v, v) for v in size_body_values]
    form.axle_count.choices = [(v, str(v)) for v in axle_values]
    form.wheel_radius.choices = [(v, v) for v in radius_values]

    # для высоты борта добавим первую опцию "не выбрано"
    form.board_height_mm.choices = [('', '— не выбрано —')] + [
        (str(v), str(v)) for v in board_values
    ]

def _find_item_for_form(form: TrailerCreateForm):
    """
    Подбирает модель прицепа (Item) по значениям из формы.
    Возвращает (item, None) или (None, 'текст ошибки').
    """
    import sqlalchemy as sa

    q = Item.query.filter_by(item_type='TRAILER', is_active=True)

    if form.size_body.data:
        q = q.filter(Item.size_body == form.size_body.data)

    if form.axle_count.data:
        q = q.filter(Item.axle_count == form.axle_count.data)

    if form.wheel_radius.data:
        q = q.filter(Item.wheel_radius == form.wheel_radius.data)

    if form.board_height_mm.data:
        try:
            bh = int(form.board_height_mm.data)
            q = q.filter(Item.board_height_mm == bh)
        except ValueError:
            return None, 'Некорректное значение высоты борта.'

    # --- Подкатное колесо (обязательный выбор) ---
    # form.has_jockey_wheel.data -> 1 (есть) или 0 (нет)
    has_jw = form.has_jockey_wheel.data
    q = q.filter(Item.has_jockey_wheel == (has_jw == 1))

    # --- Высота тента по tent_height_mm, как мы уже делали ---
    tent_h = form.tent_height_mm.data  # int

    if tent_h == 0:
        q = q.filter(
            sa.or_(
                Item.has_tent == False,
                Item.tent_hight_mm.is_(None)
            )
        )
    else:
        q = q.filter(
            sa.and_(
                Item.has_tent == True,
                Item.tent_hight_mm == tent_h
            )
        )

    item = q.first()
    if not item:
        return None, 'Не удалось подобрать модель по указанным характеристикам. Проверьте матрицу.'

    return item, None


@main_bp.route('/workspace')
@login_required
def manager_workspace():
    if current_user.is_admin:
        return redirect(url_for('main.trailers_list'))

    if not current_user.warehouse_id:
        flash('За пользователем не закреплён склад. Обратитесь к администратору.', 'warning')
        return redirect(url_for('main.trailers_list'))

    # ----- СЛЕВА: Прицепы в наличии на складе менеджера -----
    free_trailers = (
        Trailer.query
        .join(Item, Item.id == Trailer.item_id)
        .filter(
            Trailer.warehouse_id == current_user.warehouse_id,
            Trailer.status != 'SOLD'
        )
        .order_by(Item.article, Trailer.vin)
        .all()
    )

    grouped_trailers = defaultdict(list)
    for t in free_trailers:
        article = t.item.article if t.item and t.item.article else 'Без артикула'
        grouped_trailers[article].append(t)

    # ----- СПРАВА: Договоры по ВСЕЙ организации (без фильтра по складу) -----
    unpaid_contracts = (
        SalesContract.query
        .join(Trailer, SalesContract.trailer_id == Trailer.id)
        .join(Item, Item.id == Trailer.item_id)
        .filter(SalesContract.is_paid == False)
        .order_by(SalesContract.contract_date.desc().nullslast(), SalesContract.id.desc())
        .all()
    )

    paid_not_shipped = (
        SalesContract.query
        .join(Trailer, SalesContract.trailer_id == Trailer.id)
        .join(Item, Item.id == Trailer.item_id)
        .filter(
            SalesContract.is_paid == True,
            SalesContract.is_shipped == False
        )
        .order_by(SalesContract.contract_date.desc().nullslast(), SalesContract.id.desc())
        .all()
    )

    # ----- ВНИЗУ: Другие склады и прицепы -----
    warehouses = Warehouse.query.order_by(Warehouse.name).all()

    other_wh_id = request.args.get('other_wh', type=int)

    # по умолчанию выбираем первый склад НЕ равный складу менеджера
    if not other_wh_id:
        for w in warehouses:
            if w.id != current_user.warehouse_id:
                other_wh_id = w.id
                break

    other_trailers = []
    if other_wh_id:
        other_trailers = (
            Trailer.query
            .join(Item, Item.id == Trailer.item_id)
            .filter(
                Trailer.warehouse_id == other_wh_id,
                Trailer.status != 'SOLD'
            )
            .order_by(Item.article, Trailer.vin)
            .all()
        )

    return render_template(
        'manager_workspace.html',
        grouped_trailers=grouped_trailers,
        free_trailers=free_trailers,
        unpaid_contracts=unpaid_contracts,
        paid_not_shipped=paid_not_shipped,
        warehouses=warehouses,
        other_wh_id=other_wh_id,
        other_trailers=other_trailers,
    )


# ========= ПОЛЬЗОВАТЕЛИ (только админ) =========

@main_bp.route('/users')
@admin_required
def users_list():
    users = User.query.order_by(User.username).all()
    return render_template('users_list.html', users=users)


@main_bp.route('/users/new', methods=['GET', 'POST'])
@admin_required
def user_create():
    form = UserForm()
    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не привязан —')] + [
        (w.id, w.name) for w in warehouses
    ]

    if form.validate_on_submit():
        user = User(
            username=form.username.data.strip(),
            full_name=form.full_name.data.strip(),
            role=form.role.data,
            warehouse_id=form.warehouse_id.data or None,
        )

        if form.password.data:
            user.set_password(form.password.data)
        else:
            flash('Пароль обязателен при создании пользователя', 'danger')
            return render_template('user_form.html', form=form, title='Новый пользователь')

        db.session.add(user)
        db.session.commit()
        flash('Пользователь создан', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('user_form.html', form=form, title='Новый пользователь')


@main_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    form = UserForm()

    warehouses = Warehouse.query.order_by(Warehouse.name).all()
    form.warehouse_id.choices = [(0, '— не привязан —')] + [
        (w.id, w.name) for w in warehouses
    ]

    if request.method == 'GET':
        form.username.data = user.username
        form.full_name.data = user.full_name
        form.role.data = user.role
        form.warehouse_id.data = user.warehouse_id or 0

    if form.validate_on_submit():
        user.username = form.username.data.strip()
        user.full_name = form.full_name.data.strip()
        user.role = form.role.data
        user.warehouse_id = form.warehouse_id.data or None

        if form.password.data:
            user.set_password(form.password.data)

        db.session.commit()
        flash('Пользователь обновлён', 'success')
        return redirect(url_for('main.users_list'))

    return render_template('user_form.html', form=form, title='Редактирование пользователя')


@main_bp.route('/users/<int:user_id>/delete')
@admin_required
def user_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.username == 'admin':
        flash('Нельзя удалить главного администратора', 'danger')
        return redirect(url_for('main.users_list'))

    db.session.delete(user)
    db.session.commit()
    flash('Пользователь удалён', 'success')
    return redirect(url_for('main.users_list'))


# ========= ПРИЦЕПЫ =========
def _fill_trailer_form_choices(form: TrailerCreateForm):
    """Заполнить choices для склада и характеристик из номенклатуры Item."""
    # --- Склады ---
    form.warehouse_id.choices = [
        (w.id, w.name) for w in Warehouse.query.order_by(Warehouse.name).all()
    ]

    # --- Все активные модели прицепов ---
    items = Item.query.filter_by(item_type='TRAILER', is_active=True).all()

    size_body_values = sorted({i.size_body for i in items if i.size_body})
    axle_values      = sorted({i.axle_count for i in items if i.axle_count is not None})
    wheel_values     = sorted({i.wheel_radius for i in items if i.wheel_radius})
    board_values     = sorted({i.board_height_mm for i in items if i.board_height_mm is not None})
    tent_heights     = sorted({i.tent_hight_mm for i in items if i.tent_hight_mm is not None})

    # --- Размер кузова ---
    form.size_body.choices = [(v, v) for v in size_body_values]

    # --- Кол-во осей ---
    form.axle_count.choices = [(v, str(v)) for v in axle_values]

    # --- Размер колеса ---
    form.wheel_radius.choices = [(v, v) for v in wheel_values]

    # --- Высота борта ---
    form.board_height_mm.choices = [(str(v), str(v)) for v in board_values]

    # --- Высота тента ---
    # 0 = нет тента, остальные из матрицы (30, 60...)
    form.tent_height_mm.choices = [(0, 'Нет тента')] + [
        (int(v), f'{v} см') for v in tent_heights
    ]

    # По умолчанию пусть будет "нет тента"
    if form.tent_height_mm.data is None:
        form.tent_height_mm.data = 0

    # Подкатное колесо: по умолчанию "не важно"
    if form.has_jockey_wheel.data is None:
        form.has_jockey_wheel.data = 1   # по умолчанию "да"


@main_bp.route('/trailers')
@login_required
def trailers_list():
    """Список всех прицепов с фильтрами."""
    vin_filter = request.args.get('vin', '').strip()
    article_filter = request.args.get('article', '').strip()
    status_filter = request.args.get('status', 'all')
    warehouse_id = request.args.get('warehouse_id', type=int)

    query = Trailer.query.join(Item).join(Warehouse)

    # менеджер видит только свой склад
    if getattr(current_user, 'is_manager', False) and current_user.warehouse_id:
        query = query.filter(Trailer.warehouse_id == current_user.warehouse_id)

    if vin_filter:
        query = query.filter(Trailer.vin.ilike(f'%{vin_filter}%'))

    if article_filter:
        query = query.filter(Item.article.ilike(f'%{article_filter}%'))

    if status_filter and status_filter != 'all':
        query = query.filter(Trailer.status == status_filter)

    if warehouse_id:
        query = query.filter(Trailer.warehouse_id == warehouse_id)

    trailers = query.order_by(Trailer.id.desc()).all()
    warehouses = Warehouse.query.order_by(Warehouse.name).all()

    return render_template(
        'trailers_list.html',
        trailers=trailers,
        warehouses=warehouses,
        vin_filter=vin_filter,
        article_filter=article_filter,
        status_filter=status_filter,
        warehouse_filter=warehouse_id,
    )



@main_bp.route('/trailers/new', methods=['GET', 'POST'])
@login_required
def trailer_create():
    form = TrailerCreateForm()
    _fill_trailer_form_choices(form)

    if form.validate_on_submit():
        item, error = _find_item_for_form(form)
        if error:
            flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Новый прицеп')

        trailer = Trailer(
            vin=form.vin.data.strip(),
            item_id=item.id,
            warehouse_id=form.warehouse_id.data,
            manufacture_date=form.manufacture_date.data,
            status=form.status.data,
        )
        db.session.add(trailer)
        db.session.commit()
        flash('Прицеп создан', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Новый прицеп')


@main_bp.route('/trailers/<int:trailer_id>/edit', methods=['GET', 'POST'])
@login_required
def trailer_edit(trailer_id):
    trailer = Trailer.query.get_or_404(trailer_id)

    form = TrailerCreateForm(
        vin=trailer.vin,
        warehouse_id=trailer.warehouse_id,
        manufacture_date=trailer.manufacture_date,
        status=trailer.status,
    )
    _fill_trailer_form_choices(form)

    # при GET заполняем характеристики из текущего Item
    if request.method == 'GET' and trailer.item:
        form.size_body.data = trailer.item.size_body
        form.axle_count.data = trailer.item.axle_count
        form.wheel_radius.data = trailer.item.wheel_radius
        form.board_height_mm.data = (
            str(trailer.item.board_height_mm)
            if trailer.item.board_height_mm is not None else ''
        )

        # Высота тента
        if trailer.item.has_tent:
            form.tent_height_mm.data = trailer.item.tent_hight_mm or 0
        else:
            form.tent_height_mm.data = 0

        # Подкатное колесо: строго да / нет
        if trailer.item.has_jockey_wheel is True:
            form.has_jockey_wheel.data = 1
        elif trailer.item.has_jockey_wheel is False:
            form.has_jockey_wheel.data = 0
        else:
            # если в номенклатуре None — реши, как удобнее:
            form.has_jockey_wheel.data = 1  # например, по умолчанию "Да"



    if form.validate_on_submit():
        item, error = _find_item_for_form(form)
        if error:
            flash(error, 'danger')
            return render_template('trailer_form.html', form=form, title='Редактирование прицепа')

        trailer.vin = form.vin.data.strip()
        trailer.warehouse_id = form.warehouse_id.data
        trailer.manufacture_date = form.manufacture_date.data
        trailer.status = form.status.data
        trailer.item_id = item.id

        db.session.commit()
        flash('Прицеп обновлён', 'success')
        return redirect(url_for('main.trailers_list'))

    return render_template('trailer_form.html', form=form, title='Редактирование прицепа')


@main_bp.route('/trailers/<int:trailer_id>/delete')
@login_required
def trailer_delete(trailer_id):
    trailer = Trailer.query.get_or_404(trailer_id)

    if getattr(current_user, 'is_manager', False) and trailer.warehouse_id != current_user.warehouse_id:
        flash('Нет доступа к этому прицепу', 'danger')
        return redirect(url_for('main.trailers_list'))

    # если есть договоры — не даём удалить
    if trailer.sales_contracts:
        flash('Нельзя удалить прицеп, по которому есть договоры', 'danger')
    else:
        db.session.delete(trailer)
        db.session.commit()
        flash('Прицеп удалён', 'success')

    return redirect(url_for('main.trailers_list'))


# ========= СКЛАДЫ (только админ) =========

@main_bp.route('/warehouses', methods=['GET', 'POST'])
@admin_required
def warehouses_list():
    """Список складов + форма добавления нового."""
    form = WarehouseForm()

    if form.validate_on_submit():
        name = form.name.data.strip()

        existing = Warehouse.query.filter_by(name=name).first()
        if existing:
            flash('Склад с таким названием уже существует', 'danger')
        else:
            wh = Warehouse(
                name=name,
                is_active=form.is_active.data,
            )
            db.session.add(wh)
            db.session.commit()
            flash('Склад успешно добавлен', 'success')

        return redirect(url_for('main.warehouses_list'))

    warehouses = (
        Warehouse.query
        .order_by(Warehouse.is_active.desc(), Warehouse.name)
        .all()
    )

    return render_template('warehouses.html', form=form, warehouses=warehouses)

# ========= ОТТС =========

@main_bp.route('/otts')
@login_required
def otts_list():
    otts_list = (
        OTTS.query
        .order_by(OTTS.modification, OTTS.axle_count)
        .all()
    )
    return render_template(
        'otts_list.html',
        otts_list=otts_list,
        title='Справочник ОТТС'
    )


@main_bp.route('/otts/new', methods=['GET', 'POST'])
@login_required
def otts_create():
    form = OTTSForm()

    if form.validate_on_submit():
        otts = OTTS(
            number=form.number.data.strip(),
            date=form.date.data,
            modification=form.modification.data.strip(),
            name=form.name.data.strip(),
            axle_count=form.axle_count.data,
            full_mass_kg=form.full_mass_kg.data,
            is_active=form.is_active.data,
        )
        db.session.add(otts)
        db.session.commit()
        flash('Запись ОТТС создана', 'success')
        return redirect(url_for('main.otts_list'))

    return render_template(
        'otts_form.html',
        form=form,
        title='Новое ОТТС'
    )


@main_bp.route('/otts/<int:otts_id>/edit', methods=['GET', 'POST'])
@login_required
def otts_edit(otts_id):
    otts = OTTS.query.get_or_404(otts_id)
    form = OTTSForm(obj=otts)

    if form.validate_on_submit():
        # Заполняем объект из формы
        form.populate_obj(otts)

        # Чуть подчистим строки
        otts.number = (otts.number or '').strip()
        otts.modification = (otts.modification or '').strip()
        otts.name = (otts.name or '').strip()

        db.session.commit()
        flash('Запись ОТТС обновлена', 'success')
        return redirect(url_for('main.otts_list'))

    return render_template(
        'otts_form.html',
        form=form,
        title='Редактирование ОТТС'
    )


@main_bp.route('/otts/<int:otts_id>/delete')
@admin_required
def otts_delete(otts_id):
    otts = OTTS.query.get_or_404(otts_id)
    db.session.delete(otts)
    db.session.commit()
    flash('Запись ОТТС удалена', 'success')
    return redirect(url_for('main.otts_list'))

def find_trailer_item_by_features(
    axle_count: int | None = None,
    board_height_mm: int | None = None,
    wheel_radius: str | None = None,
    has_tent: bool | None = None,
    tent_hight_mm: int | None = None,   # имя как в модели
    has_jockey_wheel: bool | None = None,
):
    """
    Ищет модель прицепа (Item) по набору характеристик.
    Артикул сам по себе не вводим — он берётся из найденного Item.
    """
    q = Item.query.filter_by(item_type='TRAILER', is_active=True)

    if axle_count is not None:
        q = q.filter(Item.axle_count == axle_count)

    if board_height_mm is not None:
        q = q.filter(Item.board_height_mm == board_height_mm)

    if wheel_radius:
        q = q.filter(Item.wheel_radius == wheel_radius)

    if has_tent is not None:
        q = q.filter(Item.has_tent == has_tent)

    if tent_hight_mm is not None:
        q = q.filter(Item.tent_hight_mm == tent_hight_mm)

    if has_jockey_wheel is not None:
        q = q.filter(Item.has_jockey_wheel == has_jockey_wheel)

    return q.first()   # можно потом усложнить (если несколько совпадений)

# ========= НОМЕНКЛАТУРА =========

@main_bp.route('/items')
@login_required
def items_list():
    """Список номенклатуры (прицепы + комплектующие)."""
    items = (
        Item.query
        .order_by(Item.item_type, Item.article, Item.name)
        .all()
    )
    return render_template('items_list.html', items=items)


@main_bp.route('/items/new', methods=['GET', 'POST'])
@login_required
def item_create():
    """Создание новой позиции номенклатуры."""
    form = ItemForm()

    if form.validate_on_submit():
        item_type = form.item_type.data
        article = form.article.data.strip() if form.article.data else None

        # Для прицепов артикул обязателен
        if item_type == 'TRAILER' and not article:
            flash('Для прицепа обязательно укажите артикул', 'danger')
            return render_template('item_form.html', form=form)

        # Проверка уникальности (тип + артикул)
        if article:
            existing = Item.query.filter_by(article=article, item_type=item_type).first()
            if existing:
                flash('Такая позиция номенклатуры уже существует (артикул + тип)', 'danger')
                return render_template('item_form.html', form=form)

        # --- Тент: главный источник истины — высота тента ---
        tent_h = form.tent_height_mm.data  # может быть None или int

        if tent_h in (None, 0):
            has_tent = False
            tent_value = None
        else:
            has_tent = True
            tent_value = tent_h

        # Подкатное колесо: yes/no -> True/False/None
        has_jockey_wheel = None
        if form.has_jockey_wheel.data == 'yes':
            has_jockey_wheel = True
        elif form.has_jockey_wheel.data == 'no':
            has_jockey_wheel = False

        item = Item(
            item_type=item_type,
            article=article,
            name=form.name.data.strip(),
            axle_count=form.axle_count.data,
            board_height_mm=form.board_height_mm.data,
            wheel_radius=form.wheel_radius.data.strip() if form.wheel_radius.data else None,
            has_tent=has_tent,
            tent_hight_mm=tent_value,  # <--- ключевой момент
            has_jockey_wheel=has_jockey_wheel,
            size_external=form.size_external.data.strip() if form.size_external.data else None,
            size_body=form.size_body.data.strip() if form.size_body.data else None,
            base_price=form.base_price.data,
            unit=form.unit.data.strip() if form.unit.data else 'шт',
            is_active=form.is_active.data,
        )
        db.session.add(item)
        db.session.commit()

        flash('Позиция номенклатуры успешно добавлена', 'success')
        return redirect(url_for('main.items_list'))

    return render_template('item_form.html', form=form)



@main_bp.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def item_edit(item_id):
    item = Item.query.get_or_404(item_id)
    form = ItemForm()

    if request.method == 'GET':
        form.item_type.data = item.item_type
        form.article.data = item.article
        form.name.data = item.name
        form.axle_count.data = item.axle_count
        form.board_height_mm.data = item.board_height_mm
        form.wheel_radius.data = item.wheel_radius

        form.has_tent.data = (
            'yes' if item.has_tent is True else
            'no' if item.has_tent is False else
            ''
        )

        # Высота тента в форму
        form.tent_height_mm.data = item.tent_hight_mm or 0

        form.has_jockey_wheel.data = (
            'yes' if item.has_jockey_wheel is True else
            'no' if item.has_jockey_wheel is False else
            ''
        )

        form.size_external.data = item.size_external
        form.size_body.data = item.size_body
        form.base_price.data = item.base_price
        form.unit.data = item.unit
        form.is_active.data = item.is_active

    if form.validate_on_submit():
        item_type = form.item_type.data
        article = form.article.data.strip() if form.article.data else None

        if item_type == 'TRAILER' and not article:
            flash('Для прицепа обязательно укажите артикул', 'danger')
            return render_template('item_form.html', form=form, title='Редактирование номенклатуры')

        # Проверка уникальности при изменении типа/артикула
        if article and (article != item.article or item_type != item.item_type):
            existing = Item.query.filter_by(article=article, item_type=item_type).first()
            if existing and existing.id != item.id:
                flash('Такая позиция номенклатуры уже существует (артикул + тип)', 'danger')
                return render_template('item_form.html', form=form, title='Редактирование номенклатуры')

        # --- Тент: снова опираемся только на высоту ---
        tent_h = form.tent_height_mm.data

        if tent_h in (None, 0):
            item.has_tent = False
            item.tent_hight_mm = None
        else:
            item.has_tent = True
            item.tent_hight_mm = tent_h

        # Подкатное колесо
        has_jockey_wheel = None
        if form.has_jockey_wheel.data == 'yes':
            has_jockey_wheel = True
        elif form.has_jockey_wheel.data == 'no':
            has_jockey_wheel = False

        item.item_type = item_type
        item.article = article
        item.name = form.name.data.strip()
        item.axle_count = form.axle_count.data
        item.board_height_mm = form.board_height_mm.data
        item.wheel_radius = form.wheel_radius.data.strip() if form.wheel_radius.data else None
        item.has_jockey_wheel = has_jockey_wheel
        item.size_external = form.size_external.data.strip() if form.size_external.data else None
        item.size_body = form.size_body.data.strip() if form.size_body.data else None
        item.base_price = form.base_price.data
        item.unit = form.unit.data.strip() if form.unit.data else 'шт'
        item.is_active = form.is_active.data

        db.session.commit()
        flash('Позиция номенклатуры обновлена', 'success')
        return redirect(url_for('main.items_list'))

    return render_template('item_form.html', form=form, title='Редактирование номенклатуры')



@main_bp.route('/items/<int:item_id>/delete')
@login_required
def item_delete(item_id):
    item = Item.query.get_or_404(item_id)

    # если к позиции привязаны прицепы — лучше не удалять
    if item.trailers:  # relationship Trailer.item
        flash('Нельзя удалить номенклатуру, к которой привязаны прицепы', 'danger')
        return redirect(url_for('main.items_list'))

    db.session.delete(item)
    db.session.commit()
    flash('Позиция номенклатуры удалена', 'success')
    return redirect(url_for('main.items_list'))


# ========= КЛИЕНТЫ =========

@main_bp.route('/customers', methods=['GET'])
@login_required
def customers_list():
    """Список клиентов с простым поиском."""
    search = request.args.get('q', '').strip()

    query = Customer.query
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Customer.name.ilike(like),
                Customer.contact_person.ilike(like),
                Customer.iin_bin.ilike(like),
                Customer.phone.ilike(like),
            )
        )

    customers = (
        query
        .order_by(Customer.customer_type, Customer.name)
        .all()
    )

    return render_template(
        'customers.html',
        customers=customers,
        search=search,
    )

@main_bp.route('/customers/new', methods=['GET', 'POST'])
@login_required
def customer_create():
    form = CustomerForm()
    if form.validate_on_submit():
        customer = Customer(
            customer_type=form.customer_type.data,   # если в БД строковый Enum
            name=form.name.data.strip(),
            contact_person=form.contact_person.data or None,
            iin_bin=form.iin_bin.data or None,
            phone=form.phone.data or None,
            email=form.email.data or None,
            address=form.address.data or None,
            is_active=form.is_active.data,
            doc_type=form.doc_type.data or None,
            doc_number=form.doc_number.data or None,
            doc_issue_date=form.doc_issue_date.data,
            doc_issuer=form.doc_issuer.data or None,
        )
        db.session.add(customer)
        db.session.commit()
        flash('Клиент создан', 'success')
        return redirect(url_for('main.customers_list'))

    return render_template('customer_form.html', form=form, title='Новый клиент')


@main_bp.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def customer_edit(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    form = CustomerForm(obj=customer)

    if form.validate_on_submit():
        form.populate_obj(customer)
        db.session.commit()
        flash('Клиент обновлён', 'success')
        return redirect(url_for('main.customers_list'))

    return render_template('customer_form.html', form=form, title='Редактирование клиента')

@main_bp.route('/customers/<int:customer_id>/delete')
@login_required
def customer_delete(customer_id):
    customer = Customer.query.get_or_404(customer_id)

    # если есть договоры — не даём удалить
    if customer.sales_contracts:
        flash('Нельзя удалить клиента, по которому есть договоры', 'danger')
    else:
        db.session.delete(customer)
        db.session.commit()
        flash('Клиент удалён', 'success')

    return redirect(url_for('main.customers_list'))

# ========= ВСПОМОГАТЕЛЬНОЕ: НОМЕР ДОГОВОРА =========

def is_contract_number_unique(number: str, exclude_id: int | None = None) -> bool:
    if not number:
        return True
    q = SalesContract.query.filter(SalesContract.contract_number == number)
    if exclude_id:
        q = q.filter(SalesContract.id != exclude_id)
    return q.count() == 0

def _norm_str(s: str | None) -> str | None:
    s = (s or '').strip()
    return s or None


def get_next_contract_number() -> str:
    """
    Простой автонумератор: берём все заполненные contract_number,
    вытаскиваем цифры, max + 1.
    ⚠️ Не защищает от гонок на 100%, поэтому на commit ловим IntegrityError.
    """
    numbers = []

    rows = (
        SalesContract.query
        .with_entities(SalesContract.contract_number)
        .filter(SalesContract.contract_number.isnot(None), SalesContract.contract_number != '')
        .all()
    )

    for (cn,) in rows:
        s = ''.join(ch for ch in str(cn) if ch.isdigit())
        if not s:
            continue
        try:
            numbers.append(int(s))
        except ValueError:
            continue

    return str(max(numbers) + 1) if numbers else '1'


# ========= ДОГОВОРЫ / ПРОДАЖИ =========

def _build_contract_context(contract_id: int) -> dict:
    contract = SalesContract.query.get_or_404(contract_id)
    customer = contract.customer
    trailer = contract.trailer
    item = trailer.item if trailer else None

    size_external = getattr(item, 'size_external', None)
    size_body     = getattr(item, 'size_body', None)
    axle_count    = getattr(item, 'axle_count', None)
    payload       = getattr(item, 'payload_kg', None)
    full_mass_kg  = getattr(item, 'full_mass_kg', None)

    modification_code = _extract_modification_from_vin(trailer.vin) if trailer else None

    otts = None
    if modification_code:
        otts = OTTS.query.filter_by(modification=modification_code).first()

    if not full_mass_kg and otts and otts.full_mass_kg:
        full_mass_kg = otts.full_mass_kg

    if axle_count is None and otts and otts.axle_count is not None:
        axle_count = otts.axle_count

    return dict(
        contract=contract,
        customer=customer,
        trailer=trailer,
        item=item,
        otts=otts,
        modification_code=modification_code,
        size_external=size_external,
        size_body=size_body,
        axle_count=axle_count,
        payload=payload,
        full_mass_kg=full_mass_kg,
    )


@main_bp.route('/contracts')
@login_required
def contracts_list():
    """Список договоров / продаж. Менеджеры видят ВСЕ договоры."""
    query = SalesContract.query

    # если хочешь фильтр по складу — сделай параметром:
    warehouse_id = request.args.get('warehouse_id', type=int)
    if warehouse_id:
        query = query.join(Trailer, SalesContract.trailer_id == Trailer.id) \
                     .filter(Trailer.warehouse_id == warehouse_id)

    contracts = (
        query
        .order_by(SalesContract.contract_date.desc().nullslast(), SalesContract.id.desc())
        .all()
    )
    return render_template('contracts_list.html', contracts=contracts)


@main_bp.route('/contracts/new', methods=['GET', 'POST'])
@login_required
def contract_create():
    form = SalesContractForm()

    # ----- Клиенты -----
    customers = (
        Customer.query
        .filter_by(is_active=True)
        .order_by(Customer.customer_type, Customer.name)
        .all()
    )
    form.customer_id.choices = [
        (c.id, f"{'ФЛ' if c.customer_type == 'PERSON' else 'ЮЛ'} — {c.name}")
        for c in customers
    ]

    # ----- Прицепы (только не SOLD) -----
    trailers = (
        Trailer.query
        .filter(Trailer.status != 'SOLD')
        .order_by(Trailer.vin)
        .all()
    )
    form.trailer_id.choices = [
        (t.id, f"{t.vin} — {t.item.article if t.item else ''}")
        for t in trailers
    ]

    # Подставляем номер при открытии формы
    if request.method == 'GET' and not (form.contract_number.data or '').strip():
        form.contract_number.data = get_next_contract_number()

    if form.validate_on_submit():
        trailer = Trailer.query.get(form.trailer_id.data)
        if not trailer:
            flash('Прицеп не найден', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        # защита: на прицеп не должно быть договора (уникальность trailer_id в БД)
        exists = SalesContract.query.filter(SalesContract.trailer_id == trailer.id).first()
        if exists:
            flash('На этот прицеп уже существует договор.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        if trailer.status == 'SOLD':
            flash('Этот прицеп уже продан', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        # нормализуем номер
        cn = _norm_str(form.contract_number.data)

        # если пустой — генерим
        if not cn:
            cn = get_next_contract_number()
            form.contract_number.data = cn

        # проверка уникальности номера (в коде!)
        if not is_contract_number_unique(cn):
            form.contract_number.errors.append('Такой номер договора уже существует. Введите другой.')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        contract = SalesContract(
            contract_number=cn,
            contract_date=form.contract_date.data,
            customer_id=form.customer_id.data,
            trailer_id=trailer.id,
            price=form.price.data,
            payment_method=_norm_str(form.payment_method.data),
            source='manual',
            is_paid=bool(form.is_paid.data),
            is_shipped=bool(form.is_shipped.data),
        )

        trailer.status = 'SOLD'
        db.session.add(contract)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # обычно это конфликт uq_sales_contract_trailer (кто-то успел создать договор)
            flash('Конфликт сохранения (прицеп уже занят другим договором). Обнови страницу и попробуй снова.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Новый договор')

        flash('Договор успешно создан, прицеп помечен как "Продан"', 'success')
        return redirect(url_for('main.contracts_list'))

    return render_template('contract_form.html', form=form, form_title='Новый договор')



@main_bp.route('/contracts/<int:contract_id>/edit', methods=['GET', 'POST'])
@login_required
def contract_edit(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    form = SalesContractForm()

    # ----- Клиенты -----
    customers = (
        Customer.query
        .filter_by(is_active=True)
        .order_by(Customer.customer_type, Customer.name)
        .all()
    )
    form.customer_id.choices = [
        (c.id, f"{'ФЛ' if c.customer_type == 'PERSON' else 'ЮЛ'} — {c.name}")
        for c in customers
    ]

    # ----- Прицепы: для редактирования можно показать все -----
    trailers = Trailer.query.order_by(Trailer.vin).all()
    form.trailer_id.choices = [
        (t.id, f"{t.vin} — {t.item.article if t.item else ''}")
        for t in trailers
    ]

    if request.method == 'GET':
        form.contract_date.data = contract.contract_date
        form.contract_number.data = contract.contract_number or ''
        if contract.customer_id:
            form.customer_id.data = contract.customer_id
        if contract.trailer_id:
            form.trailer_id.data = contract.trailer_id
        form.price.data = float(contract.price) if contract.price is not None else None
        form.payment_method.data = contract.payment_method or ''
        form.is_paid.data = contract.is_paid
        form.is_shipped.data = contract.is_shipped

    if form.validate_on_submit():
        old_trailer = contract.trailer
        new_trailer = Trailer.query.get(form.trailer_id.data)

        if not new_trailer:
            flash('Прицеп не найден', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # защита: на новом прицепе не должно быть другого договора
        exists_other = (
            SalesContract.query
            .filter(SalesContract.trailer_id == new_trailer.id, SalesContract.id != contract.id)
            .first()
        )
        if exists_other:
            flash('На выбранный прицеп уже существует другой договор.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # проверка уникальности номера (в коде)
        cn = _norm_str(form.contract_number.data)
        if cn and not is_contract_number_unique(cn, exclude_id=contract.id):
            form.contract_number.errors.append('Такой номер договора уже существует. Введите другой.')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        # если поменяли прицеп — старый вернуть в IN_STOCK (если других договоров нет)
        if old_trailer and old_trailer.id != new_trailer.id:
            other_cnt = (
                SalesContract.query
                .filter(SalesContract.trailer_id == old_trailer.id, SalesContract.id != contract.id)
                .count()
            )
            if other_cnt == 0:
                old_trailer.status = 'IN_STOCK'

        # новый прицеп помечаем проданным
        new_trailer.status = 'SOLD'

        contract.contract_number = cn
        contract.contract_date = form.contract_date.data
        contract.customer_id = form.customer_id.data
        contract.trailer_id = new_trailer.id
        contract.price = form.price.data
        contract.payment_method = _norm_str(form.payment_method.data)
        contract.is_paid = bool(form.is_paid.data)
        contract.is_shipped = bool(form.is_shipped.data)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Конфликт сохранения (номер/прицеп). Проверь данные и попробуй снова.', 'danger')
            return render_template('contract_form.html', form=form, form_title='Редактирование договора')

        flash('Договор обновлён', 'success')
        return redirect(url_for('main.contracts_list'))

    return render_template('contract_form.html', form=form, form_title='Редактирование договора')



@main_bp.route('/contracts/<int:contract_id>/delete', methods=['POST'])
@login_required
def contract_delete(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    trailer = contract.trailer

    db.session.delete(contract)
    db.session.flush()

    if trailer:
        other_cnt = SalesContract.query.filter_by(trailer_id=trailer.id).count()
        if other_cnt == 0:
            trailer.status = 'IN_STOCK'

    db.session.commit()
    flash('Договор удалён', 'success')
    return redirect(url_for('main.contracts_list'))


@main_bp.route('/contracts/<int:contract_id>/print')
@login_required
def contract_print(contract_id):
    ctx = _build_contract_context(contract_id)
    return render_template('contract_print.html', **ctx)


@main_bp.route('/contracts/<int:contract_id>/pdf')
@login_required
def contract_pdf(contract_id):
    if HTML is not None:
        from flask import make_response, request
        ctx = _build_contract_context(contract_id)
        html = render_template('contract_print.html', **ctx)
        pdf = HTML(string=html, base_url=request.host_url).write_pdf()

        contract = ctx['contract']
        filename = f"contract_{contract.contract_number or contract.id}.pdf"

        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'inline; filename={filename}'
        return response

    return redirect(url_for('main.contract_print', contract_id=contract_id))


def _contract_pdf_bytes(contract_id: int) -> bytes:
    # ВАЖНО: для SIGEX нужно именно bytes документа.
    # Тут подразумевается, что WeasyPrint установлен и HTML != None как у тебя.
    ctx = _build_contract_context(contract_id)
    html = render_template('contract_print.html', **ctx)

    if HTML is None:
        raise RuntimeError("WeasyPrint (HTML) is not available. Install/configure WeasyPrint on server.")

    pdf = HTML(string=html, base_url=request.host_url).write_pdf()
    return pdf

def _sigex_title_for_contract(contract) -> str:
    num = contract.contract_number or contract.id
    return f"Договор_{num}.pdf"

@main_bp.route('/contracts/<int:contract_id>/sign')
@login_required
def contract_sign(contract_id):
    """
    Страница подписи:
      1) менеджер подписывает ЭЦП организации (через NCALayer на своём ПК)
      2) показываем QR для клиента
      3) после done — даём кнопку "Открыть карточку (DDC)"
    """
    ctx = _build_contract_context(contract_id)
    contract = ctx["contract"]
    return render_template("contract_sign.html", contract=contract)

@main_bp.route('/contracts/<int:contract_id>/sigex/pdf_base64')
@login_required
def contract_sigex_pdf_base64(contract_id):
    pdf_bytes = _contract_pdf_bytes(contract_id)
    return jsonify({"pdfBase64": base64.b64encode(pdf_bytes).decode("utf-8")})

@main_bp.route('/contracts/<int:contract_id>/sigex/preregister', methods=['POST'])
@login_required
def contract_sigex_preregister(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)

    # если уже есть documentId — просто вернём
    if contract.sigex_document_id:
        return jsonify({"documentId": contract.sigex_document_id})

    # предрегистрация без подписи возможна только при mTLS :contentReference[oaicite:3]{index=3}
    title = _sigex_title_for_contract(contract)

    payload = {
        "title": title,
        "description": f"Договор №{contract.contract_number or contract.id}",
        "settings": {
            "private": False,
            "signaturesLimit": 2,
            "switchToPrivateAfterLimitReached": True,
            # чтобы QR-подпись работала: документ должен быть в tempStorage или архиве :contentReference[oaicite:4]{index=4}
            "tempStorageAfterRegistration": 86400000,  # 24 часа
        },
    }

    reg = sigex_post_json("/api", payload)
    document_id = reg["documentId"]

    # завершить регистрацию нужно передачей тела документа :contentReference[oaicite:5]{index=5}
    pdf_bytes = _contract_pdf_bytes(contract_id)
    sigex_post_octet(f"/api/{document_id}/data", pdf_bytes)

    contract.sigex_document_id = document_id
    db.session.commit()

    return jsonify({"documentId": document_id})

@main_bp.route('/contracts/<int:contract_id>/sigex/add_org_signature', methods=['POST'])
@login_required
def contract_sigex_add_org_signature(contract_id):
    """
    Сюда фронт пришлёт CMS подпись (base64) от NCALayer.
    """
    contract = SalesContract.query.get_or_404(contract_id)

    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    data = request.get_json(silent=True) or {}
    signature = data.get("signature")
    sign_type = data.get("signType", "cms")

    if not signature:
        abort(400, "signature is required")

    # добавление подписи к документу :contentReference[oaicite:6]{index=6}
    res = sigex_post_json(f"/api/{contract.sigex_document_id}", {
        "signType": sign_type,
        "signature": signature,
    })

    contract.sigex_last_sign_id = res.get("signId")
    contract.sigex_last_status = "org_signed"
    db.session.commit()

    return jsonify({"ok": True, "signId": res.get("signId")})

@main_bp.route('/contracts/<int:contract_id>/sigex/start_qr', methods=['POST'])
@login_required
def contract_sigex_start_qr(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    payload = {
        "description": f"Подпишите договор №{contract.contract_number or contract.id}",
        "meta": [
            {"name": "Номер договора", "value": str(contract.contract_number or contract.id)},
            {"name": "Сумма", "value": str(contract.price or "")},
        ],
    }

    # инициировать процедуру QR-подписи :contentReference[oaicite:7]{index=7}
    res = sigex_post_json(f"/api/{contract.sigex_document_id}/egovQr", payload)

    contract.sigex_operation_id = res["operationId"]
    contract.sigex_last_status = "qr_started"
    db.session.commit()

    return jsonify(res)

@main_bp.route('/contracts/<int:contract_id>/sigex/qr_status')
@login_required
def contract_sigex_qr_status(contract_id):
    contract = SalesContract.query.get_or_404(contract_id)
    if not (contract.sigex_document_id and contract.sigex_operation_id):
        abort(400, "No active operation")

    # получить статус процедуры :contentReference[oaicite:8]{index=8}
    res = sigex_get_json(f"/api/{contract.sigex_document_id}/egovOperation/{contract.sigex_operation_id}")

    status = res.get("status")
    contract.sigex_last_status = status
    if status == "done":
        contract.sigex_last_sign_id = res.get("signId")
    db.session.commit()

    return jsonify(res)

@main_bp.route('/contracts/<int:contract_id>/sigex/ddc')
@login_required
def contract_sigex_ddc(contract_id):
    """
    Карточка электронного документа (DDC) — можно дать ссылку менеджеру.
    """
    contract = SalesContract.query.get_or_404(contract_id)
    if not contract.sigex_document_id:
        abort(400, "SIGEX document not preregistered")

    # buildDDC :contentReference[oaicite:9]{index=9}
    params = {
        "fileName": _sigex_title_for_contract(contract),
        "withoutDocumentVisualization": "false",
        "withoutSignaturesVisualization": "false",
        "withoutQRCodesInSignaturesVisualization": "false",
        "withoutID": "false",
        "qrWithIDLink": "false",
        "withLabelVerified": "true",
        "language": "ru",
    }

    res = sigex_post_json(f"/api/{contract.sigex_document_id}/buildDDC", payload={}, params=params)

    ddc_b64 = res["ddc"]
    ddc_bytes = base64.b64decode(ddc_b64)

    filename = f"ddc_{contract.contract_number or contract.id}.pdf"
    response = make_response(ddc_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
