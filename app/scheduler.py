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


def process_scheduled_payouts(app):
    """Process approved_pending chores — fired by APScheduler at the configured time."""
    with app.app_context():
        from .models import AssignedChore, BalanceTransaction, AppSettings
        from . import db

        cadence = AppSettings.query.get('payout_cadence')
        if not cadence or cadence.value == 'instant':
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

        db.session.commit()
        if pending:
            logger.info('Processed %d scheduled payouts', len(pending))


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
        logger.info('Payout job scheduled: cadence=%s kwargs=%s', cadence, cron_kwargs)


def check_missed_payout(app):
    """At startup, fire any payout whose scheduled time has already passed."""
    with app.app_context():
        from .models import AssignedChore, AppSettings

        cadence_s = AppSettings.query.get('payout_cadence')
        cadence = cadence_s.value if cadence_s else 'instant'
        if cadence == 'instant':
            return

        if not AssignedChore.query.filter_by(status='approved_pending').first():
            return

        time_s = AppSettings.query.get('payout_time')
        time_str = time_s.value if time_s else '18:00'
        hour, minute = (int(p) for p in time_str.split(':'))

        now = datetime.now()
        today = now.date()

        if cadence == 'daily':
            last_trigger = datetime(today.year, today.month, today.day, hour, minute)
            if now < last_trigger:
                last_trigger -= timedelta(days=1)

        elif cadence == 'weekly':
            dow_s = AppSettings.query.get('payout_day_of_week')
            dow = int(dow_s.value) if dow_s else 0
            days_since = (today.weekday() - dow) % 7
            trigger_date = today - timedelta(days=days_since)
            last_trigger = datetime(trigger_date.year, trigger_date.month, trigger_date.day, hour, minute)
            if now < last_trigger:
                last_trigger -= timedelta(weeks=1)

        elif cadence == 'monthly':
            dom_s = AppSettings.query.get('payout_day_of_month')
            dom = int(dom_s.value) if dom_s else 1
            try:
                last_trigger = datetime(today.year, today.month, dom, hour, minute)
            except ValueError:
                import calendar as _cal
                last_trigger = datetime(today.year, today.month,
                                        _cal.monthrange(today.year, today.month)[1], hour, minute)
            if now < last_trigger:
                prev_month = today.month - 1 or 12
                prev_year = today.year if today.month > 1 else today.year - 1
                try:
                    last_trigger = datetime(prev_year, prev_month, dom, hour, minute)
                except ValueError:
                    import calendar as _cal
                    last_trigger = datetime(prev_year, prev_month,
                                            _cal.monthrange(prev_year, prev_month)[1], hour, minute)
        else:
            return

        if now >= last_trigger:
            logger.info('Startup: missed payout (last trigger: %s) — processing now.', last_trigger)
            process_scheduled_payouts(app)


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
