"""Centralised environment loading for OmniFlow.

Every module that needs configuration calls :func:`load_environment` (idempotent)
and then reads values through the ``env_*`` helpers.  Real process environment
variables always win over values in ``.env`` so Docker / systemd / CI can inject
configuration without editing files.

Environment switches
--------------------
``OMNIFLOW_ENV_FILE``        Path of the dotenv file (default: ``<project>/.env``).
``OMNIFLOW_DISABLE_DOTENV``  When truthy, ``.env`` is never read (used by tests/CI).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("omniflow.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f", ""}

_load_lock = threading.Lock()
_loaded = False


def _clean(value: str | None) -> str:
    """Strip whitespace and one layer of surrounding quotes from an env value."""
    if value is None:
        return ""
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def dotenv_disabled() -> bool:
    return _clean(os.environ.get("OMNIFLOW_DISABLE_DOTENV")).lower() in _TRUTHY


def env_file_path() -> Path:
    custom = _clean(os.environ.get("OMNIFLOW_ENV_FILE"))
    return Path(custom) if custom else PROJECT_ROOT / ".env"


def load_environment(force: bool = False) -> None:
    """Load ``.env`` once (``override=False``) and normalise legacy aliases.

    Safe to call from anywhere, any number of times.
    """
    global _loaded
    with _load_lock:
        if _loaded and not force:
            return
        if not dotenv_disabled():
            path = env_file_path()
            if path.is_file():
                load_dotenv(path, override=False)
        _normalise_aliases()
        _loaded = True


def _normalise_aliases() -> None:
    # Legacy alias kept for backwards compatibility with early deployments.
    legacy_key = _clean(os.environ.get("OPEN_AI_KEY"))
    if legacy_key and not _clean(os.environ.get("OPENAI_API_KEY")):
        os.environ["OPENAI_API_KEY"] = legacy_key


def env_str(name: str, *aliases: str, default: str = "") -> str:
    """Return the first non-empty value among ``name`` and ``aliases``."""
    for key in (name, *aliases):
        value = _clean(os.environ.get(key))
        if value:
            return value
    return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = _clean(os.environ.get(name)).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY and raw != "":
        return False
    return default


def env_int(name: str, default: int) -> int:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %s", name, raw, default)
        return default


def env_float(name: str, default: float) -> float:
    raw = _clean(os.environ.get(name))
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s", name, raw, default)
        return default


def app_env() -> str:
    """Deployment environment: ``development`` (default), ``test`` or ``production``."""
    return env_str("APP_ENV", default="development").lower()


def is_production() -> bool:
    return app_env() in {"production", "prod"}
