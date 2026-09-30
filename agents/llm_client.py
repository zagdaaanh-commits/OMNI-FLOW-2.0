"""Unified OpenAI-compatible LLM client.

Defaults to Zhipu AI **GLM-4-Flash** (free tier, low latency, reachable from both
mainland China and Hong Kong) while allowing any OpenAI-compatible provider via:

``OPENAI_API_KEY``   (aliases ``OPEN_AI_KEY``, ``ZHIPUAI_API_KEY``)
``OPENAI_BASE_URL``  (default ``https://open.bigmodel.cn/api/paas/v4/``)
``MODEL_NAME``       (aliases ``OPENAI_MODEL``, legacy ``OPENANAI_BASE_URI_MODEL``; default ``glm-4-flash``)
``VISION_MODEL_NAME`` (default ``glm-4v-flash``)

Reliability: exponential backoff with full jitter for rate limits, timeouts,
connection errors and 5xx responses; ``Retry-After`` is honoured (capped); auth
failures trip a per-key circuit breaker so a bad key never hammers the provider.
Outbound traffic honours ``OUTBOUND_PROXY_URL`` (see :mod:`tools.http_client`).
"""
from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import openai

from app.config import env_float, env_int, env_str, load_environment
from tools.http_client import get_outbound_proxy

logger = logging.getLogger("omniflow.llm")

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4-flash"
DEFAULT_VISION_MODEL = "glm-4v-flash"


class LLMError(RuntimeError):
    """Base class for LLM failures surfaced to agents."""


class LLMNotConfiguredError(LLMError):
    """No API key configured."""


class LLMAuthError(LLMError):
    """The provider rejected the credentials (circuit breaker tripped)."""


class LLMRateLimitError(LLMError):
    """Rate limited even after all retries."""


class LLMUnavailableError(LLMError):
    """Provider unreachable / timing out / 5xx after all retries, or a non-retryable request error."""


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str
    vision_model: str
    timeout: float
    max_retries: int
    backoff_base: float
    backoff_max: float
    proxy: Optional[str]

    @classmethod
    def from_env(cls) -> "LLMSettings":
        load_environment()
        return cls(
            api_key=env_str("OPENAI_API_KEY", "OPEN_AI_KEY", "ZHIPUAI_API_KEY"),
            base_url=env_str("OPENAI_BASE_URL", default=DEFAULT_BASE_URL),
            model=env_str("MODEL_NAME", "OPENAI_MODEL", "OPENANAI_BASE_URI_MODEL", default=DEFAULT_MODEL),
            vision_model=env_str("VISION_MODEL_NAME", default=DEFAULT_VISION_MODEL),
            timeout=env_float("LLM_TIMEOUT_SECONDS", 30.0),
            max_retries=max(0, env_int("LLM_MAX_RETRIES", 3)),
            backoff_base=max(0.0, env_float("LLM_BACKOFF_BASE_SECONDS", 1.0)),
            backoff_max=max(0.0, env_float("LLM_BACKOFF_MAX_SECONDS", 20.0)),
            proxy=get_outbound_proxy(),
        )

    @property
    def provider_host(self) -> str:
        return urlsplit(self.base_url).hostname or self.base_url

    @property
    def key_fingerprint(self) -> str:
        """Non-reversible identifier for logs/circuit breaker (never log the key)."""
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:12] if self.api_key else "none"


_RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)
_NON_RETRYABLE = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.BadRequestError,
    openai.NotFoundError,
)

# Keys (by fingerprint) that the provider rejected; shared across client instances.
_auth_failed_keys: set[str] = set()
_auth_lock = threading.Lock()


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after-ms")
    if raw:
        try:
            return float(raw) / 1000.0
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _NON_RETRYABLE):
        return False
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


class LLMClient:
    """Thin, resilient wrapper around ``openai.OpenAI`` chat completions."""

    def __init__(
        self,
        settings: Optional[LLMSettings] = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        sdk_client: Any = None,
    ) -> None:
        self.settings = settings or LLMSettings.from_env()
        self._sleep = sleep
        self._sdk: Any = sdk_client
        if self._sdk is None and self.settings.api_key:
            http_client = openai.DefaultHttpxClient(
                proxy=self.settings.proxy,
                trust_env=False,
                timeout=self.settings.timeout,
            )
            self._sdk = openai.OpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                timeout=self.settings.timeout,
                max_retries=0,  # retries are handled here with jittered backoff
                http_client=http_client,
            )

    # ------------------------------------------------------------------ state
    @property
    def is_configured(self) -> bool:
        return bool(self.settings.api_key) and self._sdk is not None

    @property
    def auth_failed(self) -> bool:
        with _auth_lock:
            return self.settings.key_fingerprint in _auth_failed_keys

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def vision_model(self) -> str:
        return self.settings.vision_model

    def _trip_auth_breaker(self) -> None:
        with _auth_lock:
            _auth_failed_keys.add(self.settings.key_fingerprint)

    # ------------------------------------------------------------------- calls
    def _backoff_delay(self, attempt: int, exc: BaseException) -> float:
        cap = self.settings.backoff_max
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return max(0.0, min(retry_after, cap))
        exp = self.settings.backoff_base * (2 ** attempt)
        return random.uniform(0.0, min(cap, exp))  # full jitter

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 700,
        **extra: Any,
    ) -> str:
        """Run a chat completion and return the assistant text (stripped)."""
        if not self.is_configured:
            raise LLMNotConfiguredError("No LLM API key configured (set OPENAI_API_KEY).")
        if self.auth_failed:
            raise LLMAuthError(
                f"LLM credentials previously rejected by {self.settings.provider_host}; "
                "update OPENAI_API_KEY to retry."
            )

        target_model = model or self.settings.model
        attempts = self.settings.max_retries + 1
        last_exc: Optional[BaseException] = None

        for attempt in range(attempts):
            try:
                response = self._sdk.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
                content = response.choices[0].message.content if response.choices else None
                if not content or not str(content).strip():
                    raise LLMUnavailableError(f"Empty completion from {target_model}")
                return str(content).strip()
            except openai.AuthenticationError as exc:
                self._trip_auth_breaker()
                logger.error(
                    "LLM auth rejected by %s (key=%s); circuit breaker tripped",
                    self.settings.provider_host,
                    self.settings.key_fingerprint,
                )
                raise LLMAuthError(str(exc)) from exc
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if not _is_retryable(exc):
                    raise LLMUnavailableError(f"{type(exc).__name__}: {exc}") from exc
                if attempt >= attempts - 1:
                    break
                delay = self._backoff_delay(attempt, exc)
                logger.warning(
                    "LLM call to %s/%s failed (%s); retry %d/%d in %.2fs",
                    self.settings.provider_host,
                    target_model,
                    type(exc).__name__,
                    attempt + 1,
                    attempts - 1,
                    delay,
                )
                self._sleep(delay)

        assert last_exc is not None
        if isinstance(last_exc, openai.RateLimitError):
            raise LLMRateLimitError(f"Rate limited by {self.settings.provider_host}: {last_exc}") from last_exc
        raise LLMUnavailableError(f"{type(last_exc).__name__}: {last_exc}") from last_exc


_client_lock = threading.Lock()
_cached_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Return a cached client, rebuilt when key/base URL/model/proxy change at runtime."""
    global _cached_client
    settings = LLMSettings.from_env()
    with _client_lock:
        if _cached_client is None or _cached_client.settings != settings:
            _cached_client = LLMClient(settings)
            if settings.api_key:
                logger.info(
                    "LLM client ready (provider=%s, model=%s, proxy=%s)",
                    settings.provider_host,
                    settings.model,
                    "on" if settings.proxy else "off",
                )
        return _cached_client


def reset_llm_state() -> None:
    """Clear cached client and circuit breaker (tests / key rotation)."""
    global _cached_client
    with _client_lock:
        _cached_client = None
    with _auth_lock:
        _auth_failed_keys.clear()
