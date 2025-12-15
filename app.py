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

    # Регистрируем blueprint с маршрутами
    app.register_blueprint(main_bp)

    @app.route('/')
    def index():
        from flask import redirect, url_for
        from flask_login import current_user

        if current_user.is_authenticated and getattr(current_user, 'is_manager', False):
            return redirect(url_for('main.manager_workspace'))

        return redirect(url_for('main.trailers_list'))


    return app
