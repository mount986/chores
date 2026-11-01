from flask import render_template, session, redirect, url_for, request, flash
import bcrypt
from ...models import AppSettings
from .common import parent_bp


# ── Auth ─────────────────────────────────────────────────────────────────────

@parent_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pin = request.form.get('pin', '').encode()
        pin_hash = AppSettings.get('parent_pin')
        if pin_hash and bcrypt.checkpw(pin, pin_hash.encode()):
            session['parent_authenticated'] = True
            session.permanent = True
            return redirect(url_for('parent.dashboard'))
        flash('Incorrect PIN. Please try again.', 'error')
    return render_template('parent/login.html')


@parent_bp.route('/logout')
def logout():
    session.pop('parent_authenticated', None)
    return redirect(url_for('main.home'))
