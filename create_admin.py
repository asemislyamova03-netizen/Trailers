# create_admin.py
from app import create_app
from extensions import db
from models import User

app = create_app()

with app.app_context():
    # проверим, вдруг админ уже есть
    existing = User.query.filter_by(username='admin').first()
    if existing:
        print("Администратор уже существует:", existing)
    else:
        admin = User(
            username='admin',
            full_name='Администратор',
            role='admin',
            warehouse_id=None
        )
        admin.set_password('admin123')  # потом поменяешь

        db.session.add(admin)
        db.session.commit()
        print("Администратор создан: admin / admin123")

