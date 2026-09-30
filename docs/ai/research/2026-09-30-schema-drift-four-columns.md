# Карта schema drift: четыре колонки модели вне Alembic

Дата проверки: 2026-09-30  
Статус: docs only. Миграция не написана. Это не production-ready.  
База ветки: `bfa06807a7d7de7af15da7eac176ed9cc8d54a80`  
Код, история миграций, `PRAGMA foreign_keys` и backfill в этом шаге не менялись.

Рабочая `trailers.db` через SQLite не открывалась. До и после команд `stat` совпал: inode `4245445`, размер `114688`, `mtime_ns` `1790750077906554771` (человеческое время файла `2026-09-30 06:34:37`). 70 прежних тестов здесь не запускались и не подтверждают ни полную схему, ни готовность к production.

## Вывод

После полной цепочки Alembic на новой пустой SQLite четырёх колонок нет. Обычный `create_app()` держит `PRAGMA foreign_keys=0`. Любое чтение `Item`, `Trailer` или `OTTS` через ORM падает: `no such column`.

Причина не в позднем коммите кандидата. Определения уже есть в первом коммите `bd3ceb2` (`2025-12-15`). В тот же коммит легли миграции, которые эти колонки не создают. Две колонки `item` с тех пор добавляет только ручной скрипт `add_item_columns.py`, вне Alembic.

Минимальная будущая миграция, которую можно вывести из модели без догадки о данных: добавить четыре nullable-колонки без `DEFAULT` и без backfill. Внешний ключ `trailer.otts_id -> otts.id` в этот шаг не входит. На обычном соединении Flask он всё равно не проверяется, а старая ревизия `c1d03f92e3c6` уже молча пропускает его и ломает свой downgrade.

## Что отсутствует

| Колонка | Модель | Тип в модели | NULL | default / server_default | FK в модели | После `upgrade head` |
|---|---|---|---|---|---|---|
| `item.tent_hight_mm` | `models.py:316` | `Integer` | да | нет | нет | нет |
| `item.has_jockey_wheel` | `models.py:317` | `Boolean` | да | нет | нет | нет |
| `trailer.otts_id` | `models.py:610` | `Integer` | да | нет | `otts.id`, `index` не задан | нет |
| `otts.full_mass_kg` | `models.py:886` | `Integer` | да | нет | нет | нет |

Имя `tent_hight_mm` написано без буквы `e` и в модели, и в `add_item_columns.py`, и в `views.py`. Колонки `tent_height_mm` в базе тоже нет. Будущая миграция обязана сохранить опечатку. Исправление имени — отдельное изменение модели и всех чтений.

`otts.full_weight_kg` после цепочки тоже нет. Это другое имя. Его нельзя считать источником для `full_mass_kg`.

## История

`git log -S` по `models.py`: все четыре поля впервые появляются в `bd3ceb2` `2025-12-15 13:45:21 +0500`, сообщение `init: trailers app`. Поздний коммит `67255f7` только пишет имена в комментарии миграции `d8e4b1c67a02`. Он колонки не добавляет.

В том же `bd3ceb2` уже есть:

- `add_item_columns.py` — `ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER` и `ADD COLUMN has_jockey_wheel BOOLEAN`. Ошибок скрипт не различает: любой сбой печатается и выполнение идёт дальше. `otts_id` и `full_mass_kg` скрипт не трогает.
- `migrations/versions/f307927504ec_initial_trailers_structure.py` создаёт `item` с `has_tent`, но без `tent_hight_mm` и `has_jockey_wheel`. `trailer` создаётся без `otts_id`.
- `migrations/versions/d7a447fda295_add_otss_table.py:27` создаёт `otts.full_weight_kg INTEGER NULL`.
- `migrations/versions/eb0efc471291_add_otts_table.py:43` удаляет `full_weight_kg`. В downgrade, строка 51, колонка возвращается под старым именем. `full_mass_kg` не появляется ни в upgrade, ни в downgrade.
- `migrations/versions/c1d03f92e3c6_add_sigex_fields_to_sales_contract.py:36-43` делает `batch_alter_table('trailer')` и `create_foreign_key('fk_trailer_otts_id', 'otts', ['otts_id'], ['id'])`. `add_column('otts_id')` в файле нет.

Поиск по `migrations/versions` на `bfa06807`: строк `tent_hight_mm`, `has_jockey_wheel`, `full_mass_kg` в операциях миграций нет. Единственные упоминания — комментарий в `d8e4b1c67a02:28-30`, что эта миграция колонки не добавляет, и такой же смысл в шапке `e1b7c4d92a58`.

## Почему ключ `c1d03f92e3c6` молча пропадает

Alembic `1.15.2`, файл пакета `alembic/operations/batch.py`, строки 342-345: при пересборке таблицы ограничение копируется только если все его колонки уже есть в таблице. Колонки `otts_id` нет, поэтому `fk_trailer_otts_id` не попадает в новый `CREATE TABLE`. Исключения нет, ревизия считается применённой.

Проверка отдельной временной базы, остановленной ровно на `c1d03f92e3c6`:

- `PRAGMA table_info(trailer)` не содержит `otts_id`.
- `PRAGMA foreign_key_list(trailer)` содержит только `item_id` и `warehouse_id`.
- В `sqlite_master.sql` таблицы `trailer` нет текста `otts`.
- `downgrade` до `eb0efc471291` падает: `ValueError: No such constraint: 'fk_trailer_otts_id'`.

Историю `c1d03f92e3c6` этот документ не переписывает. Её downgrade уже сломан на чистой SQLite.

## Факт на новой временной SQLite

Команды ниже гонялись из `/workspace` интерпретатором `/workspace/.venv/bin/python`. URI был `sqlite:////tmp/schema-drift-map-587e.sqlite`. Перед `upgrade` код проверял, что URL и путь не являются `trailers.db`. Подмена URI такая же, как в `tests/test_produced_unit_link_sqlite_guard.py`: `db.init_app` перехватывается до `create_app()`, потому что `app.py:20` иначе открывает `sqlite:///.../trailers.db`. В `app.py` нет слова `foreign_keys`.

```bash
git rev-parse HEAD
# bfa06807a7d7de7af15da7eac176ed9cc8d54a80

/workspace/.venv/bin/python - <<'PY'
import os, sqlite3
from pathlib import Path
os.environ.setdefault('SIGEX_BASE_URL', 'https://example.invalid')
os.chdir('/workspace')
import sys
sys.path.insert(0, '/workspace')
LIVE = Path('/workspace/trailers.db').resolve()
tmp = Path('/tmp/schema-drift-map-repro.sqlite')
if tmp.exists():
    tmp.unlink()
uri = 'sqlite:///' + tmp.as_posix()
assert tmp.resolve() != LIVE and not uri.endswith('trailers.db')
before = LIVE.stat()
from extensions import db
original = db.init_app
def _init(app):
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'check_same_thread': False, 'timeout': 30}}
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    return original(app)
db.init_app = _init
from app import create_app
from flask_migrate import upgrade
from sqlalchemy import text
app = create_app()
db.init_app = original
with app.app_context():
    assert 'trailers.db' not in str(db.engine.url)
    upgrade(directory='/workspace/migrations', revision='head')
    print('version', db.session.execute(text('select version_num from alembic_version')).scalar())
    print('foreign_keys', db.session.execute(text('PRAGMA foreign_keys')).scalar())
    raw = sqlite3.connect(tmp.as_posix())
    for table, column in (
        ('item', 'tent_hight_mm'), ('item', 'has_jockey_wheel'),
        ('trailer', 'otts_id'), ('otts', 'full_mass_kg'), ('otts', 'full_weight_kg'),
    ):
        names = {row[1] for row in raw.execute(f'PRAGMA table_info({table})')}
        print(f'{table}.{column}', column in names)
    print('triggers', raw.execute("select count(*) from sqlite_master where type='trigger'").fetchone()[0])
    raw.close()
    db.session.remove(); db.engine.dispose()
after = LIVE.stat()
assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
print('live-db-stat-unchanged')
PY
```

Зафиксированный прогон 2026-09-30, SQLite `3.45.1`, Alembic `1.15.2`, SQLAlchemy `2.0.40`:

- Файлов в `migrations/versions`: 41. Один head: `e1b7c4d92a58`.
- Цепочка от пустой базы дошла до head без ошибки.
- `PRAGMA foreign_keys` на соединении `create_app()`: `0`.
- Таблиц: 68. Триггеров: 12. Таблиц `_alembic_tmp_*`: 0.
- Все четыре колонки и `otts.full_weight_kg`: `present=False`.
- `Item.query`, `Trailer.query`, `OTTS.query` без какого-либо `ALTER`:

```text
OperationalError: no such column: item.tent_hight_mm
OperationalError: no such column: trailer.otts_id
OperationalError: no such column: otts.full_mass_kg
```

`SELECT` для `Item` содержит и `tent_hight_mm`, и `has_jockey_wheel`. `SELECT` для `Trailer` из-за `relationship` сразу читает и `trailer.otts_id`, и `otts.full_mass_kg`. SQLite сообщает первое отсутствующее имя.

`tests/test_produced_unit_link_sqlite_guard.py:34-43` и `_align_model_drift` (`:176-186`) после upgrade сами делают `ALTER TABLE` только во временной базе. Зелёный прогон этого файла не доказывает, что цепочка Alembic создаёт колонки. `db.create_all()` в других тестах собирает таблицы из модели, поэтому колонки там есть, а 12 триггеров нет.

## Смежный drift, не чинить этой миграцией

На той же временной базе класс `Item` не отображает девять колонок, которые миграция `d1f2a3b4c5d6` добавляет в `item`:

`body_size_id`, `body_execution_id`, `board_height_id`, `wheel_option_id`, `hub_option_id`, `support_wheel_option_id`, `tent_option_id`, `special_options_json`, `vin_modification_code`.

`group_id` в модели есть (`models.py:281`), в базе тоже есть. Чтение ORM лишние колонки не ломает. Autogenerate на этом расхождении опасен: он увидит и четыре недостающие колонки, и девять лишних, и отсутствующий FK. Будущую миграцию писать руками, только на четыре `ADD COLUMN`.

Отдельный прогон downgrade всей цепочки с head до `eb0efc471291` на копии временной базы упал раньше, на `d1f2a3b4c5d6 -> c6e4b2a9d813`:

```text
OperationalError: no such column: vin_modification_code
CREATE INDEX ix_production_request_line_vin_modification_code ON production_request_line (vin_modification_code)
```

Это не откат четырёх колонок. Полный откат истории до декабря 2025 на этой цепочке уже не является рабочей проверкой.

## План будущей миграции

Новая ревизия после `e1b7c4d92a58`. Старые файлы не редактировать. `PRAGMA foreign_keys` не включать. Backfill не делать.

### Колонки, как они заданы в модели

| Колонка | SQL, который Alembic 1.15.2 уже отдал на копии | NULL | DEFAULT |
|---|---|---|---|
| `item.tent_hight_mm` | `ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER` | да | нет |
| `item.has_jockey_wheel` | `ALTER TABLE item ADD COLUMN has_jockey_wheel BOOLEAN` | да | нет |
| `otts.full_mass_kg` | `ALTER TABLE otts ADD COLUMN full_mass_kg INTEGER` | да | нет |
| `trailer.otts_id` | `ALTER TABLE trailer ADD COLUMN otts_id INTEGER` | да | нет |

`BOOLEAN` здесь совпадает с уже существующей `item.has_tent` и с `add_item_columns.py`. Числовой `0/1` как default не ставить: модель допускает `NULL`, а `views.py` отличает `True`, `False` и `None`.

Почему нет backfill:

- В `Column(...)` нет `default` и `server_default`.
- `full_weight_kg` к моменту head уже удалён миграцией `eb0efc471291`. Копировать нечего, и имена разные.
- `add_item_columns.py` тоже добавляет колонки без `DEFAULT`: старые строки становятся `NULL`.
- `views.py` `get_or_create_configured_item` и `import_excel.py` заполняют поля только у строки, которую пишут сейчас. Это не правило для уже лежащих строк.
- `forms.py` `OTTSForm.full_mass_kg` — обычный `IntegerField` без `DataRequired`. Пустое значение остаётся пустым.
- Скрипт `scripts/fix_vin_2635_2645.py` вставляет `otts_id` из уже существующей строки. Он не описывает схему и здесь не запускался.

На копии head перед `ADD COLUMN` были вставлены синтетические `item`, `otts`, `warehouse`, `trailer`. После четырёх `ADD COLUMN` все четыре значения были `None`, `notnull=0`, `dflt=None`. После четырёх `DROP COLUMN` строки `item` и `trailer` остались, колонки исчезли.

### Уже существующая колонка

`add_item_columns.py` мог добавить две колонки `item` в базу, которая не собиралась одним Alembic. Живую базу этот шаг не смотрел, поэтому заранее неизвестно, есть ли колонки.

Upgrade будущей ревизии должен повторить приём `_add_column` из `b1c2d3e4f5a6_product_categories_inventory_foundation.py`: если имени нет в `PRAGMA` / inspector — сделать `op.add_column`. Если имя есть — не добавлять второй раз, не менять тип и не заполнять значения. Если тип или `NOT NULL` не совпадают с таблицей выше — остановиться и не чинить молча. Это отдельный результат инвентаризации копии, а не часть этой карты.

### Внешний ключ не входит в минимальный шаг

Модель объявляет `ForeignKey('otts.id')`. На копии `batch_alter_table('trailer')` с `add_column` и `create_foreign_key('fk_trailer_otts_id', ...)` ключ действительно появился в `PRAGMA foreign_key_list(trailer)`. Цена: Alembic пересобрал таблицу (`CREATE TABLE _alembic_tmp_trailer`, `DROP TABLE trailer`, `RENAME`).

Дальше на той же копии:

- `PRAGMA foreign_keys=0`, `INSERT` с `otts_id=919999` прошёл.
- `PRAGMA foreign_keys=ON`, такой же `INSERT` отклонён: `FOREIGN KEY constraint failed`.

Обычный Flask остаётся на `0`. Ключ в схеме не защищает связь, пока pragma выключена. Включать её глобально нельзя: на head у базы много других внешних ключей. Поэтому минимальная миграция добавляет только колонку. Совпадение модели и реального FK остаётся отдельным gate.

### Триггеры

Двенадцать триггеров head ссылаются на `sales_realization`, `sales_realization_line` и `produced_unit`. Поиск по `sqlite_master.sql` не нашёл ни одного триггера, в тексте которого есть таблица `item`, `trailer`, `otts` или имена четырёх колонок.

Проверено на копиях head:

- Четыре `ADD COLUMN` и четыре `DROP COLUMN` не создают `_alembic_tmp_*`. До и после остаётся 12 триггеров.
- `batch_alter_table(..., recreate='always')` по `item`, `trailer` и `otts` на этой копии прошёл, временных таблиц не осталось, триггеров по-прежнему 12. Это не разрешение переписывать старые миграции.

Для именно этих четырёх `op.add_column` / `op.drop_column` снятие триггеров не требуется. Если будущая ревизия всё же пересобирает таблицу, в тексте триггера которой есть её имя, порядок уже записан в `e1b7c4d92a58:41-48` и его надо повторить в новой миграции, не в старых:

1. Прочитать из `sqlite_master` `name` и `sql` триггеров, чей SQL упоминает пересобираемую таблицу.
2. `DROP TRIGGER IF EXISTS` для этих имён.
3. Выполнить `batch_alter_table`.
4. Заново выполнить сохранённый `CREATE TRIGGER`.
5. `PRAGMA foreign_keys` не включать.

На сегодняшнем head шаг 1 для `item` / `trailer` / `otts` пустой. Шаги 2 и 4 тогда ничего не делают. Писать их имеет смысл только в той миграции, которая реально пересобирает таблицу.

### Downgrade будущей ревизии

На синтетической базе, где колонки создала сама ревизия: `op.drop_column` для четырёх имён. Проверка SQLite `3.45.1` это делает без пересборки, строки и 12 триггеров остаются.

Если upgrade увидел колонку уже существующей и ничего не добавлял, такой downgrade удалит чужую колонку вместе с данными. Пока копия рабочей базы не инвентаризирована, этот downgrade к ней не применять. Данные для сохранения здесь не угаданы.

Откат `c1d03f92e3c6` и откат всей цепочки до `eb0efc471291` этой ревизией не чинятся.

## Матрица теста будущей ревизии

Только новая временная SQLite. `trailers.db` не открывать. Ручной `ALTER` из `MODEL_DRIFT_COLUMNS` в этом тесте не вызывать. `PRAGMA foreign_keys` ожидать `0`.

| Проверка | Ожидание |
|---|---|
| Пустая база, `upgrade head` до текущей `e1b7c4d92a58` | четырёх колонок нет; ORM даёт `no such column` |
| Синтетические строки `item`, `otts`, `trailer` до новой ревизии | вставляются без этих колонок |
| `upgrade` новой ревизии | четыре колонки есть; тип `INTEGER` / `BOOLEAN` / `INTEGER` / `INTEGER`; `notnull=0`; `dflt=None` |
| Те же строки после upgrade | значения `NULL`; `full_weight_kg` по-прежнему нет |
| Повторный upgrade на базе, где колонка уже есть | вторая колонка не создаётся, значения не переписываются |
| Колонка есть, но тип или `NOT NULL` другие | ревизия останавливается, схему не «чинит» |
| Триггеры и временные таблицы | 12 триггеров на месте, `_alembic_tmp_*` нет |
| `INSERT` чужого `otts_id` при `foreign_keys=0` | если FK не добавляли, вставка по-прежнему возможна; это не называть защитой |
| `downgrade` только новой ревизии на базе, где колонки создала она | колонок нет, старые строки целы, ORM снова падает, триггеры целы |
| `downgrade` на базе, где колонки были до ревизии | не запускать, пока нет инвентаризации копии |
| Имя | именно `tent_hight_mm` |
| Что не доказывает успех | прежние тесты с `_align_model_drift` или `db.create_all()` |

## Что остаётся закрытым

- Самой миграции нет. Этот файл её не заменяет.
- FK `fk_trailer_otts_id` в модели есть, в базе после head нет. На `PRAGMA foreign_keys=0` даже созданный ключ не отклоняет чужой id.
- Downgrade `c1d03f92e3c6` на чистой SQLite падает: ограничения, которое upgrade не создал, нет.
- Downgrade цепочки через `d1f2a3b4c5d6` падает на индексе `vin_modification_code`.
- Девять колонок `item` есть в Alembic и отсутствуют в классе `Item`.
- Неизвестно, применялся ли `add_item_columns.py` к рабочей базе. Список её колонок отсюда не снят.
- Уже записанные `NULL` будущая миграция не заполняет. Откуда взять высоту тента, признак подкатного колеса, ссылку на ОТТС и полную массу для старых строк, в репозитории не задано.
- 12 триггеров по-прежнему не создаются через `db.create_all()`.
- Пересборка `sales_realization`, `sales_realization_line` и `produced_unit` без снятия триггеров по-прежнему описана как небезопасная в `d8e4b1c67a02` и `e1b7c4d92a58`. Этот шаг её не меняет.
