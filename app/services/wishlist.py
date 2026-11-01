"""Wishlist helpers shared by the parent and child blueprints."""
from .. import db
from ..models import WishlistItem
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
