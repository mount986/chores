import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
    db_path = os.environ.get('DATABASE_URL', 'sqlite:///chores.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['PERMANENT_SESSION_LIFETIME'] = 3600 * 8  # 8 hours

    db.init_app(app)

    from .routes.main import main_bp
    from .routes.child import child_bp
    from .routes.parent import parent_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(child_bp, url_prefix='/child')
    app.register_blueprint(parent_bp, url_prefix='/parent')

    with app.app_context():
        db.create_all()
        _migrate_db()
        _seed_defaults()

    from .scheduler import init_scheduler
    init_scheduler(app)

    return app


def _migrate_db():
    """Idempotently add columns that were introduced after initial schema creation."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    existing = {c['name'] for c in inspector.get_columns('assigned_chores')}
    with db.engine.connect() as conn:
        if 'is_recurring' not in existing:
            conn.execute(text('ALTER TABLE assigned_chores ADD COLUMN is_recurring BOOLEAN NOT NULL DEFAULT 0'))
            conn.commit()
        if 'recurrence_cadence' not in existing:
            conn.execute(text('ALTER TABLE assigned_chores ADD COLUMN recurrence_cadence VARCHAR(20)'))
            conn.commit()


def _seed_defaults():
    from .models import AppSettings, Chore
    import bcrypt

    if not AppSettings.query.get('parent_pin'):
        pin_hash = bcrypt.hashpw(b'1234', bcrypt.gensalt()).decode()
        db.session.add(AppSettings(key='parent_pin', value=pin_hash))

    if not AppSettings.query.get('payout_cadence'):
        db.session.add(AppSettings(key='payout_cadence', value='instant'))
    if not AppSettings.query.get('payout_time'):
        db.session.add(AppSettings(key='payout_time', value='18:00'))
    if not AppSettings.query.get('payout_day_of_week'):
        db.session.add(AppSettings(key='payout_day_of_week', value='0'))
    if not AppSettings.query.get('payout_day_of_month'):
        db.session.add(AppSettings(key='payout_day_of_month', value='1'))

    if Chore.query.count() == 0:
        defaults = [
            ('Make Bed', 'Make your bed neatly each morning', 0.50),
            ('Do Homework', 'Complete all homework assignments', 1.00),
            ('Clean Room', 'Tidy and organize your room', 1.50),
            ('Wash Dishes', 'Wash and dry the dishes', 1.00),
            ('Take Out Trash', 'Take trash cans out to the curb', 1.00),
            ('Vacuum Living Room', 'Vacuum the living room floor', 1.50),
            ('Feed Pet', 'Feed and water the pet', 0.50),
            ('Set Table', 'Set the dinner table before meals', 0.50),
            ('Sweep Floor', 'Sweep the kitchen floor', 0.75),
            ('Wipe Counters', 'Wipe down kitchen counters', 0.75),
            ('Clean Bathroom', 'Clean and scrub the bathroom', 2.00),
            ('Put Away Laundry', 'Fold and put away clean laundry', 1.00),
            ('Read for 20 Minutes', 'Read a book for at least 20 minutes', 0.50),
            ('Mow Lawn', 'Mow the front and back lawn', 5.00),
            ('Weed Garden', 'Pull weeds from the garden', 2.00),
        ]
        for name, desc, value in defaults:
            db.session.add(Chore(name=name, description=desc, default_value=value))

    db.session.commit()
