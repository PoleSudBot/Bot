from __future__ import annotations

import importlib

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services.ai.core.exceptions import (
    AuthenticationException,
    ContentFilteredException,
    UpstreamServerException,
)
from zhenxun.services.ai.core.messages import ToolCallPart
from zhenxun.services.llm import (
    LLMErrorCode,
    LLMException,
    ToolInvoker,
    create_multimodal_message,
)
from zhenxun.services.llm.config.generation import (
    CoreConfig,
    LLMGenerationConfig,
    ReasoningConfig,
    ReasoningEffort,
)


def test_tarot_generation_config_contract():
    config = LLMGenerationConfig(
        core=CoreConfig(),
        reasoning=ReasoningConfig(
            effort=ReasoningEffort.HIGH,
            show_thoughts=False,
        ),
    )

    assert config.common.reasoning_effort == ReasoningEffort.HIGH
    assert config.gemini_options.include_thoughts is False


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (ContentFilteredException("filtered"), LLMErrorCode.CONTENT_FILTERED),
        (AuthenticationException("invalid key"), LLMErrorCode.API_KEY_INVALID),
        (UpstreamServerException("bad response"), LLMErrorCode.API_RESPONSE_INVALID),
        (LLMException("unknown"), LLMErrorCode.API_REQUEST_FAILED),
    ],
)
def test_legacy_exception_codes(exception: LLMException, expected_code: LLMErrorCode):
    assert exception.code == expected_code


@pytest.mark.parametrize(
    "module_name",
    [
        "zhenxun.services.llm.types",
        "zhenxun.services.llm.types.exceptions",
        "zhenxun.services.llm.config",
        "zhenxun.services.llm.config.generation",
        "zhenxun.services.llm.manager",
        "zhenxun.services.llm.tools",
        "zhenxun.services.llm.adapters.gemini",
    ],
)
def test_legacy_submodule_imports(module_name: str):
    assert importlib.import_module(module_name) is not None


def test_tool_call_exposes_legacy_function_shape():
    tool_call = ToolCallPart(id="call-1", tool_name="lookup", args={"query": "x"})

    assert tool_call.function.name == "lookup"
    assert tool_call.function.arguments == '{"query": "x"}'


@pytest.mark.asyncio
async def test_tool_invoker_executes_legacy_tool_call():
    class Executable:
        async def execute(self, *, context=None, query: str):
            return {"context": context, "query": query}

    tool_call = ToolCallPart(id="call-1", tool_name="lookup", args={"query": "x"})
    returned_call, result = await ToolInvoker().execute_tool_call(
        tool_call,
        {"lookup": Executable()},
        context="ctx",
    )

    assert returned_call is tool_call
    assert result.output == {"context": "ctx", "query": "x"}


def test_create_multimodal_message_keeps_text_and_images():
    message = create_multimodal_message(
        "describe",
        [b"png", "https://example.com/image.png"],
    )

    assert message.extract_text == "describe"
    assert [part.type for part in message.content] == ["text", "image", "image"]
