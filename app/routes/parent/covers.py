import os
from flask import Response, redirect, send_file
from ...models import Book
from ...services.covers import cover_path, download_cover, placeholder_cover_svg
from .common import parent_bp


@parent_bp.route('/books/<int:book_id>/cover.svg')
def book_cover_svg(book_id):
    book = Book.query.get_or_404(book_id)
    local = cover_path(book_id)
    if not os.path.exists(local) and book.cover_url:
        download_cover(book)
    if os.path.exists(local):
        return send_file(local, mimetype='image/jpeg', max_age=86400, conditional=True)
    if book.cover_url:
        return redirect(book.cover_url)
    return Response(placeholder_cover_svg(book), mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})
