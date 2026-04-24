from datetime import datetime
from . import db


class Child(db.Model):
    __tablename__ = 'children'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    avatar_color = db.Column(db.String(7), default='#6366f1')
    balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assigned_chores = db.relationship(
        'AssignedChore', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )
    transactions = db.relationship(
        'BalanceTransaction', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )
    wishlist_items = db.relationship(
        'WishlistItem', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )


class Chore(db.Model):
    __tablename__ = 'chores'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    default_value = db.Column(db.Float, default=1.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignments = db.relationship('AssignedChore', back_populates='chore', lazy=True)


class AssignedChore(db.Model):
    __tablename__ = 'assigned_chores'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    chore_id = db.Column(db.Integer, db.ForeignKey('chores.id'), nullable=False)
    custom_value = db.Column(db.Float)  # overrides chore.default_value when set
    # Status flow: assigned -> submitted -> approved | denied -> assigned
    status = db.Column(db.String(20), default='assigned')
    assigned_date = db.Column(db.DateTime, default=datetime.utcnow)
    submitted_date = db.Column(db.DateTime)
    approved_date = db.Column(db.DateTime)
    period = db.Column(db.String(20))       # period key: '2024-04-15', '2024-W17', '2024-04'
    denial_notes = db.Column(db.Text)
    # Recurrence lives on the assignment, not on the chore template
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_cadence = db.Column(db.String(20))  # 'daily', 'weekly', 'monthly'

    child = db.relationship('Child', back_populates='assigned_chores')
    chore = db.relationship('Chore', back_populates='assignments')

    @property
    def effective_value(self):
        if self.custom_value is not None:
            return self.custom_value
        return self.chore.default_value if self.chore else 0.0


class BalanceTransaction(db.Model):
    __tablename__ = 'balance_transactions'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200))
    transaction_date = db.Column(db.DateTime, default=datetime.utcnow)
    assigned_chore_id = db.Column(db.Integer, db.ForeignKey('assigned_chores.id'))

    child = db.relationship('Child', back_populates='transactions')


class WishlistItem(db.Model):
    __tablename__ = 'wishlist_items'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False)
    url = db.Column(db.String(500))
    status = db.Column(db.String(20), default='active')  # 'active', 'purchased'
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    purchased_date = db.Column(db.DateTime)

    child = db.relationship('Child', back_populates='wishlist_items')


class AppSettings(db.Model):
    __tablename__ = 'app_settings'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)
