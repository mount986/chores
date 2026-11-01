"""Data builders for the parent child-detail page: chore rows, sorting and the ledger."""
from collections import defaultdict

from ..models import AssignedChore, BalanceTransaction, ChoreInstance, WishlistItem
from ..scheduler import get_period
from ..utils import next_recurrence_date, to_date

LEDGER_PAGE_SIZE = 10
VALID_SORTS = ('status', 'name', 'value', 'cadence')

_ACTIVE_STATUSES = {'assigned', 'submitted'}
_TERMINAL_STATUSES = {'approved', 'approved_pending', 'expired'}


def _recent_instances(config, limit=5):
    return (
        ChoreInstance.query
        .filter_by(assigned_chore_id=config.id)
        .order_by(ChoreInstance.assigned_date.desc())
        .limit(limit)
        .all()
    )


def _is_upcoming(instances, cadence, today):
    """A recurring chore is 'upcoming' when no instance exists for the current period yet."""
    current_period = get_period(cadence, today)
    return bool(current_period and not any(i.period == current_period for i in instances))


def _row_priority(instances, is_upcoming):
    """Priority order: submitted > assigned > upcoming > expired > pending payout > approved."""
    statuses = {inst.status for inst in instances}
    if 'submitted' in statuses:
        return 0
    if 'assigned' in statuses:
        return 1
    if is_upcoming:
        return 2
    if 'expired' in statuses:
        return 3
    if 'approved_pending' in statuses:
        return 4
    return 5


def _became_terminal_today(inst, today):
    # terminal_date is set at transition time; fall back to approved_date for
    # pre-migration rows that lack it.
    when = inst.terminal_date or (
        inst.approved_date if inst.status in ('approved', 'approved_pending') else None
    )
    return when is not None and to_date(when) == today


def _make_row(config, instances, *, is_recurring, is_upcoming, is_dimmed, today):
    cadence = config.recurrence_cadence if is_recurring else None
    rec_day = config.recurrence_day if is_recurring else None
    return {
        'config':        config,
        'instances':     instances,
        'is_recurring':  is_recurring,
        'is_upcoming':   is_upcoming,
        'is_dimmed':     is_dimmed,
        'cadence':       cadence,
        'rec_day':       rec_day,
        'next_date':     next_recurrence_date(cadence, rec_day, today) if is_recurring else None,
        'chore_id':      config.chore_id,
        'pending_count': sum(1 for i in instances if i.status in _ACTIVE_STATUSES),
        'priority':      _row_priority(instances, is_upcoming),
    }


def build_chore_rows(child_id, today):
    """One row per active chore config for the child, ready for the template.

    Recurring chores always show. One-time chores show while active, and stay
    visible (dimmed) for the day they reached a terminal state.
    """
    rows = []
    configs = AssignedChore.query.filter_by(child_id=child_id, is_active=True).all()
    configs.sort(key=lambda c: not c.is_recurring)  # recurring first, as before (stable)

    for config in configs:
        instances = _recent_instances(config)
        if config.is_recurring:
            if not config.recurrence_cadence:
                continue
            upcoming = _is_upcoming(instances, config.recurrence_cadence, today)
            rows.append(_make_row(config, instances, is_recurring=True,
                                  is_upcoming=upcoming, is_dimmed=False, today=today))
            continue

        has_active = any(i.status in _ACTIVE_STATUSES for i in instances)
        if not has_active and not any(
            i.status in _TERMINAL_STATUSES and _became_terminal_today(i, today)
            for i in instances
        ):
            continue
        rows.append(_make_row(config, instances, is_recurring=False,
                              is_upcoming=False, is_dimmed=not has_active, today=today))
    return rows


def sort_chore_rows(rows, sort):
    """Sort rows in place by one of VALID_SORTS (unknown values fall back to 'status')."""
    def name(r):
        return r['config'].effective_name.lower()

    if sort == 'name':
        rows.sort(key=name)
    elif sort == 'value':
        rows.sort(key=lambda r: -r['config'].effective_value)
    elif sort == 'cadence':
        rows.sort(key=lambda r: (r['cadence'] or 'zzz', name(r)))
    else:
        rows.sort(key=lambda r: (r['priority'], name(r)))
    return rows


def build_ledger(child_id, today, page):
    """Payout events (grouped by day) and purchases, newest first.

    Returns (events_for_page, clamped_page, total_pages).
    """
    credits = (
        BalanceTransaction.query
        .filter(BalanceTransaction.child_id == child_id, BalanceTransaction.amount > 0)
        .order_by(BalanceTransaction.transaction_date)
        .all()
    )
    payouts_by_day = defaultdict(list)
    for txn in credits:
        payouts_by_day[to_date(txn.transaction_date)].append(txn)

    events = [
        {
            'type':         'payout',
            'date':         day,
            'amount':       sum(t.amount for t in txns),
            'transactions': txns,  # ascending order from the query
        }
        for day, txns in payouts_by_day.items()
    ]

    purchased = WishlistItem.query.filter_by(child_id=child_id, status='purchased').all()
    events.extend(
        {
            'type':   'purchase',
            'date':   to_date(item.purchased_date) if item.purchased_date else today,
            'amount': item.price,
            'item':   item,
        }
        for item in purchased
    )
    events.sort(key=lambda e: e['date'], reverse=True)

    total_pages = max(1, -(-len(events) // LEDGER_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * LEDGER_PAGE_SIZE
    return events[start:start + LEDGER_PAGE_SIZE], page, total_pages
