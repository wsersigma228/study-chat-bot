"""Database connection shared by the offline commands."""

import os

from sqlalchemy.ext.asyncio import create_async_engine

from app.telegram_settings import load_env


def database_url() -> str:
    load_env()
    url = os.getenv("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise ValueError("Set DATABASE_URL to a postgresql+asyncpg URL (see README.md).")
    return url


def engine():
    return create_async_engine(database_url())
