from flask import render_template, redirect, url_for, request, flash
import bcrypt
from ...models import Child, AppSettings
from ... import db
from ...utils import is_payout_day_enabled, settle_pending_payouts
from .common import parent_bp, parent_required


# ── Settings ─────────────────────────────────────────────────────────────────

@parent_bp.route('/settings')
@parent_required
def settings():
    children = Child.query.order_by(Child.name).all()
    def _get(key, default):
        s = AppSettings.query.get(key)
        return s.value if s else default

    # 'instant' is a legacy stored value; the form only offers real schedules
    stored_cadence = _get('payout_cadence', 'weekly')

    return render_template(
        'parent/settings.html',
        children=children,
        payout_day_enabled=_get('payout_day_enabled', 'off'),
        payout_cadence='weekly' if stored_cadence == 'instant' else stored_cadence,
        payout_time=_get('payout_time', '18:00'),
        payout_day_of_week=_get('payout_day_of_week', '0'),
        payout_day_of_month=_get('payout_day_of_month', '1'),
        session_timeout=_get('session_timeout', '5'),
        child_suggestions_enabled=_get('child_suggestions_enabled', 'off'),
        notify_email_enabled=_get('notify_email_enabled', 'off'),
        notify_email_to=_get('notify_email_to', ''),
        notify_smtp_user=_get('notify_smtp_user', ''),
        notify_smtp_password_set=bool(_get('notify_smtp_password', '')),
        app_base_url=_get('app_base_url', ''),
        book_default_loan_days=_get('book_default_loan_days', '28'),
        google_books_api_key_set=bool(_get('google_books_api_key', '')),
        app_timezone=_get('app_timezone', 'UTC'),
    )


@parent_bp.route('/settings/update', methods=['POST'])
@parent_required
def update_settings():
    def _set(key, value):
        s = AppSettings.query.get(key)
        if s:
            s.value = value
        else:
            db.session.add(AppSettings(key=key, value=value))

    was_enabled = is_payout_day_enabled()
    enabled = bool(request.form.get('payout_day_enabled'))
    _set('payout_day_enabled', 'on' if enabled else 'off')

    cadence = request.form.get('payout_cadence')
    if cadence in ('daily', 'weekly', 'monthly'):
        _set('payout_cadence', cadence)

    payout_time = request.form.get('payout_time', '').strip()
    if payout_time:
        _set('payout_time', payout_time)

    dow = request.form.get('payout_day_of_week', '').strip()
    if dow.isdigit() and 0 <= int(dow) <= 6:
        _set('payout_day_of_week', dow)

    dom = request.form.get('payout_day_of_month', '').strip()
    if dom.isdigit() and 1 <= int(dom) <= 28:
        _set('payout_day_of_month', dom)

    new_pin = request.form.get('new_pin', '').strip()
    confirm_pin = request.form.get('confirm_pin', '').strip()
    if new_pin:
        if len(new_pin) < 4:
            flash('PIN must be at least 4 characters.', 'error')
            return redirect(url_for('parent.settings'))
        if new_pin != confirm_pin:
            flash('PINs do not match.', 'error')
            return redirect(url_for('parent.settings'))
        pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
        s = AppSettings.query.get('parent_pin')
        if s:
            s.value = pin_hash
        else:
            db.session.add(AppSettings(key='parent_pin', value=pin_hash))

    timeout = request.form.get('session_timeout', '').strip()
    if timeout.isdigit() and int(timeout) >= 1:
        _set('session_timeout', timeout)

    _set('child_suggestions_enabled', 'on' if request.form.get('child_suggestions_enabled') else 'off')

    # ── Notification settings ─────────────────────────────────────────────────
    _set('notify_email_enabled', 'on' if request.form.get('notify_email_enabled') else 'off')

    notify_to = request.form.get('notify_email_to', '').strip()
    if notify_to:
        _set('notify_email_to', notify_to)

    notify_user = request.form.get('notify_smtp_user', '').strip()
    if notify_user:
        _set('notify_smtp_user', notify_user)

    # Only overwrite the password if a new one was actually typed
    notify_pw = request.form.get('notify_smtp_password', '').strip()
    if notify_pw:
        _set('notify_smtp_password', notify_pw)

    base_url = request.form.get('app_base_url', '').strip().rstrip('/')
    _set('app_base_url', base_url)

    loan_days = request.form.get('book_default_loan_days', '').strip()
    if loan_days.isdigit() and int(loan_days) >= 1:
        _set('book_default_loan_days', loan_days)

    google_key = request.form.get('google_books_api_key', '').strip()
    if google_key:
        _set('google_books_api_key', google_key)

    tz_val = request.form.get('app_timezone', '').strip()
    if tz_val:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(tz_val)
            _set('app_timezone', tz_val)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError):
            flash('Invalid timezone selected.', 'error')
            return redirect(url_for('parent.settings'))

    # Turning payout day off: nothing will ever sweep up chores still waiting, so pay them now.
    settled = settle_pending_payouts('Payout day disabled') if was_enabled and not enabled else {}

    db.session.commit()

    from ...scheduler import reschedule_payout_job
    reschedule_payout_job()

    if settled:
        from ...utils import backup_database
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
    def _get(key):
        s = AppSettings.query.get(key)
        return s.value.strip() if s and s.value else ''

    to_addr = _get('notify_email_to')
    smtp_user = _get('notify_smtp_user')
    smtp_password = _get('notify_smtp_password')

    if not all([to_addr, smtp_user, smtp_password]):
        flash('Please save your email settings before sending a test.', 'error')
        return redirect(url_for('parent.settings'))

    from ...notifications import send_test_email
    error = send_test_email(to_addr, smtp_user, smtp_password)
    if error:
        flash(f'Test failed: {error}', 'error')
    else:
        flash(f'Test email sent to {to_addr} — check your inbox!', 'success')
    return redirect(url_for('parent.settings'))
