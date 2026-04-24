"""Shared helpers for payout period calculations."""
from datetime import datetime, date, timedelta


def _fmt_date(d: date) -> str:
    """Return e.g. 'April 23' without zero-padding (cross-platform)."""
    return d.strftime('%B ') + str(d.day)


def _fmt_time(hour: int, minute: int) -> str:
    """Return e.g. '6:00 PM'."""
    dt = datetime(2000, 1, 1, hour, minute)
    return dt.strftime('%I:%M %p').lstrip('0')


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

    cadence        = _s('payout_cadence',      'instant')
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
        tomorrow       = today + timedelta(days=1)
        next_payout    = f"Tomorrow at {_fmt_time(payout_hour, payout_minute)}"

    elif cadence == 'weekly':
        week_start     = today - timedelta(days=today.weekday())
        period_start   = datetime.combine(week_start, datetime.min.time())
        period_label   = f"Week of {_fmt_date(week_start)}"
        days_ahead     = (dow_val - today.weekday()) % 7 or 7
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
