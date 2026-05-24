"""Outbound notifications — currently Gmail SMTP only."""
import logging
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_SMTP_HOST = 'smtp.gmail.com'
_SMTP_PORT = 465


def _parse_recipients(to_addr: str) -> list[str]:
    """Split a comma-separated address string into a cleaned list."""
    return [a.strip() for a in to_addr.split(',') if a.strip()]


def _send_email(smtp_user: str, smtp_password: str, to_addr: str,
                subject: str, body_text: str) -> None:
    """Blocking SMTP send — intended to be called from a daemon thread.
    to_addr may be a single address or a comma-separated list."""
    recipients = _parse_recipients(to_addr)
    if not recipients:
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f'Chore Tracker <{smtp_user}>'
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(body_text, 'plain'))

    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=15) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        logger.info('Email sent to %s — %s', ', '.join(recipients), subject)
    except Exception:
        logger.exception('Failed to send email to %s', ', '.join(recipients))


def _fire(smtp_user, smtp_password, to_addr, subject, body):
    """Spawn a daemon thread so SMTP never blocks the HTTP response."""
    threading.Thread(
        target=_send_email,
        args=(smtp_user, smtp_password, to_addr, subject, body),
        daemon=True,
    ).start()


def _get_config():
    """Return (enabled, to_addr, smtp_user, smtp_password) from AppSettings.
    Must be called inside a Flask app context."""
    from .models import AppSettings

    def _v(key, default=''):
        row = AppSettings.query.get(key)
        return row.value.strip() if row and row.value else default

    return (
        _v('notify_email_enabled') == 'on',
        _v('notify_email_to'),
        _v('notify_smtp_user'),
        _v('notify_smtp_password'),
    )


def send_chore_submitted(inst) -> None:
    """Send a notification email when a child submits a chore for review.
    Call this after db.session.commit() inside a request context."""
    from flask import url_for

    enabled, to_addr, smtp_user, smtp_password = _get_config()
    if not enabled or not all([to_addr, smtp_user, smtp_password]):
        return

    child_name = inst.child.name
    chore_name = inst.effective_name
    value = inst.effective_value

    try:
        review_url = url_for(
            'parent.child_detail', child_id=inst.child_id, _external=True
        )
    except Exception:
        review_url = ''

    subject = f'⭐ {child_name} submitted “{chore_name}” for review'

    lines = [
        f'{child_name} has submitted a chore and is waiting for your approval.',
        '',
        f'  Chore:   {chore_name}',
        f'  Reward:  ${value:.2f}',
    ]
    if review_url:
        lines += ['', f'Review it here: {review_url}']

    _fire(smtp_user, smtp_password, to_addr, subject, '\n'.join(lines))


def send_test_email(to_addr: str, smtp_user: str, smtp_password: str) -> str | None:
    """Send a test email synchronously. Returns an error message or None on success."""
    subject = '✅ Chore Tracker — test notification'
    body = (
        'This is a test notification from Chore Tracker.\n\n'
        'If you received this, email notifications are configured correctly!'
    )
    try:
        _send_email(smtp_user, smtp_password, to_addr, subject, body)
        return None
    except smtplib.SMTPAuthenticationError:
        return 'Authentication failed — check your Gmail address and App Password.'
    except smtplib.SMTPException as e:
        return f'SMTP error: {e}'
    except OSError as e:
        return f'Connection error: {e}'
