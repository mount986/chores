import calendar as cal_module
import os
import bcrypt
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, current_app
from werkzeug.utils import secure_filename
from ..models import Child, Chore, AssignedChore, BalanceTransaction, AppSettings, WishlistItem
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
    from ..scheduler import get_period
    from ..utils import next_recurrence_date

    children = Child.query.order_by(Child.name).all()
    pending_reviews = (
        AssignedChore.query
        .filter_by(status='submitted')
        .order_by(AssignedChore.submitted_date)
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
    child = Child.query.get_or_404(child_id)

    sort = request.args.get('sort', 'date')
    active_chores = (
        AssignedChore.query
        .filter(AssignedChore.child_id == child_id, AssignedChore.status.in_(['assigned', 'submitted']))
        .all()
    )
    if sort == 'name':
        active_chores.sort(key=lambda ac: ac.chore.name.lower())
    elif sort == 'value':
        active_chores.sort(key=lambda ac: ac.effective_value, reverse=True)
    elif sort == 'cadence':
        active_chores.sort(key=lambda ac: (ac.recurrence_cadence or '', ac.chore.name.lower()))
    else:  # date
        active_chores.sort(key=lambda ac: ac.assigned_date, reverse=True)
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

    # Upcoming: recurring configs for this child with no active current-period instance
    today = date.today()
    child_configs = (
        db.session.query(
            AssignedChore.chore_id,
            AssignedChore.recurrence_cadence,
            AssignedChore.recurrence_day,
        )
        .filter(
            AssignedChore.child_id == child_id,
            AssignedChore.is_recurring == True,  # noqa: E712
            AssignedChore.recurrence_cadence != None,  # noqa: E711
        )
        .distinct()
        .all()
    )
    upcoming_chores = []
    for chore_id, cadence, rec_day in child_configs:
        current_period = get_period(cadence, today)
        active = AssignedChore.query.filter(
            AssignedChore.child_id == child_id,
            AssignedChore.chore_id == chore_id,
            AssignedChore.period == current_period,
            AssignedChore.status.in_(['assigned', 'submitted']),
        ).first()
        if active:
            continue
        template = (
            AssignedChore.query
            .filter_by(child_id=child_id, chore_id=chore_id, is_recurring=True)
            .order_by(AssignedChore.assigned_date.desc())
            .first()
        )
        if not template:
            continue
        upcoming_chores.append({
            'ac': template,
            'next_date': next_recurrence_date(cadence, rec_day, today),
            'cadence': cadence,
        })
    upcoming_chores.sort(key=lambda x: x['next_date'])

    return render_template(
        'parent/child_detail.html',
        child=child,
        active_chores=active_chores,
        all_chores=all_chores,
        wishlist_active=wishlist_active,
        wishlist_purchased=wishlist_purchased,
        sort=sort,
        upcoming_chores=upcoming_chores,
        today=today,
    )


@parent_bp.route('/child/<int:child_id>/history')
@parent_required
def child_history(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)

    # Parse month
    month_str = request.args.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = (int(p) for p in month_str.split('-'))
        date(year, month, 1)  # validate
    except (ValueError, AttributeError):
        year, month = date.today().year, date.today().month

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    days_in_month = [month_start + timedelta(days=i) for i in range((month_end - month_start).days)]

    # Parse selected day
    day_str = request.args.get('day')
    selected_day = None
    if day_str:
        try:
            selected_day = date.fromisoformat(day_str)
        except ValueError:
            pass

    # Approved chores in this month
    approved_in_month = AssignedChore.query.filter(
        AssignedChore.child_id == child_id,
        AssignedChore.status.in_(['approved', 'approved_pending']),
        AssignedChore.approved_date >= datetime.combine(month_start, datetime.min.time()),
        AssignedChore.approved_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    # Transactions in this month
    txns_in_month = BalanceTransaction.query.filter(
        BalanceTransaction.child_id == child_id,
        BalanceTransaction.transaction_date >= datetime.combine(month_start, datetime.min.time()),
        BalanceTransaction.transaction_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    # Recurring chores still pending whose period overlaps this month
    periods_in_month = set()
    for d in days_in_month:
        for cadence in ('daily', 'weekly', 'monthly'):
            p = get_period(cadence, d)
            if p:
                periods_in_month.add(p)

    pending_recurring = AssignedChore.query.filter(
        AssignedChore.child_id == child_id,
        AssignedChore.status.in_(['assigned', 'submitted']),
        AssignedChore.is_recurring == True,
        AssignedChore.period.in_(periods_in_month),
    ).all()

    # Expired (missed) recurring chores in this month
    expired_in_month = AssignedChore.query.filter(
        AssignedChore.child_id == child_id,
        AssignedChore.status == 'expired',
        AssignedChore.assigned_date >= datetime.combine(month_start, datetime.min.time()),
        AssignedChore.assigned_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    # Build activity dict keyed by date
    activity_days = {}
    for ac in approved_in_month:
        d = ac.approved_date.date()
        activity_days.setdefault(d, {})['completed'] = True
    for tx in txns_in_month:
        d = tx.transaction_date.date()
        activity_days.setdefault(d, {})['transaction'] = True
    for ac in pending_recurring:
        for d in days_in_month:
            if ac.period == get_period(ac.recurrence_cadence, d):
                activity_days.setdefault(d, {})['pending'] = True
    for ac in expired_in_month:
        d = ac.assigned_date.date()
        activity_days.setdefault(d, {})['missed'] = True

    # Build calendar grid
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

    # Day detail
    day_completed = []
    day_transactions = []
    day_pending = []
    day_expired = []

    if selected_day:
        day_completed = AssignedChore.query.filter(
            AssignedChore.child_id == child_id,
            AssignedChore.status.in_(['approved', 'approved_pending']),
            db.func.date(AssignedChore.approved_date) == selected_day.isoformat(),
        ).all()

        day_transactions = BalanceTransaction.query.filter(
            BalanceTransaction.child_id == child_id,
            db.func.date(BalanceTransaction.transaction_date) == selected_day.isoformat(),
        ).all()

        day_periods = {get_period(c, selected_day) for c in ('daily', 'weekly', 'monthly')} - {None}
        day_pending = AssignedChore.query.filter(
            AssignedChore.child_id == child_id,
            AssignedChore.status.in_(['assigned', 'submitted']),
            AssignedChore.is_recurring == True,
            AssignedChore.period.in_(day_periods),
        ).all()

        day_expired = AssignedChore.query.filter(
            AssignedChore.child_id == child_id,
            AssignedChore.status == 'expired',
            db.func.date(AssignedChore.assigned_date) == selected_day.isoformat(),
        ).all()

    # Month navigation
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

    # Stamp the period based on the most recent occurrence of the configured day,
    # so a Friday chore created on Monday gets the period that started last Friday.
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
    db.session.add(AssignedChore(
        child_id=child_id,
        chore_id=chore_id,
        custom_value=custom_value,
        status='assigned',
        is_recurring=is_recurring,
        recurrence_cadence=recurrence_cadence,
        recurrence_day=recurrence_day,
        period=period,
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


# ── Chore review ─────────────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/approve', methods=['POST'])
@parent_required
def approve_chore(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    # Credit as of when the child actually did the chore, not when the parent reviewed it.
    ac.approved_date = ac.submitted_date or datetime.now()

    awarded_raw = request.form.get('awarded_value', '').strip()
    try:
        awarded = round(float(awarded_raw), 2)
        if awarded < 0:
            awarded = 0.0
    except (ValueError, TypeError):
        awarded = None

    if awarded is not None and awarded != ac.effective_value:
        ac.awarded_value = awarded
    else:
        ac.awarded_value = None

    amount = ac.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${ac.effective_value:.2f})' if ac.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    if not cadence or cadence.value == 'instant':
        ac.status = 'approved'
        ac.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=ac.child_id,
            amount=amount,
            description=f'Chore completed: {ac.chore.name}{partial_note}',
            assigned_chore_id=ac.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {ac.child.name}\'s balance. 🎉', 'success')
    else:
        ac.status = 'approved_pending'
        flash(f'Approved{partial_note}! Will be paid out on next {cadence.value} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/deny', methods=['POST'])
@parent_required
def deny_chore(ac_id):
    from ..scheduler import get_period
    ac = AssignedChore.query.get_or_404(ac_id)
    notes = request.form.get('notes', '').strip()

    # If this is a recurring chore and its period has already rolled over,
    # expiring makes more sense than returning it to the to-do list.
    period_has_passed = (
        ac.is_recurring
        and ac.recurrence_cadence
        and ac.period
        and get_period(ac.recurrence_cadence, date.today()) != ac.period
    )

    if period_has_passed:
        ac.status = 'expired'
        ac.denial_notes = notes or None
        db.session.commit()
        flash(f'Period already passed — {ac.effective_name} marked as expired.', 'info')
    else:
        ac.status = 'assigned'
        ac.submitted_date = None
        ac.denial_notes = notes or None
        db.session.commit()
        flash(f'Chore returned to {ac.child.name} for another try.', 'info')

    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/approve-retroactive', methods=['POST'])
@parent_required
def retroactive_approve(ac_id):
    from ..scheduler import get_period as _get_period
    ac = AssignedChore.query.get_or_404(ac_id)
    payout_mode = request.form.get('payout_mode', 'immediate')
    approved_date_str = request.form.get('approved_date', '').strip()
    try:
        approved_date = datetime.combine(date.fromisoformat(approved_date_str), datetime.min.time().replace(hour=12))
    except (ValueError, AttributeError):
        approved_date = datetime.now()
    ac.approved_date = approved_date
    amount = ac.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${ac.effective_value:.2f})' if ac.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    payout_cadence = cadence.value if cadence else 'instant'

    # 'auto' mode: pending if the approved date falls in the current payout
    # period, immediate if it belongs to a past period.
    if payout_mode == 'auto' and payout_cadence != 'instant':
        current_period = _get_period(payout_cadence, date.today())
        approved_period = _get_period(payout_cadence, approved_date.date())
        payout_mode = 'pending' if approved_period == current_period else 'immediate'

    if payout_mode == 'immediate' or payout_cadence == 'instant':
        ac.status = 'approved'
        ac.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=ac.child_id,
            amount=amount,
            description=f'Chore completed: {ac.chore.name}{partial_note}',
            assigned_chore_id=ac.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {ac.child.name}\'s balance.', 'success')
    else:
        ac.status = 'approved_pending'
        flash(f'Approved{partial_note}! Will be paid out on next {payout_cadence} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/chore/<int:ac_id>/edit-value', methods=['POST'])
@parent_required
def edit_chore_value(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    custom_value_raw = request.form.get('custom_value', '').strip()
    ac.custom_value = float(custom_value_raw) if custom_value_raw else None
    override_name = request.form.get('override_name', '').strip()
    ac.override_name = override_name or None
    override_description = request.form.get('override_description', '').strip()
    ac.override_description = override_description or None
    db.session.commit()
    flash('Chore updated.', 'success')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/chore/<int:ac_id>/not-done', methods=['POST'])
@parent_required
def mark_not_done(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    ac.status = 'expired'
    db.session.commit()
    flash(f'"{ac.effective_name}" marked as not done.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/chore/<int:ac_id>/mark-incomplete', methods=['POST'])
@parent_required
def mark_incomplete(ac_id):
    from ..scheduler import get_period as _get_period
    ac = AssignedChore.query.get_or_404(ac_id)

    if ac.status not in ('approved', 'approved_pending'):
        flash('This chore cannot be marked incomplete.', 'error')
        return redirect(request.referrer or url_for('parent.dashboard'))

    was_paid = ac.status == 'approved'

    if was_paid:
        # Claw back the amount that was credited
        amount = ac.actual_payout
        ac.child.balance -= amount
        db.session.add(BalanceTransaction(
            child_id=ac.child_id,
            amount=-amount,
            description=f'Chore reversed: {ac.effective_name}',
            assigned_chore_id=ac.id,
        ))

    # Revert to assigned if the period is still current, expired if it has passed
    if ac.is_recurring and ac.recurrence_cadence and ac.period:
        current_period = _get_period(ac.recurrence_cadence, date.today())
        new_status = 'assigned' if ac.period == current_period else 'expired'
    else:
        new_status = 'assigned'

    ac.status = new_status
    ac.approved_date = None
    ac.awarded_value = None
    db.session.commit()

    if was_paid:
        flash(f'${amount:.2f} reversed from {ac.child.name}\'s balance — chore marked incomplete.', 'info')
    else:
        flash(f'"{ac.effective_name}" removed from pending payout — marked incomplete.', 'info')

    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/chore/<int:ac_id>/delete', methods=['POST'])
@parent_required
def delete_assigned_chore(ac_id):
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
            # instant: show chores approved today
            chores = (
                AssignedChore.query
                .filter(
                    AssignedChore.child_id == child.id,
                    AssignedChore.status == 'approved',
                    AssignedChore.approved_date >= period['period_start'],
                )
                .order_by(AssignedChore.approved_date.desc())
                .all()
            )
        else:
            # scheduled: show everything approved but not yet paid out
            chores = (
                AssignedChore.query
                .filter_by(child_id=child.id, status='approved_pending')
                .order_by(AssignedChore.approved_date.desc())
                .all()
            )

        subtotal = sum(ac.actual_payout for ac in chores)
        grand_total += subtotal
        child_data.append({'child': child, 'chores': chores, 'subtotal': subtotal})

    return render_template(
        'parent/payouts.html',
        child_data=child_data,
        grand_total=grand_total,
        period=period,
    )


@parent_bp.route('/payouts/process-now', methods=['POST'])
@parent_required
def process_payout_now():
    """Immediately pay out all approved_pending chores."""
    pending = AssignedChore.query.filter_by(status='approved_pending').all()
    if not pending:
        flash('No pending payouts to process.', 'info')
        return redirect(url_for('parent.payouts'))

    total_by_child: dict = {}
    for ac in pending:
        amount = ac.actual_payout
        ac.status = 'approved'
        ac.child.balance += amount
        partial_note = f' (partial: ${amount:.2f} of ${ac.effective_value:.2f})' if ac.is_partial else ''
        db.session.add(BalanceTransaction(
            child_id=ac.child_id,
            amount=amount,
            description=f'Manual payout: {ac.chore.name}{partial_note}',
            assigned_chore_id=ac.id,
        ))
        total_by_child[ac.child.name] = total_by_child.get(ac.child.name, 0) + amount

    db.session.commit()
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
