from __future__ import annotations

from types import SimpleNamespace

import pytest

from zhenxun.services.ai.core.messages import ChatRequest, LLMMessage
from zhenxun.services.ai.core.models import ModelCapabilities, ModelIdentity
from zhenxun.services.ai.llm.adapters.gemini import GeminiAdapter


@pytest.mark.asyncio
async def test_gemini_adapter_merges_multiple_system_messages_in_order(monkeypatch):
    monkeypatch.setattr(
        "zhenxun.services.ai.config.get_llm_config",
        lambda: SimpleNamespace(
            provider_settings=SimpleNamespace(
                gemini=SimpleNamespace(allow_mixed_tools=False)
            )
        ),
    )
    adapter = GeminiAdapter()
    identity = ModelIdentity(
        provider_name="Gemini",
        model_name="gemini-3-flash-preview",
        api_type="gemini",
        api_base="https://generativelanguage.googleapis.com",
        path_prefix=None,
        capabilities=ModelCapabilities(),
        generation_config=None,
    )

    request = await adapter.prepare_advanced_request(
        identity=identity,
        api_key="test-key",
        request=ChatRequest(
            messages=[
                LLMMessage.system("完整 HYW 工具协议"),
                LLMMessage.system("> 现在是第一轮任务"),
                LLMMessage.user("张雪峰 巧乐兹"),
            ]
        ),
    )

    assert request.body["systemInstruction"]["parts"] == [
        {"text": "完整 HYW 工具协议"},
        {"text": "> 现在是第一轮任务"},
    ]
    assert request.body["contents"] == [
        {"role": "user", "parts": [{"text": "张雪峰 巧乐兹"}]}
    ]
