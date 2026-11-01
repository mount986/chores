"""Book library helpers shared by the parent and child blueprints. Callers commit."""
from datetime import date, datetime, timedelta

from .. import db
from ..models import AppSettings, AssignedChore, Book, BookCheckout, ChoreInstance
from ..utils import get_or_create_book_chore

# A chore instance in one of these states has been settled and must not be touched again.
SETTLED_STATUSES = ('approved', 'approved_pending', 'expired')


def get_loan_days():
    """Default loan length in days from settings (28 when unset or invalid)."""
    setting = AppSettings.query.get('book_default_loan_days')
    try:
        return int(setting.value) if setting else 28
    except (ValueError, TypeError):
        return 28


def default_due_date():
    return date.today() + timedelta(days=get_loan_days())


def active_checkouts():
    """Checkouts that have not been returned yet."""
    return (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .join(Book)
        .all()
    )


def checkout_sort_key(checkout):
    """Reading first, then unread, then everything else; alphabetical within a group."""
    statuses = {a.status for a in checkout.assignments}
    if 'reading' in statuses:
        group = 0
    elif 'unread' in statuses:
        group = 1
    else:
        group = 2
    return (group, checkout.book.title.lower())


def expire_assignment_instance(assignment, now=None):
    """Expire the assignment's chore instance with no payout, unless it is already settled."""
    if not assignment.chore_instance_id:
        return
    inst = ChoreInstance.query.get(assignment.chore_instance_id)
    if inst and inst.status not in SETTLED_STATUSES:
        inst.status = 'expired'
        inst.awarded_value = 0
        inst.terminal_date = now or datetime.now()


def set_assignment_reward(assignment, reward):
    """Set the reward on a book assignment and on its chore, unless that chore is settled."""
    assignment.reward_value = reward
    inst = assignment.chore_instance if assignment.chore_instance_id else None
    if inst and inst.status not in SETTLED_STATUSES and inst.assigned_chore:
        inst.assigned_chore.custom_value = reward


def create_submitted_instance(assignment, now=None):
    """Create a one-off chore plus a 'submitted' instance for a finished book and link it."""
    now = now or datetime.now()
    book_chore = get_or_create_book_chore()
    ac = AssignedChore(
        child_id=assignment.child_id,
        chore_id=book_chore.id,
        custom_value=assignment.reward_value,
        override_name=assignment.checkout.book.title,
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
    return inst
