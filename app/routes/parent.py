import bcrypt
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from ..models import Child, Chore, AssignedChore, BalanceTransaction, AppSettings
from .. import db

parent_bp = Blueprint('parent', __name__)


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
        AssignedChore.query
        .filter_by(status='submitted')
        .order_by(AssignedChore.submitted_date)
        .all()
    )
    return render_template('parent/dashboard.html', children=children, pending_reviews=pending_reviews)


# ── Child detail ──────────────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>')
@parent_required
def child_detail(child_id):
    child = Child.query.get_or_404(child_id)
    active_chores = (
        AssignedChore.query
        .filter(AssignedChore.child_id == child_id, AssignedChore.status.in_(['assigned', 'submitted']))
        .order_by(AssignedChore.assigned_date.desc())
        .all()
    )
    completed_chores = (
        AssignedChore.query
        .filter_by(child_id=child_id, status='approved')
        .order_by(AssignedChore.approved_date.desc())
        .all()
    )
    transactions = (
        BalanceTransaction.query
        .filter_by(child_id=child_id)
        .order_by(BalanceTransaction.transaction_date.desc())
        .limit(20)
        .all()
    )
    all_chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    return render_template(
        'parent/child_detail.html',
        child=child,
        active_chores=active_chores,
        completed_chores=completed_chores,
        transactions=transactions,
        all_chores=all_chores,
    )


@parent_bp.route('/child/<int:child_id>/assign', methods=['POST'])
@parent_required
def assign_chore(child_id):
    child = Child.query.get_or_404(child_id)
    chore_id = request.form.get('chore_id', type=int)
    custom_value_raw = request.form.get('custom_value', '').strip()
    custom_value = float(custom_value_raw) if custom_value_raw else None

    chore = Chore.query.get_or_404(chore_id)
    db.session.add(AssignedChore(
        child_id=child_id,
        chore_id=chore_id,
        custom_value=custom_value,
        status='assigned',
    ))
    db.session.commit()
    flash(f'"{chore.name}" assigned to {child.name}!', 'success')
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


# ── Chore review ─────────────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/approve', methods=['POST'])
@parent_required
def approve_chore(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    ac.status = 'approved'
    ac.approved_date = datetime.utcnow()

    cadence = AppSettings.query.get('payout_cadence')
    if not cadence or cadence.value == 'instant':
        amount = ac.effective_value
        ac.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=ac.child_id,
            amount=amount,
            description=f'Chore completed: {ac.chore.name}',
            assigned_chore_id=ac.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {ac.child.name}\'s balance. 🎉', 'success')
    else:
        ac.status = 'approved_pending'
        flash(f'Approved! Will be paid out on next {cadence.value} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/deny', methods=['POST'])
@parent_required
def deny_chore(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    notes = request.form.get('notes', '').strip()

    ac.status = 'assigned'
    ac.submitted_date = None
    ac.denial_notes = notes or None
    db.session.commit()

    flash(f'Chore returned to {ac.child.name} for another try.', 'info')
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/approve-retroactive', methods=['POST'])
@parent_required
def retroactive_approve(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    ac.status = 'approved'
    ac.approved_date = datetime.utcnow()

    amount = ac.effective_value
    ac.child.balance += amount
    db.session.add(BalanceTransaction(
        child_id=ac.child_id,
        amount=amount,
        description=f'Retroactive approval: {ac.chore.name}',
        assigned_chore_id=ac.id,
    ))
    db.session.commit()

    flash(f'Retroactively approved! ${amount:.2f} added to {ac.child.name}\'s balance.', 'success')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/chore/<int:ac_id>/edit-value', methods=['POST'])
@parent_required
def edit_chore_value(ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    custom_value_raw = request.form.get('custom_value', '').strip()
    ac.custom_value = float(custom_value_raw) if custom_value_raw else None
    db.session.commit()
    flash('Reward value updated.', 'success')
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


# ── Chore library ─────────────────────────────────────────────────────────────

@parent_bp.route('/chores')
@parent_required
def chore_library():
    chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    return render_template('parent/chore_library.html', chores=chores)


@parent_bp.route('/chores/add', methods=['POST'])
@parent_required
def add_chore():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Chore name is required.', 'error')
        return redirect(url_for('parent.chore_library'))

    default_value_raw = request.form.get('default_value', '').strip()
    is_recurring = request.form.get('is_recurring') == 'on'
    recurrence_cadence = request.form.get('recurrence_cadence') if is_recurring else None

    db.session.add(Chore(
        name=name,
        description=request.form.get('description', '').strip(),
        default_value=float(default_value_raw) if default_value_raw else 1.0,
        is_recurring=is_recurring,
        recurrence_cadence=recurrence_cadence,
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
    default_value_raw = request.form.get('default_value', '').strip()
    if default_value_raw:
        chore.default_value = float(default_value_raw)
    chore.is_recurring = request.form.get('is_recurring') == 'on'
    chore.recurrence_cadence = request.form.get('recurrence_cadence') if chore.is_recurring else None
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
