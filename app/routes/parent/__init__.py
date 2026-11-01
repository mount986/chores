"""Parent-facing blueprint, split into one module per feature area."""
from .common import parent_bp
from . import assignments, auth, books, children, chore_action, covers, instances, library, payouts, settings, wishlist  # noqa: F401  (registers routes on parent_bp)

__all__ = ['parent_bp']
