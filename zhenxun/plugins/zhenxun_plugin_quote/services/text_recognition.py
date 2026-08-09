from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from nonebot_plugin_alconna.uniseg import Image, Text, UniMessage
from pydantic import BaseModel, Field

from zhenxun.configs.config import Config
from zhenxun.services.ai import generate_structured
from zhenxun.services.ai.core.exceptions import LLMException
from zhenxun.services.log import logger

from .paddleocr_api import PaddleOCRAPIClient, PaddleOCRAPISettings

RecognitionProvider = Literal["llm", "paddleocr_api"]


class RecognitionStatus(Enum):
    TEXT = "text"
    NO_TEXT = "no_text"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    status: RecognitionStatus
    text: str = ""


class _LLMOCRResult(BaseModel):
    has_text: bool = Field(description="图片中是否包含可识别的文字")
    recognized_text: str = Field(
        description="识别出的所有文字内容。如果无文字，则为空字符串"
    )


class TextRecognitionService:
    """为语录图片选择视觉模型或 PaddleOCR API。"""

    _DEFAULT_API_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    _DEFAULT_API_MODEL = "PaddleOCR-VL-1.6"
    _DEFAULT_API_POLL_INTERVAL_SECONDS = 5.0
    _DEFAULT_API_TIMEOUT_SECONDS = 180.0
    _VALID_PRIORITIES: ClassVar[set[str]] = {"llm", "paddleocr_api"}

    @classmethod
    async def recognize_single(cls, image_path: str | Path) -> str:
        priority = cls._get_priority("TEXT_RECOGNITION_PRIORITY", "llm")
        return await cls._recognize(image_path, priority)

    @classmethod
    async def recognize_batch(cls, image_path: str | Path) -> str:
        priority = cls._get_priority(
            "BATCH_TEXT_RECOGNITION_PRIORITY",
            "paddleocr_api",
        )
        return await cls._recognize(image_path, priority)

    @classmethod
    def _get_priority(
        cls,
        config_key: str,
        default: RecognitionProvider,
    ) -> RecognitionProvider:
        value = str(Config.get_config("quote", config_key, default) or default)
        normalized = value.strip().lower()
        if normalized not in cls._VALID_PRIORITIES:
            logger.warning(
                f"无效的文字识别优先级 {config_key}={value}，使用默认值 {default}",
                "群聊语录",
            )
            return default
        return cast(RecognitionProvider, normalized)

    @classmethod
    async def _recognize(
        cls,
        image_path: str | Path,
        priority: RecognitionProvider,
    ) -> str:
        providers: tuple[RecognitionProvider, RecognitionProvider] = (
            ("llm", "paddleocr_api") if priority == "llm" else ("paddleocr_api", "llm")
        )
        for provider in providers:
            result = (
                await cls._recognize_with_llm(image_path)
                if provider == "llm"
                else await cls._recognize_with_api(image_path)
            )
            if result.status is RecognitionStatus.TEXT:
                return result.text
            if provider == "llm" and result.status is RecognitionStatus.NO_TEXT:
                return ""
        return ""

    @classmethod
    async def _recognize_with_llm(
        cls,
        image_path: str | Path,
    ) -> RecognitionResult:
        quote_config = Config.get("quote") or {}
        if not quote_config.get("AI_ENABLED", False):
            logger.debug("视觉模型未启用，跳过文字识别", "群聊语录")
            return RecognitionResult(RecognitionStatus.UNAVAILABLE)

        model_name = quote_config.get("OCR_AI_MODEL")
        if not model_name:
            logger.warning("未配置 OCR_AI_MODEL，跳过视觉模型", "群聊语录")
            return RecognitionResult(RecognitionStatus.UNAVAILABLE)

        prompt = (
            "你是一个顶级的图像文字识别（OCR）引擎。"
            "请仔细分析这张图片，提取其中所有的文字内容。"
        )
        message = UniMessage([Text(prompt), Image(path=Path(image_path))])
        try:
            result = await generate_structured(
                message=message,
                model=model_name,
                instruction=(
                    "你是一位专业的AI分析助手。请深入、全面地分析用户提供的"
                    "所有内容（包括文本、图片、文件等），并给出结论。"
                ),
                response_model=_LLMOCRResult,
            )
        except LLMException as e:
            logger.error(
                f"视觉模型文字识别失败: {e.user_friendly_message}",
                "群聊语录",
                e=e,
            )
            return RecognitionResult(RecognitionStatus.FAILED)
        except Exception as e:
            logger.error(f"视觉模型文字识别发生异常: {e}", "群聊语录", e=e)
            return RecognitionResult(RecognitionStatus.FAILED)

        text = result.recognized_text.strip()
        if result.has_text and text:
            return RecognitionResult(RecognitionStatus.TEXT, text)
        return RecognitionResult(RecognitionStatus.NO_TEXT)

    @classmethod
    async def _recognize_with_api(
        cls,
        image_path: str | Path,
    ) -> RecognitionResult:
        client = cls._build_api_client()
        if not client.is_configured:
            logger.debug("未配置 PaddleOCR API，跳过文字识别", "群聊语录")
            return RecognitionResult(RecognitionStatus.UNAVAILABLE)
        try:
            text = (await client.recognize(image_path)).strip()
        except Exception as e:
            logger.error(f"PaddleOCR API 文字识别失败: {e}", "群聊语录", e=e)
            return RecognitionResult(RecognitionStatus.FAILED)
        if text:
            return RecognitionResult(RecognitionStatus.TEXT, text)
        return RecognitionResult(RecognitionStatus.NO_TEXT)

    @classmethod
    def _build_api_client(cls) -> PaddleOCRAPIClient:
        quote_config = Config.get("quote") or {}
        return PaddleOCRAPIClient(
            PaddleOCRAPISettings(
                token=str(quote_config.get("PADDLEOCR_API_TOKEN", "") or ""),
                job_url=str(
                    quote_config.get(
                        "PADDLEOCR_API_JOB_URL",
                        cls._DEFAULT_API_JOB_URL,
                    )
                    or cls._DEFAULT_API_JOB_URL
                ).rstrip("/"),
                model=str(
                    quote_config.get(
                        "PADDLEOCR_API_MODEL",
                        cls._DEFAULT_API_MODEL,
                    )
                    or cls._DEFAULT_API_MODEL
                ),
                poll_interval_seconds=cls._DEFAULT_API_POLL_INTERVAL_SECONDS,
                timeout_seconds=cls._positive_float(
                    quote_config.get("PADDLEOCR_API_TIMEOUT_SECONDS"),
                    cls._DEFAULT_API_TIMEOUT_SECONDS,
                ),
            )
        )

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default
