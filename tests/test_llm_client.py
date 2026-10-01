"""Unified LLM client: defaults, backoff/retry, circuit breaker, copywriter fallbacks."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import httpx
import openai
import pytest

from agents.copywriter import CopywriterAgent
from agents.llm_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_VISION_MODEL,
    LLMAuthError,
    LLMClient,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMSettings,
    LLMUnavailableError,
    get_llm_client,
    reset_llm_state,
)
from models.schemas import Campaign, CampaignCreate, ContentGenerateRequest, Platform


def _settings(**overrides: Any) -> LLMSettings:
    base = dict(
        api_key="test-key", base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, vision_model=DEFAULT_VISION_MODEL,
        timeout=5.0, max_retries=3, backoff_base=1.0, backoff_max=20.0, proxy=None,
    )
    base.update(overrides)
    return LLMSettings(**base)


def _status_error(cls, status: int, headers: dict | None = None):
    request = httpx.Request("POST", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    # openai 3.x binds errors to its own httpx fork; both expose the same duck-typed API we rely on.
    return cls("boom", response=response, body=None)


def _completion(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeSDK:
    """Stand-in for openai.OpenAI: pops scripted outcomes (exceptions are raised)."""

    def __init__(self, outcomes: List[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client(outcomes, **settings_overrides):
    sleeps: List[float] = []
    sdk = FakeSDK(outcomes)
    client = LLMClient(_settings(**settings_overrides), sleep=sleeps.append, sdk_client=sdk)
    return client, sdk, sleeps


# ------------------------------------------------------------------ settings
def test_defaults_target_zhipu_glm4_flash(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    s = LLMSettings.from_env()
    assert s.base_url == "https://open.bigmodel.cn/api/paas/v4/"
    assert s.model == "glm-4-flash"
    assert s.vision_model == "glm-4v-flash"
    assert s.provider_host == "open.bigmodel.cn"
    assert s.max_retries == 3


def test_environment_overrides_and_aliases(monkeypatch):
    monkeypatch.setenv("OPEN_AI_KEY", '"legacy-key"')
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENANAI_BASE_URI_MODEL", "legacy-model")
    s = LLMSettings.from_env()
    assert s.api_key == "legacy-key"  # alias + quote stripping
    assert s.base_url == "https://api.example.com/v1"
    assert s.model == "legacy-model"
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    assert LLMSettings.from_env().model == "gpt-4o-mini"  # MODEL_NAME wins


def test_key_fingerprint_never_contains_key():
    s = _settings(api_key="super-secret-value")
    assert "super-secret" not in s.key_fingerprint


def test_settings_follow_outbound_proxy(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://127.0.0.1:7890")
    assert LLMSettings.from_env().proxy == "http://127.0.0.1:7890"


# --------------------------------------------------------------------- calls
def test_success_returns_stripped_text():
    client, sdk, sleeps = _client([_completion("  hello  ")])
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"
    assert sdk.calls[0]["model"] == "glm-4-flash"
    assert sleeps == []


def test_retries_rate_limit_with_exponential_backoff_then_succeeds(monkeypatch):
    monkeypatch.setattr("agents.llm_client.random.uniform", lambda lo, hi: hi)  # deterministic: no jitter reduction
    err = lambda: _status_error(openai.RateLimitError, 429)
    client, sdk, sleeps = _client([err(), err(), _completion("ok")])
    assert client.chat([{"role": "user", "content": "x"}]) == "ok"
    assert len(sdk.calls) == 3
    assert sleeps == [1.0, 2.0]  # base * 2**attempt


def test_backoff_is_capped(monkeypatch):
    monkeypatch.setattr("agents.llm_client.random.uniform", lambda lo, hi: hi)
    err = lambda: _status_error(openai.InternalServerError, 503)
    client, _, sleeps = _client([err(), err(), err(), _completion("ok")], backoff_base=10.0, backoff_max=15.0)
    client.chat([{"role": "user", "content": "x"}])
    assert sleeps == [10.0, 15.0, 15.0]


def test_retry_after_header_is_honoured_and_capped():
    err_short = _status_error(openai.RateLimitError, 429, {"retry-after": "3"})
    err_long = _status_error(openai.RateLimitError, 429, {"retry-after": "9999"})
    client, _, sleeps = _client([err_short, err_long, _completion("ok")], backoff_max=20.0)
    client.chat([{"role": "user", "content": "x"}])
    assert sleeps == [3.0, 20.0]


def test_gives_up_after_max_retries_with_rate_limit_error():
    errors = [_status_error(openai.RateLimitError, 429) for _ in range(4)]
    client, sdk, sleeps = _client(errors, max_retries=3)
    with pytest.raises(LLMRateLimitError):
        client.chat([{"role": "user", "content": "x"}])
    assert len(sdk.calls) == 4 and len(sleeps) == 3


def test_server_errors_exhaust_into_unavailable():
    errors = [_status_error(openai.InternalServerError, 500) for _ in range(2)]
    client, sdk, _ = _client(errors, max_retries=1)
    with pytest.raises(LLMUnavailableError):
        client.chat([{"role": "user", "content": "x"}])
    assert len(sdk.calls) == 2


def test_connection_and_timeout_errors_are_retried():
    request = httpx.Request("POST", "https://open.bigmodel.cn")
    client, sdk, sleeps = _client([openai.APIConnectionError(request=request), openai.APITimeoutError(request=request), _completion("ok")])
    assert client.chat([{"role": "user", "content": "x"}]) == "ok"
    assert len(sleeps) == 2


@pytest.mark.parametrize("cls,status", [(openai.BadRequestError, 400), (openai.NotFoundError, 404), (openai.PermissionDeniedError, 403)])
def test_client_errors_are_not_retried(cls, status):
    client, sdk, sleeps = _client([_status_error(cls, status), _completion("never")])
    with pytest.raises(LLMUnavailableError):
        client.chat([{"role": "user", "content": "x"}])
    assert len(sdk.calls) == 1 and sleeps == []


def test_auth_error_trips_circuit_breaker_and_fails_fast():
    client, sdk, sleeps = _client([_status_error(openai.AuthenticationError, 401), _completion("never")])
    with pytest.raises(LLMAuthError):
        client.chat([{"role": "user", "content": "x"}])
    assert client.auth_failed
    with pytest.raises(LLMAuthError):  # second call: no network attempt
        client.chat([{"role": "user", "content": "x"}])
    assert len(sdk.calls) == 1 and sleeps == []


def test_circuit_breaker_resets_when_key_changes():
    client, _, _ = _client([_status_error(openai.AuthenticationError, 401)])
    with pytest.raises(LLMAuthError):
        client.chat([{"role": "user", "content": "x"}])
    fresh = LLMClient(_settings(api_key="another-key"), sdk_client=FakeSDK([_completion("fine")]))
    assert not fresh.auth_failed
    assert fresh.chat([{"role": "user", "content": "x"}]) == "fine"


def test_empty_completion_is_an_error():
    client, _, _ = _client([_completion("   ")])
    with pytest.raises(LLMUnavailableError):
        client.chat([{"role": "user", "content": "x"}])


def test_not_configured_raises():
    client = LLMClient(_settings(api_key=""))
    assert not client.is_configured
    with pytest.raises(LLMNotConfiguredError):
        client.chat([{"role": "user", "content": "x"}])


def test_get_llm_client_rebuilds_when_settings_change(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key-one")
    first = get_llm_client()
    assert get_llm_client() is first
    monkeypatch.setenv("OPENAI_API_KEY", "key-two")
    second = get_llm_client()
    assert second is not first and second.settings.api_key == "key-two"
    reset_llm_state()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert not get_llm_client().is_configured


# ----------------------------------------------------------------- copywriter
def _campaign() -> Campaign:
    return Campaign(**CampaignCreate(name="Test", platforms=[Platform.META, Platform.X]).model_dump())


def test_copywriter_without_key_uses_deterministic_fallback():
    agent = CopywriterAgent()
    assert agent.client is None
    drafts = agent.generate(_campaign(), ContentGenerateRequest(campaign_id="c", topic="Widget", count_per_platform=1))
    assert len(drafts) == 2 and all(d.body and d.hashtags for d in drafts)
    assert all(d.metadata["status"] == "deterministic_fallback" for d in drafts)


def test_copywriter_falls_back_gracefully_when_llm_fails(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    sdk = FakeSDK([_status_error(openai.RateLimitError, 429) for _ in range(20)])
    monkeypatch.setattr("agents.llm_client.LLMClient.__init__", _patched_init(sdk))
    agent = CopywriterAgent()
    drafts = agent.generate(_campaign(), ContentGenerateRequest(campaign_id="c", topic="Widget", count_per_platform=1))
    assert len(drafts) == 2
    assert all(d.body for d in drafts)
    assert all(d.metadata["status"] == "simulated_fallback" for d in drafts)


def test_copywriter_uses_llm_output_when_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    sdk = FakeSDK([_completion("Shop the widget today! #Widget #Sale")] * 4)
    monkeypatch.setattr("agents.llm_client.LLMClient.__init__", _patched_init(sdk))
    drafts = CopywriterAgent().generate(_campaign(), ContentGenerateRequest(campaign_id="c", topic="Widget", count_per_platform=1))
    assert drafts[0].metadata["status"] == "ai_generated"
    assert drafts[0].hashtags == ["#Widget", "#Sale"]
    assert "#" not in drafts[0].body
    assert drafts[0].metadata["provider"] == "open.bigmodel.cn"


def test_copywriter_vision_prefers_vision_model_then_text_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    json_reply = '```json\n{"copy": "Great product", "hashtags": "#a #b", "platforms": {"Meta": 1}}\n```'
    # vision call fails (400), text-only call succeeds
    sdk = FakeSDK([_status_error(openai.BadRequestError, 400), _completion(json_reply)])
    monkeypatch.setattr("agents.llm_client.LLMClient.__init__", _patched_init(sdk))
    res = CopywriterAgent().generate_with_vision("Lamp", image_base64="data:image/png;base64,AAAA", campaign=_campaign())
    assert sdk.calls[0]["model"] == "glm-4v-flash"
    assert sdk.calls[1]["model"] == "glm-4-flash"
    assert res["copy"] == "Great product"
    assert res["hashtags"] == "#a #b"
    assert res["platforms"] == ["Meta"]
    assert len(res["drafts"]) == 2


def test_copywriter_vision_falls_back_to_template_on_bad_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    sdk = FakeSDK([_completion("definitely not json")])
    monkeypatch.setattr("agents.llm_client.LLMClient.__init__", _patched_init(sdk))
    res = CopywriterAgent().generate_with_vision("Lamp", campaign=_campaign())
    assert res["copy"] and res["drafts"][0].metadata["status"] == "fallback"


def _patched_init(sdk: FakeSDK):
    def init(self, settings=None, *, sleep=None, sdk_client=None):
        self.settings = settings or LLMSettings.from_env()
        self._sleep = lambda _s: None
        self._sdk = sdk

    return init


# ------------------------------------------------------- honest fallback (AI unavailable)
FABRICATED = [
    "4.9", "verified", "rating", "sells out", "drop", "limited", "complimentary", "exclusive", "%",
    "transformed", "guarantee", "best-selling", "precision", "durability", "礼遇", "首发", "挖到", "爆款",
]
DEFAULT_DESCRIPTION = CampaignCreate.model_fields["product_description"].default


def _assert_no_fabrication(text: str) -> None:
    lowered = text.lower()
    found = [phrase for phrase in FABRICATED if phrase.lower() in lowered]
    assert not found, f"fallback copy invents claims {found}: {text!r}"


def test_fallback_copy_uses_only_merchant_parameters_on_every_platform():
    campaign = Campaign(**CampaignCreate(
        name="Q4 internal push",
        platforms=list(Platform),
        product_description="Stoneware mugs, 350 ml, dishwasher safe",
    ).model_dump())
    drafts = CopywriterAgent().generate(
        campaign, ContentGenerateRequest(campaign_id="c", topic="Handmade ceramic mugs", count_per_platform=1)
    )
    assert {d.platform for d in drafts} == set(Platform)
    for draft in drafts:
        assert draft.metadata["status"] == "deterministic_fallback"
        assert "Handmade ceramic mugs" in draft.body
        assert "Stoneware mugs, 350 ml, dishwasher safe" in draft.body  # merchant-supplied facts are kept verbatim
        assert "Q4 internal push" not in draft.body  # internal campaign name is never published
        _assert_no_fabrication(draft.body.replace("350 ml", ""))
        assert draft.call_to_action == "Learn More"
        assert set(draft.hashtags) <= {"#HandmadeCeramicMugs", "#Handmade", "#ceramic", "#mugs"}
        assert draft.hashtags


def test_fallback_never_uses_placeholder_defaults():
    campaign = Campaign(**CampaignCreate(platforms=[Platform.META]).model_dump())  # all defaults
    drafts = CopywriterAgent().generate(campaign, ContentGenerateRequest(campaign_id="c", count_per_platform=1))
    body = drafts[0].body
    assert DEFAULT_DESCRIPTION not in body
    assert "Product showcase" not in body and "Global Growth Campaign" not in body
    assert body == "Learn more."
    assert drafts[0].hashtags == []


def test_fallback_x_post_stays_within_the_limit():
    campaign = Campaign(**CampaignCreate(platforms=[Platform.X], product_description="d" * 400).model_dump())
    draft = CopywriterAgent().generate(campaign, ContentGenerateRequest(campaign_id="c", topic="Desk lamp", count_per_platform=1))[0]
    assert len(draft.body) <= 240 and draft.body.startswith("Desk lamp — ") and draft.body.endswith("Learn more.")


def test_fallback_chinese_copy_is_neutral():
    campaign = Campaign(**CampaignCreate(platforms=[Platform.XIAOHONGSHU, Platform.WECHAT], languages=["zh-CN"]).model_dump())
    drafts = CopywriterAgent().generate(campaign, ContentGenerateRequest(campaign_id="c", topic="手工陶瓷杯", count_per_platform=1))
    for draft in drafts:
        assert draft.body == "手工陶瓷杯\n\n了解更多。"
        assert draft.call_to_action == "了解更多"
        assert draft.hashtags == ["#手工陶瓷杯"]
        _assert_no_fabrication(draft.body)


def test_vision_fallback_is_honest_without_an_llm():
    agent = CopywriterAgent()
    assert agent.client is None
    res = agent.generate_with_vision("Handmade ceramic mugs", campaign=_campaign())
    assert res["copy"] == "Handmade ceramic mugs\n\nLearn more."
    assert res["hashtags"] == "#HandmadeCeramicMugs #Handmade #ceramic #mugs"
    meta, tiktok = res["drafts"]
    assert meta.metadata["status"] == tiktok.metadata["status"] == "fallback"
    assert tiktok.body == "Handmade ceramic mugs\n\nLearn more."
    assert meta.call_to_action == tiktok.call_to_action == "Learn More"
    for draft in res["drafts"]:
        _assert_no_fabrication(draft.body)


def test_vision_fallback_after_bad_json_is_honest_too(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    sdk = FakeSDK([_completion("definitely not json")])
    monkeypatch.setattr("agents.llm_client.LLMClient.__init__", _patched_init(sdk))
    res = CopywriterAgent().generate_with_vision("Lamp", campaign=_campaign())
    assert res["drafts"][0].metadata["status"] == "fallback"
    assert res["copy"] == "Lamp\n\nLearn more."
    _assert_no_fabrication(res["copy"])
