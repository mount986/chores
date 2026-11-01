"""Wishlist helpers shared by the parent and child blueprints."""
from datetime import datetime

from .. import db
from ..models import BalanceTransaction, WishlistItem
from ..utils import parse_money


def add_wishlist_item(child_id, form):
    """Create an active wishlist item at the end of the child's list from submitted form fields.

    Raises ValueError with a user-facing message when the name or price is missing or invalid.
    The caller commits.
    """
    name = form.get('name', '').strip()
    try:
        price = parse_money(form.get('price'))
    except ValueError:
        price = None
    if not name or price is None:
        raise ValueError('Item name and a valid price are required.')
    if price < 0:
        raise ValueError('Price cannot be negative.')

    max_order = db.session.query(db.func.max(WishlistItem.sort_order)).filter_by(
        child_id=child_id, status='active'
    ).scalar() or 0
    item = WishlistItem(
        child_id=child_id,
        name=name,
        description=form.get('description', '').strip() or None,
        price=price,
        url=form.get('url', '').strip() or None,
        sort_order=max_order + 1,
    )
    db.session.add(item)
    return item


def active_wishlist(child_id):
    return (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )


def purchased_wishlist(child_id):
    return (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .order_by(WishlistItem.purchased_date.desc())
        .all()
    )


def record_purchase(child, price, item=None, name=None, description=None):
    """Deduct `price` from the child's balance and log it.

    Marks `item` purchased, or creates an already-purchased item from `name`/`description`
    when no wishlist item is given. The caller commits.
    """
    if item is None:
        item = WishlistItem(child_id=child.id, name=name, description=description or None, price=price)
        db.session.add(item)
    item.status = 'purchased'
    item.purchased_date = datetime.now()
    child.balance -= price
    db.session.add(BalanceTransaction(
        child_id=child.id,
        amount=-price,
        description=f'Purchased: {item.name}',
    ))
    return item
