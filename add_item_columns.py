# add_item_columns.py

from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    # 1) добавляем высоту тента
    try:
        db.session.execute(text("ALTER TABLE item ADD COLUMN tent_hight_mm INTEGER"))
        print("Добавлен столбец item.tent_hight_mm")
    except Exception as e:
        print("tent_hight_mm: возможно уже существует или другая ошибка:", e)

    # 2) добавляем признак подкатного колеса
    try:
        db.session.execute(text("ALTER TABLE item ADD COLUMN has_jockey_wheel BOOLEAN"))
        print("Добавлен столбец item.has_jockey_wheel")
    except Exception as e:
        print("has_jockey_wheel: возможно уже существует или другая ошибка:", e)

    db.session.commit()
    print("Готово.")
