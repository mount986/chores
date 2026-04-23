import logging
from datetime import date
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
            )
            .filter(AssignedChore.is_recurring == True)  # noqa: E712
            .distinct()
            .all()
        )

        for child_id, chore_id, cadence in combos:
            if not cadence:
                continue
            period = get_period(cadence, today)
            if period is None:
                continue
            exists = AssignedChore.query.filter_by(
                child_id=child_id, chore_id=chore_id, period=period
            ).first()
            if not exists:
                db.session.add(AssignedChore(
                    child_id=child_id,
                    chore_id=chore_id,
                    status='assigned',
                    is_recurring=True,
                    recurrence_cadence=cadence,
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
            amount = ac.effective_value
            ac.status = 'approved'
            ac.child.balance += amount
            db.session.add(BalanceTransaction(
                child_id=ac.child_id,
                amount=amount,
                description=f'Scheduled payout: {ac.chore.name}',
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
