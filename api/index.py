"""Vercel serverless entrypoint — exposes the FastAPI ASGI app."""

import os
import sys

# Make the project root importable so we can reuse app.py / room.py / personas.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

# Vercel's Python runtime serves the module-level `app` ASGI application.
