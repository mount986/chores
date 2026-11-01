from functools import wraps
from flask import Blueprint, session, redirect, url_for, request
from datetime import date
from ...models import Child
from ...utils import is_payout_day_enabled

parent_bp = Blueprint('parent', __name__)


@parent_bp.context_processor
def inject_today():
    return {'today_iso': date.today().isoformat()}


@parent_bp.context_processor
def inject_nav_children():
    """Make all children available in every parent template for the quick-switch bar."""
    if not session.get('parent_authenticated'):
        return {}
    children = Child.query.order_by(Child.name).all()
    return {'nav_children': children, 'payout_day_enabled': is_payout_day_enabled()}


def parent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('parent_authenticated'):
            return redirect(url_for('parent.login'))
        return f(*args, **kwargs)
    return decorated


def _parse_unit_form(flag_field='is_per_unit', label_field='unit_label'):
    """Read the per-unit toggle and unit label from the submitted form, as Chore kwargs."""
    is_per_unit = request.form.get(flag_field) == 'on'
    label = request.form.get(label_field, '').strip()[:30] if is_per_unit else ''
    return {'is_per_unit': is_per_unit, 'unit_label': label or None}


def _parse_recurrence_form():
    """Read is_recurring / recurrence_cadence / recurrence_day from the submitted form."""
    is_recurring = request.form.get('is_recurring') == 'on'
    cadence = request.form.get('recurrence_cadence', '').strip() if is_recurring else None
    if cadence not in ('daily', 'weekly', 'monthly'):
        return False, None, None
    day = None
    if cadence in ('weekly', 'monthly'):
        default_day = 0 if cadence == 'weekly' else 1
        try:
            day = int(request.form.get('recurrence_day', default_day))
        except (ValueError, TypeError):
            day = default_day
    return True, cadence, day
