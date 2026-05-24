"""Outbound notifications — currently Gmail SMTP only."""
import logging
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_SMTP_HOST = 'smtp.gmail.com'
_SMTP_PORT = 465
_BATCH_SECONDS = 60

# ── Batching state ────────────────────────────────────────────────────────────
# All access must be done under _batch_lock.
_batch_lock = threading.Lock()
_batch_pending: list[dict] = []   # pre-gathered chore dicts
_batch_timer: threading.Timer | None = None
_batch_config: dict | None = None  # SMTP config snapshot from first submission


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


def _flush_batch() -> None:
    """Timer callback: drain the pending list and send one batched email."""
    global _batch_pending, _batch_timer, _batch_config

    with _batch_lock:
        items = list(_batch_pending)
        cfg = _batch_config
        _batch_pending = []
        _batch_timer = None
        _batch_config = None

    if not items or not cfg:
        return

    smtp_user = cfg['smtp_user']
    smtp_password = cfg['smtp_password']
    to_addr = cfg['to_addr']

    n = len(items)
    if n == 1:
        item = items[0]
        subject = f'⭐ {item["child_name"]} submitted "{item["chore_name"]}" for review'
        lines = [
            f'{item["child_name"]} has submitted a chore and is waiting for your approval.',
            '',
            f'  Chore:   {item["chore_name"]}',
            f'  Reward:  ${item["value"]:.2f}',
        ]
        if item['review_url']:
            lines += ['', f'Review it here: {item["review_url"]}']
    else:
        subject = f'⭐ {n} chores submitted for review'
        lines = [
            f'{n} chores have been submitted and are waiting for your approval.',
            '',
        ]
        for item in items:
            lines.append(f'  {item["child_name"]} — {item["chore_name"]} (${item["value"]:.2f})')
            if item['review_url']:
                lines.append(f'    {item["review_url"]}')
        # Deduplicate URLs if all point to the same child
        unique_urls = list(dict.fromkeys(
            item['review_url'] for item in items if item['review_url']
        ))
        if len(unique_urls) == 1:
            # All same child — append a single link at the bottom
            lines += ['', f'Review here: {unique_urls[0]}']

    _send_email(smtp_user, smtp_password, to_addr, subject, '\n'.join(lines))


def send_chore_submitted(inst) -> None:
    """Queue a notification for batched delivery.
    The first submission in a window starts a 60-second timer; all
    submissions within that window are folded into one email.
    Call this after db.session.commit() inside a request context."""
    global _batch_pending, _batch_timer, _batch_config

    from flask import url_for

    enabled, to_addr, smtp_user, smtp_password = _get_config()
    if not enabled or not all([to_addr, smtp_user, smtp_password]):
        return

    try:
        review_url = url_for(
            'parent.child_detail', child_id=inst.child_id, _external=True
        )
    except Exception:
        review_url = ''

    item = {
        'child_name': inst.child.name,
        'chore_name': inst.effective_name,
        'value': inst.effective_value,
        'child_id': inst.child_id,
        'review_url': review_url,
    }

    with _batch_lock:
        _batch_pending.append(item)
        # Snapshot config on the first submission (in a valid request context)
        if _batch_config is None:
            _batch_config = {
                'to_addr': to_addr,
                'smtp_user': smtp_user,
                'smtp_password': smtp_password,
            }
        # Start the timer only if one isn't already running
        if _batch_timer is None:
            _batch_timer = threading.Timer(_BATCH_SECONDS, _flush_batch)
            _batch_timer.daemon = True
            _batch_timer.start()
            logger.debug('Batch notification timer started (%ds)', _BATCH_SECONDS)


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
