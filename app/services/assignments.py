"""Creating chore assignments."""
import calendar
from datetime import date, datetime, timedelta

from .. import db
from ..models import AssignedChore, ChoreInstance
from ..scheduler import get_period


def create_assignment(child_id, chore_id, custom_value, is_recurring, cadence, day):
    """Add an AssignedChore config row plus its first instance. Caller commits."""
    today = date.today()
    if is_recurring and cadence == 'weekly' and day is not None:
        ref_date = today - timedelta(days=(today.weekday() - day) % 7)
    elif is_recurring and cadence == 'monthly' and day is not None:
        if today.day >= day:
            ref_date = today.replace(day=day)
        else:
            prev_month = today.month - 1 or 12
            prev_year = today.year if today.month > 1 else today.year - 1
            last_day = calendar.monthrange(prev_year, prev_month)[1]
            ref_date = date(prev_year, prev_month, min(day, last_day))
    else:
        ref_date = today

    period = get_period(cadence, ref_date) if is_recurring and cadence else None

    ac_config = AssignedChore(
        child_id=child_id,
        chore_id=chore_id,
        custom_value=custom_value,
        is_recurring=is_recurring,
        recurrence_cadence=cadence,
        recurrence_day=day,
        is_active=True,
    )
    db.session.add(ac_config)
    db.session.flush()  # get ac_config.id

    db.session.add(ChoreInstance(
        assigned_chore_id=ac_config.id,
        status='assigned',
        period=period,
        assigned_date=datetime.now(),
    ))
    return ac_config
