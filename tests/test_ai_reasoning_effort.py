from __future__ import annotations

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services.ai.core.options import (
    GenerationConfig,
    ReasoningEffort,
    normalize_reasoning_effort,
)
from zhenxun.services.ai.llm.adapters.deepseek import DeepSeekConfigMapper
from zhenxun.services.ai.llm.adapters.handlers.gemini_handlers import (
    GeminiConfigMapper,
)
from zhenxun.services.ai.llm.system.capabilities import get_model_capabilities


def _config_with(effort: ReasoningEffort | str | None) -> GenerationConfig:
    config = GenerationConfig()
    config.common.reasoning_effort = effort
    return config


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (ReasoningEffort.HIGH, "high"),
        (ReasoningEffort.NONE, "none"),
        (ReasoningEffort.MAX, "max"),
        ("HIGH", "high"),
        ("medium", "medium"),
        (None, "none"),
    ],
)
def test_normalize_reasoning_effort_never_leaks_enum_class_name(effort, expected):
    """枚举必须取 value，不能拼出 "reasoningeffort.high" 这类厂商非法值。"""
    assert normalize_reasoning_effort(effort) == expected


@pytest.mark.parametrize("effort", [ReasoningEffort.HIGH, ReasoningEffort.MAX, "high"])
def test_gemini_level_mode_thinking_level_is_valid(effort):
    """gemini-3 系列走 LEVEL 模式，thinkingLevel 必须是能力表能识别的值。"""
    capabilities = get_model_capabilities("gemini-3.6-flash")
    params = GeminiConfigMapper().map_config(_config_with(effort), None, capabilities)

    assert params["thinkingConfig"]["thinkingLevel"] == "high"


def test_gemini_level_mode_none_degrades_without_leaking_enum_class_name():
    """关闭思考时按能力映射降级为 minimal，而不是发出非法字符串。"""
    capabilities = get_model_capabilities("gemini-3.6-flash")
    params = GeminiConfigMapper().map_config(
        _config_with(ReasoningEffort.NONE), None, capabilities
    )

    assert params["thinkingConfig"]["thinkingLevel"] == "minimal"


@pytest.mark.parametrize("effort", [ReasoningEffort.HIGH, "high"])
def test_deepseek_reasoning_effort_is_a_plain_lowercase_value(effort):
    capabilities = get_model_capabilities("deepseek-v4-flash")
    params = DeepSeekConfigMapper().map_config(_config_with(effort), None, capabilities)

    assert params["reasoning_effort"] == "high"


def test_deepseek_none_disables_thinking_instead_of_sending_junk():
    capabilities = get_model_capabilities("deepseek-v4-flash")
    params = DeepSeekConfigMapper().map_config(
        _config_with(ReasoningEffort.NONE), None, capabilities
    )

    assert params["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in params
