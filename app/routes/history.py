from datetime import date

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from .. import history as hist
from ..models import Child

history_bp = Blueprint('history', __name__)


def _link_builder(endpoint, keys=('month', 'child', 'types', 'day'), **fixed):
    """url(**overrides): this page's URL with the current filters, overridden and with blanks dropped."""
    current = {k: request.args.get(k) for k in keys}

    def link(**overrides):
        args = {k: v for k, v in {**current, **overrides}.items() if v}
        return url_for(endpoint, **fixed, **args)
    return link


def _toggle_kind(active, key):
    """`types` value after clicking a chip: from "all", isolate it; otherwise toggle it. Empty or full means all."""
    if len(active) == len(hist.KINDS):
        picked = [key]
    else:
        picked = [k for k in hist.KIND_KEYS if (k in active) != (k == key)]
    return None if not picked or len(picked) == len(hist.KINDS) else ','.join(picked)


def _render(child_id, children, link):
    """Shared by the parent view (child_id optional) and the kid view (child_id fixed)."""
    today = date.today()
    year, month = hist.parse_month(request.args.get('month'), today)
    start, end = hist.month_bounds(year, month)
    kinds = hist.parse_kinds(request.args.get('types'))

    events = hist.build_events(start, end, child_id, kinds)

    try:
        selected_day = date.fromisoformat(request.args.get('day', ''))
    except ValueError:
        selected_day = today if (today.year, today.month) == (year, month) else None
    if selected_day and (selected_day.year, selected_day.month) != (year, month):
        selected_day = None

    kind_chips = [
        dict(key=key, label=label, color=color, active=key in kinds,
             url=link(types=_toggle_kind(kinds, key)))
        for key, label, color in hist.KINDS
    ]

    return dict(
        year=year, month=month, month_label=start.strftime('%B %Y'),
        prev_month=hist.shift_month(year, month, -1), next_month=hist.shift_month(year, month, 1),
        this_month=today.strftime('%Y-%m'),
        kind_chips=kind_chips, link=link,
        children=children, selected_child_id=child_id,
        grid=hist.month_grid(year, month, events, selected_day, today),
        show_kid_dots=len({e.child.id for e in events}) > 1,
        selected_day=selected_day,
        day_groups=hist.group_day(events, selected_day) if selected_day else [],
        totals=hist.month_totals(events),
    )


@history_bp.route('/')
def parent_view():
    if not session.get('parent_authenticated'):
        return redirect(url_for('parent.login'))
    children = Child.query.order_by(Child.name).all()
    child_id = request.args.get('child', type=int)
    if child_id not in {c.id for c in children}:
        child_id = None
    ctx = _render(child_id, children, _link_builder('history.parent_view'))
    return render_template('history.html', is_parent=True, child=None, **ctx)


@history_bp.route('/child/<int:child_id>')
def child_view(child_id):
    """A kid's own calendar: always locked to them, whatever the query string says."""
    child = Child.query.get(child_id)
    if child is None:
        abort(404)
    link = _link_builder('history.child_view', keys=('month', 'types', 'day'), child_id=child.id)
    ctx = _render(child.id, [child], link)
    return render_template('history.html', is_parent=False, child=child, **ctx)
