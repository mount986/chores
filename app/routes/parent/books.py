from datetime import datetime, date
from flask import render_template, redirect, url_for, request, flash, render_template_string
from ...models import ChoreInstance, BalanceTransaction, AppSettings, Book, BookCheckout, BookAssignment
from ... import db
from ...notifications import send_chore_submitted
from ...services.book_search import search_google_books
from ...services.books import (
    active_checkouts, checkout_sort_key, create_submitted_instance, default_due_date,
    expire_assignment_instance, set_assignment_reward,
)
from ...services.covers import download_cover, maybe_delete_cover
from ...utils import get_payout_cadence, parse_money
from .common import parent_bp, parent_required, redirect_back


# ── Library Books ─────────────────────────────────────────────────────────────

@parent_bp.route('/books')
@parent_required
def books_dashboard():
    checkouts = active_checkouts()
    sort = request.args.get('sort', 'status')
    if sort == 'title':
        checkouts.sort(key=lambda co: co.book.title.lower())
    elif sort == 'reward':
        checkouts.sort(
            key=lambda co: max((a.reward_value for a in co.assignments), default=0.0),
            reverse=True,
        )
    else:
        sort = 'status'
        checkouts.sort(key=checkout_sort_key)
    return render_template('parent/books.html', checkouts=checkouts, current_sort=sort)


@parent_bp.route('/books/add', methods=['POST'])
@parent_required
def add_book():
    title = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip() or None
    isbn = request.form.get('isbn', '').strip() or None
    cover_url = request.form.get('cover_url', '').strip() or None
    due_date_str = request.form.get('due_date', '').strip()

    book = None
    if isbn:
        book = Book.query.filter_by(isbn=isbn).first()
        if book:
            flash('Book already in library — created a new checkout.', 'success')

    if not book:
        book = Book(title=title, author=author, isbn=isbn, cover_url=cover_url)
        db.session.add(book)
        db.session.flush()

    try:
        due_date = date.fromisoformat(due_date_str)
    except ValueError:
        due_date = default_due_date()

    checkout = BookCheckout(book_id=book.id, due_date=due_date)
    db.session.add(checkout)
    db.session.commit()
    download_cover(book)
    flash(f'"{book.title}" checked out!', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/search-external')
@parent_required
def search_books_external():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return ''
    results, error = search_google_books(q, AppSettings.get('google_books_api_key'))
    return render_template('parent/book_search_results.html', results=results, error=error)


@parent_bp.route('/checkouts/<int:checkout_id>/return', methods=['POST'])
@parent_required
def return_checkout(checkout_id):
    checkout = BookCheckout.query.get_or_404(checkout_id)
    checkout.returned_date = datetime.now()
    db.session.commit()
    maybe_delete_cover(checkout.book_id)
    flash(f'"{checkout.book.title}" marked as returned.', 'success')
    return redirect(url_for('parent.review_checkout', checkout_id=checkout_id))


@parent_bp.route('/checkouts/<int:checkout_id>/review', methods=['GET', 'POST'])
@parent_required
def review_checkout(checkout_id):
    checkout = BookCheckout.query.get_or_404(checkout_id)

    if request.method == 'POST':
        for assignment in checkout.assignments:
            new_status = request.form.get(f'status_{assignment.id}')
            reward_raw = request.form.get(f'reward_{assignment.id}', '').strip()

            if new_status in ('unread', 'reading', 'finished', 'unfinished'):
                if new_status == 'unfinished':
                    expire_assignment_instance(assignment)
                assignment.status = new_status

            try:
                new_reward = parse_money(reward_raw)
            except ValueError:
                new_reward = None
            if new_reward is not None:
                set_assignment_reward(assignment, new_reward)

        db.session.commit()
        flash('Review saved.', 'success')
        return redirect(url_for('parent.books_dashboard'))

    return render_template('parent/book_review.html', checkout=checkout)


@parent_bp.route('/books/<int:book_id>/checkout-again', methods=['POST'])
@parent_required
def checkout_again(book_id):
    book = Book.query.get_or_404(book_id)
    checkout = BookCheckout(book_id=book.id, due_date=default_due_date())
    db.session.add(checkout)
    db.session.commit()
    flash('Checked out again!', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/history')
@parent_required
def books_history():
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date != None)  # noqa: E711
        .join(Book)
        .order_by(BookCheckout.returned_date.desc())
        .all()
    )
    return render_template('parent/book_history.html', checkouts=checkouts)


def _remove_assignment(assignment):
    """Delete a book assignment, expiring its pending chore instance without payout."""
    expire_assignment_instance(assignment)
    db.session.delete(assignment)
    db.session.commit()
    flash('Assignment removed.', 'success')


@parent_bp.route('/assignments/<int:assignment_id>/edit-reward', methods=['POST'])
@parent_required
def edit_assignment_reward(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    reward_raw = request.form.get('reward', '').strip()
    try:
        new_reward = parse_money(reward_raw)
        if new_reward is None:
            raise ValueError
    except ValueError:
        flash('Invalid reward value.', 'error')
        return redirect_back('parent.books_dashboard')

    set_assignment_reward(assignment, new_reward)
    db.session.commit()
    flash('Reward updated.', 'success')

    if request.headers.get('HX-Request'):
        return render_template_string(f'<span>${new_reward:.2f} saved</span>')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/assignments/<int:assignment_id>/update', methods=['POST'])
@parent_required
def update_assignment(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    status = request.form.get('status', '').strip()

    if status == 'unassigned':
        _remove_assignment(assignment)
        return redirect_back('parent.books_dashboard')

    try:
        new_reward = parse_money(request.form.get('reward'))
    except ValueError:
        new_reward = None
    if new_reward is not None:
        set_assignment_reward(assignment, max(new_reward, 0.0))

    if status in ('unread', 'reading', 'unfinished'):
        if status == 'unfinished':
            expire_assignment_instance(assignment)
        assignment.status = status

    db.session.commit()
    flash('Assignment updated.', 'success')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/assignments/<int:assignment_id>/mark-finished', methods=['POST'])
@parent_required
def mark_book_finished(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    book = assignment.checkout.book
    now = datetime.now()

    if not assignment.finished_date:
        assignment.finished_date = now

    inst = ChoreInstance.query.get(assignment.chore_instance_id) if assignment.chore_instance_id else None

    if inst and inst.status in ('approved', 'approved_pending'):
        assignment.status = 'approved'
        db.session.commit()
        flash('Already approved.', 'success')
        return redirect_back('parent.books_dashboard')

    if not inst:
        inst = create_submitted_instance(assignment, now)

    amount = assignment.reward_value
    inst.approved_date = now
    inst.terminal_date = now

    cadence = get_payout_cadence()
    if cadence == 'instant':
        inst.status = 'approved'
        assignment.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=assignment.child_id,
            amount=amount,
            description=f'Book finished: {book.title}',
            chore_instance_id=inst.id,
        ))
        flash(f'Finished! ${amount:.2f} added to {assignment.child.name}\'s balance. 🎉', 'success')
    else:
        inst.status = 'approved_pending'
        flash(f'Finished! Reward queued for next {cadence} payout.', 'success')

    assignment.status = 'approved'
    db.session.commit()
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/assignments/<int:assignment_id>/set-status', methods=['POST'])
@parent_required
def set_assignment_status(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    status = request.form.get('status', '').strip()
    if status not in ('unread', 'reading', 'finished', 'unfinished', 'unassigned'):
        flash('Invalid status.', 'error')
        return redirect_back('parent.books_dashboard')

    if status == 'unassigned':
        _remove_assignment(assignment)
        return redirect_back('parent.books_dashboard')

    if status == 'finished' and not assignment.chore_instance_id:
        if not assignment.finished_date:
            assignment.finished_date = datetime.now()
        send_chore_submitted(create_submitted_instance(assignment))

    if status == 'unfinished':
        expire_assignment_instance(assignment)

    assignment.status = status
    db.session.commit()
    flash('Status updated.', 'success')

    if request.headers.get('HX-Request'):
        return render_template_string(f'<span>Status: {status}</span>')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/books/search')
@parent_required
def search_books():
    from thefuzz import process as fuzz_process
    q = request.args.get('q', '').strip()
    checkouts = active_checkouts()
    if q:
        choices = {co.book.title: co for co in checkouts}
        results = fuzz_process.extractBests(q, choices, score_cutoff=65)
        checkouts = [co for co, score in results]
    return render_template('parent/books_list_partial.html', checkouts=checkouts, searching=bool(q))


@parent_bp.route('/checkouts/bulk-return', methods=['POST'])
@parent_required
def bulk_return_checkouts():
    checkout_ids = request.form.getlist('checkout_ids')
    book_ids = []
    for cid in checkout_ids:
        co = BookCheckout.query.get(int(cid))
        if co and not co.returned_date:
            co.returned_date = datetime.now()
            book_ids.append(co.book_id)
    db.session.commit()
    for bid in book_ids:
        maybe_delete_cover(bid)
    flash(f'Marked {len(checkout_ids)} book(s) as returned.', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/assignments/bulk-assign', methods=['POST'])
@parent_required
def bulk_assign_books():
    checkout_ids = request.form.getlist('checkout_ids')
    child_ids = request.form.getlist('child_ids')
    try:
        reward = parse_money(request.form.get('reward')) or 0.0
    except ValueError:
        flash('Invalid reward value.', 'error')
        return redirect(url_for('parent.books_dashboard'))
    count = 0
    for cid in checkout_ids:
        for child_id in child_ids:
            existing = BookAssignment.query.filter_by(
                checkout_id=int(cid), child_id=int(child_id)
            ).first()
            if not existing:
                db.session.add(BookAssignment(
                    checkout_id=int(cid), child_id=int(child_id),
                    reward_value=reward, status='unread',
                ))
                count += 1
    db.session.commit()
    flash(f'Assigned {count} book(s).', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/checkouts/bulk-set-status', methods=['POST'])
@parent_required
def bulk_set_status_checkouts():
    checkout_ids = request.form.getlist('checkout_ids')
    status = request.form.get('status', '').strip()
    if status not in ('unread', 'reading', 'finished', 'unfinished'):
        flash('Invalid status.', 'error')
        return redirect(url_for('parent.books_dashboard'))

    count = 0
    for cid in checkout_ids:
        co = BookCheckout.query.get(int(cid))
        if not co:
            continue
        for a in co.assignments:
            if a.status == status:
                continue
            a.status = status
            if status == 'unfinished':
                expire_assignment_instance(a)
            count += 1
    db.session.commit()
    flash(f'Updated {count} assignment(s) to {status}.', 'success')
    return redirect(url_for('parent.books_dashboard'))
