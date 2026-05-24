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
_TOKEN_MAX_AGE = 7 * 24 * 3600  # 7 days

# ── Batching state ────────────────────────────────────────────────────────────
_batch_lock = threading.Lock()
_batch_pending: list[dict] = []
_batch_timer: threading.Timer | None = None
_batch_config: dict | None = None


def _parse_recipients(to_addr: str) -> list[str]:
    """Split a comma-separated address string into a cleaned list."""
    return [a.strip() for a in to_addr.split(',') if a.strip()]


def _make_action_token(inst_id: int, action: str) -> str:
    """Sign a chore-action token using the app's secret key."""
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(current_app.secret_key)
    return s.dumps({'id': inst_id, 'action': action})


def verify_action_token(token: str, secret_key: str) -> dict | None:
    """Verify and decode a chore-action token.  Returns None on failure."""
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    s = URLSafeTimedSerializer(secret_key)
    try:
        return s.loads(token, max_age=_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def _send_email(smtp_user: str, smtp_password: str, to_addr: str,
                subject: str, body_text: str, body_html: str | None = None) -> None:
    """Blocking SMTP send.  to_addr may be comma-separated.
    Sends multipart/alternative when body_html is provided."""
    recipients = _parse_recipients(to_addr)
    if not recipients:
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f'Chore Tracker <{smtp_user}>'
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(body_text, 'plain'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html'))

    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=15) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        logger.info('Email sent to %s — %s', ', '.join(recipients), subject)
    except Exception:
        logger.exception('Failed to send email to %s', ', '.join(recipients))


def _fire(smtp_user, smtp_password, to_addr, subject, body_text, body_html=None):
    """Spawn a daemon thread so SMTP never blocks the HTTP response."""
    threading.Thread(
        target=_send_email,
        args=(smtp_user, smtp_password, to_addr, subject, body_text),
        kwargs={'body_html': body_html},
        daemon=True,
    ).start()


def _get_config():
    """Return (enabled, to_addr, smtp_user, smtp_password, base_url) from AppSettings."""
    from .models import AppSettings

    def _v(key, default=''):
        row = AppSettings.query.get(key)
        return row.value.strip() if row and row.value else default

    return (
        _v('notify_email_enabled') == 'on',
        _v('notify_email_to'),
        _v('notify_smtp_user'),
        _v('notify_smtp_password'),
        _v('app_base_url'),
    )


def _make_url(path: str, base_url: str) -> str:
    """Combine the configured base URL with a path, stripping any trailing slash."""
    return base_url.rstrip('/') + path


# ── HTML email builder ────────────────────────────────────────────────────────

def _chore_row_html(item: dict) -> str:
    """One table row per chore with Approve / Deny buttons."""
    approve_url = item['approve_url']
    deny_url = item['deny_url']
    child = item['child_name']
    chore = item['chore_name']
    value = item['value']
    return f"""
    <tr>
      <td style="padding:10px 12px;vertical-align:middle;">
        <span style="font-weight:bold;color:#1f2937;">{child}</span>
        <span style="color:#9ca3af;margin:0 4px;">&mdash;</span>
        <span style="color:#374151;">{chore}</span>
        <span style="color:#059669;font-weight:bold;margin-left:6px;">${value:.2f}</span>
      </td>
      <td style="padding:10px 12px;vertical-align:middle;text-align:right;white-space:nowrap;">
        <a href="{approve_url}"
           style="display:inline-block;background:#22c55e;color:#ffffff;padding:7px 14px;
                  border-radius:6px;text-decoration:none;font-size:13px;font-weight:bold;
                  margin-right:6px;">Approve &#10003;</a>
        <a href="{deny_url}"
           style="display:inline-block;background:#ef4444;color:#ffffff;padding:7px 14px;
                  border-radius:6px;text-decoration:none;font-size:13px;font-weight:bold;">
          Deny &#10007;</a>
      </td>
    </tr>
    <tr><td colspan="2" style="padding:0 12px;"><hr style="border:none;border-top:1px solid #f3f4f6;margin:0;"></td></tr>"""


def _build_html(items: list[dict], n: int, dashboard_url: str = '') -> str:
    blurb = ('1 chore has been submitted and is waiting for your approval.'
             if n == 1
             else f'{n} chores have been submitted and are waiting for your approval.')
    rows = ''.join(_chore_row_html(item) for item in items)
    dashboard_link = (
        f'<a href="{dashboard_url}" style="color:#6366f1;text-decoration:none;font-weight:bold;">Open parent dashboard</a>'
        if dashboard_url else ''
    )
    footer_parts = ['Approving from this email awards the full amount. To adjust the amount or add denial notes, open the app.']
    if dashboard_link:
        footer_parts.append(dashboard_link)
    footer_html = ' &nbsp;·&nbsp; '.join(footer_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:580px;margin:0 auto;background:#ffffff;border-radius:12px;
              overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
    <div style="background:#fef3c7;padding:14px 20px;border-bottom:1px solid #fde68a;">
      <h2 style="margin:0;color:#92400e;font-size:17px;">&#128276; Needs Your Review</h2>
    </div>
    <div style="padding:14px 8px 6px;">
      <p style="margin:0 12px 14px;color:#6b7280;font-size:14px;">{blurb}</p>
      <table style="width:100%;border-collapse:collapse;">
        {rows}
      </table>
    </div>
    <div style="padding:12px 20px;background:#f9fafb;border-top:1px solid #f3f4f6;">
      <p style="margin:0;color:#9ca3af;font-size:12px;">{footer_html}</p>
    </div>
  </div>
</body>
</html>"""


# ── Batch flush ───────────────────────────────────────────────────────────────

def _flush_batch() -> None:
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
    dashboard_url = cfg.get('dashboard_url', '')
    n = len(items)

    # ── Plain-text body ───────────────────────────────────────────────────────
    if n == 1:
        item = items[0]
        subject = f'⭐ {item["child_name"]} submitted "{item["chore_name"]}" for review'
        lines = [
            f'{item["child_name"]} has submitted a chore and is waiting for your approval.',
            '',
            f'  Chore:   {item["chore_name"]}',
            f'  Reward:  ${item["value"]:.2f}',
        ]
        if item['approve_url']:
            lines += [
                '',
                f'Approve: {item["approve_url"]}',
                f'Deny:    {item["deny_url"]}',
            ]
    else:
        subject = f'⭐ {n} chores submitted for review'
        lines = [
            f'{n} chores have been submitted and are waiting for your approval.',
            '',
        ]
        for item in items:
            lines += [
                f'  {item["child_name"]} — {item["chore_name"]} (${item["value"]:.2f})',
            ]
            if item['approve_url']:
                lines += [
                    f'    Approve: {item["approve_url"]}',
                    f'    Deny:    {item["deny_url"]}',
                ]
            lines.append('')

    if dashboard_url:
        lines += [f'Open app: {dashboard_url}']

    body_text = '\n'.join(lines)
    body_html = _build_html(items, n, dashboard_url)

    _send_email(smtp_user, smtp_password, to_addr, subject, body_text, body_html)


# ── Public API ────────────────────────────────────────────────────────────────

def send_chore_submitted(inst) -> None:
    """Queue a chore for batched notification delivery.
    Call after db.session.commit() inside a request context."""
    global _batch_pending, _batch_timer, _batch_config

    enabled, to_addr, smtp_user, smtp_password, base_url = _get_config()
    if not enabled or not all([to_addr, smtp_user, smtp_password]):
        return

    if not base_url:
        logger.warning(
            'app_base_url is not configured — email action links will not work. '
            'Set it in Settings → Email Notifications.'
        )

    # Generate signed action URLs while still in the request context
    try:
        approve_token = _make_action_token(inst.id, 'approve')
        deny_token = _make_action_token(inst.id, 'deny')
        approve_url = _make_url(f'/parent/chore-action/{approve_token}', base_url) if base_url else ''
        deny_url = _make_url(f'/parent/chore-action/{deny_token}', base_url) if base_url else ''
        dashboard_url = _make_url('/parent/', base_url) if base_url else ''
    except Exception:
        logger.exception('Failed to generate action URLs for chore %s', inst.id)
        approve_url = deny_url = dashboard_url = ''

    item = {
        'child_name': inst.child.name,
        'chore_name': inst.effective_name,
        'value': inst.effective_value,
        'child_id': inst.child_id,
        'approve_url': approve_url,
        'deny_url': deny_url,
    }

    with _batch_lock:
        _batch_pending.append(item)
        if _batch_config is None:
            _batch_config = {
                'to_addr': to_addr,
                'smtp_user': smtp_user,
                'smtp_password': smtp_password,
                'dashboard_url': dashboard_url,
            }
        if _batch_timer is None:
            _batch_timer = threading.Timer(_BATCH_SECONDS, _flush_batch)
            _batch_timer.daemon = True
            _batch_timer.start()
            logger.debug('Batch notification timer started (%ds)', _BATCH_SECONDS)


def send_test_email(to_addr: str, smtp_user: str, smtp_password: str) -> str | None:
    """Send a test email synchronously. Returns an error string or None on success."""
    subject = '✅ Chore Tracker — test notification'
    body_text = (
        'This is a test notification from Chore Tracker.\n\n'
        'If you received this, email notifications are configured correctly!'
    )
    body_html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;padding:20px;background:#f3f4f6;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;
              padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08);">
    <h2 style="color:#22c55e;margin-top:0;">&#9989; Test notification</h2>
    <p style="color:#374151;">
      This is a test notification from <strong>Chore Tracker</strong>.<br>
      If you received this, email notifications are configured correctly!
    </p>
  </div>
</body>
</html>"""
    try:
        _send_email(smtp_user, smtp_password, to_addr, subject, body_text, body_html)
        return None
    except smtplib.SMTPAuthenticationError:
        return 'Authentication failed — check your Gmail address and App Password.'
    except smtplib.SMTPException as e:
        return f'SMTP error: {e}'
    except OSError as e:
        return f'Connection error: {e}'
