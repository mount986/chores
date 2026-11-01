"""Shared helpers for payout period calculations."""
import calendar as _cal
import logging
import math
import os
import sqlite3
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

CHORE_ICONS = [
    ('🛏️', 'Make Bed'),
    ('🍽️', 'Dishes'),
    ('🧹', 'Sweep/Vacuum'),
    ('👕', 'Laundry'),
    ('🗑️', 'Trash'),
    ('🚿', 'Bathroom'),
    ('🌿', 'Lawn/Garden'),
    ('🐾', 'Feed Pet'),
    ('🍴', 'Set Table'),
    ('🧽', 'Wipe/Clean'),
    ('📚', 'Homework'),
    ('📖', 'Reading'),
    ('🪟', 'Windows'),
    ('📦', 'Organize'),
    ('🍳', 'Cooking'),
    ('🛒', 'Groceries'),
    ('♻️', 'Recycling'),
    ('🚗', 'Car'),
    ('🪴', 'Plants'),
    ('🐕', 'Walk Dog'),
    ('❄️', 'Freezer/Fridge'),
    ('🪣', 'Mopping'),
]


def backup_database() -> str | None:
    """
    Copy the SQLite database to a *_backup.db file in the same directory.
    Uses sqlite3's online-backup API so the copy is always consistent even
    under concurrent reads/writes.  The backup file is overwritten each call.
    Returns the backup path on success, or None if skipped (non-SQLite or
    source file not found).  Must be called inside a Flask app context.
    """
    from flask import current_app

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:'):
        logger.debug('backup_database: skipped (non-SQLite URI)')
        return None

    # sqlite:///relative  → relative to instance folder
    # sqlite:////absolute → absolute path
    rel_path = uri[len('sqlite:///'):]
    if os.path.isabs(rel_path):
        db_path = rel_path
    else:
        db_path = os.path.join(current_app.instance_path, rel_path)

    if not os.path.exists(db_path):
        logger.warning('backup_database: source not found at %s', db_path)
        return None

    stem, ext = os.path.splitext(db_path)
    backup_path = f'{stem}_backup{ext or ".db"}'

    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        logger.info('Database backed up → %s', backup_path)
        return backup_path
    except Exception:
        logger.exception('backup_database: failed to back up %s', db_path)
        return None


def next_recurrence_date(cadence: str, rec_day, after_date: date) -> date:
    """Return the next calendar date this recurring chore will be scheduled, strictly after after_date."""
    if cadence == 'daily':
        return after_date + timedelta(days=1)

    if cadence == 'weekly':
        target_dow = rec_day if rec_day is not None else 0
        days_ahead = (target_dow - after_date.weekday()) % 7 or 7
        return after_date + timedelta(days=days_ahead)

    if cadence == 'monthly':
        target_dom = rec_day if rec_day is not None else 1
        # Try this month if the day is still in the future
        last_this = _cal.monthrange(after_date.year, after_date.month)[1]
        d_this = min(target_dom, last_this)
        if d_this > after_date.day:
            return date(after_date.year, after_date.month, d_this)
        # Otherwise next month
        y, m = (after_date.year + 1, 1) if after_date.month == 12 else (after_date.year, after_date.month + 1)
        return date(y, m, min(target_dom, _cal.monthrange(y, m)[1]))

    return after_date + timedelta(days=1)


def _fmt_date(d: date) -> str:
    """Return e.g. 'April 23' without zero-padding (cross-platform)."""
    return d.strftime('%B ') + str(d.day)


def _fmt_time(hour: int, minute: int) -> str:
    """Return e.g. '6:00 PM'."""
    dt = datetime(2000, 1, 1, hour, minute)
    return dt.strftime('%I:%M %p').lstrip('0')


def is_payout_day_enabled() -> bool:
    """True when approved chores wait for a scheduled payout day. Needs an app context."""
    from .models import AppSettings
    row = AppSettings.query.get('payout_day_enabled')
    return bool(row) and row.value == 'on'


def get_payout_cadence() -> str:
    """Effective cadence: 'instant' whenever payout day is disabled. Needs an app context."""
    from .models import AppSettings
    if not is_payout_day_enabled():
        return 'instant'
    row = AppSettings.query.get('payout_cadence')
    return row.value if row else 'instant'


def settle_pending_payouts(description_prefix: str) -> dict:
    """
    Pay out every approved_pending chore instance. Adds to balances and logs transactions
    but does not commit. Returns {child name: total paid}.
    """
    from .models import ChoreInstance, BalanceTransaction, Payout
    from . import db

    totals: dict = {}
    payouts: dict = {}  # child_id → Payout
    now = datetime.now()
    for inst in ChoreInstance.query.filter_by(status='approved_pending').all():
        if inst.child_id not in payouts:
            payouts[inst.child_id] = Payout(child_id=inst.child_id, paid_date=now)
            db.session.add(payouts[inst.child_id])
        amount = inst.actual_payout
        inst.status = 'approved'
        inst.terminal_date = datetime.now()
        inst.child.balance += amount
        partial_note = (
            f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})'
            if inst.is_partial else ''
        )
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'{description_prefix}: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
            payout=payouts[inst.child_id],
        ))
        totals[inst.child.name] = totals.get(inst.child.name, 0) + amount
    return totals


def get_payout_period_info() -> dict:
    """
    Returns a dict describing the current payout period:
      cadence, period_label, period_start (datetime),
      next_payout_str, payout_time_str
    Must be called inside a Flask app context.
    """
    from .models import AppSettings

    def _s(key, default):
        row = AppSettings.query.get(key)
        return row.value if row else default

    cadence        = get_payout_cadence()
    time_str       = _s('payout_time',         '18:00')
    dow_val        = int(_s('payout_day_of_week',  '0'))   # 0 = Monday
    dom_val        = int(_s('payout_day_of_month', '1'))

    payout_hour, payout_minute = (int(p) for p in time_str.split(':'))
    today = date.today()

    DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                 'Friday', 'Saturday', 'Sunday']

    if cadence == 'instant':
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = f"Today, {_fmt_date(today)}"
        next_payout    = "Immediately on approval"

    elif cadence == 'daily':
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = f"Today, {_fmt_date(today)}"
        next_payout    = f"Tomorrow at {_fmt_time(payout_hour, payout_minute)}"

    elif cadence == 'weekly':
        week_start     = today - timedelta(days=today.weekday())
        period_start   = datetime.combine(week_start, datetime.min.time())
        period_label   = f"Week of {_fmt_date(week_start)}"
        days_ahead     = (dow_val - today.weekday()) % 7
        if days_ahead == 0:
            payout_dt = datetime.combine(today, datetime.min.time().replace(hour=payout_hour, minute=payout_minute))
            if datetime.now() >= payout_dt:
                days_ahead = 7
        next_day       = today + timedelta(days=days_ahead)
        next_payout    = (f"{DAY_NAMES[dow_val]}, {_fmt_date(next_day)}"
                          f" at {_fmt_time(payout_hour, payout_minute)}")

    elif cadence == 'monthly':
        period_start   = datetime.combine(today.replace(day=1), datetime.min.time())
        period_label   = today.strftime('%B %Y')
        if today.day < dom_val:
            next_day   = today.replace(day=dom_val)
        else:
            y, m       = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            next_day   = date(y, m, dom_val)
        next_payout    = f"{_fmt_date(next_day)} at {_fmt_time(payout_hour, payout_minute)}"

    else:
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = _fmt_date(today)
        next_payout    = "—"

    return {
        'cadence':       cadence,
        'period_label':  period_label,
        'period_start':  period_start,
        'next_payout':   next_payout,
        'payout_time':   time_str,
    }


def parse_quantity(raw) -> int:
    """Whole number of units from a form value; anything missing, invalid or below 1 counts as 1."""
    try:
        return max(int(raw), 1)
    except (ValueError, TypeError):
        return 1


def parse_money(raw):
    """Dollar amount from a form value, rounded to cents.

    Returns None when blank; raises ValueError when not a finite number.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError('Amount must be a finite number.')
    return round(value, 2)


def to_date(value) -> date:
    """Coerce a datetime or date to a date."""
    return value.date() if isinstance(value, datetime) else value


def parse_completed_date(raw):
    """Date a chore was completed, from a 'YYYY-MM-DD' form value.

    Returns None when blank; raises ValueError when malformed or in the future.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    day = date.fromisoformat(raw)
    if day > date.today():
        raise ValueError('Completed date cannot be in the future.')
    return day


def is_child_suggestions_enabled() -> bool:
    """True when children may suggest new chores from their dashboard. Needs an app context."""
    from .models import AppSettings
    row = AppSettings.query.get('child_suggestions_enabled')
    return bool(row) and row.value == 'on'


def get_or_create_book_chore():
    from .models import Chore
    from . import db
    chore = Chore.query.filter_by(name='Read a Book').first()
    if not chore:
        chore = Chore(name='Read a Book', description='Finish a library book',
                      icon='📚', default_value=0.0, is_active=False)
        db.session.add(chore)
        db.session.flush()
    return chore
