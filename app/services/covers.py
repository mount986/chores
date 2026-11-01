"""Local cache of book cover images, plus a generated placeholder for books without one."""
import os
import urllib.request
import zlib
from xml.sax.saxutils import escape

from flask import current_app

from ..models import BookCheckout


def covers_dir():
    path = os.path.join(current_app.instance_path, 'covers')
    os.makedirs(path, exist_ok=True)
    return path


def cover_path(book_id):
    return os.path.join(covers_dir(), f'{book_id}.jpg')


def download_cover(book):
    """Download book.cover_url to the local cache. No-op if already cached or no URL."""
    if not book.cover_url:
        return
    path = cover_path(book.id)
    if os.path.exists(path):
        return
    try:
        req = urllib.request.Request(book.cover_url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
        with open(path, 'wb') as f:
            f.write(data)
    except Exception as e:
        current_app.logger.warning('Cover download failed for book %s: %s', book.id, e)


def maybe_delete_cover(book_id):
    """Delete the cached cover if no active (unreturned) checkouts remain for this book."""
    active = BookCheckout.query.filter(
        BookCheckout.book_id == book_id,
        BookCheckout.returned_date == None  # noqa: E711
    ).first()
    if not active:
        try:
            os.remove(cover_path(book_id))
        except OSError:
            pass


def _wrap_text(text, max_chars):
    lines, current = [], ''
    for word in text.split():
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
    return escape(s, {'"': '&quot;'})


def placeholder_cover_svg(book):
    """A simple coloured SVG cover showing the title and author."""
    hue = zlib.crc32(book.title.encode()) % 360  # stable across processes, unlike hash()
    title_lines = _wrap_text(book.title, 18)
    title_y_start = 120 - (len(title_lines) - 1) * 14
    title_blocks = ''.join(
        f'<tspan x="100" dy="{0 if i == 0 else 28}">{_xml_escape(line)}</tspan>'
        for i, line in enumerate(title_lines)
    )
    author = (
        f'<text x="100" y="220" text-anchor="middle" font-family="Georgia,serif" font-size="12" '
        f'fill="hsl({hue},35%,40%)">{_xml_escape(book.author)}</text>'
        if book.author else ''
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 280" width="200" height="280">
  <rect width="200" height="280" fill="hsl({hue},55%,82%)"/>
  <rect x="10" y="10" width="180" height="260" rx="4" fill="none" stroke="hsl({hue},40%,60%)" stroke-width="3"/>
  <text x="100" y="{title_y_start}" text-anchor="middle" font-family="Georgia,serif" font-size="16" font-weight="bold" fill="hsl({hue},40%,25%)">{title_blocks}</text>
  {author}
  <text x="100" y="260" text-anchor="middle" font-family="Georgia,serif" font-size="28" fill="hsl({hue},40%,50%)" opacity="0.4">📚</text>
</svg>'''
