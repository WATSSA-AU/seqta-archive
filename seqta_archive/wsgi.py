"""WSGI entry point for gunicorn / waitress: seqta_archive.wsgi:app"""

from .web import create_app

app = create_app()
