from __future__ import annotations

from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services.ai.core.messages import ChatRequest, LLMMessage
from zhenxun.services.ai.core.models import ModelCapabilities, ModelIdentity
from zhenxun.services.ai.core.options import GenerationConfig, ReasoningEffort
from zhenxun.services.ai.llm.adapters.gemini import GeminiAdapter
from zhenxun.services.ai.llm.system.capabilities import get_model_capabilities


def _patch_llm_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "zhenxun.services.ai.config.get_llm_config",
        lambda: SimpleNamespace(
            provider_settings=SimpleNamespace(
                gemini=SimpleNamespace(allow_mixed_tools=False)
            )
        ),
    )


@pytest.mark.asyncio
async def test_gemini_adapter_merges_multiple_system_messages_in_order(monkeypatch):
    _patch_llm_config(monkeypatch)
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


@pytest.mark.asyncio
async def test_gemini_adapter_enum_effort_sends_valid_thinking_level(monkeypatch):
    """LEVEL 模式模型收到 ReasoningEffort 枚举时必须下发合法 thinkingLevel。

    回归：原先用 str(枚举).lower() 取值得到 "reasoningeffort.high"，被 Gemini
    当作非法 thinking_level 直接拒绝（400）。
    """
    _patch_llm_config(monkeypatch)
    adapter = GeminiAdapter()
    generation_config = GenerationConfig()
    generation_config.common.reasoning_effort = ReasoningEffort.HIGH
    identity = ModelIdentity(
        provider_name="Gemini",
        model_name="gemini-3.6-flash",
        api_type="gemini",
        api_base="https://generativelanguage.googleapis.com",
        path_prefix=None,
        capabilities=get_model_capabilities("gemini-3.6-flash"),
        generation_config=generation_config,
    )

    request = await adapter.prepare_advanced_request(
        identity=identity,
        api_key="test-key",
        request=ChatRequest(messages=[LLMMessage.user("总结一下这段聊天")]),
    )

    thinking_config = request.body["generationConfig"]["thinkingConfig"]
    assert thinking_config["thinkingLevel"] == "high"
    assert "reasoningeffort" not in str(thinking_config).lower()
