import calendar as cal_module
import os
import bcrypt
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, current_app
from werkzeug.utils import secure_filename
from ..models import Child, Chore, AssignedChore, ChoreInstance, BalanceTransaction, AppSettings, WishlistItem
from .. import db

parent_bp = Blueprint('parent', __name__)


@parent_bp.context_processor
def inject_nav_children():
    """Make all children available in every parent template for the quick-switch bar."""
    if not session.get('parent_authenticated'):
        return {}
    children = Child.query.order_by(Child.name).all()
    return {'nav_children': children}


def parent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('parent_authenticated'):
            return redirect(url_for('parent.login'))
        return f(*args, **kwargs)
    return decorated


# ── Auth ─────────────────────────────────────────────────────────────────────

@parent_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pin = request.form.get('pin', '').encode()
        setting = AppSettings.query.get('parent_pin')
        if setting and bcrypt.checkpw(pin, setting.value.encode()):
            session['parent_authenticated'] = True
            session.permanent = True
            return redirect(url_for('parent.dashboard'))
        flash('Incorrect PIN. Please try again.', 'error')
    return render_template('parent/login.html')


@parent_bp.route('/logout')
def logout():
    session.pop('parent_authenticated', None)
    return redirect(url_for('main.home'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@parent_bp.route('/')
@parent_required
def dashboard():
    children = Child.query.order_by(Child.name).all()
    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(ChoreInstance.status == 'submitted')
        .order_by(ChoreInstance.submitted_date)
        .all()
    )
    return render_template(
        'parent/dashboard.html',
        children=children,
        pending_reviews=pending_reviews,
    )


# ── Child detail ──────────────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>')
@parent_required
def child_detail(child_id):
    from ..scheduler import get_period
    from ..utils import next_recurrence_date

    child = Child.query.get_or_404(child_id)
    today = date.today()

    # Priority order for sorting: submitted > assigned > upcoming > expired > pending payout > approved
    PRIORITY = {'submitted': 0, 'assigned': 1, 'upcoming': 2, 'expired': 3, 'approved_pending': 4, 'approved': 5}

    def _row_priority(instances, is_recurring, cadence):
        statuses = {inst.status for inst in instances}
        if 'submitted' in statuses:
            return 0
        if 'assigned' in statuses:
            return 1
        if is_recurring and cadence:
            current_p = get_period(cadence, today)
            if current_p and not any(i.period == current_p for i in instances):
                return 2  # upcoming
        if 'expired' in statuses:
            return 3
        if 'approved_pending' in statuses:
            return 4
        return 5

    chore_rows = []

    # 1. Recurring chores — one row per active config
    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=True, is_active=True
    ).all():
        cadence = ac_config.recurrence_cadence
        rec_day = ac_config.recurrence_day
        if not cadence:
            continue

        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        current_period = get_period(cadence, today)
        is_upcoming = bool(current_period and not any(i.period == current_period for i in instances))

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  True,
            'is_upcoming':   is_upcoming,
            'cadence':       cadence,
            'rec_day':       rec_day,
            'next_date':     next_recurrence_date(cadence, rec_day, today),
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, True, cadence),
        })

    # 2. Non-recurring — one row per active config (with its instances)
    #    Skip rows where every instance is already in a terminal state
    #    (approved, approved_pending, expired) — nothing left to act on.
    _active_statuses = {'assigned', 'submitted'}
    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=False, is_active=True
    ).all():
        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        if not any(i.status in _active_statuses for i in instances):
            continue

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  False,
            'is_upcoming':   False,
            'cadence':       None,
            'rec_day':       None,
            'next_date':     None,
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, False, None),
        })

    chore_rows.sort(key=lambda r: (r['priority'], r['config'].effective_name.lower()))

    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'submitted',
        )
        .order_by(ChoreInstance.submitted_date)
        .all()
    )

    all_chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    wishlist_active = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )
    wishlist_purchased = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .order_by(WishlistItem.purchased_date.desc())
        .all()
    )

    return render_template(
        'parent/child_detail.html',
        child=child,
        chore_rows=chore_rows,
        pending_reviews=pending_reviews,
        all_chores=all_chores,
        wishlist_active=wishlist_active,
        wishlist_purchased=wishlist_purchased,
        today=today,
    )


@parent_bp.route('/child/<int:child_id>/history')
@parent_required
def child_history(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)

    month_str = request.args.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = (int(p) for p in month_str.split('-'))
        date(year, month, 1)
    except (ValueError, AttributeError):
        year, month = date.today().year, date.today().month

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    days_in_month = [month_start + timedelta(days=i) for i in range((month_end - month_start).days)]

    day_str = request.args.get('day')
    selected_day = None
    if day_str:
        try:
            selected_day = date.fromisoformat(day_str)
        except ValueError:
            pass

    # Approved instances in this month
    approved_in_month = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status.in_(['approved', 'approved_pending']),
            ChoreInstance.approved_date >= datetime.combine(month_start, datetime.min.time()),
            ChoreInstance.approved_date < datetime.combine(month_end, datetime.min.time()),
        ).all()
    )

    txns_in_month = BalanceTransaction.query.filter(
        BalanceTransaction.child_id == child_id,
        BalanceTransaction.transaction_date >= datetime.combine(month_start, datetime.min.time()),
        BalanceTransaction.transaction_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    periods_in_month = set()
    for d in days_in_month:
        for cadence in ('daily', 'weekly', 'monthly'):
            p = get_period(cadence, d)
            if p:
                periods_in_month.add(p)

    pending_recurring = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status.in_(['assigned', 'submitted']),
            AssignedChore.is_recurring == True,  # noqa: E712
            ChoreInstance.period.in_(periods_in_month),
        ).all()
    )

    expired_in_month = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'expired',
            ChoreInstance.assigned_date >= datetime.combine(month_start, datetime.min.time()),
            ChoreInstance.assigned_date < datetime.combine(month_end, datetime.min.time()),
        ).all()
    )

    activity_days = {}
    for inst in approved_in_month:
        d = inst.approved_date.date()
        activity_days.setdefault(d, {})['completed'] = True
    for tx in txns_in_month:
        d = tx.transaction_date.date()
        activity_days.setdefault(d, {})['transaction'] = True
    for inst in pending_recurring:
        for d in days_in_month:
            if inst.period == get_period(inst.recurrence_cadence, d):
                activity_days.setdefault(d, {})['pending'] = True
    for inst in expired_in_month:
        d = inst.assigned_date.date()
        activity_days.setdefault(d, {})['missed'] = True

    cal_grid = []
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(None)
            else:
                d = date(year, month, day_num)
                row.append({
                    'date': d,
                    'day': day_num,
                    'activity': activity_days.get(d, {}),
                    'is_today': d == date.today(),
                    'is_selected': d == selected_day,
                })
        cal_grid.append(row)

    day_completed = []
    day_transactions = []
    day_pending = []
    day_expired = []

    if selected_day:
        day_completed = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status.in_(['approved', 'approved_pending']),
                db.func.date(ChoreInstance.approved_date) == selected_day.isoformat(),
            ).all()
        )

        day_transactions = BalanceTransaction.query.filter(
            BalanceTransaction.child_id == child_id,
            db.func.date(BalanceTransaction.transaction_date) == selected_day.isoformat(),
        ).all()

        day_periods = {get_period(c, selected_day) for c in ('daily', 'weekly', 'monthly')} - {None}
        day_pending = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status.in_(['assigned', 'submitted']),
                AssignedChore.is_recurring == True,  # noqa: E712
                ChoreInstance.period.in_(day_periods),
            ).all()
        )

        day_expired = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status == 'expired',
                db.func.date(ChoreInstance.assigned_date) == selected_day.isoformat(),
            ).all()
        )

    prev_month = f'{year-1}-12' if month == 1 else f'{year}-{month-1:02d}'
    next_month = f'{year+1}-01' if month == 12 else f'{year}-{month+1:02d}'

    cadence_setting = AppSettings.query.get('payout_cadence')
    payout_cadence = cadence_setting.value if cadence_setting else 'instant'

    return render_template(
        'parent/child_history.html',
        child=child,
        year=year,
        month=month,
        month_name=month_start.strftime('%B %Y'),
        cal_grid=cal_grid,
        selected_day=selected_day,
        day_completed=day_completed,
        day_transactions=day_transactions,
        day_pending=day_pending,
        day_expired=day_expired,
        prev_month=prev_month,
        next_month=next_month,
        today=date.today(),
        payout_cadence=payout_cadence,
    )


@parent_bp.route('/child/<int:child_id>/assign', methods=['POST'])
@parent_required
def assign_chore(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)
    chore_id = request.form.get('chore_id', type=int)
    custom_value_raw = request.form.get('custom_value', '').strip()
    custom_value = float(custom_value_raw) if custom_value_raw else None
    is_recurring = request.form.get('is_recurring') == 'on'
    recurrence_cadence = request.form.get('recurrence_cadence', '').strip() if is_recurring else None
    recurrence_day = None
    if is_recurring and recurrence_cadence in ('weekly', 'monthly'):
        try:
            recurrence_day = int(request.form.get('recurrence_day', 0 if recurrence_cadence == 'weekly' else 1))
        except (ValueError, TypeError):
            recurrence_day = 0 if recurrence_cadence == 'weekly' else 1

    today = date.today()
    if is_recurring and recurrence_cadence == 'weekly' and recurrence_day is not None:
        days_since = (today.weekday() - recurrence_day) % 7
        ref_date = today - timedelta(days=days_since)
    elif is_recurring and recurrence_cadence == 'monthly' and recurrence_day is not None:
        if today.day >= recurrence_day:
            ref_date = today.replace(day=recurrence_day)
        else:
            prev_month = today.month - 1 or 12
            prev_year = today.year if today.month > 1 else today.year - 1
            ref_date = date(prev_year, prev_month, recurrence_day)
    else:
        ref_date = today

    period = get_period(recurrence_cadence, ref_date) if is_recurring and recurrence_cadence else None

    chore = Chore.query.get_or_404(chore_id)

    # Create config row
    ac_config = AssignedChore(
        child_id=child_id,
        chore_id=chore_id,
        custom_value=custom_value,
        is_recurring=is_recurring,
        recurrence_cadence=recurrence_cadence,
        recurrence_day=recurrence_day,
        is_active=True,
    )
    db.session.add(ac_config)
    db.session.flush()  # get ac_config.id

    # Create first instance
    db.session.add(ChoreInstance(
        assigned_chore_id=ac_config.id,
        status='assigned',
        period=period,
        assigned_date=datetime.now(),
    ))
    db.session.commit()

    cadence_label = f' (🔁 {recurrence_cadence})' if is_recurring else ''
    flash(f'"{chore.name}"{cadence_label} assigned to {child.name}!', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/adjust-balance', methods=['POST'])
@parent_required
def adjust_balance(child_id):
    child = Child.query.get_or_404(child_id)
    amount = request.form.get('amount', type=float)
    description = request.form.get('description', 'Manual adjustment').strip() or 'Manual adjustment'

    if amount is None:
        flash('Please enter an amount.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    child.balance += amount
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=amount,
        description=description,
    ))
    db.session.commit()

    direction = 'added to' if amount >= 0 else 'deducted from'
    flash(f'${abs(amount):.2f} {direction} {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/penalty', methods=['POST'])
@parent_required
def apply_penalty(child_id):
    child = Child.query.get_or_404(child_id)
    amount = request.form.get('amount', type=float)
    reason = request.form.get('reason', '').strip()

    if not amount or amount <= 0:
        flash('Please enter a positive penalty amount.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))
    if not reason:
        flash('Please describe the reason for the penalty.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    child.balance -= amount
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-amount,
        description=f'Penalty: {reason}',
    ))
    db.session.commit()
    flash(f'${amount:.2f} penalty applied to {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


# ── Chore instance actions ─────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/approve', methods=['POST'])
@parent_required
def approve_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.approved_date = inst.submitted_date or datetime.now()

    awarded_raw = request.form.get('awarded_value', '').strip()
    try:
        awarded = round(float(awarded_raw), 2)
        if awarded < 0:
            awarded = 0.0
    except (ValueError, TypeError):
        awarded = None

    if awarded is not None and awarded != inst.effective_value:
        inst.awarded_value = awarded
    else:
        inst.awarded_value = None

    amount = inst.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    if not cadence or cadence.value == 'instant':
        inst.status = 'approved'
        inst.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Chore completed: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {inst.child.name}\'s balance. 🎉', 'success')
    else:
        inst.status = 'approved_pending'
        flash(f'Approved{partial_note}! Will be paid out on next {cadence.value} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/deny', methods=['POST'])
@parent_required
def deny_chore(ac_id):
    from ..scheduler import get_period
    inst = ChoreInstance.query.get_or_404(ac_id)
    notes = request.form.get('notes', '').strip()

    period_has_passed = (
        inst.is_recurring
        and inst.recurrence_cadence
        and inst.period
        and get_period(inst.recurrence_cadence, date.today()) != inst.period
    )

    if period_has_passed:
        inst.status = 'expired'
        inst.denial_notes = notes or None
        db.session.commit()
        flash(f'Period already passed — {inst.effective_name} marked as expired.', 'info')
    else:
        inst.status = 'assigned'
        inst.submitted_date = None
        inst.denial_notes = notes or None
        db.session.commit()
        flash(f'Chore returned to {inst.child.name} for another try.', 'info')

    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/approve-retroactive', methods=['POST'])
@parent_required
def retroactive_approve(ac_id):
    from ..scheduler import get_period as _get_period
    inst = ChoreInstance.query.get_or_404(ac_id)
    payout_mode = request.form.get('payout_mode', 'immediate')
    approved_date_str = request.form.get('approved_date', '').strip()
    try:
        approved_date = datetime.combine(
            date.fromisoformat(approved_date_str),
            datetime.min.time().replace(hour=12),
        )
    except (ValueError, AttributeError):
        approved_date = datetime.now()
    inst.approved_date = approved_date
    amount = inst.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    payout_cadence = cadence.value if cadence else 'instant'

    if payout_mode == 'auto' and payout_cadence != 'instant':
        current_period = _get_period(payout_cadence, date.today())
        approved_period = _get_period(payout_cadence, approved_date.date())
        payout_mode = 'pending' if approved_period == current_period else 'immediate'

    if payout_mode == 'immediate' or payout_cadence == 'instant':
        inst.status = 'approved'
        inst.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Chore completed: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {inst.child.name}\'s balance.', 'success')
    else:
        inst.status = 'approved_pending'
        flash(f'Approved{partial_note}! Will be paid out on next {payout_cadence} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/reactivate', methods=['POST'])
@parent_required
def reactivate_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'assigned'
    db.session.commit()
    flash(f'"{inst.effective_name}" reactivated.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/not-done', methods=['POST'])
@parent_required
def mark_not_done(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'expired'
    db.session.commit()
    flash(f'"{inst.effective_name}" marked as not done.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/mark-incomplete', methods=['POST'])
@parent_required
def mark_incomplete(ac_id):
    from ..scheduler import get_period as _get_period
    inst = ChoreInstance.query.get_or_404(ac_id)

    if inst.status not in ('approved', 'approved_pending'):
        flash('This chore cannot be marked incomplete.', 'error')
        return redirect(request.referrer or url_for('parent.dashboard'))

    was_paid = inst.status == 'approved'

    if was_paid:
        amount = inst.actual_payout
        inst.child.balance -= amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=-amount,
            description=f'Chore reversed: {inst.effective_name}',
            chore_instance_id=inst.id,
        ))

    if inst.is_recurring and inst.recurrence_cadence and inst.period:
        current_period = _get_period(inst.recurrence_cadence, date.today())
        new_status = 'assigned' if inst.period == current_period else 'expired'
    else:
        new_status = 'assigned'

    inst.status = new_status
    inst.approved_date = None
    inst.awarded_value = None
    db.session.commit()

    if was_paid:
        flash(f'${amount:.2f} reversed from {inst.child.name}\'s balance — chore marked incomplete.', 'info')
    else:
        flash(f'"{inst.effective_name}" removed from pending payout — marked incomplete.', 'info')

    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


# ── Chore config actions ───────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/edit-value', methods=['POST'])
@parent_required
def edit_chore_value(ac_id):
    """Edit config for an AssignedChore: name, value, description, and recurrence."""
    from ..scheduler import get_period

    ac = AssignedChore.query.get_or_404(ac_id)
    was_recurring = ac.is_recurring

    custom_value_raw = request.form.get('custom_value', '').strip()
    ac.custom_value = float(custom_value_raw) if custom_value_raw else None
    ac.override_name = request.form.get('override_name', '').strip() or None
    ac.override_description = request.form.get('override_description', '').strip() or None

    # Recurrence fields
    is_recurring = request.form.get('is_recurring') == 'on'
    ac.is_recurring = is_recurring

    if is_recurring:
        cadence = request.form.get('recurrence_cadence', '').strip()
        if cadence in ('daily', 'weekly', 'monthly'):
            ac.recurrence_cadence = cadence
        try:
            ac.recurrence_day = int(request.form.get('recurrence_day', '')) if cadence in ('weekly', 'monthly') else None
        except (ValueError, TypeError):
            ac.recurrence_day = None

        # If this was just converted from one-time → recurring, stamp the
        # current period on any existing 'assigned' instance so the scheduler
        # doesn't create a duplicate.
        if not was_recurring and ac.recurrence_cadence:
            today = date.today()
            period = get_period(ac.recurrence_cadence, today)
            existing = ChoreInstance.query.filter_by(
                assigned_chore_id=ac.id, status='assigned'
            ).first()
            if existing and period:
                existing.period = period
    else:
        ac.recurrence_cadence = None
        ac.recurrence_day = None

    db.session.commit()
    flash('Chore updated.', 'success')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/child/<int:child_id>/recurring/<int:config_id>/cancel', methods=['POST'])
@parent_required
def cancel_recurring_chore(child_id, config_id):
    """Deactivate a recurring chore — stops new instances from being created."""
    ac = AssignedChore.query.filter_by(id=config_id, child_id=child_id).first_or_404()
    ac.is_active = False
    db.session.commit()
    flash(f'"{ac.effective_name}" recurring schedule cancelled.', 'info')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/recurring/<int:config_id>/edit', methods=['POST'])
@parent_required
def edit_recurring_chore(child_id, config_id):
    """Edit the config of a recurring chore (cadence, day, value, name, description)."""
    ac = AssignedChore.query.filter_by(id=config_id, child_id=child_id).first_or_404()

    new_cadence = request.form.get('recurrence_cadence')
    new_day     = request.form.get('recurrence_day', type=int)
    new_value   = request.form.get('custom_value', '').strip()
    new_name    = request.form.get('override_name', '').strip() or None
    new_desc    = request.form.get('override_description', '').strip() or None

    if new_cadence in ('daily', 'weekly', 'monthly'):
        ac.recurrence_cadence = new_cadence
    ac.recurrence_day = new_day  # None is fine for daily
    ac.custom_value = float(new_value) if new_value else None
    ac.override_name = new_name
    ac.override_description = new_desc

    db.session.commit()
    flash(f'"{ac.effective_name}" recurring schedule updated.', 'info')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/chore/<int:ac_id>/delete', methods=['POST'])
@parent_required
def delete_assigned_chore(ac_id):
    """Delete an AssignedChore config and all its instances (cascade)."""
    ac = AssignedChore.query.get_or_404(ac_id)
    child_id = ac.child_id
    db.session.delete(ac)
    db.session.commit()
    flash('Chore removed.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=child_id))


# ── Wishlist management ───────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>/wishlist')
@parent_required
def child_wishlist(child_id):
    child = Child.query.get_or_404(child_id)
    active = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )
    purchased = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .order_by(WishlistItem.purchased_date.desc())
        .all()
    )
    return render_template('parent/child_wishlist.html', child=child, active=active, purchased=purchased)


@parent_bp.route('/child/<int:child_id>/wishlist/add', methods=['POST'])
@parent_required
def parent_add_wish(child_id):
    child = Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if not name or not price_raw:
        flash('Item name and price are required.', 'error')
        return redirect(url_for('parent.child_wishlist', child_id=child_id))

    max_order = db.session.query(db.func.max(WishlistItem.sort_order)).filter_by(
        child_id=child_id, status='active'
    ).scalar() or 0

    db.session.add(WishlistItem(
        child_id=child_id,
        name=name,
        description=request.form.get('description', '').strip() or None,
        price=float(price_raw),
        url=request.form.get('url', '').strip() or None,
        sort_order=max_order + 1,
    ))
    db.session.commit()
    flash(f'"{name}" added to {child.name}\'s wishlist!', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/purchase', methods=['POST'])
@parent_required
def purchase_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    child = item.child

    if child.balance < item.price:
        flash(
            f'Not enough balance — {child.name} has ${child.balance:.2f} but needs ${item.price:.2f}.',
            'error',
        )
        return redirect(url_for('parent.child_wishlist', child_id=child_id))

    item.status = 'purchased'
    item.purchased_date = datetime.now()
    child.balance -= item.price
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-item.price,
        description=f'Purchased: {item.name}',
    ))
    db.session.commit()
    flash(f'"{item.name}" purchased! ${item.price:.2f} deducted from {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/edit', methods=['POST'])
@parent_required
def edit_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if name:
        item.name = name
    if price_raw:
        item.price = float(price_raw)
    item.description = request.form.get('description', '').strip() or None
    item.url = request.form.get('url', '').strip() or None
    db.session.commit()
    flash('Wishlist item updated.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/delete', methods=['POST'])
@parent_required
def parent_delete_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'"{name}" removed from wishlist.', 'info')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


# ── Chore library ─────────────────────────────────────────────────────────────

@parent_bp.route('/chores')
@parent_required
def chore_library():
    from ..utils import CHORE_ICONS
    chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    return render_template('parent/chore_library.html', chores=chores, chore_icons=CHORE_ICONS)


@parent_bp.route('/chores/add', methods=['POST'])
@parent_required
def add_chore():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Chore name is required.', 'error')
        return redirect(url_for('parent.chore_library'))

    default_value_raw = request.form.get('default_value', '').strip()

    db.session.add(Chore(
        name=name,
        description=request.form.get('description', '').strip(),
        icon=request.form.get('icon', '').strip() or None,
        default_value=float(default_value_raw) if default_value_raw else 1.0,
    ))
    db.session.commit()
    flash(f'"{name}" added to the chore library!', 'success')
    return redirect(url_for('parent.chore_library'))


@parent_bp.route('/chores/<int:chore_id>/edit', methods=['POST'])
@parent_required
def edit_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    name = request.form.get('name', '').strip()
    if name:
        chore.name = name
    chore.description = request.form.get('description', '').strip()
    chore.icon = request.form.get('icon', '').strip() or None
    default_value_raw = request.form.get('default_value', '').strip()
    if default_value_raw:
        chore.default_value = float(default_value_raw)
    db.session.commit()
    flash(f'"{chore.name}" updated.', 'success')
    return redirect(url_for('parent.chore_library'))


@parent_bp.route('/chores/<int:chore_id>/delete', methods=['POST'])
@parent_required
def delete_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    chore.is_active = False
    db.session.commit()
    flash(f'"{chore.name}" removed from library.', 'success')
    return redirect(url_for('parent.chore_library'))


# ── Payout summary ───────────────────────────────────────────────────────────

@parent_bp.route('/payouts')
@parent_required
def payouts():
    from ..utils import get_payout_period_info

    period = get_payout_period_info()
    cadence = period['cadence']
    children = Child.query.order_by(Child.name).all()

    child_data = []
    grand_total = 0.0

    for child in children:
        if cadence == 'instant':
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved',
                    ChoreInstance.approved_date >= period['period_start'],
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )
        else:
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved_pending',
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )

        subtotal = sum(inst.actual_payout for inst in instances)
        grand_total += subtotal
        child_data.append({'child': child, 'chores': instances, 'subtotal': subtotal})

    return render_template(
        'parent/payouts.html',
        child_data=child_data,
        grand_total=grand_total,
        period=period,
    )


@parent_bp.route('/payouts/process-now', methods=['POST'])
@parent_required
def process_payout_now():
    """Immediately pay out all approved_pending chore instances."""
    pending = ChoreInstance.query.filter_by(status='approved_pending').all()
    if not pending:
        flash('No pending payouts to process.', 'info')
        return redirect(url_for('parent.payouts'))

    total_by_child: dict = {}
    for inst in pending:
        amount = inst.actual_payout
        inst.status = 'approved'
        inst.child.balance += amount
        partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Manual payout: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        total_by_child[inst.child.name] = total_by_child.get(inst.child.name, 0) + amount

    db.session.commit()
    from ..utils import backup_database
    backup_database()
    summary = ', '.join(f'{name} +${amt:.2f}' for name, amt in total_by_child.items())
    flash(f'Payout processed! {summary}', 'success')
    return redirect(url_for('parent.payouts'))


# ── Settings ─────────────────────────────────────────────────────────────────

@parent_bp.route('/settings')
@parent_required
def settings():
    children = Child.query.order_by(Child.name).all()
    def _get(key, default):
        s = AppSettings.query.get(key)
        return s.value if s else default

    return render_template(
        'parent/settings.html',
        children=children,
        payout_cadence=_get('payout_cadence', 'instant'),
        payout_time=_get('payout_time', '18:00'),
        payout_day_of_week=_get('payout_day_of_week', '0'),
        payout_day_of_month=_get('payout_day_of_month', '1'),
        session_timeout=_get('session_timeout', '5'),
    )


@parent_bp.route('/settings/update', methods=['POST'])
@parent_required
def update_settings():
    def _set(key, value):
        s = AppSettings.query.get(key)
        if s:
            s.value = value
        else:
            db.session.add(AppSettings(key=key, value=value))

    cadence = request.form.get('payout_cadence')
    if cadence in ('instant', 'daily', 'weekly', 'monthly'):
        _set('payout_cadence', cadence)

    payout_time = request.form.get('payout_time', '').strip()
    if payout_time:
        _set('payout_time', payout_time)

    dow = request.form.get('payout_day_of_week', '').strip()
    if dow.isdigit() and 0 <= int(dow) <= 6:
        _set('payout_day_of_week', dow)

    dom = request.form.get('payout_day_of_month', '').strip()
    if dom.isdigit() and 1 <= int(dom) <= 28:
        _set('payout_day_of_month', dom)

    new_pin = request.form.get('new_pin', '').strip()
    confirm_pin = request.form.get('confirm_pin', '').strip()
    if new_pin:
        if len(new_pin) < 4:
            flash('PIN must be at least 4 characters.', 'error')
            return redirect(url_for('parent.settings'))
        if new_pin != confirm_pin:
            flash('PINs do not match.', 'error')
            return redirect(url_for('parent.settings'))
        pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
        s = AppSettings.query.get('parent_pin')
        if s:
            s.value = pin_hash
        else:
            db.session.add(AppSettings(key='parent_pin', value=pin_hash))

    timeout = request.form.get('session_timeout', '').strip()
    if timeout.isdigit() and int(timeout) >= 1:
        _set('session_timeout', timeout)

    db.session.commit()

    from ..scheduler import reschedule_payout_job
    reschedule_payout_job()

    flash('Settings saved!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/add-child', methods=['POST'])
@parent_required
def add_child():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Name is required.', 'error')
        return redirect(url_for('parent.settings'))
    color = request.form.get('color', '#6366f1')
    db.session.add(Child(name=name, avatar_color=color))
    db.session.commit()
    flash(f'{name} added!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/remove-child/<int:child_id>', methods=['POST'])
@parent_required
def remove_child(child_id):
    child = Child.query.get_or_404(child_id)
    name = child.name
    db.session.delete(child)
    db.session.commit()
    flash(f'{name} removed.', 'info')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/child/<int:child_id>/avatar', methods=['POST'])
@parent_required
def upload_avatar(child_id):
    child = Child.query.get_or_404(child_id)
    file = request.files.get('avatar')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('parent.settings'))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        flash('Invalid file type. Use JPG, PNG, GIF, or WebP.', 'error')
        return redirect(url_for('parent.settings'))
    filename = secure_filename(f'child_{child_id}{ext}')
    upload_dir = os.path.join(current_app.root_path, 'static', 'avatars')
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))
    child.avatar_filename = filename
    db.session.commit()
    flash(f'Avatar updated for {child.name}!', 'success')
    return redirect(url_for('parent.settings'))
