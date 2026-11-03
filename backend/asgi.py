"""Vercel entrypoint: deployment environment only, no local dotenv discovery."""

from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.main import create_app


app = create_app(load_settings(None))
