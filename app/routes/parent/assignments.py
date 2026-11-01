from datetime import datetime
from flask import redirect, url_for, request, flash
from ...models import Child, Chore, ChoreSuggestion
from ... import db
from ...services.assignments import create_assignment
from ...utils import parse_money
from .common import parent_bp, parent_required, redirect_back
from .forms import parse_unit_form, parse_recurrence_form


@parent_bp.route('/child/<int:child_id>/assign', methods=['POST'])
@parent_required
def assign_chore(child_id):
    child = Child.query.get_or_404(child_id)
    try:
        custom_value = parse_money(request.form.get('custom_value'))
    except ValueError:
        flash('Please enter a valid value.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

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
            **parse_unit_form('new_chore_per_unit', 'new_chore_unit_label'),
        )
        db.session.add(chore)
        db.session.flush()
        chore_id = chore.id
        custom_value = None
    else:
        chore_id = request.form.get('chore_id', type=int)
        chore = Chore.query.get_or_404(chore_id)
    is_recurring, recurrence_cadence, recurrence_day = parse_recurrence_form()
    create_assignment(child_id, chore_id, custom_value, is_recurring, recurrence_cadence, recurrence_day)
    db.session.commit()

    cadence_label = f' (🔁 {recurrence_cadence})' if is_recurring else ''
    flash(f'"{chore.name}"{cadence_label} assigned to {child.name}!', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


# ── Child chore suggestions ───────────────────────────────────────────────────

@parent_bp.route('/suggestions/<int:suggestion_id>/approve', methods=['POST'])
@parent_required
def approve_suggestion(suggestion_id):
    """Approve a child's idea: add it to the library and assign it (one-time, or recurring if chosen)."""
    s = ChoreSuggestion.query.get_or_404(suggestion_id)
    if s.status != 'pending':
        flash('This suggestion has already been handled.', 'info')
        return redirect_back('parent.dashboard')

    name = request.form.get('name', '').strip()[:100] or s.name
    value = request.form.get('value', type=float)
    if value is None or value < 0:
        value = s.suggested_value if s.suggested_value is not None else 1.0

    chore = Chore(name=name, description=s.description, default_value=value)
    db.session.add(chore)
    db.session.flush()

    is_recurring, cadence, day = parse_recurrence_form()
    create_assignment(s.child_id, chore.id, None, is_recurring, cadence, day)

    s.status = 'approved'
    s.resolved_date = datetime.now()
    db.session.commit()

    cadence_label = f' (🔁 {cadence})' if is_recurring else ''
    flash(f'"{name}"{cadence_label} added to the library and assigned to {s.child.name}!', 'success')
    return redirect_back('parent.dashboard')


@parent_bp.route('/suggestions/<int:suggestion_id>/deny', methods=['POST'])
@parent_required
def deny_suggestion(suggestion_id):
    """Deny a child's idea. Nothing is added to the chore library."""
    s = ChoreSuggestion.query.get_or_404(suggestion_id)
    if s.status != 'pending':
        flash('This suggestion has already been handled.', 'info')
        return redirect_back('parent.dashboard')

    s.status = 'denied'
    s.denial_notes = request.form.get('notes', '').strip() or None
    s.resolved_date = datetime.now()
    db.session.commit()
    flash(f'Suggestion from {s.child.name} declined.', 'info')
    return redirect_back('parent.dashboard')
