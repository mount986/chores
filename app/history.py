"""History page data: turns chores, payouts and balance transactions into one flat list of events.

Only things that actually happened are included: approved chores (dated by when the work was
done), delayed payouts, deductions and manual adjustments. Pending, denied and expired chores
never appear.
"""
import calendar as cal_module
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import joinedload

from . import db
from .models import AssignedChore, BalanceTransaction, ChoreInstance, Payout

APPROVED = ('approved', 'approved_pending')

# (kind, label, tailwind colour) in display order
KINDS = [
    ('chore', 'Chores', 'emerald'),
    ('payout', 'Payouts', 'indigo'),
    ('deduction', 'Deductions', 'rose'),
    ('adjustment', 'Adjustments', 'amber'),
]
KIND_KEYS = [k for k, _, _ in KINDS]

# Left behind when a per-unit submission is removed (its transactions lose their chore link).
_ORPHANED_CHORE_PREFIXES = ('Chore completed:', 'Chore reversed:')


@dataclass
class HistoryEvent:
    kind: str
    when: datetime
    child: object
    title: str
    amount: float
    note: str = ''
    items: list = field(default_factory=list)  # payouts: [{'title', 'amount', 'done'}]

    @property
    def day(self):
        return self.when.date()


def parse_month(raw, today=None):
    """(year, month) from 'YYYY-MM'; falls back to the current month."""
    today = today or date.today()
    try:
        year, month = (int(p) for p in raw.split('-'))
        date(year, month, 1)
        return year, month
    except (ValueError, AttributeError):
        return today.year, today.month


def month_bounds(year, month):
    """Half-open [start, end) datetimes covering a month."""
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def shift_month(year, month, delta):
    """'YYYY-MM' for the month `delta` months away."""
    index = year * 12 + (month - 1) + delta
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def parse_kinds(raw):
    """Kinds selected by a comma-separated filter value; blank or unrecognised means all."""
    picked = [k for k in (raw or '').split(',') if k in KIND_KEYS]
    return picked or list(KIND_KEYS)


def build_events(start, end, child_id=None, kinds=None):
    """Events with `start <= when < end`, optionally for one child and a subset of kinds."""
    kinds = set(kinds or KIND_KEYS)
    events = []
    if 'chore' in kinds:
        events += _chore_events(start, end, child_id)
    if 'payout' in kinds:
        events += _payout_events(start, end, child_id)
    if kinds & {'deduction', 'adjustment'}:
        events += [e for e in _transaction_events(start, end, child_id) if e.kind in kinds]
    return events


def _chore_events(start, end, child_id):
    done_on = db.func.coalesce(
        ChoreInstance.completed_date, ChoreInstance.submitted_date, ChoreInstance.approved_date,
    )
    query = (
        ChoreInstance.query
        .join(AssignedChore)
        .options(joinedload(ChoreInstance.assigned_chore).joinedload(AssignedChore.chore),
                 joinedload(ChoreInstance.assigned_chore).joinedload(AssignedChore.child))
        .filter(ChoreInstance.status.in_(APPROVED), done_on >= start, done_on < end)
    )
    if child_id:
        query = query.filter(AssignedChore.child_id == child_id)
    return [
        HistoryEvent(
            kind='chore', when=inst.completed_on, child=inst.child, title=inst.display_name,
            amount=inst.actual_payout,
            note='partial credit' if inst.is_partial else '',
        )
        for inst in query
    ]


def _payout_events(start, end, child_id):
    query = (
        Payout.query
        .options(joinedload(Payout.child), joinedload(Payout.transactions))
        .filter(Payout.paid_date >= start, Payout.paid_date < end)
    )
    if child_id:
        query = query.filter(Payout.child_id == child_id)
    payouts = query.all()

    instance_ids = {t.chore_instance_id for p in payouts for t in p.transactions if t.chore_instance_id}
    instances = {
        i.id: i for i in ChoreInstance.query.filter(ChoreInstance.id.in_(instance_ids))
    } if instance_ids else {}

    events = []
    for payout in payouts:
        # A chore that was later marked incomplete was reversed, so it no longer counts.
        items = []
        for txn in sorted(payout.transactions, key=lambda t: t.id):
            inst = instances.get(txn.chore_instance_id)
            if inst is None or inst.status not in APPROVED:
                continue
            items.append({'title': inst.display_name, 'amount': txn.amount, 'done': inst.completed_on})
        if not items:
            continue
        events.append(HistoryEvent(
            kind='payout', when=payout.paid_date, child=payout.child, title='Payout',
            amount=round(sum(i['amount'] for i in items), 2), items=items,
        ))
    return events


def _transaction_events(start, end, child_id):
    """Money that isn't tied to a chore or payout: purchases and penalties out, bonuses and fixes in."""
    query = (
        BalanceTransaction.query
        .options(joinedload(BalanceTransaction.child))
        .filter(
            BalanceTransaction.chore_instance_id.is_(None),
            BalanceTransaction.payout_id.is_(None),
            BalanceTransaction.transaction_date >= start,
            BalanceTransaction.transaction_date < end,
        )
    )
    if child_id:
        query = query.filter(BalanceTransaction.child_id == child_id)
    return [
        HistoryEvent(
            kind='deduction' if t.amount < 0 else 'adjustment', when=t.transaction_date,
            child=t.child, title=t.description or ('Deduction' if t.amount < 0 else 'Adjustment'),
            amount=t.amount,
        )
        for t in query
        if not (t.description or '').startswith(_ORPHANED_CHORE_PREFIXES)
    ]


def month_grid(year, month, events, selected_day, today=None):
    """Weeks of day cells (None for padding), each with per-kind counts and the kids involved."""
    today = today or date.today()
    by_day = {}
    for e in events:
        by_day.setdefault(e.day, []).append(e)

    weeks = []
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day_num in week:
            if not day_num:
                row.append(None)
                continue
            d = date(year, month, day_num)
            day_events = by_day.get(d, [])
            row.append({
                'date': d,
                'day': day_num,
                'counts': {k: n for k, _, _ in KINDS if (n := sum(e.kind == k for e in day_events))},
                'kids': sorted({e.child.avatar_color for e in day_events}),
                'is_today': d == today,
                'is_selected': d == selected_day,
            })
        weeks.append(row)
    return weeks


def group_day(events, day):
    """The day's events grouped by child (name order), each group in kind order then time."""
    order = {k: i for i, k in enumerate(KIND_KEYS)}
    groups = {}
    for e in sorted((e for e in events if e.day == day), key=lambda e: (order[e.kind], e.when)):
        groups.setdefault(e.child.id, {'child': e.child, 'events': []})['events'].append(e)
    return sorted(groups.values(), key=lambda g: g['child'].name.lower())


def month_totals(events):
    """Dollar totals for the summary tiles."""
    def total(kind):
        return round(sum(e.amount for e in events if e.kind == kind), 2)
    return {
        'chores': total('chore'),
        'chore_count': sum(e.kind == 'chore' for e in events),
        'paid': total('payout'),
        'spent': abs(total('deduction')),
        'adjusted': total('adjustment'),
    }

