from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, flash
from ..models import Child, AssignedChore
from .. import db

child_bp = Blueprint('child', __name__)


@child_bp.route('/')
def select():
    children = Child.query.order_by(Child.name).all()
    return render_template('child/select.html', children=children)


@child_bp.route('/<int:child_id>')
def dashboard(child_id):
    child = Child.query.get_or_404(child_id)
    session['child_id'] = child_id

    assigned = (
        AssignedChore.query
        .filter_by(child_id=child_id, status='assigned')
        .order_by(AssignedChore.assigned_date.desc())
        .all()
    )
    submitted = (
        AssignedChore.query
        .filter_by(child_id=child_id, status='submitted')
        .order_by(AssignedChore.submitted_date.desc())
        .all()
    )
    approved = (
        AssignedChore.query
        .filter_by(child_id=child_id, status='approved')
        .order_by(AssignedChore.approved_date.desc())
        .limit(10)
        .all()
    )
    return render_template(
        'child/dashboard.html',
        child=child,
        assigned=assigned,
        submitted=submitted,
        approved=approved,
    )


@child_bp.route('/<int:child_id>/submit/<int:ac_id>', methods=['POST'])
def submit_chore(child_id, ac_id):
    ac = AssignedChore.query.get_or_404(ac_id)
    if ac.child_id != child_id or ac.status != 'assigned':
        flash('Cannot submit this chore right now.', 'error')
        return redirect(url_for('child.dashboard', child_id=child_id))

    ac.status = 'submitted'
    ac.submitted_date = datetime.utcnow()
    db.session.commit()
    flash('Nice work! Your chore has been sent to a parent for review. 🌟', 'success')
    return redirect(url_for('child.dashboard', child_id=child_id))
