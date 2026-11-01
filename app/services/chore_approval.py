"""Approving, reversing and resetting chore instances. Callers commit."""
from datetime import datetime

from .. import db
from ..models import BalanceTransaction, BookAssignment


def finalize_approval(inst, immediate):
    """Mark an instance approved: paid now (`immediate`) or left as pending payout.

    Returns (amount, partial_note) for building the confirmation message.
    """
    amount = inst.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''
    inst.terminal_date = datetime.now()
    if immediate:
        inst.status = 'approved'
        inst.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Chore completed: {inst.display_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
    else:
        inst.status = 'approved_pending'
    return amount, partial_note


def reverse_payment(inst):
    """Take back a paid chore's payout from the child's balance. Returns the amount reversed."""
    amount = inst.actual_payout
    inst.child.balance -= amount
    db.session.add(BalanceTransaction(
        child_id=inst.child_id,
        amount=-amount,
        description=f'Chore reversed: {inst.display_name}',
        chore_instance_id=inst.id,
    ))
    return amount


def mark_book_unfinished(inst):
    """If the instance came from a book assignment, send that assignment back to 'unfinished'."""
    book_assignment = BookAssignment.query.filter_by(chore_instance_id=inst.id).first()
    if book_assignment:
        book_assignment.status = 'unfinished'
