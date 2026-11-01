"""Wipe all chore/child/book data and fill the database with demo data.

Settings (PIN, email, API keys, timezone) are kept. Payout day is switched on (weekly,
Friday 18:00) so the pending-payout screens have something to show.

Usage:  python seed_demo.py            # uses the same DATABASE_URL default as the app
"""
import random
from datetime import datetime, date, timedelta

from app import create_app, db
from app.models import (
    Child, Chore, AssignedChore, ChoreInstance, BalanceTransaction, WishlistItem,
    Book, BookCheckout, BookAssignment, AppSettings,
)
from app.scheduler import get_period, reschedule_payout_job
from app.utils import get_or_create_book_chore

rng = random.Random(42)
NOW = datetime.now()
TODAY = NOW.date()
PAYOUT_DOW, PAYOUT_HOUR = 4, 18  # Friday 18:00


def at(day: date, hour=19, minute=30) -> datetime:
    return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute))


def payout_after(approved: datetime) -> datetime:
    """The first payout-day moment at or after `approved`."""
    d = approved.date() + timedelta(days=(PAYOUT_DOW - approved.weekday()) % 7)
    p = at(d, PAYOUT_HOUR, 0)
    return p if p >= approved else p + timedelta(days=7)


def wipe():
    for model in (BalanceTransaction, BookAssignment, BookCheckout, Book, WishlistItem,
                  ChoreInstance, AssignedChore, Chore, Child):
        model.query.delete()
    db.session.commit()


def set_setting(key, value):
    row = AppSettings.query.get(key)
    if row:
        row.value = value
    else:
        db.session.add(AppSettings(key=key, value=value))


def txn(child, amount, description, when, inst=None):
    db.session.add(BalanceTransaction(
        child_id=child.id, amount=amount, description=description,
        transaction_date=when, chore_instance_id=inst.id if inst else None,
    ))
    child.balance += amount


def instance(ac, status, assigned, period=None, submitted=None, approved=None, notes=None, awarded=None):
    inst = ChoreInstance(
        assigned_chore_id=ac.id, status=status, period=period, assigned_date=assigned,
        submitted_date=submitted, approved_date=approved, denial_notes=notes, awarded_value=awarded,
    )
    if status in ('approved', 'approved_pending', 'expired'):
        inst.terminal_date = approved or assigned
    db.session.add(inst)
    db.session.flush()
    return inst


def settle(child, ac, inst, label='Scheduled payout'):
    """Approved instance -> paid if its payout day has passed, otherwise waiting for it."""
    paid_at = payout_after(inst.approved_date)
    if paid_at <= NOW:
        inst.status = 'approved'
        inst.terminal_date = paid_at
        note = f' (partial: ${inst.actual_payout:.2f} of ${ac.effective_value:.2f})' if inst.is_partial else ''
        txn(child, inst.actual_payout, f'{label}: {ac.effective_name}{note}', paid_at, inst)
    else:
        inst.status = 'approved_pending'


# ── Library ──────────────────────────────────────────────────────────────────

CHORES = {
    'Make Bed':           ('🛏️', 'Make your bed neatly each morning', 0.50),
    'Feed Pet':           ('🐾', 'Feed and water the pet', 0.50),
    'Set Table':          ('🍴', 'Set the dinner table before meals', 0.50),
    'Take Out Trash':     ('🗑️', 'Take trash cans out to the curb', 1.00),
    'Clean Room':         ('🧹', 'Tidy and organize your room', 1.50),
    'Vacuum Living Room': ('🧹', 'Vacuum the living room floor', 1.50),
    'Wash Dishes':        ('🍽️', 'Wash and dry the dishes', 1.00),
    'Do Homework':        ('📚', 'Complete all homework assignments', 1.00),
    'Put Away Laundry':   ('👕', 'Fold and put away clean laundry', 1.00),
    'Clean Bathroom':     ('🚿', 'Clean and scrub the bathroom', 2.00),
    'Mow Lawn':           ('🌿', 'Mow the front and back lawn', 5.00),
    'Weed Garden':        ('🪴', 'Pull weeds from the garden', 2.00),
    'Wash the Car':       ('🚗', 'Wash and dry the family car', 4.00),
    'Rake Leaves':        ('🌿', 'Rake and bag the leaves in the yard', 5.00),
    'Organize Bookshelf': ('📦', 'Sort and tidy the bookshelf', 3.00),
}


def seed():
    chores = {}
    for name, (icon, desc, value) in CHORES.items():
        chores[name] = Chore(name=name, icon=icon, description=desc, default_value=value)
        db.session.add(chores[name])
    db.session.flush()
    book_chore = get_or_create_book_chore()

    kids = {
        'Emma': Child(name='Emma', avatar_color='#ec4899', balance=0.0, created_at=NOW - timedelta(days=120)),
        'Noah': Child(name='Noah', avatar_color='#3b82f6', balance=0.0, created_at=NOW - timedelta(days=120)),
        'Ava':  Child(name='Ava',  avatar_color='#10b981', balance=0.0, created_at=NOW - timedelta(days=90)),
    }
    db.session.add_all(kids.values())
    db.session.flush()

    def assign(child, chore, cadence=None, day=None, value=None, name=None, desc=None, created_days_ago=35):
        ac = AssignedChore(
            child_id=child.id, chore_id=chores[chore].id, custom_value=value,
            override_name=name, override_description=desc,
            is_recurring=bool(cadence), recurrence_cadence=cadence, recurrence_day=day,
            created_at=NOW - timedelta(days=created_days_ago),
        )
        db.session.add(ac)
        db.session.flush()
        return ac

    # (child, chore, cadence, recurrence_day, custom value, approval rate)
    recurring = [
        ('Emma', 'Make Bed', 'daily', None, None, 0.9),
        ('Emma', 'Feed Pet', 'daily', None, None, 0.8),
        ('Emma', 'Clean Room', 'weekly', 5, None, 0.95),
        ('Emma', 'Wash the Car', 'monthly', 1, None, 1.0),
        ('Noah', 'Make Bed', 'daily', None, None, 0.7),
        ('Noah', 'Take Out Trash', 'weekly', 1, None, 0.9),
        ('Noah', 'Mow Lawn', 'weekly', 5, 6.00, 0.85),
        ('Noah', 'Wash Dishes', 'daily', None, None, 0.75),
        ('Ava',  'Set Table', 'daily', None, None, 0.9),
        ('Ava',  'Put Away Laundry', 'weekly', 3, None, 0.9),
    ]

    # ── Recurring history: last 5 weeks, then this period's instance ─────────
    for kid_name, chore, cadence, rec_day, value, rate in recurring:
        child = kids[kid_name]
        ac = assign(child, chore, cadence, rec_day, value)
        if cadence == 'daily':
            days = [TODAY - timedelta(days=n) for n in range(35, 0, -1)]
        elif cadence == 'weekly':
            days = [TODAY - timedelta(weeks=n) for n in range(5, 0, -1)]
        else:
            last_month_end = TODAY.replace(day=1) - timedelta(days=1)
            days = [(last_month_end.replace(day=1) - timedelta(days=1)).replace(day=1), last_month_end.replace(day=1)]
        seen = set()
        for d in days:
            period = get_period(cadence, d)
            if period in seen:
                continue
            seen.add(period)
            assigned = at(d, 7, 0)
            if rng.random() > rate:
                instance(ac, 'expired', assigned, period)
                continue
            approved = at(d, 19, 30)
            if cadence == 'monthly':
                approved = at(d.replace(day=1), 17, 0)
            awarded = None
            if cadence != 'daily' and rng.random() < 0.2:
                awarded = round(ac.effective_value * 0.5, 2)  # partial credit
            inst = instance(ac, 'approved', assigned, period, submitted=approved - timedelta(hours=2),
                            approved=approved, awarded=awarded)
            settle(child, ac, inst)

        # This period's instance, in a state worth looking at
        period = get_period(cadence, TODAY)
        if period not in seen:
            instance(ac, 'assigned', at(TODAY, 0, 1), period)

    # Make today's daily chores more interesting: some submitted, some already approved this morning
    today_daily = (ChoreInstance.query.join(AssignedChore)
                   .filter(ChoreInstance.period == TODAY.isoformat()).all())
    for inst in today_daily:
        ac = inst.assigned_chore
        child = ac.child
        if ac.chore.name == 'Make Bed' and child.name in ('Emma', 'Ava', 'Noah'):
            inst.status, inst.submitted_date = 'submitted', at(TODAY, 8, 10)
        elif ac.chore.name == 'Feed Pet':
            inst.submitted_date, inst.approved_date = at(TODAY, 8, 0), at(TODAY, 8, 30)
            settle(child, ac, inst)

    # ── One-off chores ───────────────────────────────────────────────────────
    e, n, a = kids['Emma'], kids['Noah'], kids['Ava']

    ac = assign(e, 'Organize Bookshelf', created_days_ago=2)
    instance(ac, 'submitted', at(TODAY - timedelta(days=2), 9, 0), submitted=at(TODAY, 10, 15))

    ac = assign(n, 'Rake Leaves', value=5.00, desc='Front yard only — put the bags by the garage.', created_days_ago=1)
    instance(ac, 'submitted', at(TODAY - timedelta(days=1), 9, 0), submitted=at(TODAY, 11, 40))

    ac = assign(n, 'Clean Bathroom', created_days_ago=3)
    instance(ac, 'assigned', at(TODAY - timedelta(days=3), 9, 0),
             notes='Mirror still has streaks and the floor needs another pass — try again!')

    ac = assign(a, 'Weed Garden', value=2.50, name='Weed the Flower Bed', desc='The bed by the front porch.', created_days_ago=4)
    instance(ac, 'assigned', at(TODAY - timedelta(days=4), 9, 0))

    ac = assign(e, 'Vacuum Living Room', created_days_ago=10)
    instance(ac, 'assigned', at(TODAY - timedelta(days=10), 9, 0))

    ac = assign(a, 'Clean Room', created_days_ago=12)
    instance(ac, 'expired', at(TODAY - timedelta(days=12), 9, 0))

    for child, chore, days_ago, partial in ((e, 'Clean Bathroom', 9, None), (n, 'Weed Garden', 16, 1.00),
                                            (a, 'Wash Dishes', 6, None), (n, 'Rake Leaves', 22, None)):
        ac = assign(child, chore, created_days_ago=days_ago + 2)
        d = TODAY - timedelta(days=days_ago)
        inst = instance(ac, 'approved', at(d, 9, 0), submitted=at(d, 15, 0), approved=at(d, 16, 0), awarded=partial)
        settle(child, ac, inst)

    # ── Library books ────────────────────────────────────────────────────────
    def book(title, author, isbn, notes=None):
        b = Book(title=title, author=author, isbn=isbn, notes=notes)
        db.session.add(b)
        db.session.flush()
        return b

    def checkout(b, out_days_ago, due_in_days, returned_days_ago=None):
        c = BookCheckout(
            book_id=b.id, checked_out_date=NOW - timedelta(days=out_days_ago),
            due_date=TODAY + timedelta(days=due_in_days),
            returned_date=NOW - timedelta(days=returned_days_ago) if returned_days_ago is not None else None,
        )
        db.session.add(c)
        db.session.flush()
        return c

    def book_assignment(c, child, reward, status, started=None, finished=None, inst=None):
        ba = BookAssignment(
            checkout_id=c.id, child_id=child.id, reward_value=reward, status=status,
            assigned_date=c.checked_out_date, started_date=started, finished_date=finished,
            chore_instance_id=inst.id if inst else None,
        )
        db.session.add(ba)
        return ba

    # Currently reading
    c = checkout(book("Charlotte's Web", 'E. B. White', '9780064400558'), 10, 18)
    book_assignment(c, e, 2.00, 'reading', started=NOW - timedelta(days=8))
    # Overdue and not started
    c = checkout(book('Matilda', 'Roald Dahl', '9780142410370'), 40, -12)
    book_assignment(c, n, 3.00, 'unread')
    # Due soon, shared by two kids
    c = checkout(book('The Wild Robot', 'Peter Brown', '9780316381994'), 25, 3)
    book_assignment(c, a, 2.00, 'reading', started=NOW - timedelta(days=5))
    book_assignment(c, n, 2.00, 'unread')
    # Finished and rewarded
    b = book('Wonder', 'R. J. Palacio', '9780375869020')
    c = checkout(b, 45, -17, returned_days_ago=14)
    ac = AssignedChore(child_id=e.id, chore_id=book_chore.id, custom_value=2.50, override_name=b.title)
    db.session.add(ac)
    db.session.flush()
    fin = NOW - timedelta(days=16)
    inst = instance(ac, 'approved', fin, fin.date().isoformat(), submitted=fin, approved=fin)
    txn(e, 2.50, f'Book finished: {b.title}', fin, inst)
    book_assignment(c, e, 2.50, 'approved', started=NOW - timedelta(days=40), finished=fin, inst=inst)
    # Returned without finishing
    c = checkout(book('Harry Potter and the Goblet of Fire', 'J. K. Rowling', '9780439139601'), 50, -22, returned_days_ago=20)
    book_assignment(c, n, 4.00, 'unfinished', started=NOW - timedelta(days=45))
    # Returned, no assignment
    c = checkout(book('Dog Man', 'Dav Pilkey', '9780545581606', 'Favourite of the whole house.'), 60, -32, returned_days_ago=30)

    # ── Wishlists & purchases ────────────────────────────────────────────────
    def wish(child, name, price, url=None, desc=None, order=0, purchased_days_ago=None):
        w = WishlistItem(child_id=child.id, name=name, price=price, url=url, description=desc,
                         sort_order=order, created_at=NOW - timedelta(days=40))
        if purchased_days_ago is not None:
            when = NOW - timedelta(days=purchased_days_ago)
            w.status, w.purchased_date = 'purchased', when
            txn(child, -price, f'Purchased: {name}', when)
        db.session.add(w)

    wish(e, 'LEGO Friends Heartlake Café', 24.99, 'https://www.lego.com', 'The one with the little bakery.', 0)
    wish(e, 'Sketchbook & marker set', 12.50, None, None, 1)
    wish(e, 'Friendship bracelet kit', 8.00, None, None, 2, purchased_days_ago=18)
    wish(n, 'Soccer ball', 15.00, None, 'Size 4, the red one.', 0)
    wish(n, 'Minecraft gift card', 20.00, 'https://www.minecraft.net', None, 1)
    wish(n, 'Pokémon card pack', 6.00, None, None, 2, purchased_days_ago=9)
    wish(a, 'Stuffed owl', 14.00, None, None, 0)
    wish(a, 'Glitter gel pens', 5.50, None, None, 1, purchased_days_ago=12)

    # A manual bonus so the ledger shows a non-chore deposit
    txn(a, 10.00, 'Birthday money from Grandma', NOW - timedelta(days=21))

    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        wipe()
        set_setting('payout_day_enabled', 'on')
        set_setting('payout_cadence', 'weekly')
        set_setting('payout_day_of_week', str(PAYOUT_DOW))
        set_setting('payout_time', f'{PAYOUT_HOUR:02d}:00')
        db.session.commit()
        seed()
        reschedule_payout_job()

        for c in Child.query.order_by(Child.name):
            pending = (ChoreInstance.query.join(AssignedChore)
                       .filter(AssignedChore.child_id == c.id, ChoreInstance.status == 'approved_pending').count())
            submitted = c.pending_submission_count
            print(f'{c.name:5} balance ${c.balance:6.2f}  pending payout: {pending:2}  awaiting review: {submitted}')
        print(f'chore instances: {ChoreInstance.query.count()}, transactions: {BalanceTransaction.query.count()}, '
              f'books: {Book.query.count()}, wishlist: {WishlistItem.query.count()}')


if __name__ == '__main__':
    main()
