from datetime import datetime
from . import db


class Child(db.Model):
    __tablename__ = 'children'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    avatar_color = db.Column(db.String(7), default='#6366f1')
    avatar_filename = db.Column(db.String(200))
    balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.now)

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
    icon = db.Column(db.String(10))
    default_value = db.Column(db.Float, default=1.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    assignments = db.relationship('AssignedChore', back_populates='chore', lazy=True)


class AssignedChore(db.Model):
    __tablename__ = 'assigned_chores'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    chore_id = db.Column(db.Integer, db.ForeignKey('chores.id'), nullable=False)
    custom_value = db.Column(db.Float)  # overrides chore.default_value when set
    override_name = db.Column(db.String(100))
    override_description = db.Column(db.Text)
    # Status flow: assigned -> submitted -> approved | denied -> assigned
    status = db.Column(db.String(20), default='assigned')
    assigned_date = db.Column(db.DateTime, default=datetime.now)
    submitted_date = db.Column(db.DateTime)
    approved_date = db.Column(db.DateTime)
    period = db.Column(db.String(20))       # period key: '2024-04-15', '2024-W17', '2024-04'
    denial_notes = db.Column(db.Text)
    # Recurrence lives on the assignment, not on the chore template
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_cadence = db.Column(db.String(20))  # 'daily', 'weekly', 'monthly'
    recurrence_day = db.Column(db.Integer)          # weekly: 0=Mon…6=Sun; monthly: 1–31
    awarded_value = db.Column(db.Float)              # set when partial credit is given at approval

    child = db.relationship('Child', back_populates='assigned_chores')
    chore = db.relationship('Chore', back_populates='assignments')

    @property
    def effective_name(self):
        return self.override_name or (self.chore.name if self.chore else '')

    @property
    def effective_description(self):
        return self.override_description or (self.chore.description if self.chore else '')

    @property
    def recurrence_label(self):
        if not self.is_recurring or not self.recurrence_cadence:
            return None
        if self.recurrence_cadence == 'daily':
            return 'Daily'
        if self.recurrence_cadence == 'weekly':
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            day = day_names[self.recurrence_day] if self.recurrence_day is not None else 'Monday'
            return f'Weekly · Every {day}'
        if self.recurrence_cadence == 'monthly':
            d = self.recurrence_day if self.recurrence_day is not None else 1
            suffix = 'th' if 11 <= d <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')
            return f'Monthly · {d}{suffix} of each month'
        return self.recurrence_cadence.capitalize()

    @property
    def effective_value(self):
        if self.custom_value is not None:
            return self.custom_value
        return self.chore.default_value if self.chore else 0.0

    @property
    def actual_payout(self):
        """What was (or will be) actually credited — may be less than effective_value for partial credit."""
        if self.awarded_value is not None:
            return self.awarded_value
        return self.effective_value

    @property
    def is_partial(self):
        return self.awarded_value is not None and self.awarded_value < self.effective_value


class BalanceTransaction(db.Model):
    __tablename__ = 'balance_transactions'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200))
    transaction_date = db.Column(db.DateTime, default=datetime.now)
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
    created_at = db.Column(db.DateTime, default=datetime.now)
    purchased_date = db.Column(db.DateTime)

    child = db.relationship('Child', back_populates='wishlist_items')


class AppSettings(db.Model):
    __tablename__ = 'app_settings'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)
