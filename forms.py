# forms.py
from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, BooleanField, SubmitField,
    SelectField, DecimalField, IntegerField, TextAreaField, DateField
)
from wtforms.validators import DataRequired, Optional, Length, NumberRange, Email, input_required
from wtforms import ValidationError
from models import SalesContract

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

class UserForm(FlaskForm):
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
            ('manager', 'Менеджер'),
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

class WarehouseForm(FlaskForm):
    name = StringField(
        'Название склада',
        validators=[DataRequired(), Length(max=128)]
    )
    is_active = BooleanField('Активен', default=True)
    submit = SubmitField('Сохранить')


# -------- Номенклатура (прицепы + комплектующие) --------

class ItemForm(FlaskForm):
    item_type = SelectField(
        'Тип позиции',
        choices=[
            ('TRAILER', 'Прицеп'),
            ('PART', 'Комплектующее'),
        ],
        validators=[DataRequired()]
    )
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
    unit = StringField(
        'Ед. изм.',
        default='шт',
        validators=[Optional(), Length(max=16)]
    )

    is_active = BooleanField('Активен', default=True)

    submit = SubmitField('Сохранить')


# -------- Прицепы (конкретные единицы с VIN) --------


class TrailerCreateForm(FlaskForm):
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


# -------- Клиенты --------

class CustomerForm(FlaskForm):
    customer_type = SelectField(
        'Тип клиента',
        choices=[
            ('PERSON', 'Физическое лицо'),
            ('COMPANY', 'Юридическое лицо'),
        ],
        validators=[DataRequired()]
    )

    name = StringField('ФИО / Название', validators=[DataRequired(), Length(max=255)])
    contact_person = StringField('Контактное лицо', validators=[Optional(), Length(max=255)])
    iin_bin = StringField('ИИН / БИН', validators=[Optional(), Length(max=20)])

    # --- НОВОЕ: документ ---
    doc_type = SelectField(
        'Документ',
        choices=[
            ('ID', 'Удостоверение личности РК'),
            ('RESIDENT', 'Вид на жительство (резидент РК)'),
            ('FOREIGN_PASSPORT', 'Паспорт иностранного гражданина'),
        ],
        default='ID',
        validators=[Optional()]
    )
    doc_number = StringField('Номер документа', validators=[Optional(), Length(max=50)])
    doc_issue_date = DateField('Дата выдачи', format='%Y-%m-%d', validators=[Optional()])
    doc_issuer = StringField('Кем выдан', validators=[Optional(), Length(max=255)])

    phone = StringField('Телефон', validators=[Optional(), Length(max=50)])
    email = StringField('Email', validators=[Optional(), Length(max=120)])
    address = StringField('Адрес', validators=[Optional(), Length(max=255)])

    is_active = BooleanField('Активен', default=True)
    submit = SubmitField('Сохранить')


# -------- Договоры / продажи --------

class SalesContractForm(FlaskForm):
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
        

class OTTSForm(FlaskForm):
    number = StringField('Номер ОТТС', validators=[DataRequired()])
    date = DateField('Дата ОТТС', format='%Y-%m-%d', validators=[Optional()])
    modification = StringField('Модификация (например, 002)', validators=[DataRequired()])
    name = StringField('Наименование', validators=[DataRequired()])
    axle_count = IntegerField('Количество осей', validators=[DataRequired()])
    is_active = BooleanField('Активен', default=True)
    full_mass_kg = IntegerField('Полная масса, кг')

    submit = SubmitField('Сохранить')

