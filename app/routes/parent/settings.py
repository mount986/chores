import zoneinfo

import bcrypt
from flask import render_template, redirect, url_for, request, flash
from ... import db
from ...models import Child, AppSettings
from ...notifications import send_test_email
from ...scheduler import reschedule_payout_job
from ...utils import backup_database, is_payout_day_enabled, settle_pending_payouts
from .common import parent_bp, parent_required

MIN_PIN_LENGTH = 4

# Form fields saved verbatim, but only when the submitted value passes the check.
# A blank field leaves the stored value alone (so secrets aren't wiped by an empty box).
_VALIDATED_FIELDS = {
    'payout_cadence':         lambda v: v in ('daily', 'weekly', 'monthly'),
    'payout_time':            bool,
    'payout_day_of_week':     lambda v: v.isdigit() and 0 <= int(v) <= 6,
    'payout_day_of_month':    lambda v: v.isdigit() and 1 <= int(v) <= 28,
    'session_timeout':        lambda v: v.isdigit() and int(v) >= 1,
    'book_default_loan_days': lambda v: v.isdigit() and int(v) >= 1,
    'notify_email_to':        bool,
    'notify_smtp_user':       bool,
    'notify_smtp_password':   bool,
    'google_books_api_key':   bool,
}
_TOGGLE_FIELDS = ('payout_day_enabled', 'child_suggestions_enabled', 'notify_email_enabled')


@parent_bp.route('/settings')
@parent_required
def settings():
    children = Child.query.order_by(Child.name).all()
    get = AppSettings.get

    # 'instant' is a legacy stored value; the form only offers real schedules
    stored_cadence = get('payout_cadence', 'weekly')

    return render_template(
        'parent/settings.html',
        children=children,
        payout_day_enabled=get('payout_day_enabled', 'off'),
        payout_cadence='weekly' if stored_cadence == 'instant' else stored_cadence,
        payout_time=get('payout_time', '18:00'),
        payout_day_of_week=get('payout_day_of_week', '0'),
        payout_day_of_month=get('payout_day_of_month', '1'),
        session_timeout=get('session_timeout', '5'),
        child_suggestions_enabled=get('child_suggestions_enabled', 'off'),
        notify_email_enabled=get('notify_email_enabled', 'off'),
        notify_email_to=get('notify_email_to'),
        notify_smtp_user=get('notify_smtp_user'),
        notify_smtp_password_set=bool(get('notify_smtp_password')),
        app_base_url=get('app_base_url'),
        book_default_loan_days=get('book_default_loan_days', '28'),
        google_books_api_key_set=bool(get('google_books_api_key')),
        app_timezone=get('app_timezone', 'UTC'),
    )


def _settings_error(message):
    db.session.rollback()
    flash(message, 'error')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/update', methods=['POST'])
@parent_required
def update_settings():
    form = request.form
    was_enabled = is_payout_day_enabled()

    for key in _TOGGLE_FIELDS:
        AppSettings.set(key, 'on' if form.get(key) else 'off')
    enabled = bool(form.get('payout_day_enabled'))

    for key, is_valid in _VALIDATED_FIELDS.items():
        value = form.get(key, '').strip()
        if value and is_valid(value):
            AppSettings.set(key, value)

    AppSettings.set('app_base_url', form.get('app_base_url', '').strip().rstrip('/'))

    new_pin = form.get('new_pin', '').strip()
    if new_pin:
        if len(new_pin) < MIN_PIN_LENGTH:
            return _settings_error(f'PIN must be at least {MIN_PIN_LENGTH} characters.')
        if new_pin != form.get('confirm_pin', '').strip():
            return _settings_error('PINs do not match.')
        AppSettings.set('parent_pin', bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode())

    timezone = form.get('app_timezone', '').strip()
    if timezone:
        try:
            zoneinfo.ZoneInfo(timezone)
        except (KeyError, ValueError, zoneinfo.ZoneInfoNotFoundError):
            return _settings_error('Invalid timezone selected.')
        AppSettings.set('app_timezone', timezone)

    # Turning payout day off: nothing will ever sweep up chores still waiting, so pay them now.
    settled = settle_pending_payouts('Payout day disabled') if was_enabled and not enabled else {}

    db.session.commit()
    reschedule_payout_job()

    if settled:
        backup_database()
        summary = ', '.join(f'{name} +${amt:.2f}' for name, amt in settled.items())
        flash(f'Settings saved! Pending chores paid out: {summary}', 'success')
    else:
        flash('Settings saved!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/test-email', methods=['POST'])
@parent_required
def test_email_notification():
    """Send a test email using the currently saved SMTP settings."""
    to_addr, smtp_user, smtp_password = (
        AppSettings.get(key).strip()
        for key in ('notify_email_to', 'notify_smtp_user', 'notify_smtp_password')
    )

    if not all([to_addr, smtp_user, smtp_password]):
        flash('Please save your email settings before sending a test.', 'error')
        return redirect(url_for('parent.settings'))

    error = send_test_email(to_addr, smtp_user, smtp_password)
    if error:
        flash(f'Test failed: {error}', 'error')
    else:
        flash(f'Test email sent to {to_addr} — check your inbox!', 'success')
    return redirect(url_for('parent.settings'))
