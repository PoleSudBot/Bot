from __future__ import annotations

from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services.ai.config import manager as config_manager
from zhenxun.services.ai.config.models import LLMConfig, ProviderConfig
from zhenxun.services.ai.context.memory.builder import MemoryBuilder
from zhenxun.services.ai.context.rag.backends import DictStorageBackend
from zhenxun.services.ai.context.rag.builder import RAGBuilder
from zhenxun.services.ai.core.exceptions import SandboxFatalError
from zhenxun.services.ai.core.models import (
    ModelCapabilities,
    ModelDetail,
    ModelIdentity,
)
from zhenxun.services.ai.llm.adapters.factory import (
    LLMAdapterFactory,
    SmartAdapter,
    get_adapter_for_api_type,
)
from zhenxun.services.ai.llm.builder import IntentBuilder
from zhenxun.services.ai.sandbox import manager as sandbox_manager


def test_provider_config_keeps_local_transport_extensions():
    provider = ProviderConfig(
        name="Gateway",
        api_key="test-key",
        api_type="openai",
        proxy="http://127.0.0.1:7890",
        extra_headers={"X-Gateway": "local"},
        models=[
            ModelDetail(
                model_name="custom-model",
                api_type="gemini",
                endpoint="/custom-endpoint",
            )
        ],
    )

    assert provider.proxy == "http://127.0.0.1:7890"
    assert provider.extra_headers == {"X-Gateway": "local"}
    assert provider.models[0].api_type == "gemini"
    assert provider.models[0].endpoint == "/custom-endpoint"


@pytest.mark.parametrize(
    ("legacy_api_type", "expected_api_type"),
    [("zhipu", "glm"), ("ark", "doubao"), ("openai_image", "openai")],
)
def test_legacy_api_type_aliases(
    monkeypatch: pytest.MonkeyPatch,
    legacy_api_type: str,
    expected_api_type: str,
):
    captured: list[str] = []
    sentinel = object()

    monkeypatch.setattr(
        LLMAdapterFactory,
        "get_adapter",
        lambda api_type: captured.append(api_type) or sentinel,
    )

    assert get_adapter_for_api_type(legacy_api_type) is sentinel
    assert captured == [expected_api_type]


def test_legacy_default_model_name_fills_missing_chat_model(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        config_manager,
        "get_ai_config",
        lambda: {
            "default_model_name": "Legacy/legacy-chat",
            "default_models": {"embedding": "Embed/embed-v1"},
        },
    )
    config_manager.get_llm_config.cache_clear()
    try:
        config = config_manager.get_llm_config()
    finally:
        config_manager.get_llm_config.cache_clear()

    assert config.default_models.chat == "Legacy/legacy-chat"
    assert config.default_models.embedding == "Embed/embed-v1"


def test_sandbox_is_disabled_by_default():
    assert LLMConfig().sandbox.enable_sandbox is False


def test_smart_adapter_routes_and_caches_by_model_name(monkeypatch: pytest.MonkeyPatch):
    routed: list[str] = []
    adapters = {}

    def fake_get_adapter(api_type: str):
        routed.append(api_type)
        return adapters.setdefault(api_type, object())

    monkeypatch.setattr(
        "zhenxun.services.ai.llm.adapters.factory.get_adapter_for_api_type",
        fake_get_adapter,
    )
    identity = ModelIdentity(
        provider_name="Gateway",
        model_name="gemini-3-flash",
        api_type="smart",
        api_base=None,
        path_prefix=None,
        capabilities=ModelCapabilities(),
        generation_config=None,
    )
    adapter = SmartAdapter()

    assert (
        adapter._get_delegate_adapter(identity)
        is adapter._get_delegate_adapter(identity)
    )
    assert routed == ["gemini"]


def test_intent_and_context_builders_preserve_structured_and_isolated_settings():
    config = (
        IntentBuilder()
        .with_reasoning("high")
        .require_structured_output({"type": "object"})
        .build()
    )
    memory = MemoryBuilder.resolve(True)
    rag = (
        RAGBuilder(storage=DictStorageBackend())
        .with_scope(["/group-a", "/user-a"])
        .build()
    )

    assert config.common.reasoning_effort == "high"
    assert config.output.response_schema == {"type": "object"}
    assert memory.short_term.enable is True
    assert rag.scopes == ["/group-a", "/user-a"]


@pytest.mark.asyncio
async def test_disabled_sandbox_does_not_create_driver(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sandbox_manager,
        "get_llm_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(enable_sandbox=False)),
    )

    manager = sandbox_manager.SandboxManager()
    with pytest.raises(SandboxFatalError, match="enable_sandbox=False"):
        await manager.get_or_create_session("disabled-test")
