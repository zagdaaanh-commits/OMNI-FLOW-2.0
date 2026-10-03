#!/usr/bin/env python
"""Apply database migrations.

* ``DATABASE_URL`` set to postgres://... -> runs ``scripts/init_supabase.sql`` and then
  ``scripts/supabase_subscriptions.sql`` in ONE transaction (idempotent; safe on every deploy).
* otherwise -> initialises / migrates the SQLite database at ``DATABASE_PATH``.

Usage (project root, or /app inside Docker)::

    python scripts/migrate.py
    python scripts/migrate.py --sql path/to/custom.sql            # only that file
    python scripts/migrate.py --sql scripts/supabase_storage_setup.sql   # Supabase Storage (once)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import env_str, load_environment  # noqa: E402
from db import is_postgres_url  # noqa: E402

logger = logging.getLogger("omniflow.migrate")
DEFAULT_SQL = [PROJECT_ROOT / "scripts" / "init_supabase.sql", PROJECT_ROOT / "scripts" / "supabase_subscriptions.sql"]


def migrate_postgres(dsn: str, sql_paths: list[Path]) -> None:
    import psycopg

    # autocommit=False + an explicit transaction: either every script applies or nothing does.
    with psycopg.connect(dsn, autocommit=False, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            for sql_path in sql_paths:
                logger.info("Applying %s to PostgreSQL ...", sql_path.name)
                cur.execute(sql_path.read_text(encoding="utf-8"))  # simple-query protocol: many statements
        conn.commit()
    logger.info("PostgreSQL schema is up to date.")


def migrate_sqlite(path: str) -> None:
    from db.sqlite_store import SQLiteStore

    logger.info("Initialising / migrating SQLite database at %s ...", path)
    SQLiteStore(path)
    logger.info("SQLite schema is up to date.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sql", type=Path, action="append", help="SQL file for PostgreSQL (repeatable; default: the schema + billing files)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_environment()
    dsn = env_str("DATABASE_URL")
    try:
        if dsn and is_postgres_url(dsn):
            sql_paths = args.sql or DEFAULT_SQL
            missing = [str(p) for p in sql_paths if not p.is_file()]
            if missing:
                logger.error("SQL file not found: %s", ", ".join(missing))
                return 2
            migrate_postgres(dsn, sql_paths)
        else:
            migrate_sqlite(env_str("DATABASE_PATH", default="./data/marketing.db"))
    except Exception as exc:  # noqa: BLE001 - report and fail the deploy
        logger.error("Migration failed: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
