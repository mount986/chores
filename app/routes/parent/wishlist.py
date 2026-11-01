from datetime import datetime
from flask import render_template, redirect, url_for, request, flash
from ...models import Child, BalanceTransaction, WishlistItem
from ... import db
from ...services.wishlist import add_wishlist_item
from ...utils import parse_money
from .common import parent_bp, parent_required


# ── Wishlist management ───────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>/wishlist')
@parent_required
def child_wishlist(child_id):
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
    return render_template('parent/child_wishlist.html', child=child, active=active, purchased=purchased)


@parent_bp.route('/child/<int:child_id>/wishlist/add', methods=['POST'])
@parent_required
def parent_add_wish(child_id):
    child = Child.query.get_or_404(child_id)
    try:
        item = add_wishlist_item(child_id, request.form)
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('parent.child_wishlist', child_id=child_id))
    db.session.commit()
    flash(f'"{item.name}" added to {child.name}\'s wishlist!', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/quick-purchase', methods=['POST'])
@parent_required
def quick_purchase(child_id):
    """Record a purchase directly without going through the wishlist."""
    child = Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    try:
        price = parse_money(request.form.get('price'))
        if price is None or price <= 0:
            raise ValueError
    except ValueError:
        flash('A valid price greater than zero is required.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    if not name:
        flash('Item name is required.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    db.session.add(WishlistItem(
        child_id=child_id,
        name=name,
        description=description or None,
        price=price,
        status='purchased',
        purchased_date=datetime.now(),
    ))
    child.balance -= price
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-price,
        description=f'Purchased: {name}',
    ))
    db.session.commit()
    flash(f'Purchase recorded — ${price:.2f} deducted from {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/purchase', methods=['POST'])
@parent_required
def purchase_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    child = item.child

    if child.balance < item.price:
        flash(
            f'Not enough balance — {child.name} has ${child.balance:.2f} but needs ${item.price:.2f}.',
            'error',
        )
        return redirect(url_for('parent.child_wishlist', child_id=child_id))

    item.status = 'purchased'
    item.purchased_date = datetime.now()
    child.balance -= item.price
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-item.price,
        description=f'Purchased: {item.name}',
    ))
    db.session.commit()
    flash(f'"{item.name}" purchased! ${item.price:.2f} deducted from {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/edit', methods=['POST'])
@parent_required
def edit_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = request.form.get('name', '').strip()
    try:
        price = parse_money(request.form.get('price'))
    except ValueError:
        flash('Please enter a valid price.', 'error')
        return redirect(url_for('parent.child_wishlist', child_id=child_id))
    if name:
        item.name = name
    if price is not None:
        item.price = price
    item.description = request.form.get('description', '').strip() or None
    item.url = request.form.get('url', '').strip() or None
    db.session.commit()
    flash('Wishlist item updated.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/delete', methods=['POST'])
@parent_required
def parent_delete_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'"{name}" removed from wishlist.', 'info')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))
