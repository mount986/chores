from datetime import datetime
from flask import render_template, current_app
from ...models import ChoreInstance
from ... import db
from ...notifications import verify_action_token
from ...services.chore_approval import deny_instance, finalize_approval
from ...utils import get_payout_cadence
from .common import parent_bp


def _render(state, child_name, chore_name, **context):
    return render_template('parent/chore_action.html', state=state,
                           child_name=child_name, chore_name=chore_name, **context)


def _error(message):
    return render_template('parent/chore_action.html', state='error', message=message)


@parent_bp.route('/chore-action/<token>')
def chore_action(token):
    """One-click approve/deny from an email notification link. No login required —
    the signed token is the credential."""
    data = verify_action_token(token, current_app.secret_key)
    if not data:
        return _error('This link is invalid or has expired (links work for 7 days).')

    inst = ChoreInstance.query.get(data.get('id'))
    if not inst:
        return _error('Chore not found — it may have been deleted.')

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
        return _render('already_done', child_name, chore_name, friendly_status=friendly)

    # ── Approve ───────────────────────────────────────────────────────────────
    if action == 'approve':
        inst.approved_date = inst.submitted_date or datetime.now()
        inst.awarded_value = None  # full value

        cadence = get_payout_cadence()
        amount, _ = finalize_approval(inst, cadence == 'instant')
        if cadence == 'instant':
            payout_note = f'${amount:.2f} added to their balance.'
        else:
            payout_note = f'Will be paid out on the next {cadence} payout.'

        db.session.commit()
        return _render('approved', child_name, chore_name, value=amount, payout_note=payout_note)

    # ── Deny ──────────────────────────────────────────────────────────────────
    if action == 'deny':
        outcome = deny_instance(inst)
        db.session.commit()
        return _render('denied', child_name, chore_name, note={
            'unit_returned': 'Submission sent back for another try.',
            'expired': 'The period already passed — marked as expired.',
            'returned': 'Chore sent back for another try.',
        }[outcome])

    return _error('Unknown action in token.')
