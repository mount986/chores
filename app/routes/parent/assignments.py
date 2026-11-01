from datetime import datetime, date, timedelta
from flask import redirect, url_for, request, flash
from ...models import Child, Chore, AssignedChore, ChoreInstance, ChoreSuggestion
from ... import db
from .common import parent_bp, parent_required, _parse_unit_form, _parse_recurrence_form


@parent_bp.route('/child/<int:child_id>/assign', methods=['POST'])
@parent_required
def assign_chore(child_id):
    child = Child.query.get_or_404(child_id)
    custom_value_raw = request.form.get('custom_value', '').strip()
    custom_value = float(custom_value_raw) if custom_value_raw else None

    if request.form.get('chore_id') == 'new':
        new_name = request.form.get('new_chore_name', '').strip()
        if not new_name:
            flash('Chore name is required.', 'error')
            return redirect(url_for('parent.child_detail', child_id=child_id))
        # The value entered on the form becomes the new chore's library default,
        # so it is not also stored as a per-child override.
        chore = Chore(
            name=new_name,
            description=request.form.get('new_chore_description', '').strip(),
            icon=request.form.get('new_chore_icon', '').strip() or None,
            default_value=custom_value if custom_value is not None else 1.0,
            **_parse_unit_form('new_chore_per_unit', 'new_chore_unit_label'),
        )
        db.session.add(chore)
        db.session.flush()
        chore_id = chore.id
        custom_value = None
    else:
        chore_id = request.form.get('chore_id', type=int)
        chore = Chore.query.get_or_404(chore_id)
    is_recurring, recurrence_cadence, recurrence_day = _parse_recurrence_form()
    _create_assignment(child_id, chore_id, custom_value, is_recurring, recurrence_cadence, recurrence_day)
    db.session.commit()

    cadence_label = f' (🔁 {recurrence_cadence})' if is_recurring else ''
    flash(f'"{chore.name}"{cadence_label} assigned to {child.name}!', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


def _create_assignment(child_id, chore_id, custom_value, is_recurring, cadence, day):
    """Add an AssignedChore config row plus its first instance. Caller commits."""
    from ...scheduler import get_period

    today = date.today()
    if is_recurring and cadence == 'weekly' and day is not None:
        ref_date = today - timedelta(days=(today.weekday() - day) % 7)
    elif is_recurring and cadence == 'monthly' and day is not None:
        if today.day >= day:
            ref_date = today.replace(day=day)
        else:
            prev_month = today.month - 1 or 12
            prev_year = today.year if today.month > 1 else today.year - 1
            ref_date = date(prev_year, prev_month, day)
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


# ── Child chore suggestions ───────────────────────────────────────────────────

@parent_bp.route('/suggestions/<int:suggestion_id>/approve', methods=['POST'])
@parent_required
def approve_suggestion(suggestion_id):
    """Approve a child's idea: add it to the library and assign it (one-time, or recurring if chosen)."""
    s = ChoreSuggestion.query.get_or_404(suggestion_id)
    if s.status != 'pending':
        flash('This suggestion has already been handled.', 'info')
        return redirect(request.referrer or url_for('parent.dashboard'))

    name = request.form.get('name', '').strip()[:100] or s.name
    value = request.form.get('value', type=float)
    if value is None or value < 0:
        value = s.suggested_value if s.suggested_value is not None else 1.0

    chore = Chore(name=name, description=s.description, default_value=value)
    db.session.add(chore)
    db.session.flush()

    is_recurring, cadence, day = _parse_recurrence_form()
    _create_assignment(s.child_id, chore.id, None, is_recurring, cadence, day)

    s.status = 'approved'
    s.resolved_date = datetime.now()
    db.session.commit()

    cadence_label = f' (🔁 {cadence})' if is_recurring else ''
    flash(f'"{name}"{cadence_label} added to the library and assigned to {s.child.name}!', 'success')
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/suggestions/<int:suggestion_id>/deny', methods=['POST'])
@parent_required
def deny_suggestion(suggestion_id):
    """Deny a child's idea. Nothing is added to the chore library."""
    s = ChoreSuggestion.query.get_or_404(suggestion_id)
    if s.status != 'pending':
        flash('This suggestion has already been handled.', 'info')
        return redirect(request.referrer or url_for('parent.dashboard'))

    s.status = 'denied'
    s.denial_notes = request.form.get('notes', '').strip() or None
    s.resolved_date = datetime.now()
    db.session.commit()
    flash(f'Suggestion from {s.child.name} declined.', 'info')
    return redirect(request.referrer or url_for('parent.dashboard'))
