"""Backwards-compatible import shim.

The storage layer now lives in the :mod:`db` package (multi-tenant SQLite and
PostgreSQL/Supabase implementations).  ``from storage import SQLiteStore`` keeps working.
"""
from __future__ import annotations

from db import create_store
from db.base import DEFAULT_TENANT_ID, CrossTenantWriteError, Store
from db.sqlite_store import SQLiteStore

__all__ = ["DEFAULT_TENANT_ID", "CrossTenantWriteError", "SQLiteStore", "Store", "create_store"]
