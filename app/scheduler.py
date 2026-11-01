import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler()
_app = None  # stored at init so reschedule_payout_job() needs no argument


def _get_timezone():
    """Return the configured ZoneInfo, falling back to UTC on any error."""
    try:
        from .models import AppSettings
        return ZoneInfo(AppSettings.get('app_timezone', 'UTC'))
    except Exception:
        return ZoneInfo('UTC')


def get_period(cadence: str, today: date) -> str | None:
    if cadence == 'daily':
        return today.isoformat()
    if cadence == 'weekly':
        iso = today.isocalendar()
        return f'{iso.year}-W{iso.week:02d}'
    if cadence == 'monthly':
        return f'{today.year}-{today.month:02d}'
    return None


def assign_recurring_chores(app):
    with app.app_context():
        from .models import AssignedChore, ChoreInstance
        from . import db

        tz = _get_timezone()
        today = datetime.now(tz).date()

        # Iterate active recurring configs (one per child+chore combo)
        configs = (
            AssignedChore.query
            .filter_by(is_recurring=True, is_active=True)
            .all()
        )

        for ac in configs:
            cadence = ac.recurrence_cadence
            rec_day = ac.recurrence_day
            if not cadence:
                continue

            # Gate: only create on the configured day
            if cadence == 'weekly':
                target_dow = rec_day if rec_day is not None else 0
                if today.weekday() != target_dow:
                    continue
            elif cadence == 'monthly':
                target_dom = rec_day if rec_day is not None else 1
                if today.day != target_dom:
                    continue

            period = get_period(cadence, today)
            if period is None:
                continue

            # Skip if an instance already exists for this period
            exists = ChoreInstance.query.filter_by(
                assigned_chore_id=ac.id, period=period
            ).first()
            if exists:
                continue

            # Expire any unsubmitted instances from previous periods
            old_assigned = ChoreInstance.query.filter(
                ChoreInstance.assigned_chore_id == ac.id,
                ChoreInstance.period != period,
                ChoreInstance.status == 'assigned',
            ).all()
            for old in old_assigned:
                if old.assigned_chore.is_per_unit and old.unit_tally != {'submitted': 0, 'approved': 0}:
                    # Units were submitted last period, so the open slot isn't a miss — just retire it.
                    db.session.delete(old)
                    continue
                old.status = 'expired'
                old.terminal_date = datetime.now()
                logger.info(
                    'Expired chore %s for child %s (period %s)',
                    ac.chore_id, ac.child_id, old.period,
                )

            db.session.add(ChoreInstance(
                assigned_chore_id=ac.id,
                status='assigned',
                period=period,
                assigned_date=datetime.now(),
            ))
            logger.info(
                'Auto-assigned chore %s to child %s for %s',
                ac.chore_id, ac.child_id, period,
            )

        db.session.commit()


def _compute_next_payout(cadence, hour, minute, dow=0, dom=1, tz=None) -> datetime:
    """Return the next future naive datetime (in the configured timezone) when a payout should fire."""
    _tz = tz or ZoneInfo('UTC')
    now = datetime.now(_tz).replace(tzinfo=None)
    today = now.date()

    if cadence == 'daily':
        candidate = datetime(today.year, today.month, today.day, hour, minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if cadence == 'weekly':
        days_ahead = (dow - today.weekday()) % 7
        if days_ahead == 0:
            candidate = datetime(today.year, today.month, today.day, hour, minute)
            if candidate <= now:
                days_ahead = 7
        if days_ahead:
            target = today + timedelta(days=days_ahead)
            candidate = datetime(target.year, target.month, target.day, hour, minute)
        return candidate

    if cadence == 'monthly':
        import calendar as _cal
        def _try(year, month):
            last_day = _cal.monthrange(year, month)[1]
            d = min(dom, last_day)
            return datetime(year, month, d, hour, minute)

        candidate = _try(today.year, today.month)
        if candidate <= now:
            next_month = today.month % 12 + 1
            next_year = today.year + (1 if next_month == 1 else 0)
            candidate = _try(next_year, next_month)
        return candidate

    raise ValueError(f'Unknown cadence: {cadence}')


def _payout_hour_minute():
    """The configured payout time of day as (hour, minute)."""
    from .models import AppSettings
    hour, minute = (int(p) for p in AppSettings.get('payout_time', '18:00').split(':'))
    return hour, minute


def _save_next_payout(cadence, hour, minute, dow=0, dom=1, tz=None):
    """Compute and persist next_payout_at in AppSettings."""
    from .models import AppSettings
    nxt = _compute_next_payout(cadence, hour, minute, dow, dom, tz=tz)
    AppSettings.set('next_payout_at', nxt.isoformat())
    return nxt


def process_scheduled_payouts(app):
    """Process approved_pending chore instances — fired by APScheduler at the configured time."""
    with app.app_context():
        from .models import AppSettings
        from .utils import get_payout_cadence, settle_pending_payouts
        from . import db

        cadence = get_payout_cadence()
        if cadence == 'instant':
            return

        pending_totals = settle_pending_payouts('Scheduled payout')

        # Advance next_payout_at
        tz = _get_timezone()
        hour, minute = _payout_hour_minute()
        dow = int(AppSettings.get('payout_day_of_week', '0'))
        dom = int(AppSettings.get('payout_day_of_month', '1'))
        nxt = _save_next_payout(cadence, hour, minute, dow, dom, tz=tz)

        db.session.commit()
        if pending_totals:
            logger.info('Processed scheduled payouts for %d children — next payout at %s', len(pending_totals), nxt)
            from .utils import backup_database
            backup_database()


def reschedule_payout_job():
    """Read payout settings and rebuild the APScheduler cron jobs to match."""
    if _app is None:
        return

    with _app.app_context():
        from .models import AppSettings
        from .utils import get_payout_cadence
        from . import db

        tz = _get_timezone()

        # Re-add recurring chores job with updated timezone
        _scheduler.add_job(
            assign_recurring_chores,
            trigger='cron',
            hour=0, minute=1,
            args=[_app],
            id='recurring_chores',
            replace_existing=True,
            timezone=tz,
        )

        cadence = get_payout_cadence()

        if _scheduler.get_job('scheduled_payouts'):
            _scheduler.remove_job('scheduled_payouts')

        if cadence == 'instant':
            logger.info('Payout day disabled or instant — no scheduled job needed')
            return

        hour, minute = _payout_hour_minute()

        cron_kwargs: dict = {'hour': hour, 'minute': minute}

        if cadence == 'weekly':
            cron_kwargs['day_of_week'] = int(AppSettings.get('payout_day_of_week', '0'))
        elif cadence == 'monthly':
            cron_kwargs['day'] = int(AppSettings.get('payout_day_of_month', '1'))

        _scheduler.add_job(
            process_scheduled_payouts,
            trigger='cron',
            args=[_app],
            id='scheduled_payouts',
            replace_existing=True,
            timezone=tz,
            **cron_kwargs,
        )

        dow = cron_kwargs.get('day_of_week', 0)
        dom = cron_kwargs.get('day', 1)
        nxt = _save_next_payout(cadence, hour, minute, dow, dom, tz=tz)
        db.session.commit()

        logger.info('Payout job scheduled: cadence=%s tz=%s kwargs=%s next=%s', cadence, tz, cron_kwargs, nxt)


def check_missed_payout(app):
    """At startup, fire a payout only if now is past the stored next_payout_at time."""
    with app.app_context():
        from .models import AppSettings
        from .utils import get_payout_cadence

        if get_payout_cadence() == 'instant':
            return

        next_value = AppSettings.get('next_payout_at')
        if not next_value:
            return

        next_payout = datetime.fromisoformat(next_value)
        tz = _get_timezone()
        now = datetime.now(tz).replace(tzinfo=None)

        if now >= next_payout:
            logger.info('Startup: payout was due at %s — processing now.', next_payout)
            process_scheduled_payouts(app)
        else:
            logger.info('Startup: next payout not due until %s — skipping.', next_payout)


def init_scheduler(app):
    global _app
    _app = app

    with app.app_context():
        tz = _get_timezone()

    _scheduler.add_job(
        assign_recurring_chores,
        trigger='cron',
        hour=0, minute=1,
        args=[app],
        id='recurring_chores',
        replace_existing=True,
        timezone=tz,
    )

    if not _scheduler.running:
        _scheduler.start()

    reschedule_payout_job()
    assign_recurring_chores(app)
    check_missed_payout(app)
