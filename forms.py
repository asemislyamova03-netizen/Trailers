# forms.py
from flask_wtf import FlaskForm
from uuid import uuid4
from wtforms import (
    StringField, PasswordField, BooleanField, SubmitField,
    SelectField, DecimalField, IntegerField, TextAreaField, DateField, HiddenField
)
from wtforms.validators import DataRequired, Optional, Length, NumberRange, Email, input_required
from wtforms import ValidationError
from models import SalesContract, CustomerOrder
from inventory_units import inventory_unit_choices


class IdempotentFlaskForm(FlaskForm):
    form_token = HiddenField(default=lambda: uuid4().hex)

# -------- Аутентификация --------

class LoginForm(FlaskForm):
    username = StringField(
        'Логин',
        validators=[DataRequired(), Length(max=64)]
    )
    password = PasswordField(
        'Пароль',
        validators=[DataRequired(), Length(max=128)]
    )
    submit = SubmitField('Войти')


# -------- Пользователи --------

class UserForm(IdempotentFlaskForm):
    username = StringField(
        'Логин',
        validators=[DataRequired(), Length(max=64)]
    )
    full_name = StringField(
        'ФИО',
        validators=[Optional(), Length(max=128)]
    )
    role = SelectField(
        'Роль',
        choices=[
            ('admin', 'Администратор'),
            ('director', 'Директор'),
            ('manager', 'Менеджер'),
            ('production', 'Производство'),
            ('logistics', 'Логистика'),
            ('viewer', 'Просмотр'),
        ],
        validators=[DataRequired()]
    )
    # 0 = не привязан, реально вьюха подставляет (0, "— не привязан —") + склады
    warehouse_id = SelectField(
        'Склад',
        coerce=int,
        validators=[Optional()]
    )
    # при создании мы дополнительно руками проверяем, что пароль не пустой
    password = PasswordField(
        'Пароль',
        validators=[Optional(), Length(min=4, max=128)]
    )
    submit = SubmitField('Сохранить')


# -------- Склады --------

class WarehouseForm(IdempotentFlaskForm):
    name = StringField(
        'Название склада',
        validators=[DataRequired(), Length(max=128)]
    )
    is_active = BooleanField('Активен', default=True)
    is_production = BooleanField('Производственный склад / склад выпуска', default=False)
    warehouse_kind = SelectField(
        'Тип склада',
        choices=[
            ('finished_goods', 'Готовая продукция / продажи'),
            ('assembly', 'Сборка / производственный учет'),
            ('raw_materials', 'Сырьё / металлопрокат'),
            ('service', 'Сервисный'),
            ('other', 'Другое'),
        ],
        default='finished_goods',
        validators=[DataRequired()],
    )
    can_sell = BooleanField('Можно продавать с этого склада', default=True)
    can_ship_to_customer = BooleanField('Можно отгружать клиенту', default=True)
    primary_product_category = SelectField('Основная категория', coerce=str, validators=[Optional()])
    submit = SubmitField('Сохранить')


# -------- Номенклатура (прицепы + комплектующие) --------

class ItemForm(IdempotentFlaskForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unit.choices = inventory_unit_choices()

    item_type = SelectField(
        'Тип позиции',
        choices=[
            ('TRAILER', 'Прицеп'),
            ('COMPONENT', 'Комплектующее'),
        ],
        validators=[DataRequired()]
    )
    product_category_id = SelectField('Категория номенклатуры', coerce=int, validators=[Optional()])
    is_sellable = BooleanField('Можно продавать отдельно', default=True)
    requires_vin = BooleanField('Требует VIN', default=False)
    is_internal_bom_item = BooleanField('Полуфабрикат / заготовка (учёт ТМЦ)', default=False)
    article = StringField(
        'Артикул',
        validators=[Optional(), Length(max=64)]
    )
    name = StringField(
        'Наименование',
        validators=[DataRequired(), Length(max=255)]
    )

    axle_count = IntegerField(
        'Количество осей',
        validators=[Optional(), NumberRange(min=0)]
    )
    board_height_mm = IntegerField(
        'Высота борта, мм',
        validators=[Optional(), NumberRange(min=0)]
    )
    wheel_radius = StringField(
        'Размер колеса (радиус)',
        validators=[Optional(), Length(max=32)]
    )

    has_tent = SelectField(
        'Тент',
        choices=[
            ('yes', 'Есть тент'),
            ('no', 'Нет тента'),
        ],
        validators=[Optional()]
    )

        # НОВОЕ: высота тента
    tent_height_mm = IntegerField('Высота тента, мм', validators=[Optional()])

    # НОВОЕ: подкатное колесо
    has_jockey_wheel = SelectField(
        'Подкатное колесо',
        choices=[
            ('yes', 'Есть'),
            ('no', 'Нет'),
        ],
        validators=[Optional()]
    )

    size_external = StringField(
        'Габариты внешние',
        validators=[Optional(), Length(max=128)]
    )
    size_body = StringField(
        'Габариты кузова',
        validators=[Optional(), Length(max=128)]
    )

    base_price = DecimalField(
        'Базовая цена',
        places=2,
        validators=[Optional(), NumberRange(min=0)]
    )
    unit = SelectField(
        'Ед. изм.',
        choices=[],
        default='шт',
        validators=[DataRequired()],
    )

    is_active = BooleanField('Активен', default=True)

    submit = SubmitField('Сохранить')


# -------- Прицепы (конкретные единицы с VIN) --------


class TrailerCreateForm(IdempotentFlaskForm):
    vin = StringField('VIN', validators=[DataRequired()])
    warehouse_id = SelectField('Склад', coerce=int, validators=[DataRequired()])
    manufacture_date = DateField('Дата выпуска', format='%Y-%m-%d', validators=[Optional()])
    status = SelectField(
        'Статус',
        choices=[
            ('IN_STOCK', 'В наличии'),
            ('SOLD', 'Продан')
        ],
        validators=[DataRequired()]
    )

    # Характеристики для подбора модели
    size_body = SelectField('Размер кузова', choices=[], validators=[DataRequired()])
    axle_count = SelectField('Количество осей', choices=[], coerce=int, validators=[DataRequired()])
    wheel_radius = SelectField('Размер колеса', choices=[], validators=[DataRequired()])
    board_height_mm = SelectField('Высота борта, мм', choices=[], validators=[DataRequired()])

    # НОВОЕ: только высота тента, без отдельного "есть/нет"
    tent_height_mm = SelectField(
        'Высота тента',
        choices=[],          # заполним в _fill_trailer_form_choices
        coerce=int,
        validators=[Optional()]
    )

    # Подкатное колесо: тоже как фильтр (есть/нет/любой)
    has_jockey_wheel = SelectField(
        'Подкатное колесо',
        choices=[
            (1, 'Есть'),
            (0, 'Нет')
        ],
        coerce=int,
        validators=[DataRequired()]
    )

    submit = SubmitField('Сохранить')


class TrailerItemChangeForm(IdempotentFlaskForm):
    size_body = SelectField('Размер кузова', choices=[], validators=[DataRequired()])
    axle_count = SelectField('Количество осей', choices=[], coerce=int, validators=[DataRequired()])
    wheel_radius = SelectField('Размер колеса', choices=[], validators=[DataRequired()])
    board_height_mm = SelectField('Высота борта, мм', choices=[], validators=[DataRequired()])
    tent_height_mm = SelectField('Высота тента', choices=[], coerce=int, validators=[Optional()])
    has_jockey_wheel = SelectField(
        'Подкатное колесо',
        choices=[(1, 'Есть'), (0, 'Нет')],
        coerce=int,
        validators=[DataRequired()]
    )
    submit = SubmitField('Сохранить комплектацию')

# -------- Клиенты --------

class CustomerForm(IdempotentFlaskForm):
    customer_type = SelectField(
        'Тип клиента',
        choices=[
            ('PERSON', 'Физическое лицо'),
            ('COMPANY', 'Юридическое лицо / ИП / КХ'),
        ],
        validators=[DataRequired()]
    )

    name = StringField('ФИО / Название', validators=[DataRequired(), Length(max=255)])
    contact_person = StringField('Контактное лицо', validators=[Optional(), Length(max=255)])
    iin_bin = StringField('ИИН / БИН', validators=[Optional(), Length(max=20)])

    # Документ (актуально для физлиц)
    doc_type = SelectField(
        'Документ',
        choices=[
            ('', '—'),
            ('ID_RK', 'Удостоверение личности РК'),
            ('RESIDENCE_PERMIT', 'Вид на жительство РК'),
            ('PASSPORT_FOREIGN', 'Паспорт иностранного гражданина'),
        ],
        default='',
        validators=[Optional()]
    )
    doc_number = StringField('Номер документа', validators=[Optional(), Length(max=50)])
    doc_issue_date = DateField('Дата выдачи', format='%Y-%m-%d', validators=[Optional()])
    doc_issuer = StringField('Кем выдан', validators=[Optional(), Length(max=255)])

    phone = StringField('Телефон', validators=[Optional(), Length(max=50)])
    email = StringField('Email', validators=[Optional(), Length(max=120)])
    address = StringField('Адрес', validators=[Optional(), Length(max=255)])

    # --- ОПФ (только для COMPANY) ---
    opf = SelectField(
        'ОПФ',
        choices=[
            ('', '—'),
            ('ТОО', 'ТОО'),
            ('ИП', 'ИП'),
            ('КХ', 'КХ'),
        ],
        default='',
        validators=[Optional()]
    )

    # Реквизиты (для COMPANY)
    bank_account = StringField('Расчётный счёт (IBAN)', validators=[Optional(), Length(max=34)])
    bank_name = StringField('Банк (наименование)', validators=[Optional(), Length(max=255)])
    bank_bic = StringField('БИК банка', validators=[Optional(), Length(max=20)])

    # Руководитель (для COMPANY, кроме ИП)
    director_position = StringField('Должность руководителя', validators=[Optional(), Length(max=100)])
    director_fio = StringField('ФИО руководителя', validators=[Optional(), Length(max=255)])

    is_active = BooleanField('Активен', default=True)
    submit = SubmitField('Сохранить')

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False

        ctype = (self.customer_type.data or '').strip()

        # PERSON: чистим COMPANY-поля
        if ctype == 'PERSON':
            self.opf.data = ''
            self.bank_account.data = ''
            self.bank_name.data = ''
            self.bank_bic.data = ''
            self.director_position.data = ''
            self.director_fio.data = ''
            return True

        # COMPANY: обязательные реквизиты + ОПФ
        if ctype == 'COMPANY':
            opf = (self.opf.data or '').strip()
            is_ip = (opf == 'ИП')

            # 1) ОПФ обязательно
            if not opf:
                self.opf.errors.append('Выберите ОПФ (ТОО / ИП / КХ)')
                return False

            # 2) Реквизиты банка обязательны для всех COMPANY (в т.ч. ИП/КХ)
            required_bank = {
                'bank_account': 'Укажите расчётный счёт (IBAN)',
                'bank_name': 'Укажите банк (наименование)',
                'bank_bic': 'Укажите БИК банка',
            }
            for fname, msg in required_bank.items():
                field = getattr(self, fname)
                if not (field.data or '').strip():
                    field.errors.append(msg)
                    return False

            # 3) Руководитель обязателен только для ТОО/КХ (для ИП — НЕ нужен)
            if not is_ip:
                required_head = {
                    'director_position': 'Укажите должность руководителя',
                    'director_fio': 'Укажите ФИО руководителя',
                }
                for fname, msg in required_head.items():
                    field = getattr(self, fname)
                    if not (field.data or '').strip():
                        field.errors.append(msg)
                        return False
            else:
                # для ИП чистим руководителя и контактное лицо, чтобы не было дублей
                self.director_position.data = ''
                self.director_fio.data = ''
                self.contact_person.data = ''

            # 4) Документные поля для COMPANY не нужны — чистим
            self.doc_type.data = ''
            self.doc_number.data = ''
            self.doc_issue_date.data = None
            self.doc_issuer.data = ''

        return True

class SalesContractForm(IdempotentFlaskForm):
    def __init__(self, *args, **kwargs):
        # передай сюда contract_id при редактировании
        self.contract_id = kwargs.pop('contract_id', None)
        super().__init__(*args, **kwargs)

    contract_date = DateField(
        'Дата договора',
        format='%Y-%m-%d',
        validators=[Optional()]
    )

    contract_number = StringField(
        'Номер договора',
        validators=[Optional(), Length(max=64)]
    )

    customer_id = SelectField(
        'Клиент',
        coerce=int,
        validators=[DataRequired(message='Выберите клиента')]
    )

    trailer_id = SelectField(
        'Прицеп',
        coerce=int,
        validators=[DataRequired(message='Выберите прицеп')]
    )

    warehouse_id = SelectField(
        'Склад для исторических отчетов',
        coerce=int,
        validators=[Optional()]
    )

    assigned_user_id = SelectField(
        'Ответственный для исторических отчетов',
        coerce=int,
        validators=[Optional()]
    )

    price = DecimalField(
        'Сумма',
        places=2,
        validators=[Optional(), NumberRange(min=0)]
    )

    payment_method = StringField(
        'Способ оплаты',
        validators=[Optional(), Length(max=64)]
    )

    is_paid = BooleanField('Оплачено', default=False)
    is_shipped = BooleanField('Отгружено', default=False)

    submit = SubmitField('Сохранить')

    def validate_contract_number(self, field):
        num = (field.data or '').strip()

        # если пусто — ок (будет NULL)
        if not num:
            return

        q = SalesContract.query.filter(SalesContract.contract_number == num)
        if self.contract_id:
            q = q.filter(SalesContract.id != self.contract_id)

        if q.first():
            raise ValidationError('Такой номер договора уже существует.')
        

class OTTSForm(IdempotentFlaskForm):
    number = StringField('Номер ОТТС', validators=[DataRequired()])
    date = DateField('Дата ОТТС', format='%Y-%m-%d', validators=[Optional()])
    modification = StringField('Модификация (например, 002)', validators=[DataRequired()])
    name = StringField('Наименование', validators=[DataRequired()])
    axle_count = IntegerField('Количество осей', validators=[DataRequired()])
    is_active = BooleanField('Активен', default=True)
    full_mass_kg = IntegerField('Полная масса, кг')

    submit = SubmitField('Сохранить')



class LeadForm(IdempotentFlaskForm):
    created_at = DateField('Дата заявки', format='%Y-%m-%d', validators=[Optional()])
    source_channel = SelectField(
        'Канал',
        choices=[
            ('manual', 'Вручную'),
            ('website', 'Сайт'),
            ('bot', 'Бот'),
            ('whatsapp', 'WhatsApp'),
            ('instagram', 'Instagram Direct'),
            ('telegram', 'Telegram'),
            ('facebook', 'Facebook / Meta'),
        ],
        validators=[DataRequired()]
    )
    source_name = StringField('Источник / аккаунт', validators=[Optional(), Length(max=100)])
    source_platform = StringField('Площадка / аккаунт', validators=[Optional(), Length(max=50)])
    customer_name = StringField('Имя клиента', validators=[DataRequired(), Length(max=255)])
    phone = StringField('Телефон', validators=[Optional(), Length(max=50)])
    messenger_username = StringField('Ник / username', validators=[Optional(), Length(max=100)])
    desired_item_search = StringField('Поиск модели', validators=[Optional(), Length(max=255)])
    desired_item_id = SelectField('Модель из номенклатуры', coerce=int, validators=[Optional()])
    desired_model = StringField('Модель текстом', validators=[Optional(), Length(max=255)])
    desired_specs = TextAreaField('Пожелания / комплектация', validators=[Optional()])
    warehouse_id = SelectField('Склад продажи', coerce=int, validators=[Optional()])
    assigned_user_id = SelectField('Ответственный', coerce=int, validators=[Optional()])
    status = SelectField(
        'Статус',
        choices=[
            ('NEW', 'Новая'),
            ('IN_PROGRESS', 'В работе'),
            ('QUOTED', 'Коммерческое предложение'),
            ('AWAITING_PAYMENT', 'Ждём оплату'),
            ('CONVERTED', 'Переведена в заказ'),
            ('CANCELED', 'Отменена'),
        ],
        validators=[DataRequired()]
    )
    text = TextAreaField('Текст обращения', validators=[Optional()])
    comment = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')


class CustomerOrderForm(IdempotentFlaskForm):
    def __init__(self, *args, **kwargs):
        self.order_id = kwargs.pop('order_id', None)
        super().__init__(*args, **kwargs)

    order_number = StringField('Номер заказа', validators=[Optional(), Length(max=50)])
    order_date = DateField('Дата заказа', format='%Y-%m-%d', validators=[Optional()])
    lead_id = SelectField('Заявка', coerce=int, validators=[Optional()])
    customer_search = StringField('Поиск клиента', validators=[Optional(), Length(max=255)])
    customer_id = SelectField('Клиент', coerce=int, validators=[DataRequired()])
    item_search = StringField('Поиск модели', validators=[Optional(), Length(max=255)])
    item_id = SelectField('Модель', coerce=int, validators=[Optional()])
    trailer_id = SelectField('Конкретный прицеп', coerce=int, validators=[Optional()])
    warehouse_id = SelectField('Склад продажи', coerce=int, validators=[Optional()])
    assigned_user_id = SelectField('Ответственный', coerce=int, validators=[Optional()])
    quantity = IntegerField('Количество', validators=[DataRequired(), NumberRange(min=1)], default=1)
    price = DecimalField('Сумма заказа', places=2, validators=[Optional(), NumberRange(min=0)])
    prepayment_percent = DecimalField('Предоплата, %', places=2, validators=[Optional(), NumberRange(min=0, max=100)], default=30)
    status = SelectField(
        'Статус заказа',
        choices=[
            ('draft', 'Черновик'),
            ('waiting_payment', 'Ждем оплату'),
            ('prepaid', 'Предоплата внесена'),
            ('confirmed', 'Подтвержден'),
            ('waiting_production', 'Ожидает производства'),
            ('in_production', 'В производстве'),
            ('produced_waiting_vin', 'Выпущен, ждёт VIN'),
            ('waiting_transfer', 'Ждёт отправки'),
            ('in_transit', 'В пути'),
            ('arrived', 'Прибыл'),
            ('waiting_arrival', 'Ожидает прибытия'),
            ('ready_to_ship', 'Готов к отгрузке'),
            ('sold_not_shipped', 'Продан, не отгружен'),
            ('shipped', 'Отгружен'),
            ('done', 'Завершен'),
            ('cancelled', 'Отменен'),
        ],
        validators=[DataRequired()]
    )
    fulfillment_source = SelectField(
        'Источник обеспечения',
        choices=[
            ('later', 'Подобрать позже'),
            ('stock', 'Из наличия'),
            ('production', 'Заказать в производство'),
        ],
        validators=[Optional()]
    )
    expected_date = DateField('Плановая дата готовности', format='%Y-%m-%d', validators=[Optional()])
    planned_ship_date = DateField('Плановая дата выдачи клиенту', format='%Y-%m-%d', validators=[Optional()])
    planned_ship_comment = TextAreaField('Комментарий к выдаче', validators=[Optional()])
    documents_issued = BooleanField('Документы выданы')
    is_shipped = BooleanField('Отгружен')
    note = TextAreaField('Комментарий', validators=[Optional()])
    manager_comment = TextAreaField('Комментарий менеджера', validators=[Optional()])
    submit = SubmitField('Сохранить')


class ContractTemplateForm(IdempotentFlaskForm):
    code = StringField('Код шаблона', validators=[DataRequired(), Length(max=60)])
    name = StringField('Название', validators=[DataRequired(), Length(max=160)])
    template_type = SelectField(
        'Тип шаблона',
        choices=[('sale', 'Договор продажи')],
        default='sale',
        validators=[DataRequired()],
    )
    product_group_code = StringField('Группа / тип прицепа', validators=[Optional(), Length(max=20)])
    otss_number = StringField('Номер ОТТС', validators=[Optional(), Length(max=120)])
    otss_type = StringField('Тип ОТТС', validators=[Optional(), Length(max=20)])
    otss_modification = StringField('Модификация ОТТС', validators=[Optional(), Length(max=20)])
    body_execution_code = StringField('Исполнение кузова', validators=[Optional(), Length(max=30)])
    customer_type = SelectField(
        'Тип клиента',
        choices=[('', 'Любой'), ('PERSON', 'Физическое лицо'), ('COMPANY', 'Юридическое лицо / ИП / КХ')],
        default='',
        validators=[Optional()],
    )
    content_path = StringField('Файл шаблона HTML', validators=[Optional(), Length(max=500)])
    is_default = BooleanField('Шаблон по умолчанию')
    is_active = BooleanField('Активен', default=True)
    sort_order = IntegerField('Приоритет', validators=[Optional(), NumberRange(min=0)], default=0)
    comment = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')

    def validate_order_number(self, field):
        num = (field.data or '').strip()
        if not num:
            return
        q = CustomerOrder.query.filter(CustomerOrder.order_number == num)
        if self.order_id:
            q = q.filter(CustomerOrder.id != self.order_id)
        if q.first():
            raise ValidationError('Такой номер заказа уже существует.')


class OrderPaymentForm(IdempotentFlaskForm):
    paid_at = DateField('Дата оплаты', format='%Y-%m-%d', validators=[Optional()])
    stage = SelectField(
        'Этап оплаты',
        choices=[
            ('PREPAYMENT', 'Предоплата'),
            ('FINAL', 'Доплата'),
            ('FULL', 'Полная оплата'),
            ('OTHER', 'Другое'),
        ],
        validators=[DataRequired()]
    )
    method = SelectField(
        'Способ оплаты',
        choices=[
            ('BANK', 'Безнал / банк'),
            ('KASPI_QR', 'Kaspi QR'),
            ('KASPI_LINK', 'Kaspi ссылка'),
            ('CARD', 'Карта'),
            ('KASPI_INSTALLMENT', 'Kaspi рассрочка'),
            ('TRANSFER', 'Перевод'),
            ('CASH', 'Наличные'),
        ],
        validators=[DataRequired()]
    )
    amount = DecimalField('Сумма', places=2, validators=[DataRequired(), NumberRange(min=0.01)])
    status = SelectField(
        'Статус платежа',
        choices=[
            ('CONFIRMED', 'Подтвержден'),
            ('PENDING', 'Ожидает подтверждения'),
            ('CANCELED', 'Отменен'),
        ],
        validators=[DataRequired()]
    )
    transaction_ref = StringField('Номер / ссылка / транзакция', validators=[Optional(), Length(max=120)])
    payment_link = StringField('Ссылка на оплату', validators=[Optional(), Length(max=255)])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')


class KaspiOrderImportForm(FlaskForm):
    order_code = StringField('Номер заказа Kaspi', validators=[DataRequired(), Length(max=120)])
    warehouse_id = SelectField('Склад / филиал', coerce=int, validators=[Optional()])
    assigned_user_id = SelectField('Ответственный', coerce=int, validators=[Optional()])
    submit = SubmitField('Импортировать в заявки')


class KaspiOrderListImportForm(FlaskForm):
    state = SelectField(
        'Состояние',
        choices=[
            ('NEW', 'Новые'),
            ('SIGN_REQUIRED', 'Нужно подписать'),
            ('PICKUP', 'Самовывоз'),
            ('DELIVERY', 'Ваша доставка'),
            ('KASPI_DELIVERY', 'Kaspi Доставка'),
            ('ARCHIVE', 'Архив'),
        ],
        validators=[DataRequired()],
        default='NEW',
    )
    status = SelectField(
        'Статус',
        choices=[
            ('', '— любой —'),
            ('APPROVED_BY_BANK', 'Продавец должен принять'),
            ('ACCEPTED_BY_MERCHANT', 'Принят'),
            ('COMPLETED', 'Завершен'),
            ('CANCELLED', 'Отменен'),
            ('CANCELLING', 'В процессе отмены'),
            ('KASPI_DELIVERY_RETURN_REQUESTED', 'Ожидает возврата'),
            ('RETURNED', 'Возвращен'),
        ],
        validators=[Optional()],
    )
    date_from = StringField('С даты', validators=[Optional(), Length(max=20)])
    date_to = StringField('По дату', validators=[Optional(), Length(max=20)])
    page_number = IntegerField('Страница', validators=[DataRequired(), NumberRange(min=0)], default=0)
    page_size = IntegerField('Кол-во', validators=[DataRequired(), NumberRange(min=1, max=5)], default=5)
    warehouse_id = SelectField('Склад / филиал', coerce=int, validators=[Optional()])
    assigned_user_id = SelectField('Ответственный', coerce=int, validators=[Optional()])
    submit = SubmitField('Импортировать список')


class SupplyNeedForm(IdempotentFlaskForm):
    need_type = SelectField(
        'Тип потребности',
        choices=[
            ('CUSTOMER_ORDER', 'Под клиента'),
            ('STOCK_REPLENISHMENT', 'Пополнение склада'),
        ],
        validators=[DataRequired()]
    )
    status = SelectField(
        'Статус',
        choices=[
            ('NEW', 'Новая'),
            ('IN_PRODUCTION', 'В производстве'),
            ('IN_TRANSIT', 'В пути'),
            ('ARRIVED', 'Прибыла'),
            ('CLOSED', 'Закрыта'),
            ('CANCELLED', 'Отменена'),
        ],
        validators=[DataRequired()]
    )
    order_id = SelectField('Заказ', coerce=int, validators=[Optional()])
    item_id = SelectField('Модель', coerce=int, validators=[DataRequired()])
    warehouse_id = SelectField('Склад', coerce=int, validators=[Optional()])
    quantity = IntegerField('Количество', validators=[DataRequired(), NumberRange(min=1)], default=1)
    priority = IntegerField('Приоритет', validators=[DataRequired(), NumberRange(min=1)], default=100)
    required_by = DateField('Нужно к дате', format='%Y-%m-%d', validators=[Optional()])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')


class StockReplenishmentForm(IdempotentFlaskForm):
    warehouse_id = SelectField('Склад назначения', coerce=int, validators=[DataRequired()])
    item_id = SelectField('Номенклатура', coerce=int, validators=[Optional()])
    quantity = IntegerField('Количество', validators=[DataRequired(), NumberRange(min=1)], default=1)
    required_by = DateField('Желаемый срок', format='%Y-%m-%d', validators=[Optional()])
    comment = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Создать заявку')


class ProductionRequestForm(IdempotentFlaskForm):
    request_number = StringField('Номер заявки', validators=[Optional(), Length(max=50)])
    status = SelectField(
        'Статус',
        choices=[
            ('draft', 'Черновик'),
            ('approved', 'Утверждена'),
            ('in_progress', 'В работе'),
            ('partial_ready', 'Частично готова'),
            ('ready', 'Готова'),
            ('shipped', 'Отправлена'),
            ('closed', 'Закрыта'),
        ],
        validators=[DataRequired()]
    )
    target_warehouse_id = SelectField('Склад назначения', coerce=int, validators=[Optional()])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')


class ProductionRequestLineForm(IdempotentFlaskForm):
    supply_need_id = SelectField('Потребность', coerce=int, validators=[Optional()])
    item_id = SelectField('Модель', coerce=int, validators=[DataRequired()])
    quantity = IntegerField('Количество', validators=[DataRequired(), NumberRange(min=1)], default=1)
    status = SelectField(
        'Статус позиции',
        choices=[
            ('planned', 'Запланирована'),
            ('in_production', 'В производстве'),
            ('ready', 'Готова'),
            ('in_transit', 'В пути'),
            ('arrived', 'Прибыла'),
            ('reserved', 'Зарезервирована'),
            ('shipped_to_customer', 'Отгружена клиенту'),
        ],
        validators=[DataRequired()]
    )
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Добавить позицию')


class AssignVinForm(IdempotentFlaskForm):
    vin_registry_id = SelectField('VIN из реестра', coerce=int, validators=[DataRequired()])
    manufacture_date = DateField('Дата выпуска', format='%Y-%m-%d', validators=[Optional()])
    submit = SubmitField('Присвоить VIN')


class SendTrailerForm(IdempotentFlaskForm):
    trailer_id = SelectField('Прицеп', coerce=int, validators=[DataRequired()])
    to_warehouse_id = SelectField('Склад назначения', coerce=int, validators=[DataRequired()])
    order_id = SelectField('Заказ', coerce=int, validators=[Optional()])
    departure_date = DateField('Дата отправки', format='%Y-%m-%d', validators=[Optional()])
    arrival_date = DateField('Ожидаемое прибытие', format='%Y-%m-%d', validators=[Optional()])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Отправить')


class StockMovementForm(IdempotentFlaskForm):
    from_warehouse_id = SelectField('Со склада', coerce=int, validators=[Optional()])
    to_warehouse_id = SelectField('На склад', coerce=int, validators=[Optional()])
    trailer_search = StringField('Поиск прицепа', validators=[Optional(), Length(max=255)])
    trailer_id = SelectField('Прицеп', coerce=int, validators=[Optional()])
    order_id = SelectField('Заказ', coerce=int, validators=[Optional()])
    movement_type = SelectField(
        'Тип перемещения',
        choices=[
            ('warehouse_transfer', 'Между складами'),
            ('production_arrival', 'Поступление с производства'),
        ],
        validators=[DataRequired()]
    )
    status = SelectField(
        'Статус',
        choices=[
            ('draft', 'Черновик'),
            ('sent', 'Отправлено'),
            ('in_transit', 'В пути'),
            ('arrived', 'Прибыло'),
            ('cancelled', 'Отменено'),
        ],
        validators=[DataRequired()]
    )
    departure_date = DateField('Дата отправки', format='%Y-%m-%d', validators=[Optional()])
    arrival_date = DateField('Дата прибытия', format='%Y-%m-%d', validators=[Optional()])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Сохранить')


class StockMovementBatchForm(IdempotentFlaskForm):
    from_warehouse_id = SelectField('Со склада', coerce=int, validators=[DataRequired()])
    to_warehouse_id = SelectField('На склад', coerce=int, validators=[DataRequired()])
    movement_type = SelectField(
        'Тип перемещения',
        choices=[
            ('warehouse_transfer', 'Между складами'),
            ('production_arrival', 'Поступление с производства'),
        ],
        validators=[DataRequired()]
    )
    status = SelectField(
        'Статус',
        choices=[
            ('sent', 'Отправлено'),
            ('in_transit', 'В пути'),
            ('draft', 'Черновик'),
        ],
        validators=[DataRequired()]
    )
    departure_date = DateField('Дата отправки', format='%Y-%m-%d', validators=[Optional()])
    arrival_date = DateField('Ожидаемое прибытие', format='%Y-%m-%d', validators=[Optional()])
    note = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Создать партию')


class InventoryReceiptForm(IdempotentFlaskForm):
    warehouse_id = SelectField('Склад', coerce=int, validators=[DataRequired()])
    storage_area_id = SelectField('Зона хранения', coerce=int, validators=[Optional()])
    document_ref = StringField('Накладная / документ', validators=[Optional(), Length(max=120)])
    comment = TextAreaField('Комментарий', validators=[Optional()])
    submit = SubmitField('Провести приход')
