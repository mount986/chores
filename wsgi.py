"""WSGI entry point for gunicorn / any WSGI server."""
from app import create_app

app = create_app()
