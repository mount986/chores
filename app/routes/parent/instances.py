from datetime import datetime, date
from flask import redirect, url_for, request, flash
from ...models import AssignedChore, ChoreInstance
from ... import db
from ...utils import get_payout_cadence, parse_quantity, parse_completed_date, parse_money
from ...services.chore_approval import finalize_approval, mark_book_unfinished, reverse_payment
from .common import parent_bp, parent_required, redirect_back


def _approve_and_flash(inst, immediate, cadence, celebrate=''):
    """Approve `inst` (paid now or pending payout) and flash the matching confirmation."""
    amount, partial_note = finalize_approval(inst, immediate)
    if immediate:
        flash(f'Approved! ${amount:.2f} added to {inst.child.name}\'s balance.{celebrate}', 'success')
    else:
        flash(f'Approved{partial_note}! Will be paid out on next {cadence} payout.', 'success')


# ── Chore instance actions ─────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/approve', methods=['POST'])
@parent_required
def approve_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    try:
        completed_day = parse_completed_date(request.form.get('completed_date'))
    except ValueError as e:
        flash(str(e) if 'future' in str(e) else 'Invalid completed date.', 'error')
        return redirect_back('parent.dashboard')
    if inst.status == 'assigned' and inst.assigned_chore.is_per_unit:
        if inst.pending_unit_submission() is not None:
            flash('This chore already has a submission waiting for review — approve or deny it first.', 'error')
            return redirect_back('parent.child_detail', child_id=inst.child_id)
        inst = inst.spawn_unit_submission(parse_quantity(request.form.get('quantity')))
    if inst.assigned_chore.is_per_unit and request.form.get('quantity'):
        inst.quantity = parse_quantity(request.form.get('quantity'))  # parent may correct the count
    inst.approved_date = inst.submitted_date or datetime.now()
    inst.set_completed_on(completed_day)

    try:
        awarded = parse_money(request.form.get('awarded_value'))
    except ValueError:
        awarded = None
    if awarded is not None:
        awarded = max(awarded, 0.0)

    if awarded is not None and awarded != inst.effective_value:
        inst.awarded_value = awarded
    else:
        inst.awarded_value = None

    cadence = get_payout_cadence()
    _approve_and_flash(inst, cadence == 'instant', cadence, ' 🎉')

    db.session.commit()
    return redirect_back('parent.dashboard')


@parent_bp.route('/chore/<int:ac_id>/deny', methods=['POST'])
@parent_required
def deny_chore(ac_id):
    from ...scheduler import get_period
    inst = ChoreInstance.query.get_or_404(ac_id)
    notes = request.form.get('notes', '').strip()

    child_name = inst.child.name
    if inst.discard_unit_submission(notes):
        db.session.commit()
        flash(f'Submission returned to {child_name} for another try.', 'info')
        return redirect_back('parent.dashboard')

    period_has_passed = (
        inst.is_recurring
        and inst.recurrence_cadence
        and inst.period
        and get_period(inst.recurrence_cadence, date.today()) != inst.period
    )

    if period_has_passed:
        inst.status = 'expired'
        inst.terminal_date = datetime.now()
        inst.denial_notes = notes or None
        mark_book_unfinished(inst)
        db.session.commit()
        flash(f'Period already passed — {inst.effective_name} marked as expired.', 'info')
    else:
        inst.status = 'assigned'
        inst.submitted_date = None
        inst.completed_date = None
        inst.denial_notes = notes or None
        mark_book_unfinished(inst)
        db.session.commit()
        flash(f'Chore returned to {inst.child.name} for another try.', 'info')

    return redirect_back('parent.dashboard')


@parent_bp.route('/chore/<int:ac_id>/approve-retroactive', methods=['POST'])
@parent_required
def retroactive_approve(ac_id):
    from ...scheduler import get_period as _get_period
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

    payout_cadence = get_payout_cadence()

    if payout_mode == 'auto' and payout_cadence != 'instant':
        current_period = _get_period(payout_cadence, date.today())
        approved_period = _get_period(payout_cadence, approved_date.date())
        payout_mode = 'pending' if approved_period == current_period else 'immediate'

    _approve_and_flash(inst, payout_mode == 'immediate' or payout_cadence == 'instant', payout_cadence)

    db.session.commit()
    return redirect_back('parent.child_detail', child_id=inst.child_id)


@parent_bp.route('/chore/<int:ac_id>/reactivate', methods=['POST'])
@parent_required
def reactivate_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'assigned'
    db.session.commit()
    flash(f'"{inst.effective_name}" reactivated.', 'info')
    return redirect_back('parent.child_detail', child_id=inst.child_id)


@parent_bp.route('/chore/<int:ac_id>/not-done', methods=['POST'])
@parent_required
def mark_not_done(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'expired'
    inst.terminal_date = datetime.now()
    db.session.commit()
    flash(f'"{inst.effective_name}" marked as not done.', 'info')
    return redirect_back('parent.child_detail', child_id=inst.child_id)


@parent_bp.route('/chore/<int:ac_id>/mark-incomplete', methods=['POST'])
@parent_required
def mark_incomplete(ac_id):
    from ...scheduler import get_period as _get_period
    inst = ChoreInstance.query.get_or_404(ac_id)

    if inst.status not in ('approved', 'approved_pending'):
        flash('This chore cannot be marked incomplete.', 'error')
        return redirect_back('parent.dashboard')

    was_paid = inst.status == 'approved'

    if was_paid:
        amount = reverse_payment(inst)

    chore_name, child_name, child_id = inst.display_name, inst.child.name, inst.child_id
    if inst.discard_unit_submission():
        db.session.commit()
        if was_paid:
            flash(f'${amount:.2f} reversed from {child_name}\'s balance — "{chore_name}" submission removed.', 'info')
        else:
            flash(f'"{chore_name}" submission removed from pending payout.', 'info')
        return redirect_back('parent.child_detail', child_id=child_id)

    if inst.is_recurring and inst.recurrence_cadence and inst.period:
        current_period = _get_period(inst.recurrence_cadence, date.today())
        new_status = 'assigned' if inst.period == current_period else 'expired'
    else:
        new_status = 'assigned'

    inst.status = new_status
    inst.approved_date = None
    inst.completed_date = None
    inst.terminal_date = datetime.now() if new_status == 'expired' else None
    inst.awarded_value = None

    mark_book_unfinished(inst)

    db.session.commit()

    if was_paid:
        flash(f'${amount:.2f} reversed from {inst.child.name}\'s balance — chore marked incomplete.', 'info')
    else:
        flash(f'"{inst.effective_name}" removed from pending payout — marked incomplete.', 'info')

    return redirect_back('parent.child_detail', child_id=inst.child_id)


# ── Chore config actions ───────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/edit-value', methods=['POST'])
@parent_required
def edit_chore_value(ac_id):
    """Edit config for an AssignedChore: name, value, description, and recurrence."""
    from ...scheduler import get_period

    ac = AssignedChore.query.get_or_404(ac_id)
    was_recurring = ac.is_recurring

    custom_value_raw = request.form.get('custom_value', '').strip()
    try:
        ac.custom_value = parse_money(custom_value_raw)
    except ValueError:
        flash('Please enter a valid value.', 'error')
        return redirect_back('parent.child_detail', child_id=ac.child_id)
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
    return redirect_back('parent.child_detail', child_id=ac.child_id)


@parent_bp.route('/child/<int:child_id>/recurring/<int:config_id>/cancel', methods=['POST'])
@parent_required
def cancel_recurring_chore(child_id, config_id):
    """Deactivate a recurring chore — stops new instances from being created."""
    ac = AssignedChore.query.filter_by(id=config_id, child_id=child_id).first_or_404()
    ac.is_active = False
    db.session.commit()
    flash(f'"{ac.effective_name}" recurring schedule cancelled.', 'info')
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
    return redirect_back('parent.child_detail', child_id=child_id)
