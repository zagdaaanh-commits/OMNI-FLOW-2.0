"""Storage backends: SQLite (dev/test) and PostgreSQL/Supabase (production)."""
from __future__ import annotations

import logging

from app.config import env_str, load_environment
from db.base import DEFAULT_TENANT_ID, CrossTenantWriteError, Store

logger = logging.getLogger("omniflow.db")

__all__ = ["DEFAULT_TENANT_ID", "CrossTenantWriteError", "Store", "create_store", "integrity_errors", "is_postgres_url"]


def is_postgres_url(url: str) -> bool:
    return url.lower().startswith(("postgres://", "postgresql://"))


def integrity_errors() -> tuple:
    """Exception types raised by the backends on unique/foreign-key violations."""
    import sqlite3

    errors: tuple = (sqlite3.IntegrityError,)
    try:
        import psycopg

        errors += (psycopg.errors.IntegrityError,)
    except Exception:  # noqa: BLE001 - psycopg optional in SQLite-only setups
        pass
    return errors


def create_store() -> Store:
    """``DATABASE_URL`` (postgres://...) selects PostgreSQL; otherwise SQLite at ``DATABASE_PATH``."""
    load_environment()
    url = env_str("DATABASE_URL")
    if url and is_postgres_url(url):
        from db.postgres_store import PostgresStore

        logger.info("Using PostgreSQL store")
        return PostgresStore(url)  # type: ignore[return-value]
    from db.sqlite_store import SQLiteStore

    path = env_str("DATABASE_PATH", default="./data/marketing.db")
    logger.info("Using SQLite store at %s", path)
    return SQLiteStore(path)  # type: ignore[return-value]
