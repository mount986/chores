import logging
from datetime import date, datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler()
_app = None  # stored at init so reschedule_payout_job() needs no argument


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
        from .models import AssignedChore
        from . import db

        today = date.today()

        # Find all unique (child, chore, cadence) combos that are flagged recurring.
        # We look across all statuses so completed/approved instances still seed future ones.
        combos = (
            db.session.query(
                AssignedChore.child_id,
                AssignedChore.chore_id,
                AssignedChore.recurrence_cadence,
                AssignedChore.recurrence_day,
            )
            .filter(AssignedChore.is_recurring == True)  # noqa: E712
            .distinct()
            .all()
        )

        for child_id, chore_id, cadence, rec_day in combos:
            if not cadence:
                continue

            # Enforce day-of-week / day-of-month gate
            if cadence == 'weekly':
                target_dow = rec_day if rec_day is not None else 0  # default Monday
                if today.weekday() != target_dow:
                    continue
            elif cadence == 'monthly':
                target_dom = rec_day if rec_day is not None else 1  # default 1st
                if today.day != target_dom:
                    continue

            period = get_period(cadence, today)
            if period is None:
                continue
            exists = AssignedChore.query.filter_by(
                child_id=child_id, chore_id=chore_id, period=period
            ).first()
            if not exists:
                # Expire any incomplete assignments from previous periods
                old_incomplete = AssignedChore.query.filter(
                    AssignedChore.child_id == child_id,
                    AssignedChore.chore_id == chore_id,
                    AssignedChore.is_recurring == True,  # noqa: E712
                    AssignedChore.period != period,
                    AssignedChore.status.in_(['assigned', 'submitted']),
                ).all()
                for old in old_incomplete:
                    old.status = 'expired'
                    logger.info('Expired chore %s for child %s (period %s)', chore_id, child_id, old.period)

                db.session.add(AssignedChore(
                    child_id=child_id,
                    chore_id=chore_id,
                    status='assigned',
                    is_recurring=True,
                    recurrence_cadence=cadence,
                    recurrence_day=rec_day,
                    period=period,
                ))
                logger.info('Auto-assigned chore %s to child %s for %s', chore_id, child_id, period)

        db.session.commit()


def _compute_next_payout(cadence, hour, minute, dow=0, dom=1) -> datetime:
    """Return the next future datetime when a payout should fire."""
    now = datetime.now()
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


def _save_next_payout(app_settings_module, db_module, cadence, hour, minute, dow=0, dom=1):
    """Compute and persist next_payout_at in AppSettings."""
    AppSettings = app_settings_module
    db = db_module
    nxt = _compute_next_payout(cadence, hour, minute, dow, dom)
    setting = AppSettings.query.get('next_payout_at')
    if setting:
        setting.value = nxt.isoformat()
    else:
        db.session.add(AppSettings(key='next_payout_at', value=nxt.isoformat()))
    return nxt


def process_scheduled_payouts(app):
    """Process approved_pending chores — fired by APScheduler at the configured time."""
    with app.app_context():
        from .models import AssignedChore, BalanceTransaction, AppSettings
        from . import db

        cadence_s = AppSettings.query.get('payout_cadence')
        cadence = cadence_s.value if cadence_s else 'instant'
        if cadence == 'instant':
            return

        pending = AssignedChore.query.filter_by(status='approved_pending').all()
        for ac in pending:
            amount = ac.actual_payout
            ac.status = 'approved'
            ac.child.balance += amount
            partial_note = f' (partial: ${amount:.2f} of ${ac.effective_value:.2f})' if ac.is_partial else ''
            db.session.add(BalanceTransaction(
                child_id=ac.child_id,
                amount=amount,
                description=f'Scheduled payout: {ac.chore.name}{partial_note}',
                assigned_chore_id=ac.id,
            ))

        # Advance next_payout_at to the following period so the next restart check is correct.
        time_s = AppSettings.query.get('payout_time')
        hour, minute = (int(p) for p in (time_s.value if time_s else '18:00').split(':'))
        dow = int(AppSettings.query.get('payout_day_of_week').value) if AppSettings.query.get('payout_day_of_week') else 0
        dom = int(AppSettings.query.get('payout_day_of_month').value) if AppSettings.query.get('payout_day_of_month') else 1
        nxt = _save_next_payout(AppSettings, db, cadence, hour, minute, dow, dom)

        db.session.commit()
        if pending:
            logger.info('Processed %d scheduled payouts — next payout at %s', len(pending), nxt)


def reschedule_payout_job():
    """Read payout settings and rebuild the APScheduler cron job to match."""
    if _app is None:
        return

    with _app.app_context():
        from .models import AppSettings

        cadence_s = AppSettings.query.get('payout_cadence')
        cadence = cadence_s.value if cadence_s else 'instant'

        # Remove any existing job first
        if _scheduler.get_job('scheduled_payouts'):
            _scheduler.remove_job('scheduled_payouts')

        if cadence == 'instant':
            logger.info('Payout cadence is instant — no scheduled job needed')
            return

        time_s = AppSettings.query.get('payout_time')
        time_str = time_s.value if time_s else '18:00'
        hour, minute = (int(p) for p in time_str.split(':'))

        cron_kwargs: dict = {'hour': hour, 'minute': minute}

        if cadence == 'weekly':
            dow_s = AppSettings.query.get('payout_day_of_week')
            cron_kwargs['day_of_week'] = int(dow_s.value) if dow_s else 0
        elif cadence == 'monthly':
            dom_s = AppSettings.query.get('payout_day_of_month')
            cron_kwargs['day'] = int(dom_s.value) if dom_s else 1

        _scheduler.add_job(
            process_scheduled_payouts,
            trigger='cron',
            args=[_app],
            id='scheduled_payouts',
            replace_existing=True,
            **cron_kwargs,
        )

        # Persist next_payout_at so startup checks work correctly.
        with _app.app_context():
            from .models import AppSettings
            from . import db
            dow = cron_kwargs.get('day_of_week', 0)
            dom = cron_kwargs.get('day', 1)
            nxt = _save_next_payout(AppSettings, db, cadence, hour, minute, dow, dom)
            db.session.commit()

        logger.info('Payout job scheduled: cadence=%s kwargs=%s next=%s', cadence, cron_kwargs, nxt)


def check_missed_payout(app):
    """At startup, fire a payout only if now is past the stored next_payout_at time."""
    with app.app_context():
        from .models import AssignedChore, AppSettings

        cadence_s = AppSettings.query.get('payout_cadence')
        if not cadence_s or cadence_s.value == 'instant':
            return

        next_s = AppSettings.query.get('next_payout_at')
        if not next_s:
            return  # no stored schedule yet — reschedule_payout_job will set it

        next_payout = datetime.fromisoformat(next_s.value)
        now = datetime.now()

        if now >= next_payout:
            logger.info('Startup: payout was due at %s — processing now.', next_payout)
            process_scheduled_payouts(app)
        else:
            logger.info('Startup: next payout not due until %s — skipping.', next_payout)


def init_scheduler(app):
    global _app
    _app = app

    _scheduler.add_job(
        assign_recurring_chores,
        trigger='cron',
        hour=0, minute=1,
        args=[app],
        id='recurring_chores',
        replace_existing=True,
    )

    if not _scheduler.running:
        _scheduler.start()

    reschedule_payout_job()
    assign_recurring_chores(app)
    check_missed_payout(app)
