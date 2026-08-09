"""旧 LLM API 的兼容门面。

新代码应直接使用 :mod:`zhenxun.services.ai`。本模块只为尚未迁移的外部插件
保留稳定导入路径，并把旧配置对象转换为新 AI 框架的数据契约。
"""

from enum import Enum
import importlib
import json
import sys
from types import ModuleType
from typing import Any

from pydantic import BaseModel, ConfigDict

from zhenxun.configs.config import Config
from zhenxun.services.ai.config import (
    DebugLogOptions,
    LLMConfig,
    ProviderConfig,
    get_gemini_safety_threshold,
    get_llm_config,
    register_llm_configs,
)
from zhenxun.services.ai.core import exceptions as ai_exceptions
from zhenxun.services.ai.core.exceptions import (
    AuthenticationException,
    ConfigurationException,
    ContentFilteredException,
    ContextLengthExceededException,
    LLMException,
    LocationNotSupportedException,
    NetworkTimeoutException,
    QuotaExceededException,
    RateLimitException,
    ResponseParseException,
    UpstreamServerException,
    get_user_friendly_error_message,
)
from zhenxun.services.ai.core.messages import (
    ChatResponse as LLMResponse,
)
from zhenxun.services.ai.core.messages import (
    ImagePart,
    LLMContentPart,
    LLMMessage,
    TextPart,
    ToolCallPart,
)
from zhenxun.services.ai.core.models import (
    ModelCapabilities,
    ModelDetail,
    ModelModality,
    ToolDefinition,
)
from zhenxun.services.ai.core.options import (
    CommonLLMConfig,
    GenerationConfig,
    ImageAspectRatio,
    ImageResolution,
    OutputFormatConfig,
    ReasoningEffort,
    ResponseFormat,
    StructuredOutputStrategy,
    ToolCallConfig,
)
from zhenxun.services.ai.core.protocols.tool import ToolExecutable
from zhenxun.services.ai.llm import (
    IntentBuilder,
    chat,
    create_image,
    create_speech,
    embed,
    generate,
    generate_structured,
    rerank,
)
from zhenxun.services.ai.llm.manager import (
    clear_all_cache,
    get_default_model,
    get_key_usage_stats,
    get_model_instance,
    list_available_models,
    list_embedding_models,
    list_model_identifiers,
    reset_key_status,
)
from zhenxun.services.ai.message_builder import MessageBuilder
from zhenxun.services.ai.run import RunContext
from zhenxun.services.ai.tools import tool as function_tool
from zhenxun.services.ai.tools.models import ToolResult
from zhenxun.utils.pydantic_compat import model_dump


class LLMErrorCode(Enum):
    MODEL_INIT_FAILED = 2000
    MODEL_NOT_FOUND = 2001
    API_REQUEST_FAILED = 2002
    API_RESPONSE_INVALID = 2003
    API_KEY_INVALID = 2004
    API_QUOTA_EXCEEDED = 2005
    API_TIMEOUT = 2006
    API_RATE_LIMITED = 2007
    NO_AVAILABLE_KEYS = 2008
    UNKNOWN_API_TYPE = 2009
    CONFIGURATION_ERROR = 2010
    RESPONSE_PARSE_ERROR = 2011
    CONTEXT_LENGTH_EXCEEDED = 2012
    CONTENT_FILTERED = 2013
    USER_LOCATION_NOT_SUPPORTED = 2014
    GENERATION_FAILED = 2015
    EMBEDDING_FAILED = 2016
    INVALID_PARAMETER = 2017


def _legacy_error_code(error: LLMException) -> LLMErrorCode:
    mappings = (
        (ContentFilteredException, LLMErrorCode.CONTENT_FILTERED),
        (ContextLengthExceededException, LLMErrorCode.CONTEXT_LENGTH_EXCEEDED),
        (AuthenticationException, LLMErrorCode.API_KEY_INVALID),
        (QuotaExceededException, LLMErrorCode.API_QUOTA_EXCEEDED),
        (RateLimitException, LLMErrorCode.API_RATE_LIMITED),
        (NetworkTimeoutException, LLMErrorCode.API_TIMEOUT),
        (LocationNotSupportedException, LLMErrorCode.USER_LOCATION_NOT_SUPPORTED),
        (ConfigurationException, LLMErrorCode.CONFIGURATION_ERROR),
        (ResponseParseException, LLMErrorCode.RESPONSE_PARSE_ERROR),
        (UpstreamServerException, LLMErrorCode.API_RESPONSE_INVALID),
    )
    for exception_type, code in mappings:
        if isinstance(error, exception_type):
            return code
    return LLMErrorCode.API_REQUEST_FAILED


if not hasattr(LLMException, "code"):
    setattr(LLMException, "code", property(_legacy_error_code))


class ReasoningConfig(BaseModel):
    effort: ReasoningEffort | None = None
    budget_tokens: int | None = None
    show_thoughts: bool | None = None


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="allow")


CoreConfig = CommonLLMConfig
OutputConfig = OutputFormatConfig


class LLMGenerationConfig(GenerationConfig):
    def __init__(
        self,
        *,
        core: CommonLLMConfig | dict[str, Any] | None = None,
        reasoning: ReasoningConfig | dict[str, Any] | None = None,
        visual: Any | None = None,
        output: OutputFormatConfig | dict[str, Any] | None = None,
        tools: ToolCallConfig | dict[str, Any] | None = None,
        custom_params: dict[str, Any] | None = None,
        **data: Any,
    ) -> None:
        if core is not None:
            data["common"] = model_dump(core) if isinstance(core, BaseModel) else core
        if output is not None:
            data["output"] = (
                model_dump(output) if isinstance(output, BaseModel) else output
            )
        if tools is not None:
            data["tools"] = model_dump(tools) if isinstance(tools, BaseModel) else tools
        if visual is not None:
            data["media"] = (
                model_dump(visual) if isinstance(visual, BaseModel) else visual
            )
        if custom_params is not None:
            data["custom_kwargs"] = custom_params
        super().__init__(**data)
        if reasoning is not None:
            reasoning_config = (
                reasoning
                if isinstance(reasoning, ReasoningConfig)
                else ReasoningConfig(**reasoning)
            )
            self.common.reasoning_effort = reasoning_config.effort
            self.gemini_options.include_thoughts = reasoning_config.show_thoughts
            if reasoning_config.budget_tokens is not None:
                self.custom_kwargs["thinking_budget"] = reasoning_config.budget_tokens


AIConfig = LLMGenerationConfig


class GenConfigBuilder:
    def __init__(self) -> None:
        self._config = LLMGenerationConfig()

    def config_visual(
        self,
        *,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        media_resolution: str | None = None,
        **_: Any,
    ) -> "GenConfigBuilder":
        if aspect_ratio is not None:
            self._config.media.aspect_ratio = aspect_ratio
        if resolution is not None:
            self._config.media.resolution = resolution
        if media_resolution is not None:
            self._config.media.quality = media_resolution.lower()
        return self

    def build(self) -> LLMGenerationConfig:
        return self._config


class CommonOverrides:
    @staticmethod
    def _json_config() -> GenerationConfig:
        return IntentBuilder().with_json_output().build()

    gemini_json = _json_config
    gemini_structured = _json_config

    @staticmethod
    def _default_config() -> GenerationConfig:
        return GenerationConfig()

    gemini_2_5_thinking = _default_config
    gemini_3_thinking = _default_config
    gemini_safe = _default_config
    gemini_code_execution = _default_config
    gemini_grounding = _default_config
    gemini_nano_banana = _default_config
    gemini_high_res = _default_config


class LLMToolFunction(BaseModel):
    name: str
    arguments: str


class LLMToolCall(BaseModel):
    id: str
    function: LLMToolFunction
    thought_signature: str | None = None
    type: str = "function"


@property
def _legacy_function(tool_call: ToolCallPart) -> LLMToolFunction:
    arguments = (
        tool_call.args
        if isinstance(tool_call.args, str)
        else json.dumps(tool_call.args, ensure_ascii=False)
    )
    return LLMToolFunction(name=tool_call.tool_name, arguments=arguments)


if not hasattr(ToolCallPart, "function"):
    setattr(ToolCallPart, "function", _legacy_function)


class ToolInvoker:
    def __init__(self, callbacks: list[Any] | None = None) -> None:
        self.callbacks = callbacks or []

    async def execute_tool_call(
        self,
        tool_call: Any,
        available_tools: dict[str, Any],
        context: Any = None,
    ) -> tuple[Any, ToolResult]:
        function = getattr(tool_call, "function", None)
        tool_name = getattr(function, "name", None) or getattr(
            tool_call, "tool_name", "unknown"
        )
        raw_arguments = getattr(function, "arguments", None) or getattr(
            tool_call, "args", {}
        )
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}
        else:
            arguments = raw_arguments or {}
        executable = available_tools.get(tool_name)
        if executable is None:
            return tool_call, ToolResult(output=f"Error: Tool '{tool_name}' not found.")
        try:
            result = await executable.execute(context=context, **arguments)
            if not isinstance(result, ToolResult):
                result = ToolResult(output=result)
            return tool_call, result
        except Exception as error:
            return tool_call, ToolResult(output=f"System Execution Error: {error}")


def create_multimodal_message(
    text: str | None = None,
    images: list[bytes | str] | None = None,
    **_: Any,
) -> LLMMessage:
    parts: list[Any] = []
    if text:
        parts.append(TextPart(text=text))
    for image in images or []:
        if isinstance(image, bytes):
            parts.append(ImagePart(raw=image, mime_type="image/png"))
        elif image.startswith(("http://", "https://")):
            parts.append(ImagePart(url=image))
        else:
            from pathlib import Path

            parts.append(ImagePart(path=Path(image)))
    return LLMMessage.user(parts)


message_to_unimessage = MessageBuilder.message_to_unimessage
unimsg_to_llm_parts = MessageBuilder.unimsg_to_llm_parts
clear_model_cache = clear_all_cache


def get_global_default_model_name() -> str | None:
    return get_default_model("chat")


def set_global_default_model_name(model_name: str) -> bool:
    if model_name not in {item["full_name"] for item in list_available_models()}:
        return False
    current = Config.get_config("AI", "default_models", {}) or {}
    Config.set_config(
        "AI",
        "default_models",
        {**current, "chat": model_name},
        auto_save=True,
    )
    get_llm_config.cache_clear()
    clear_all_cache()
    return True


class AI:
    def __init__(self, session_id: str | None = None, **_: Any) -> None:
        self.session_id = session_id

    async def generate_internal(self, messages: list[LLMMessage], **kwargs: Any):
        return await generate(messages, **kwargs)

    async def generate_structured(
        self, message: Any, response_model: type, **kwargs: Any
    ):
        if "max_validation_retries" in kwargs:
            kwargs["max_retries"] = kwargs.pop("max_validation_retries")
        for obsolete in (
            "tools",
            "tool_choice",
            "template_vars",
            "validation_callback",
            "auto_thinking",
        ):
            kwargs.pop(obsolete, None)
        return await generate_structured(message, response_model, **kwargs)


def _install_legacy_modules() -> None:
    types_module = ModuleType("zhenxun.services.llm.types")
    for source in (
        importlib.import_module("zhenxun.services.ai.core.messages"),
        importlib.import_module("zhenxun.services.ai.core.models"),
        importlib.import_module("zhenxun.services.ai.core.options"),
        ai_exceptions,
    ):
        for name in dir(source):
            if not name.startswith("_"):
                setattr(types_module, name, getattr(source, name))
    for name, value in {
        "LLMErrorCode": LLMErrorCode,
        "LLMResponse": LLMResponse,
        "LLMToolCall": LLMToolCall,
        "LLMToolFunction": LLMToolFunction,
        "ToolResult": ToolResult,
    }.items():
        setattr(types_module, name, value)

    generation_module = ModuleType("zhenxun.services.llm.config.generation")
    for name, value in {
        "CoreConfig": CoreConfig,
        "GenConfigBuilder": GenConfigBuilder,
        "ImageAspectRatio": ImageAspectRatio,
        "ImageResolution": ImageResolution,
        "LLMGenerationConfig": LLMGenerationConfig,
        "OutputConfig": OutputConfig,
        "ReasoningConfig": ReasoningConfig,
        "ReasoningEffort": ReasoningEffort,
        "ToolConfig": ToolConfig,
    }.items():
        setattr(generation_module, name, value)

    config_module = ModuleType("zhenxun.services.llm.config")
    for name, value in {
        "CommonOverrides": CommonOverrides,
        "DebugLogOptions": DebugLogOptions,
        "GenConfigBuilder": GenConfigBuilder,
        "LLMConfig": LLMConfig,
        "LLMGenerationConfig": LLMGenerationConfig,
        "ProviderConfig": ProviderConfig,
        "get_gemini_safety_threshold": get_gemini_safety_threshold,
        "get_llm_config": get_llm_config,
        "register_llm_configs": register_llm_configs,
    }.items():
        setattr(config_module, name, value)
    config_module.__path__ = []  # type: ignore[attr-defined]

    providers_module = ModuleType("zhenxun.services.llm.config.providers")
    for name in (
        "DebugLogOptions",
        "LLMConfig",
        "ProviderConfig",
        "get_gemini_safety_threshold",
        "get_llm_config",
        "register_llm_configs",
    ):
        setattr(providers_module, name, getattr(config_module, name))

    sys.modules["zhenxun.services.llm.types"] = types_module
    sys.modules["zhenxun.services.llm.types.models"] = types_module
    sys.modules["zhenxun.services.llm.types.exceptions"] = types_module
    sys.modules["zhenxun.services.llm.types.capabilities"] = types_module
    sys.modules["zhenxun.services.llm.config"] = config_module
    sys.modules["zhenxun.services.llm.config.generation"] = generation_module
    sys.modules["zhenxun.services.llm.config.providers"] = providers_module
    sys.modules["zhenxun.services.llm.manager"] = importlib.import_module(
        "zhenxun.services.ai.llm.manager"
    )
    sys.modules["zhenxun.services.llm.tools"] = importlib.import_module(
        "zhenxun.services.ai.tools"
    )
    sys.modules["zhenxun.services.llm.adapters"] = importlib.import_module(
        "zhenxun.services.ai.llm.adapters"
    )
    for module_name in ("factory", "gemini", "openai"):
        sys.modules[f"zhenxun.services.llm.adapters.{module_name}"] = (
            importlib.import_module(f"zhenxun.services.ai.llm.adapters.{module_name}")
        )


_install_legacy_modules()

__all__ = [
    "AI",
    "AIConfig",
    "CommonOverrides",
    "CoreConfig",
    "GenConfigBuilder",
    "LLMContentPart",
    "LLMErrorCode",
    "LLMException",
    "LLMGenerationConfig",
    "LLMMessage",
    "LLMResponse",
    "LLMToolCall",
    "LLMToolFunction",
    "ModelCapabilities",
    "ModelDetail",
    "ModelModality",
    "ReasoningConfig",
    "ReasoningEffort",
    "ResponseFormat",
    "RunContext",
    "StructuredOutputStrategy",
    "ToolDefinition",
    "ToolExecutable",
    "ToolInvoker",
    "ToolResult",
    "chat",
    "clear_model_cache",
    "create_image",
    "create_multimodal_message",
    "create_speech",
    "embed",
    "function_tool",
    "generate",
    "generate_structured",
    "get_global_default_model_name",
    "get_key_usage_stats",
    "get_model_instance",
    "get_user_friendly_error_message",
    "list_available_models",
    "list_embedding_models",
    "list_model_identifiers",
    "message_to_unimessage",
    "rerank",
    "reset_key_status",
    "set_global_default_model_name",
    "unimsg_to_llm_parts",
]
