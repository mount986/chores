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


def redirect_back(endpoint, **values):
    """Redirect to the page the form was submitted from, or to `endpoint` when unknown."""
    return redirect(request.referrer or url_for(endpoint, **values))
