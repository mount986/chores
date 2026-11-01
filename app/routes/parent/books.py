from datetime import datetime, date, timedelta
from flask import render_template, redirect, url_for, request, flash, current_app
from ...models import AssignedChore, ChoreInstance, BalanceTransaction, AppSettings, Book, BookCheckout, BookAssignment
from ... import db
from ...utils import get_payout_cadence, parse_money
from .common import parent_bp, parent_required, redirect_back
from .covers import _download_cover, _maybe_delete_cover


# ── Library Books ─────────────────────────────────────────────────────────────

def _book_sort_key(checkout):
    statuses = {a.status for a in checkout.assignments}
    if 'reading' in statuses:
        group = 0
    elif 'unread' in statuses:
        group = 1
    else:
        group = 2
    return (group, checkout.book.title.lower())


def _get_loan_days():
    setting = AppSettings.query.get('book_default_loan_days')
    try:
        return int(setting.value) if setting else 28
    except (ValueError, TypeError):
        return 28


@parent_bp.route('/books')
@parent_required
def books_dashboard():
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .join(Book)
        .all()
    )
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
        checkouts.sort(key=_book_sort_key)
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

    if due_date_str:
        try:
            due_date = date.fromisoformat(due_date_str)
        except ValueError:
            due_date = date.today() + timedelta(days=_get_loan_days())
    else:
        due_date = date.today() + timedelta(days=_get_loan_days())

    checkout = BookCheckout(book_id=book.id, due_date=due_date)
    db.session.add(checkout)
    db.session.commit()
    _download_cover(book)
    flash(f'"{book.title}" checked out!', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/search-external')
@parent_required
def search_books_external():
    import json
    import re
    import urllib.request
    import urllib.error
    import urllib.parse
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return ''
    isbn_clean = re.sub(r'[-\s]', '', q)
    is_isbn = isbn_clean.isdigit() and len(isbn_clean) in (10, 13)
    api_key_row = AppSettings.query.get('google_books_api_key')
    api_key = api_key_row.value if api_key_row else ''
    if is_isbn:
        api_url = f'https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn_clean}&maxResults=1'
    else:
        api_url = (f'https://www.googleapis.com/books/v1/volumes'
                   f'?q={urllib.parse.quote(q)}&maxResults=10')
    if api_key:
        api_url += f'&key={api_key}'
    results, error = [], None
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        for item in (data.get('items') or []):
            info = item.get('volumeInfo', {})
            title = info.get('title', '')
            if not title:
                continue
            authors = info.get('authors', [])
            author = ', '.join(authors) if authors else ''
            isbn = ''
            for id_entry in info.get('industryIdentifiers', []):
                if id_entry.get('type') == 'ISBN_13':
                    isbn = id_entry['identifier']
                    break
            if not isbn:
                for id_entry in info.get('industryIdentifiers', []):
                    if id_entry.get('type') == 'ISBN_10':
                        isbn = id_entry['identifier']
                        break
            image_links = info.get('imageLinks', {})
            cover = image_links.get('thumbnail') or image_links.get('smallThumbnail') or ''
            if cover:
                cover = cover.replace('&edge=curl', '').replace('http://', 'https://')
            results.append({'title': title, 'author': author, 'isbn': isbn, 'cover': cover})
    except urllib.error.HTTPError as e:
        current_app.logger.warning('Book search HTTP %s for q=%r', e.code, q)
        error = 'Rate limited — try again in a moment.' if e.code == 429 else f'Google Books error ({e.code}).'
    except Exception as e:
        current_app.logger.warning('Book search failed for q=%r: %s', q, e)
        error = 'Could not reach Google Books.'
    return render_template('parent/book_search_results.html', results=results, error=error)


@parent_bp.route('/checkouts/<int:checkout_id>/return', methods=['POST'])
@parent_required
def return_checkout(checkout_id):
    checkout = BookCheckout.query.get_or_404(checkout_id)
    checkout.returned_date = datetime.now()
    db.session.commit()
    _maybe_delete_cover(checkout.book_id)
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
                if new_status == 'unfinished' and assignment.chore_instance_id:
                    inst = ChoreInstance.query.get(assignment.chore_instance_id)
                    if inst:
                        inst.status = 'expired'
                        inst.awarded_value = 0
                        inst.terminal_date = datetime.now()
                assignment.status = new_status

            if reward_raw:
                try:
                    new_reward = parse_money(reward_raw)
                    assignment.reward_value = new_reward
                    if (assignment.chore_instance_id
                            and assignment.chore_instance
                            and assignment.chore_instance.status not in ('approved', 'approved_pending')):
                        ac = assignment.chore_instance.assigned_chore
                        if ac:
                            ac.custom_value = new_reward
                except ValueError:
                    pass

        db.session.commit()
        flash('Review saved.', 'success')
        return redirect(url_for('parent.books_dashboard'))

    return render_template('parent/book_review.html', checkout=checkout)


@parent_bp.route('/books/<int:book_id>/checkout-again', methods=['POST'])
@parent_required
def checkout_again(book_id):
    book = Book.query.get_or_404(book_id)
    due_date = date.today() + timedelta(days=_get_loan_days())
    checkout = BookCheckout(book_id=book.id, due_date=due_date)
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

    assignment.reward_value = new_reward
    if (assignment.chore_instance_id
            and assignment.chore_instance
            and assignment.chore_instance.status not in ('approved', 'approved_pending', 'expired')):
        ac = assignment.chore_instance.assigned_chore
        if ac:
            ac.custom_value = new_reward
    db.session.commit()
    flash('Reward updated.', 'success')

    if request.headers.get('HX-Request'):
        from flask import render_template_string
        return render_template_string(f'<span>${new_reward:.2f} saved</span>')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/assignments/<int:assignment_id>/update', methods=['POST'])
@parent_required
def update_assignment(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    status = request.form.get('status', '').strip()

    if status == 'unassigned':
        if assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'approved_pending', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        db.session.delete(assignment)
        db.session.commit()
        flash('Assignment removed.', 'success')
        return redirect_back('parent.books_dashboard')

    reward_raw = request.form.get('reward', '').strip()
    try:
        new_reward = parse_money(reward_raw)
        if new_reward is None:
            raise ValueError
        if new_reward < 0:
            new_reward = 0.0
        assignment.reward_value = new_reward
        if (assignment.chore_instance_id
                and assignment.chore_instance
                and assignment.chore_instance.status not in ('approved', 'approved_pending', 'expired')):
            ac = assignment.chore_instance.assigned_chore
            if ac:
                ac.custom_value = new_reward
    except (ValueError, TypeError):
        pass

    if status in ('unread', 'reading', 'unfinished'):
        if status == 'unfinished' and assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'approved_pending', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        assignment.status = status

    db.session.commit()
    flash('Assignment updated.', 'success')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/assignments/<int:assignment_id>/mark-finished', methods=['POST'])
@parent_required
def mark_book_finished(assignment_id):
    from ...utils import get_or_create_book_chore
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
        book_chore = get_or_create_book_chore()
        ac = AssignedChore(
            child_id=assignment.child_id,
            chore_id=book_chore.id,
            custom_value=assignment.reward_value,
            override_name=book.title,
            is_recurring=False,
            is_active=True,
        )
        db.session.add(ac)
        db.session.flush()
        inst = ChoreInstance(
            assigned_chore_id=ac.id,
            status='submitted',
            assigned_date=now,
            submitted_date=now,
            period=date.today().isoformat(),
        )
        db.session.add(inst)
        db.session.flush()
        assignment.chore_instance_id = inst.id

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
        if assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        db.session.delete(assignment)
        db.session.commit()
        flash('Assignment removed.', 'success')
        return redirect_back('parent.books_dashboard')

    if status == 'finished' and not assignment.chore_instance_id:
        from ...utils import get_or_create_book_chore
        from ...notifications import send_chore_submitted
        book = assignment.checkout.book
        if not assignment.finished_date:
            assignment.finished_date = datetime.now()
        book_chore = get_or_create_book_chore()
        ac = AssignedChore(
            child_id=assignment.child_id,
            chore_id=book_chore.id,
            custom_value=assignment.reward_value,
            override_name=book.title,
            is_recurring=False,
            is_active=True,
        )
        db.session.add(ac)
        db.session.flush()
        inst = ChoreInstance(
            assigned_chore_id=ac.id,
            status='submitted',
            assigned_date=datetime.now(),
            submitted_date=datetime.now(),
            period=date.today().isoformat(),
        )
        db.session.add(inst)
        db.session.flush()
        assignment.chore_instance_id = inst.id
        send_chore_submitted(inst)

    if status == 'unfinished' and assignment.chore_instance_id:
        inst = ChoreInstance.query.get(assignment.chore_instance_id)
        if inst and inst.status not in ('approved', 'expired'):
            inst.status = 'expired'
            inst.awarded_value = 0
            inst.terminal_date = datetime.now()

    assignment.status = status
    db.session.commit()
    flash('Status updated.', 'success')

    if request.headers.get('HX-Request'):
        from flask import render_template_string
        return render_template_string(f'<span>Status: {status}</span>')
    return redirect_back('parent.books_dashboard')


@parent_bp.route('/books/search')
@parent_required
def search_books():
    from thefuzz import process as fuzz_process
    q = request.args.get('q', '').strip()
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .join(Book)
        .all()
    )
    if q:
        choices = {co.book.title: co for co in checkouts}
        results = fuzz_process.extractBests(q, choices, score_cutoff=65)
        checkouts = [co for co, score in results]
    return render_template('parent/books_list_partial.html', checkouts=checkouts)


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
        _maybe_delete_cover(bid)
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
            if status == 'unfinished' and a.chore_instance_id:
                inst = ChoreInstance.query.get(a.chore_instance_id)
                if inst and inst.status not in ('approved', 'expired'):
                    inst.status = 'expired'
                    inst.awarded_value = 0
            count += 1
    db.session.commit()
    flash(f'Updated {count} assignment(s) to {status}.', 'success')
    return redirect(url_for('parent.books_dashboard'))
