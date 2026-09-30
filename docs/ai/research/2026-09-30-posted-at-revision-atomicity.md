# Candidate: атомарность ревизии даты смены

Дата: 2026-09-30  
Вердикт: **PASS** для узкой атомарности `b4e8c1a90d27`.  
Это не production-ready и не запуск живых остатков. Finding 1 не закрыт.

База: `9aa93f06a229b6757e4c73f5b107297696c13630`  
Ревизия та же: `b4e8c1a90d27`, `down_revision = f6b2d8c14e90`.  
Новой ревизии нет: на live эта версия не ставилась, меняется только её тело.

## Риск и область

Когорта отчёта берёт дату из `ProductionShift.posted_at`. Finding 2: с уже установленного `f6b2d8c14e90` голый `CREATE TRIGGER` фиксируется раньше `alembic_version`. Повтор тогда падает с `already exists`, версия не догоняет схему.

В область входят только эта ревизия, регрессии и этот отчёт. Не входят статус смены, отмена смены, глобальный pragma, 12 старых триггеров, старые downgrade, backfill, отчёты и рабочая база.

## Что сделано

Правило даты не менялось. `WHEN` по-прежнему: `OLD.status = 'posted'` или `OLD.posted_at IS NOT NULL`.

`SQLiteImpl.transactional_ddl` остаётся False. Alembic по-прежнему пишет `Will assume non-transactional DDL`. Внешний `begin_transaction` в `env.py` для SQLite пустой. pysqlite фиксирует голый DDL отдельно от версии.

Ревизия читает `sqlite_master` до DDL. Если триггера нет, делает `BEGIN` на DBAPI-соединении и создаёт канонический триггер. Своего COMMIT и ROLLBACK нет: шаг закрывает транзакция Alembic вместе с `UPDATE alembic_version`. Исключение после DDL, закрытие соединения и отказ самого `UPDATE` откатывают и триггер, и версию. Повтор после этого проходит.

Если точный канонический триггер уже есть, а версия ещё `f6b2d8c14e90`, DDL нет: `rowid` и SQL не меняются, версию дописывает Alembic. Это восстановление уже случившегося обрыва, не stamp.

Чужой триггер с тем же именем, другой таблицей или другим SQL даёт отказ до DDL и до смены версии. Он не снимается и не пересоздаётся. `IF NOT EXISTS` нет.

Downgrade этой ревизии такой же. Канонический триггер снимается внутри `BEGIN`. Если его уже нет, версия только сдвигается. Чужой триггер сохраняется, версия остаётся `b4e8c1a90d27`. Старые downgrade не чинились.

## Что проверено

Новая синтетическая SQLite, полная цепочка до `f6b2d8c14e90`, затем эта ревизия. Движок — `create_app()` через `migrations/env.py`, `PRAGMA foreign_keys = 0`. Пробы обрыва идут через `alembic.command`, чтобы исключение не превращалось в `SystemExit` обёртки Flask-Migrate. Соединение то же.

После `engine.dispose()` свежий `sqlite3.connect` видит согласованные схему и версию. Повтор успешен. Заранее сохранённый канонический триггер при старой версии не переписывается. Несовпадающий триггер и строка-свидетель остаются. Тексты прежних 12 триггеров те же. Backfill нет.

Стек: Python 3.12.3, SQLite 3.45.1, Alembic 1.15.2, Flask-Migrate 4.0.7, SQLAlchemy 2.0.40, Flask 3.0.3.

## Что остаётся открытым

- Finding 1: `posted` и `posted_at IS NULL` после `posted → open` принимает первую дату. Правило статуса HQ не утверждала, здесь его нет.
- Прямой `UPDATE status`, `INSERT` новой строки и `DELETE` смены триггер не держит.
- 56 тестов смен и реализаций по-прежнему собирают схему через `db.create_all()` и этот триггер не создают.
- Откат `f6b2d8c14e90` по-прежнему оставляет четыре колонки.
- Downgrade `d1f2a3b4c5d6` и `c1d03f92e3c6` остаются отдельным BLOCKED.

## Команды

Не на `trailers.db`:

`/workspace/.venv/bin/python -m unittest tests.test_production_shift_posted_at_revision_atomicity`

13 тестов, OK, 3.704 с.

`/workspace/.venv/bin/python -m unittest tests.test_schema_drift_four_columns tests.test_produced_unit_link_sqlite_guard tests.test_realization_produced_unit_link tests.test_shift_director_report tests.test_shift_mass_post tests.test_alembic_shift_realization_integration tests.test_production_shift_posted_at_guard tests.test_production_shift_posted_at_revision_atomicity`

100 тестов, OK, 111.856 с: прежние 87 и 13 новых.

Рабочая `trailers.db` через SQLite не открывалась: inode `4245445`, размер `114688`, время `2026-09-30 17:51:39` те же до и после. PR, merge и deploy нет.
