import os
from datetime import date
from flask import render_template, redirect, url_for, request, flash, current_app, make_response
from werkzeug.utils import secure_filename
from ...models import Child, Chore, AssignedChore, ChoreInstance, ChoreSuggestion, BalanceTransaction, BookCheckout
from ... import db
from ...services.child_detail import VALID_SORTS, build_chore_rows, build_ledger, sort_chore_rows
from ...services.wishlist import active_wishlist
from ...utils import CHORE_ICONS
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
    child = Child.query.get_or_404(child_id)
    today = date.today()

    chore_rows = build_chore_rows(child_id, today)

    sort = request.args.get('sort')          # explicit URL param takes priority
    save_cookie = sort in VALID_SORTS        # only save when explicitly chosen
    if not save_cookie:
        sort = request.cookies.get('chore_sort', 'status')  # fall back to cookie
    sort_chore_rows(chore_rows, sort)

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
    wishlist_active = active_wishlist(child_id)
    ledger_events, ledger_page, ledger_total_pages = build_ledger(
        child_id, today, request.args.get('ledger_page', 1, type=int))

    resp = make_response(render_template(
        'parent/child_detail.html',
        child=child,
        chore_rows=chore_rows,
        sort=sort,
        pending_reviews=pending_reviews,
        all_chores=Chore.query.filter_by(is_active=True).order_by(Chore.name).all(),
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
