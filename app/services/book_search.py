"""Google Books lookup."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

_API_URL = 'https://www.googleapis.com/books/v1/volumes'


def _isbn(identifiers):
    """Prefer the ISBN-13, falling back to the ISBN-10."""
    for wanted in ('ISBN_13', 'ISBN_10'):
        for entry in identifiers:
            if entry.get('type') == wanted:
                return entry['identifier']
    return ''


def _parse_volume(item):
    info = item.get('volumeInfo', {})
    title = info.get('title', '')
    if not title:
        return None
    links = info.get('imageLinks', {})
    cover = links.get('thumbnail') or links.get('smallThumbnail') or ''
    if cover:
        cover = cover.replace('&edge=curl', '').replace('http://', 'https://')
    return {
        'title': title,
        'author': ', '.join(info.get('authors', [])),
        'isbn': _isbn(info.get('industryIdentifiers', [])),
        'cover': cover,
    }


def search_google_books(query, api_key=''):
    """Search Google Books by title/author, or by ISBN when the query looks like one.

    Returns (results, error) where error is a user-facing message or None.
    """
    isbn_clean = re.sub(r'[-\s]', '', query)
    if isbn_clean.isdigit() and len(isbn_clean) in (10, 13):
        url = f'{_API_URL}?q=isbn:{isbn_clean}&maxResults=1'
    else:
        url = f'{_API_URL}?q={urllib.parse.quote(query)}&maxResults=10'
    if api_key:
        url += f'&key={api_key}'

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        volumes = (_parse_volume(item) for item in (data.get('items') or []))
        return [v for v in volumes if v], None
    except urllib.error.HTTPError as e:
        current_app.logger.warning('Book search HTTP %s for q=%r', e.code, query)
        return [], ('Rate limited — try again in a moment.' if e.code == 429
                    else f'Google Books error ({e.code}).')
    except Exception as e:
        current_app.logger.warning('Book search failed for q=%r: %s', query, e)
        return [], 'Could not reach Google Books.'
