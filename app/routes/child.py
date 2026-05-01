import calendar as cal_module
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, session, redirect, url_for, flash, request
from ..models import Child, AssignedChore, BalanceTransaction, AppSettings, WishlistItem
from ..utils import get_payout_period_info
from .. import db

child_bp = Blueprint('child', __name__)


@child_bp.route('/')
def select():
    children = Child.query.order_by(Child.name).all()
    # Disable inactivity timeout on the select screen — it's the landing page.
    return render_template('child/select.html', children=children, inactivity_timeout_ms=0)


@child_bp.route('/<int:child_id>')
def dashboard(child_id):
    child = Child.query.get_or_404(child_id)
    session['child_id'] = child_id

    sort = request.args.get('sort', 'date')
    assigned = AssignedChore.query.filter_by(child_id=child_id, status='assigned').all()
    if sort == 'name':
        assigned.sort(key=lambda ac: ac.chore.name.lower())
    elif sort == 'value':
        assigned.sort(key=lambda ac: ac.effective_value, reverse=True)
    elif sort == 'cadence':
        assigned.sort(key=lambda ac: (ac.recurrence_cadence or '', ac.chore.name.lower()))
    else:
        assigned.sort(key=lambda ac: ac.assigned_date, reverse=True)
    submitted = (
        AssignedChore.query
        .filter_by(child_id=child_id, status='submitted')
        .order_by(AssignedChore.submitted_date.desc())
        .all()
    )

    # Period earnings — what the child has earned this payout cycle
    period = get_payout_period_info()
    cadence = period['cadence']

    if cadence == 'instant':
        # Show today's paid chores as "recently earned"
        period_chores = (
            AssignedChore.query
            .filter(
                AssignedChore.child_id == child_id,
                AssignedChore.status == 'approved',
                AssignedChore.approved_date >= period['period_start'],
            )
            .order_by(AssignedChore.approved_date.desc())
            .all()
        )
    else:
        # Show chores approved but not yet paid out
        period_chores = (
            AssignedChore.query
            .filter_by(child_id=child_id, status='approved_pending')
            .order_by(AssignedChore.approved_date.desc())
            .all()
        )

    period_total = sum(ac.actual_payout for ac in period_chores)

    return render_template(
        'child/dashboard.html',
        child=child,
        assigned=assigned,
        submitted=submitted,
        sort=sort,
        period=period,
        period_chores=period_chores,
        period_total=period_total,
    )


@child_bp.route('/<int:child_id>/history')
def history(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)

    month_str = request.args.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = (int(p) for p in month_str.split('-'))
        date(year, month, 1)
    except (ValueError, AttributeError):
        year, month = date.today().year, date.today().month

    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days_in_month = [month_start + timedelta(days=i) for i in range((month_end - month_start).days)]

    day_str = request.args.get('day')
    selected_day = None
    if day_str:
        try:
            selected_day = date.fromisoformat(day_str)
        except ValueError:
            pass

    approved_in_month = AssignedChore.query.filter(
        AssignedChore.child_id == child_id,
        AssignedChore.status.in_(['approved', 'approved_pending']),
        AssignedChore.approved_date >= datetime.combine(month_start, datetime.min.time()),
        AssignedChore.approved_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    txns_in_month = BalanceTransaction.query.filter(
        BalanceTransaction.child_id == child_id,
        BalanceTransaction.transaction_date >= datetime.combine(month_start, datetime.min.time()),
        BalanceTransaction.transaction_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    expired_in_month = AssignedChore.query.filter(
        AssignedChore.child_id == child_id,
        AssignedChore.status == 'expired',
        AssignedChore.assigned_date >= datetime.combine(month_start, datetime.min.time()),
        AssignedChore.assigned_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    activity_days = {}
    for ac in approved_in_month:
        activity_days.setdefault(ac.approved_date.date(), {})['completed'] = True
    for tx in txns_in_month:
        activity_days.setdefault(tx.transaction_date.date(), {})['transaction'] = True
    for ac in expired_in_month:
        activity_days.setdefault(ac.assigned_date.date(), {})['missed'] = True

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

        day_expired = AssignedChore.query.filter(
            AssignedChore.child_id == child_id,
            AssignedChore.status == 'expired',
            db.func.date(AssignedChore.assigned_date) == selected_day.isoformat(),
        ).all()

    prev_month = f'{year-1}-12' if month == 1 else f'{year}-{month-1:02d}'
    next_month = f'{year+1}-01' if month == 12 else f'{year}-{month+1:02d}'

    return render_template(
        'child/history.html',
        child=child,
        year=year,
        month=month,
        month_name=month_start.strftime('%B %Y'),
        cal_grid=cal_grid,
        selected_day=selected_day,
        day_completed=day_completed,
        day_transactions=day_transactions,
        day_expired=day_expired,
        prev_month=prev_month,
        next_month=next_month,
    )


@child_bp.route('/<int:child_id>/wishlist')
def wishlist(child_id):
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
    return render_template('child/wishlist.html', child=child, active=active, purchased=purchased)


@child_bp.route('/<int:child_id>/wishlist/add', methods=['POST'])
def add_wish(child_id):
    child = Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if not name or not price_raw:
        flash('Item name and price are required.', 'error')
        return redirect(url_for('child.wishlist', child_id=child_id))

    # Place at end of list
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
    flash(f'"{name}" added to your wishlist! 🌟', 'success')
    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/wishlist/<int:item_id>/move', methods=['POST'])
def move_wish(child_id, item_id):
    direction = request.form.get('direction')  # 'up' or 'down'
    item = WishlistItem.query.get_or_404(item_id)
    if item.child_id != child_id:
        return redirect(url_for('child.wishlist', child_id=child_id))

    siblings = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )
    ids = [s.id for s in siblings]
    idx = ids.index(item_id)

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(siblings):
        other = siblings[swap_idx]
        item.sort_order, other.sort_order = other.sort_order, item.sort_order
        # Ensure distinct values if they were equal
        if item.sort_order == other.sort_order:
            item.sort_order = swap_idx
            other.sort_order = idx
        db.session.commit()

    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/wishlist/<int:item_id>/delete', methods=['POST'])
def delete_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    if item.child_id != child_id:
        return redirect(url_for('child.wishlist', child_id=child_id))
    db.session.delete(item)
    db.session.commit()
    flash('Item removed from wishlist.', 'info')
    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/submit/<int:ac_id>', methods=['POST'])
def submit_chore(child_id, ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    if ac.child_id != child_id or ac.status != 'assigned':
        flash('Cannot submit this chore right now.', 'error')
        return redirect(url_for('child.dashboard', child_id=child_id))

    ac.status = 'submitted'
    ac.submitted_date = datetime.now()
    db.session.commit()
    flash('Nice work! Your chore has been sent to a parent for review. 🌟', 'success')
    return redirect(url_for('child.dashboard', child_id=child_id))
