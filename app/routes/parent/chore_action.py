from datetime import datetime, date
from flask import render_template, current_app
from ...models import ChoreInstance, BalanceTransaction
from ... import db
from ...utils import get_payout_cadence
from .common import parent_bp


@parent_bp.route('/chore-action/<token>')
def chore_action(token):
    """One-click approve/deny from an email notification link. No login required —
    the signed token is the credential."""
    from ...notifications import verify_action_token
    from ...scheduler import get_period

    data = verify_action_token(token, current_app.secret_key)
    if not data:
        return render_template('parent/chore_action.html',
                               state='error',
                               message='This link is invalid or has expired (links work for 7 days).')

    inst = ChoreInstance.query.get(data.get('id'))
    if not inst:
        return render_template('parent/chore_action.html',
                               state='error',
                               message='Chore not found — it may have been deleted.')

    action = data.get('action')
    child_name = inst.child.name
    chore_name = inst.display_name

    # ── Already actioned ──────────────────────────────────────────────────────
    if inst.status != 'submitted':
        friendly = {
            'approved': 'already approved ✓',
            'approved_pending': 'already approved (pending payout) ✓',
            'assigned': 'already sent back for another try',
            'expired': 'already marked expired',
        }.get(inst.status, inst.status)
        return render_template('parent/chore_action.html',
                               state='already_done',
                               child_name=child_name,
                               chore_name=chore_name,
                               friendly_status=friendly)

    # ── Approve ───────────────────────────────────────────────────────────────
    if action == 'approve':
        inst.approved_date = inst.submitted_date or datetime.now()
        inst.awarded_value = None  # full value
        amount = inst.actual_payout

        cadence = get_payout_cadence()
        if cadence == 'instant':
            inst.status = 'approved'
            inst.terminal_date = datetime.now()
            inst.child.balance += amount
            db.session.add(BalanceTransaction(
                child_id=inst.child_id,
                amount=amount,
                description=f'Chore completed: {chore_name}',
                chore_instance_id=inst.id,
            ))
            payout_note = f'${amount:.2f} added to their balance.'
        else:
            inst.status = 'approved_pending'
            inst.terminal_date = datetime.now()
            payout_note = f'Will be paid out on the next {cadence} payout.'

        db.session.commit()
        return render_template('parent/chore_action.html',
                               state='approved',
                               child_name=child_name,
                               chore_name=chore_name,
                               value=amount,
                               payout_note=payout_note)

    # ── Deny ──────────────────────────────────────────────────────────────────
    if action == 'deny':
        if inst.discard_unit_submission():
            db.session.commit()
            return render_template('parent/chore_action.html',
                                   state='denied',
                                   child_name=child_name,
                                   chore_name=chore_name,
                                   note='Submission sent back for another try.')
        period_has_passed = (
            inst.is_recurring
            and inst.recurrence_cadence
            and inst.period
            and get_period(inst.recurrence_cadence, date.today()) != inst.period
        )
        if period_has_passed:
            inst.status = 'expired'
            inst.terminal_date = datetime.now()
            db.session.commit()
            return render_template('parent/chore_action.html',
                                   state='denied',
                                   child_name=child_name,
                                   chore_name=chore_name,
                                   note='The period already passed — marked as expired.')
        else:
            inst.status = 'assigned'
            inst.submitted_date = None
            inst.completed_date = None
            db.session.commit()
            return render_template('parent/chore_action.html',
                                   state='denied',
                                   child_name=child_name,
                                   chore_name=chore_name,
                                   note='Chore sent back for another try.')

    return render_template('parent/chore_action.html',
                           state='error',
                           message='Unknown action in token.')
