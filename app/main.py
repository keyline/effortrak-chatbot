"""ASGI entry point: uvicorn app.main:app."""

from app.query.application import create_app

app = create_app()
