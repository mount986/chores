from flask import render_template, redirect, url_for, flash
from ...models import Child, AssignedChore, ChoreInstance
from ... import db
from ...utils import is_payout_day_enabled, settle_pending_payouts
from .common import parent_bp, parent_required


# ── Payout summary ───────────────────────────────────────────────────────────

@parent_bp.route('/payouts')
@parent_required
def payouts():
    from ...utils import get_payout_period_info

    if not is_payout_day_enabled():
        flash('Payout day is turned off — chores are paid as soon as they are approved.', 'info')
        return redirect(url_for('parent.settings'))

    period = get_payout_period_info()
    cadence = period['cadence']
    children = Child.query.order_by(Child.name).all()

    child_data = []
    grand_total = 0.0

    for child in children:
        if cadence == 'instant':
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved',
                    ChoreInstance.approved_date >= period['period_start'],
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )
        else:
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved_pending',
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )

        subtotal = sum(inst.actual_payout for inst in instances)
        grand_total += subtotal
        child_data.append({'child': child, 'chores': instances, 'subtotal': subtotal})

    return render_template(
        'parent/payouts.html',
        child_data=child_data,
        grand_total=grand_total,
        period=period,
    )


@parent_bp.route('/payouts/process-now', methods=['POST'])
@parent_required
def process_payout_now():
    """Immediately pay out all approved_pending chore instances."""
    total_by_child = settle_pending_payouts('Manual payout')
    if not total_by_child:
        flash('No pending payouts to process.', 'info')
        return redirect(url_for('parent.payouts'))

    db.session.commit()
    from ...utils import backup_database
    backup_database()
    summary = ', '.join(f'{name} +${amt:.2f}' for name, amt in total_by_child.items())
    flash(f'Payout processed! {summary}', 'success')
    return redirect(url_for('parent.payouts'))
