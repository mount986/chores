from flask import render_template, redirect, url_for, request, flash
from ...models import Chore
from ... import db
from .common import parent_bp, parent_required
from .forms import parse_unit_form


# ── Chore library ─────────────────────────────────────────────────────────────

@parent_bp.route('/chores')
@parent_required
def chore_library():
    from ...utils import CHORE_ICONS
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
        **parse_unit_form(),
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
    unit = parse_unit_form()
    chore.is_per_unit = unit['is_per_unit']
    chore.unit_label = unit['unit_label']
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
