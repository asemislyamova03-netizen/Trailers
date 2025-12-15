# import_excel.py
"""
Импорт данных из файла РЕЕСТР.xlsx в базу trailers.db

1. Номенклатура прицепов (лист "Матрица основная") -> Item (item_type='TRAILER')
2. Прицепы с VIN (лист "Прицепы") -> Trailer
3. Склады -> Warehouse (из листа "Прицепы")
4. Клиенты:
   - физлица (лист "Покупатели") -> Customer (customer_type='PERSON')
   - юрлица (лист "юр лица")     -> Customer (customer_type='COMPANY')
5. Договоры/продажи:
   - физлица (лист "Покупатели") -> SalesContract
   - юрлица (лист "юр лица")     -> SalesContract
"""

import math
import pandas as pd

from app import create_app
from extensions import db
from models import Item, Trailer, Warehouse, Customer, SalesContract
from datetime import datetime



EXCEL_FILE = 'РЕЕСТР.xlsx'


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def clean_str(value):
    """Аккуратно превращаем в строку, убираем NaN и пустое."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
        return str(value)
    s = str(value).strip()
    return s or None


def to_float(value):
    """Безопасно превратить в float, если нельзя — 0.0."""
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        return float(value)
    except Exception:
        return 0.0

def parse_date(value):
    """Аккуратно парсим дату из Excel / строки в date()."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None

    # Если это уже datetime/Timestamp из pandas
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date()

    s = str(value).strip()
    if not s:
        return None

    # Пробуем несколько форматов
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # Если не смогли разобрать — просто None
    return None


def get_or_create_warehouse(name: str) -> Warehouse:
    """Найти или создать склад по имени."""
    name = clean_str(name) or 'Не указан'

    wh = Warehouse.query.filter_by(name=name).first()
    if not wh:
        wh = Warehouse(name=name, is_active=True)
        db.session.add(wh)
        db.session.flush()
        print(f'Создан новый склад: {name}')

    return wh


# ---------- ИМПОРТ НОМЕНКЛАТУРЫ (МАТРИЦА ОСНОВНАЯ) ----------

def import_items_from_matrix(xls: pd.ExcelFile) -> None:
    print('=== Импорт номенклатуры из листа "Матрица основная" ===')
    df = pd.read_excel(xls, sheet_name='Матрица основная')

    # ----- Определяем количество осей по строкам "осные" -----
    markers_mask = df['Артикул'].astype(str).str.contains('осные', na=False)
    axle_marker = df['Артикул'].where(markers_mask).ffill()

    def parse_axle(val):
        if isinstance(val, str):
            if '1' in val:
                return 1
            if '2' in val:
                return 2
        return None

    df['axle_count'] = axle_marker.map(parse_axle)

    df_data = df[
        df['Артикул'].notna()
        & df['Цена с НДС, тенге'].notna()
    ].copy()

    created = 0
    updated = 0

    for _, row in df_data.iterrows():
        article = clean_str(row['Артикул'])
        if not article:
            continue

        name = clean_str(row.get('Наименование')) or article

        # ---- ПОДКАТНОЕ КОЛЕСО: 6-й столбец (index 5) ----
        has_jockey_wheel = None
        raw_jw = row.iloc[5] if len(row) > 5 else None

        if isinstance(raw_jw, str):
            val = raw_jw.strip()
            upper = val.upper()
            # 0 или пусто = НЕТ колеса
            if val == '' or val == '0':
                has_jockey_wheel = False
            # ОК (кириллица) или OK (на всякий случай) = ЕСТЬ колесо
            elif upper in ('ОК', 'OK'):
                has_jockey_wheel = True
            else:
                # что-то странное — не трогаем (останется None)
                has_jockey_wheel = None
        elif isinstance(raw_jw, (int, float)):
            try:
                if not math.isnan(raw_jw):
                    has_jockey_wheel = (raw_jw != 0)
            except Exception:
                pass

        # ---- ТЕНТ / ВЫСОТА ТЕНТА: 7-й столбец (index 6) ----
        tent_height_mm = None
        raw_tent = row.iloc[6] if len(row) > 6 else None

        if isinstance(raw_tent, (int, float)):
            # числовой формат Excel
            if not pd.isna(raw_tent) and raw_tent > 0:
                tent_height_mm = int(raw_tent)
        elif isinstance(raw_tent, str):
            txt = raw_tent.strip()
            if txt and txt != '0':
                # вытащим только цифры: "30" / "30см" / " 60 " → 30 / 60
                digits = ''.join(ch for ch in txt if ch.isdigit())
                if digits:
                    try:
                        tent_height_mm = int(digits)
                    except ValueError:
                        tent_height_mm = None

        # ---- ЦЕНА ----
        base_price = None
        if not pd.isna(row['Цена с НДС, тенге']):
            try:
                base_price = float(row['Цена с НДС, тенге'])
            except Exception:
                base_price = None

        # ---- ГАБАРИТЫ / КУЗОВ ----
        size_external = None
        if 'габариты' in df.columns and not pd.isna(row.get('габариты')):
            size_external = clean_str(row['габариты'])

        size_body = None
        if 'размеры кузова' in df.columns and not pd.isna(row.get('размеры кузова')):
            size_body = clean_str(row['размеры кузова'])

        # ---- ВЫСОТА БОРТА ----
        board_height = None
        for col in ['h борта', 'h борта.1']:
            if col in df.columns and not pd.isna(row.get(col)) and row[col] != 0:
                try:
                    board_height = int(row[col])
                except Exception:
                    board_height = None
                if board_height:
                    break

        # ---- РАЗМЕР КОЛЕСА ----
        wheel_radius = None
        for col in ['R', 'R.1']:
            if col in df.columns and not pd.isna(row.get(col)) and row[col] != 0:
                wheel_radius = clean_str(row[col])
                if wheel_radius:
                    break

        # ---- КОЛ-ВО ОСЕЙ ----
        axle_count = None
        ax = row.get('axle_count', None)
        if isinstance(ax, (int, float)) and not pd.isna(ax):
            axle_count = int(ax)

        # ----- ИЩЕМ/СОЗДАЁМ ITEM -----
        item = Item.query.filter_by(article=article, item_type='TRAILER').first()
        if item:
            updated += 1
        else:
            item = Item(
                item_type='TRAILER',
                article=article,
                unit='шт',
                is_active=True,
            )
            created += 1

        # ----- ОБНОВЛЯЕМ ПОЛЯ (И ДЛЯ НОВЫХ, И ДЛЯ СТАРЫХ) -----
        item.name = name
        item.board_height_mm = board_height
        item.wheel_radius = wheel_radius
        item.tent_hight_mm = tent_height_mm      # поле в модели/БД
        item.has_jockey_wheel = has_jockey_wheel
        item.axle_count = axle_count
        item.size_external = size_external
        item.size_body = size_body
        item.base_price = base_price

        db.session.add(item)

    db.session.commit()
    total = Item.query.filter_by(item_type='TRAILER').count()
    print(f'Создано моделей прицепов: {created}, обновлено: {updated}')
    print(f'Итого моделей прицепов в базе: {total}')
    print()


# ---------- ИМПОРТ ПРИЦЕПОВ (ЛИСТ "Прицепы") ----------

def import_trailers_from_sheet(xls: pd.ExcelFile) -> None:
    print('=== Импорт прицепов с VIN из листа "Прицепы" ===')
    df = pd.read_excel(xls, sheet_name='Прицепы')

    df = df[df['VIN код'].notna()].copy()

    created = 0
    skipped = 0
    no_model = 0

    for _, row in df.iterrows():
        vin = clean_str(row['VIN код'])
        if not vin:
            continue

        existing = Trailer.query.filter_by(vin=vin).first()
        if existing:
            skipped += 1
            continue

        article = clean_str(row.get('Артикул'))
        item = None
        if article:
            item = Item.query.filter_by(article=article, item_type='TRAILER').first()

        if not item:
            print(f'ВНИМАНИЕ: для VIN {vin} не найдена модель (артикул: {article})')
            no_model += 1
            continue

        warehouse_name = clean_str(row.get('Склад'))
        warehouse = get_or_create_warehouse(warehouse_name)

        manufacture_date = None
        if not pd.isna(row.get('Дата выпуска')):
            try:
                manufacture_date = pd.to_datetime(row['Дата выпуска']).date()
            except Exception:
                manufacture_date = None

        status_str = clean_str(row.get('Статус')) or ''
        if status_str.lower().startswith('продано'):
            status = 'SOLD'
        else:
            status = 'IN_STOCK'

        trailer = Trailer(
            vin=vin,
            item=item,
            warehouse=warehouse,
            manufacture_date=manufacture_date,
            status=status,
        )
        db.session.add(trailer)
        created += 1

    db.session.commit()
    total = Trailer.query.count()
    print(f'Создано прицепов: {created}, пропущено (были в базе): {skipped}, без модели: {no_model}')
    print(f'Итого прицепов в базе: {total}')
    print()

# ---------- ИМПОРТ КЛИЕНТОВ: ФИЗЛИЦА ----------

def import_customers_persons(xls: pd.ExcelFile) -> None:
    print('=== Импорт клиентов-физлиц из листа "Покупатели" ===')
    try:
        df = pd.read_excel(xls, sheet_name='Покупатели')
    except ValueError:
        print('Лист "Покупатели" не найден, пропускаю физлиц')
        return

    created = 0
    skipped = 0

    for _, row in df.iterrows():
        last_name = clean_str(row.get('Фамилия'))
        first_name = clean_str(row.get('Имя'))
        middle_name = clean_str(row.get('Отчество'))

        iin = clean_str(row.get('ИИН'))
        phone = clean_str(row.get('Телефон'))

        if not (iin or phone or (last_name and first_name)):
            continue

        name_parts = [p for p in (last_name, first_name, middle_name) if p]
        name = ' '.join(name_parts) if name_parts else (phone or iin)

        addr_parts = []
        for col in ['Адрес(Область)', 'Адрес(Город, Район)', 'Адрес(Улица)', 'Адрес( номер дома, квартиры)']:
            if col in df.columns:
                val = clean_str(row.get(col))
                if val:
                    addr_parts.append(val)
        address = ', '.join(addr_parts) if addr_parts else None

        q = Customer.query.filter_by(customer_type='PERSON')
        if iin:
            existing = q.filter_by(iin_bin=iin).first()
        else:
            existing = q.filter(
                Customer.name == name,
                Customer.phone == phone
            ).first()

        if existing:
            skipped += 1
            continue

        customer = Customer(
            customer_type='PERSON',
            name=name,
            contact_person=None,
            iin_bin=iin,
            phone=phone,
            email=None,
            address=address,
            is_active=True,
        )
        db.session.add(customer)
        created += 1

    db.session.commit()
    total = Customer.query.filter_by(customer_type='PERSON').count()
    print(f'Создано клиентов-физлиц: {created}, пропущено (были в базе): {skipped}')
    print(f'Итого физлиц в базе: {total}')
    print()

def enrich_customer_docs_from_persons(xls: pd.ExcelFile) -> None:
    """
    Обновляем для клиентов-физлиц:
      - номер документа
      - дату выдачи
      - кем выдан
    Берём данные из листа 'Покупатели' и ищем клиентов по ИИН.
    """
    print('=== Обновление документов клиентов-физлиц из листа "Покупатели" ===')
    try:
        df = pd.read_excel(xls, sheet_name='Покупатели')
    except ValueError:
        print('Лист "Покупатели" не найден, пропускаю обновление документов')
        return

    # --- Ищем подходящие колонки в файле ---
    num_col = None
    date_col = None
    issuer_col = None

    for col in df.columns:
        col_l = str(col).strip().lower()

        # номер удостоверения
        if num_col is None and any(
            key in col_l for key in ['номер удостовер', '№ удостовер', 'удостоверение №', 'номер уд']
        ):
            num_col = col

        # дата выдачи
        if date_col is None and ('дата' in col_l and 'выдач' in col_l):
            date_col = col

        # кем выдан
        if issuer_col is None and ('кем' in col_l and ('выдан' in col_l or 'выдал' in col_l)):
            issuer_col = col

    if not any([num_col, date_col, issuer_col]):
        print('В листе "Покупатели" не найдено колонок с документами, пропускаю.')
        return

    print(f'Колонка номера документа: {num_col}')
    print(f'Колонка даты выдачи:      {date_col}')
    print(f'Колонка кем выдан:        {issuer_col}')

    updated = 0
    not_found = 0

    for _, row in df.iterrows():
        iin = clean_str(row.get('ИИН'))
        if not iin:
            continue

        customer = Customer.query.filter_by(customer_type='PERSON', iin_bin=iin).first()
        if not customer:
            not_found += 1
            continue

        changed = False

        # Номер документа
        if num_col is not None:
            num_val = clean_str(row.get(num_col))
            if num_val:
                customer.doc_number = num_val
                changed = True

        # Дата выдачи
        if date_col is not None:
            date_val = parse_date(row.get(date_col))
            if date_val:
                customer.doc_issue_date = date_val
                changed = True

        # Кем выдан
        if issuer_col is not None:
            issuer_val = clean_str(row.get(issuer_col))
            if issuer_val:
                customer.doc_issuer = issuer_val
                changed = True

        # Если что-то изменилось — проставим тип документа по умолчанию
        if changed:
            if not customer.doc_type:
                customer.doc_type = 'ID'  # по умолчанию удостоверение личности
            updated += 1

    db.session.commit()
    print(f'Документы обновлены у клиентов: {updated}, не найдено по ИИН: {not_found}')
    print()


# ---------- ИМПОРТ КЛИЕНТОВ: ЮРЛИЦА ----------

def import_customers_companies(xls: pd.ExcelFile) -> None:
    print('=== Импорт клиентов-юрлиц из листа "юр лица" ===')
    try:
        df = pd.read_excel(xls, sheet_name='юр лица')
    except ValueError:
        print('Лист "юр лица" не найден, пропускаю юрлиц')
        return

    created = 0
    skipped = 0

    for _, row in df.iterrows():
        name = clean_str(row.get('Наименование'))
        if not name:
            continue

        bin_value = clean_str(row.get('БИН'))
        phone = clean_str(row.get('Телефон'))
        email = clean_str(row.get('Электронная почта'))
        address = clean_str(row.get('Адрес'))

        q = Customer.query.filter_by(customer_type='COMPANY')
        if bin_value:
            existing = q.filter_by(iin_bin=bin_value).first()
        else:
            existing = q.filter(Customer.name == name).first()

        if existing:
            skipped += 1
            continue

        first_name = clean_str(row.get('Имя'))
        last_name = clean_str(row.get('Фамилия'))
        middle_name = clean_str(row.get('Отчество'))
        cp_parts = [p for p in (last_name, first_name, middle_name) if p]
        contact_person = ' '.join(cp_parts) if cp_parts else None

        customer = Customer(
            customer_type='COMPANY',
            name=name,
            contact_person=contact_person,
            iin_bin=bin_value,
            phone=phone,
            email=email,
            address=address,
            is_active=True,
        )
        db.session.add(customer)
        created += 1

    db.session.commit()
    total = Customer.query.filter_by(customer_type='COMPANY').count()
    print(f'Создано клиентов-юрлиц: {created}, пропущено (были в базе): {skipped}')
    print(f'Итого юрлиц в базе: {total}')
    print()


# ---------- ИМПОРТ ПРОДАЖ: ФИЗЛИЦА ----------

def import_sales_persons(xls: pd.ExcelFile) -> None:
    print('=== Импорт продаж (физлица) из листа "Покупатели" ===')
    try:
        df = pd.read_excel(xls, sheet_name='Покупатели')
    except ValueError:
        print('Лист "Покупатели" не найден, пропускаю продажи физлиц')
        return

    created = 0
    skipped = 0
    no_trailer = 0
    no_customer = 0

    for _, row in df.iterrows():
        vin = clean_str(row.get('VIN прицепа'))
        if not vin:
            continue

        trailer = Trailer.query.filter_by(vin=vin).first()
        if not trailer:
            no_trailer += 1
            print(f'ВНИМАНИЕ: не найден прицеп VIN={vin} для продажи (Покупатели)')
            continue

        existing = SalesContract.query.filter_by(trailer_id=trailer.id).first()
        if existing:
            skipped += 1
            continue

        iin = clean_str(row.get('ИИН'))
        phone = clean_str(row.get('Телефон'))

        customer = None
        if iin:
            customer = Customer.query.filter_by(customer_type='PERSON', iin_bin=iin).first()
        if not customer and phone:
            customer = Customer.query.filter_by(customer_type='PERSON', phone=phone).first()

        if not customer:
            no_customer += 1
            print(f'ВНИМАНИЕ: не найден клиент для VIN={vin}, ИИН={iin}, Телефон={phone}')

        kaspi_val = to_float(row.get('Оплата KASPI'))
        cash_val = to_float(row.get('Оплата наличные'))
        total = kaspi_val + cash_val
        if total == 0:
            total = None

        if kaspi_val and cash_val:
            payment_method = 'Kaspi + наличные'
        elif kaspi_val:
            payment_method = 'Kaspi'
        elif cash_val:
            payment_method = 'Наличные'
        else:
            payment_method = None

        dt_val = row.get('Отметка времени')
        contract_date = None
        if dt_val is not None and not pd.isna(dt_val):
            try:
                contract_date = pd.to_datetime(dt_val).date()
            except Exception:
                contract_date = None

        contract = SalesContract(
            contract_number=None,
            contract_date=contract_date,
            customer_id=customer.id if customer else None,
            trailer_id=trailer.id,
            price=total,
            payment_method=payment_method,
            source='Покупатели',
        )
        db.session.add(contract)

        trailer.status = 'SOLD'
        created += 1

    db.session.commit()
    total = SalesContract.query.count()
    print(f'Создано продаж (физлица): {created}, пропущено (договор уже был по прицепу): {skipped}')
    print(f'Не найдено прицепов: {no_trailer}, не найдено клиентов: {no_customer}')
    print(f'Итого договоров в базе: {total}')
    print()


# ---------- ИМПОРТ ПРОДАЖ: ЮРЛИЦА ----------

def import_sales_companies(xls: pd.ExcelFile) -> None:
    print('=== Импорт продаж (юрлица) из листа "юр лица" ===')
    try:
        df = pd.read_excel(xls, sheet_name='юр лица')
    except ValueError:
        print('Лист "юр лица" не найден, пропускаю продажи юрлиц')
        return

    created = 0
    skipped = 0
    no_trailer = 0
    no_customer = 0

    for _, row in df.iterrows():
        vin = clean_str(row.get('VIN прицепа'))
        if not vin:
            continue

        trailer = Trailer.query.filter_by(vin=vin).first()
        if not trailer:
            no_trailer += 1
            print(f'ВНИМАНИЕ: не найден прицеп VIN={vin} для продажи (юр лица)')
            continue

        existing = SalesContract.query.filter_by(trailer_id=trailer.id).first()
        if existing:
            skipped += 1
            continue

        bin_value = clean_str(row.get('БИН'))
        name = clean_str(row.get('Наименование'))

        customer = None
        if bin_value:
            customer = Customer.query.filter_by(customer_type='COMPANY', iin_bin=bin_value).first()
        if not customer and name:
            customer = Customer.query.filter_by(customer_type='COMPANY', name=name).first()

        if not customer:
            no_customer += 1
            print(f'ВНИМАНИЕ: не найден юрклиент для VIN={vin}, БИН={bin_value}, Наименование={name}')

        price_val = to_float(row.get('Цена'))
        if price_val == 0:
            price_val = None

        payment_method = clean_str(row.get('Способ оплаты'))

        dt_val = row.get('Отметка времени')
        contract_date = None
        if dt_val is not None and not pd.isna(dt_val):
            try:
                contract_date = pd.to_datetime(dt_val).date()
            except Exception:
                contract_date = None

        contract = SalesContract(
            contract_number=None,
            contract_date=contract_date,
            customer_id=customer.id if customer else None,
            trailer_id=trailer.id,
            price=price_val,
            payment_method=payment_method,
            source='юр лица',
        )
        db.session.add(contract)

        trailer.status = 'SOLD'
        created += 1

    db.session.commit()
    total = SalesContract.query.count()
    print(f'Создано продаж (юрлица): {created}, пропущено (договор уже был по прицепу): {skipped}')
    print(f'Не найдено прицепов: {no_trailer}, не найдено клиентов: {no_customer}')
    print(f'Итого договоров в базе: {total}')
    print()

# ---------- ДОБАВЛЕНИЕ НОМЕРОВ И ДАТ ДОГОВОРОВ ИЗ "Реестр договоров" ----------

def enrich_contracts_from_registry(xls: pd.ExcelFile) -> None:
    """
    Берём лист 'Реестр договоров' и:
      - ищем прицеп по VIN,
      - ищем договор по trailer_id,
      - подставляем номер договора и дату.
    Новые договоры не создаём, только дополняем существующие.
    """
    print('=== Обновление номеров договоров из листа "Реестр договоров" ===')
    try:
        df = pd.read_excel(xls, sheet_name='Реестр договоров')
    except ValueError:
        print('Лист "Реестр договоров" не найден, пропускаю обновление номеров')
        return

    updated = 0
    no_trailer = 0
    no_contract = 0

    for _, row in df.iterrows():
        vin = clean_str(row.get('VIN'))
        if not vin:
            continue

        trailer = Trailer.query.filter_by(vin=vin).first()
        if not trailer:
            no_trailer += 1
            continue

        contract = (
            SalesContract.query
            .filter_by(trailer_id=trailer.id)
            .order_by(SalesContract.id)
            .first()
        )
        if not contract:
            no_contract += 1
            continue

        # номер договора (колонка Unnamed: 0)
        raw_num = row.get('Unnamed: 0')
        contract_number = clean_str(raw_num)

        # дата договора
        date_val = row.get('дата')
        contract_date = None
        if date_val is not None and not pd.isna(date_val):
            try:
                contract_date = pd.to_datetime(date_val).date()
            except Exception:
                contract_date = None

        changed = False

        if contract_number and not contract.contract_number:
            contract.contract_number = contract_number
            changed = True

        if contract_date and not contract.contract_date:
            contract.contract_date = contract_date
            changed = True

        if changed:
            updated += 1

    db.session.commit()
    print(f'Обновлено договоров: {updated}, без прицепа: {no_trailer}, без найденного договора: {no_contract}')
    print()


# ---------- ТОЧКА ВХОДА ----------

def main():
    app = create_app()
    with app.app_context():
        print('Открываю файл:', EXCEL_FILE)
        xls = pd.ExcelFile(EXCEL_FILE)

        import_items_from_matrix(xls)
        import_trailers_from_sheet(xls)

        import_customers_persons(xls)
        import_customers_companies(xls)

        # после создания клиентов подтягиваем данные по документам
        enrich_customer_docs_from_persons(xls)

        import_sales_persons(xls)
        import_sales_companies(xls)

        enrich_contracts_from_registry(xls)



if __name__ == '__main__':
    main()
