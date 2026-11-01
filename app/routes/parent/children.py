import os
from datetime import date
from flask import render_template, redirect, url_for, request, flash, current_app, make_response
from werkzeug.utils import secure_filename
from ...models import Child, Chore, AssignedChore, ChoreInstance, ChoreSuggestion, BalanceTransaction, WishlistItem, BookCheckout
from ... import db
from .common import parent_bp, parent_required


# ── Dashboard ─────────────────────────────────────────────────────────────────

@parent_bp.route('/')
@parent_required
def dashboard():
    children = Child.query.order_by(Child.name).all()
    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(ChoreInstance.status == 'submitted')
        .order_by(ChoreInstance.submitted_date)
        .all()
    )
    pending_suggestions = (
        ChoreSuggestion.query
        .filter_by(status='pending')
        .order_by(ChoreSuggestion.created_at)
        .all()
    )
    active_checkouts = BookCheckout.query.filter(BookCheckout.returned_date == None).all()  # noqa: E711
    books_active_count = len(active_checkouts)
    books_overdue_count = sum(1 for co in active_checkouts if co.is_overdue)
    return render_template(
        'parent/dashboard.html',
        children=children,
        pending_reviews=pending_reviews,
        pending_suggestions=pending_suggestions,
        books_active_count=books_active_count,
        books_overdue_count=books_overdue_count,
    )


# ── Child detail ──────────────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>')
@parent_required
def child_detail(child_id):
    from ...scheduler import get_period
    from ...utils import next_recurrence_date, CHORE_ICONS

    child = Child.query.get_or_404(child_id)
    today = date.today()

    # Priority order: submitted > assigned > upcoming > expired > pending payout > approved
    def _row_priority(instances, is_recurring, cadence):
        statuses = {inst.status for inst in instances}
        if 'submitted' in statuses:
            return 0
        if 'assigned' in statuses:
            return 1
        if is_recurring and cadence:
            current_p = get_period(cadence, today)
            if current_p and not any(i.period == current_p for i in instances):
                return 2  # upcoming
        if 'expired' in statuses:
            return 3
        if 'approved_pending' in statuses:
            return 4
        return 5

    chore_rows = []

    # 1. Recurring chores — one row per active config
    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=True, is_active=True
    ).all():
        cadence = ac_config.recurrence_cadence
        rec_day = ac_config.recurrence_day
        if not cadence:
            continue

        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        current_period = get_period(cadence, today)
        is_upcoming = bool(current_period and not any(i.period == current_period for i in instances))

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  True,
            'is_upcoming':   is_upcoming,
            'is_dimmed':     False,
            'cadence':       cadence,
            'rec_day':       rec_day,
            'next_date':     next_recurrence_date(cadence, rec_day, today),
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, True, cadence),
        })

    # 2. Non-recurring — one row per active config (with its instances)
    #    • Active/submitted → show normally.
    #    • Terminal (approved/approved_pending/expired) assigned today → show
    #      dimmed (grey) so the parent can still see it briefly.
    #    • Terminal from a previous day → hide entirely.
    _active_statuses    = {'assigned', 'submitted'}
    _terminal_statuses  = {'approved', 'approved_pending', 'expired'}

    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=False, is_active=True
    ).all():
        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        has_active = any(i.status in _active_statuses for i in instances)

        if not has_active:
            # Keep only if a terminal instance became terminal today.
            # Use terminal_date (set at transition time); fall back to
            # approved_date for pre-migration rows that lack terminal_date.
            def _became_terminal_today(i):
                td = i.terminal_date or (
                    i.approved_date if i.status in ('approved', 'approved_pending') else None
                )
                if td is None:
                    return False
                d = td.date() if hasattr(td, 'date') else td
                return d == today

            if not any(
                i.status in _terminal_statuses and _became_terminal_today(i)
                for i in instances
            ):
                continue
            is_dimmed = True
        else:
            is_dimmed = False

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  False,
            'is_upcoming':   False,
            'is_dimmed':     is_dimmed,
            'cadence':       None,
            'rec_day':       None,
            'next_date':     None,
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, False, None),
        })

    _valid_sorts = {'status', 'name', 'value', 'cadence'}
    sort = request.args.get('sort')          # explicit URL param takes priority
    save_cookie = sort in _valid_sorts       # only save when explicitly chosen
    if sort not in _valid_sorts:
        sort = request.cookies.get('chore_sort', 'status')  # fall back to cookie

    if sort == 'name':
        chore_rows.sort(key=lambda r: r['config'].effective_name.lower())
    elif sort == 'value':
        chore_rows.sort(key=lambda r: -r['config'].effective_value)
    elif sort == 'cadence':
        chore_rows.sort(key=lambda r: (r['cadence'] or 'zzz', r['config'].effective_name.lower()))
    else:  # 'status' — priority first, then alphabetical
        chore_rows.sort(key=lambda r: (r['priority'], r['config'].effective_name.lower()))

    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'submitted',
        )
        .order_by(ChoreInstance.submitted_date)
        .all()
    )

    all_chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    wishlist_active = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )

    # ── Ledger: payout events + purchases, newest first, paginated ────────────
    from collections import defaultdict

    LEDGER_PAGE_SIZE = 10
    ledger_page = request.args.get('ledger_page', 1, type=int)

    # Positive balance transactions → payout events grouped by calendar date
    pos_txns = (
        BalanceTransaction.query
        .filter(
            BalanceTransaction.child_id == child_id,
            BalanceTransaction.amount > 0,
        )
        .order_by(BalanceTransaction.transaction_date)
        .all()
    )
    payout_by_date = defaultdict(list)
    for t in pos_txns:
        d = t.transaction_date.date() if hasattr(t.transaction_date, 'date') else t.transaction_date
        payout_by_date[d].append(t)

    payout_events = [
        {
            'type':         'payout',
            'date':         d,
            'amount':       sum(t.amount for t in txns),
            'transactions': txns,  # already in ascending order from the query
        }
        for d, txns in payout_by_date.items()
    ]

    # Purchased wishlist items → purchase events
    wishlist_purchased = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .all()
    )
    purchase_events = []
    for item in wishlist_purchased:
        d = item.purchased_date
        d = (d.date() if hasattr(d, 'date') else d) if d else today
        purchase_events.append({
            'type':   'purchase',
            'date':   d,
            'amount': item.price,
            'item':   item,
        })

    all_ledger = sorted(payout_events + purchase_events, key=lambda e: e['date'], reverse=True)

    ledger_total_pages = max(1, (len(all_ledger) + LEDGER_PAGE_SIZE - 1) // LEDGER_PAGE_SIZE)
    ledger_page = max(1, min(ledger_page, ledger_total_pages))
    ledger_events = all_ledger[(ledger_page - 1) * LEDGER_PAGE_SIZE: ledger_page * LEDGER_PAGE_SIZE]

    resp = make_response(render_template(
        'parent/child_detail.html',
        child=child,
        chore_rows=chore_rows,
        sort=sort,
        pending_reviews=pending_reviews,
        all_chores=all_chores,
        chore_icons=CHORE_ICONS,
        wishlist_active=wishlist_active,
        ledger_events=ledger_events,
        ledger_page=ledger_page,
        ledger_total_pages=ledger_total_pages,
        today=today,
    ))
    if save_cookie:
        resp.set_cookie('chore_sort', sort, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


@parent_bp.route('/child/<int:child_id>/adjust-balance', methods=['POST'])
@parent_required
def adjust_balance(child_id):
    """Unified balance adjustment: positive = credit, negative = penalty."""
    child = Child.query.get_or_404(child_id)
    amount = request.form.get('amount', type=float)
    reason = request.form.get('reason', '').strip()

    if amount is None or amount == 0:
        flash('Please enter a non-zero amount.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    if amount < 0:
        description = f'Penalty: {reason}' if reason else 'Penalty'
        flash(f'${abs(amount):.2f} penalty applied to {child.name}\'s balance.', 'success')
    else:
        description = reason or 'Manual adjustment'
        flash(f'${amount:.2f} added to {child.name}\'s balance.', 'success')

    child.balance += amount
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=amount,
        description=description,
    ))
    db.session.commit()
    return redirect(url_for('parent.child_detail', child_id=child_id))


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
    upload_dir = os.path.join(current_app.instance_path, 'avatars')
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))
    child.avatar_filename = filename
    db.session.commit()
    flash(f'Avatar updated for {child.name}!', 'success')
    return redirect(url_for('parent.settings'))
