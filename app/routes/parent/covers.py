import os
from flask import redirect, current_app, send_file
from ...models import Book, BookCheckout
from .common import parent_bp


@parent_bp.route('/books/<int:book_id>/cover.svg')
def book_cover_svg(book_id):
    book = Book.query.get_or_404(book_id)
    local = _cover_path(book_id)
    if not os.path.exists(local) and book.cover_url:
        _download_cover(book)
    if os.path.exists(local):
        return send_file(local, mimetype='image/jpeg',
                         max_age=86400, conditional=True)
    if book.cover_url:
        return redirect(book.cover_url)
    hue = hash(book.title) % 360
    title_lines = _wrap_text(book.title, 18)
    author_line = book.author or ''
    title_y_start = 120 - (len(title_lines) - 1) * 14
    title_blocks = ''.join(
        f'<tspan x="100" dy="{0 if i == 0 else 28}">{_xml_escape(line)}</tspan>'
        for i, line in enumerate(title_lines)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 280" width="200" height="280">
  <rect width="200" height="280" fill="hsl({hue},55%,82%)"/>
  <rect x="10" y="10" width="180" height="260" rx="4" fill="none" stroke="hsl({hue},40%,60%)" stroke-width="3"/>
  <text x="100" y="{title_y_start}" text-anchor="middle" font-family="Georgia,serif" font-size="16" font-weight="bold" fill="hsl({hue},40%,25%)">{title_blocks}</text>
  {f'<text x="100" y="220" text-anchor="middle" font-family="Georgia,serif" font-size="12" fill="hsl({hue},35%,40%)">{_xml_escape(author_line)}</text>' if author_line else ''}
  <text x="100" y="260" text-anchor="middle" font-family="Georgia,serif" font-size="28" fill="hsl({hue},40%,50%)" opacity="0.4">📚</text>
</svg>'''
    from flask import Response
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


def _wrap_text(text, max_chars):
    words = text.split()
    lines, current = [], ''
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= max_chars:
            current = (current + ' ' + word).strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text[:max_chars]]


def _xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ── Cover image local cache ───────────────────────────────────────────────────

def _covers_dir():
    d = os.path.join(current_app.instance_path, 'covers')
    os.makedirs(d, exist_ok=True)
    return d


def _cover_path(book_id):
    return os.path.join(_covers_dir(), f'{book_id}.jpg')


def _download_cover(book):
    """Download book.cover_url to the local covers cache. No-op if already cached or no URL."""
    if not book.cover_url:
        return
    path = _cover_path(book.id)
    if os.path.exists(path):
        return
    try:
        import urllib.request as _ur
        req = _ur.Request(book.cover_url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with _ur.urlopen(req, timeout=8) as r:
            data = r.read()
        with open(path, 'wb') as f:
            f.write(data)
    except Exception as e:
        current_app.logger.warning('Cover download failed for book %s: %s', book.id, e)


def _maybe_delete_cover(book_id):
    """Delete cached cover if no active (unreturned) checkouts remain for this book."""
    active = BookCheckout.query.filter(
        BookCheckout.book_id == book_id,
        BookCheckout.returned_date == None  # noqa: E711
    ).first()
    if not active:
        path = _cover_path(book_id)
        try:
            os.remove(path)
        except OSError:
            pass
