from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import aiofiles
import markdown
from nonebot.adapters.onebot.v11 import Bot
from nonebot_plugin_alconna.uniseg import MsgTarget, UniMessage

from zhenxun.services.ai import LLMMessage, generate
from zhenxun.services.ai.core.exceptions import LLMException
from zhenxun.services.ai.core.options import GenerationConfig, ReasoningEffort
from zhenxun.services.ai.llm.manager import (
    get_default_model,
    get_model_instance,
    list_available_models,
)
from zhenxun.services.log import logger

from .. import base_config
from ..store import store
from .core import ErrorCode, SummaryException
from .message_processing import serialize_messages_for_summary

md_to_pic, html_to_pic = None, None
if base_config.get("summary_output_type") == "image":
    try:
        from nonebot import require

        require("nonebot_plugin_htmlrender")
        from nonebot_plugin_htmlrender import html_to_pic
    except Exception as e:
        logger.warning(f"加载 htmlrender 失败，图片模式不可用: {e}")


@dataclass(frozen=True)
class SummaryGenerationResult:
    summary_text: str
    resolved_model_name: str | None


SUMMARY_THINKING_MODES = {"off", "low", "medium", "high"}


def resolve_summary_model_name(
    group_id: str | None,
    model_name: str | None = None,
) -> str | None:
    if model_name:
        return model_name

    if group_id:
        group_model_name = store.get_group_setting(str(group_id), "default_model_name")
        if group_model_name:
            logger.debug(f"群聊 {group_id} 使用特定模型: {group_model_name}")
            return group_model_name

    plugin_default_model = base_config.get("SUMMARY_MODEL_NAME")
    if plugin_default_model:
        logger.debug(f"使用插件默认模型: {plugin_default_model}")
        return plugin_default_model

    global_default_model = get_default_model("chat")
    if global_default_model:
        logger.debug(f"使用LLM全局默认模型: {global_default_model}")
        return global_default_model

    available_models = list_available_models()
    if available_models:
        fallback_model = available_models[0]["full_name"]
        logger.warning(f"未显式配置总结模型，回退到首个可用模型: {fallback_model}")
        return fallback_model
    return None


def _get_summary_thinking_mode() -> str:
    """读取总结任务的思考模式配置，未知值回退到默认关闭。"""
    raw_mode = base_config.get("SUMMARY_THINKING_MODE", "off")
    mode = str(raw_mode or "off").strip().lower()
    if mode in SUMMARY_THINKING_MODES:
        return mode
    logger.warning(
        f"SUMMARY_THINKING_MODE={raw_mode!r} 无效，已回退为 off",
        command="messages_summary",
    )
    return "off"


def _looks_like_gemini(model: Any, resolved_model_name: str | None) -> bool:
    """判断当前模型是否走 Gemini 协议或 Gemini 命名。"""
    values = [
        getattr(model, "api_type", None),
        getattr(model, "provider_name", None),
        getattr(model, "model_name", None),
        resolved_model_name,
    ]
    return any("gemini" in str(value).lower() for value in values if value)


def _looks_like_deepseek(model: Any, resolved_model_name: str | None) -> bool:
    """判断当前模型是否为 DeepSeek，即使配置成 OpenAI 兼容协议也能识别。"""
    values = [
        getattr(model, "api_type", None),
        getattr(model, "provider_name", None),
        getattr(model, "model_name", None),
        resolved_model_name,
    ]
    return any("deepseek" in str(value).lower() for value in values if value)


def _build_summary_generation_config(
    model: Any, resolved_model_name: str | None
) -> GenerationConfig:
    """为总结任务构造只返回最终答案的模型配置。"""
    mode = _get_summary_thinking_mode()
    config = GenerationConfig()

    # Gemini 的思考摘要由 includeThoughts 控制；总结场景只需要最终答案。
    if _looks_like_gemini(model, resolved_model_name):
        config.common.reasoning_effort = (
            ReasoningEffort.NONE
            if mode == "off"
            else ReasoningEffort(mode.upper())
        )
        config.gemini_options.include_thoughts = False
        return config

    # DeepSeek V4 默认启用 thinking；总结默认显式关闭，避免 reasoning_content 泄漏。
    if _looks_like_deepseek(model, resolved_model_name):
        thinking_type = "disabled" if mode == "off" else "enabled"
        config.custom_kwargs = {"thinking": {"type": thinking_type}}
        return config

    return config


def _strip_visible_thinking(summary_text: str) -> str:
    """兜底移除可见思考标签，正常路径应依赖 provider 原生字段隔离。"""
    if not base_config.get("SUMMARY_STRIP_THINKING_FALLBACK", True):
        return summary_text.strip()

    cleaned = re.sub(
        r"<\s*think\b[^>]*>.*?<\s*/\s*think\s*>",
        "",
        summary_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<\s*/?\s*think\b[^>]*>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


async def messages_summary(
    target: MsgTarget,
    messages: list,
    content: str | None = None,
    target_user_names: list[str] | None = None,
    style: str | None = None,
    model_name: str | None = None,
) -> SummaryGenerationResult:
    if not messages:
        logger.warning("没有足够的聊天记录可供总结", command="messages_summary")
        return SummaryGenerationResult(
            summary_text="没有足够的聊天记录可供总结。",
            resolved_model_name=model_name,
        )

    prompt_parts = []
    group_id = target.id if not target.private else None

    final_style = style
    if not final_style and group_id:
        final_style = store.get_group_setting(str(group_id), "default_style")
        if final_style:
            logger.debug(f"群聊 {group_id} 使用特定默认风格: '{final_style}'")
    if not final_style:
        final_style = base_config.get("SUMMARY_DEFAULT_STYLE")
        if final_style:
            logger.debug(f"使用插件全局默认风格: '{final_style}'")

    if final_style:
        prompt_parts.append(f"重要指令：请严格使用 '{final_style}' 的风格进行总结。")

    if target_user_names:
        user_list_str = ", ".join(target_user_names)
        task_desc = f"任务：在以下聊天记录中，详细总结用户 [{user_list_str}] "
        if content:
            task_desc += f"仅与'{content}'相关的发言内容和主要观点。"
        else:
            task_desc += "的所有发言内容和主要观点。"
        prompt_parts.append(task_desc)
        if len(target_user_names) > 1:
            prompt_parts.append(
                f"请注意：这里有 {len(target_user_names)} 个不同的用户，"
                "必须分别对每个用户的发言进行单独总结。"
            )
    elif content:
        prompt_parts.append(f"任务：请详细总结以下对话中仅与'{content}'相关的内容。")
    else:
        prompt_parts.append("任务：请分析并总结以下聊天记录的主要讨论内容和信息脉络。")

    prompt_parts.append(
        "要求：排版需层次清晰，用中文回答，请包含谁说了什么重要内容。\n"
        "只输出最终总结，不要输出思考过程、分析草稿或任何思考标签。\n"
        "注意使用丰富的markdown格式让内容更美观，注意要在合适的场景使用合适的样式,包括："
        "标题层级(h1-h6),分隔线(hr)、表格(table)、斜体(em)、"
        "任务列表(chekbox)、删除线 (Strikethrough)、"
        "emoji增强格式(emoji-enhanced formatting)等。\n"
        "避免使用graph td样式"
    )
    final_prompt = "\n\n".join(prompt_parts)

    llm_messages: list[LLMMessage] = []

    llm_messages.append(LLMMessage.system(final_prompt))

    user_content = serialize_messages_for_summary(messages)
    llm_messages.append(LLMMessage.user(user_content))

    final_model_name_str = resolve_summary_model_name(
        str(group_id) if group_id else None,
        model_name=model_name,
    )

    try:
        logger.info(
            f"开始调用LLM服务进行总结，模型: {final_model_name_str or 'LLM全局默认'}"
        )

        model = await get_model_instance(final_model_name_str)
        from ..config import summary_config

        config = _build_summary_generation_config(model, final_model_name_str)
        response = await generate(
            llm_messages,
            model=final_model_name_str,
            config=config,
            timeout=summary_config.get_timeout(),
        )
        summary_text = response.text

        summary_text = _strip_visible_thinking(summary_text)

        return SummaryGenerationResult(
            summary_text=summary_text,
            resolved_model_name=final_model_name_str,
        )
    except LLMException as e:
        logger.error(
            f"总结生成失败 (LLMException): {e}", command="messages_summary", e=e
        )
        raise
    except Exception as e:
        logger.error(
            f"总结生成过程中出现意外错误: {e}",
            command="messages_summary",
            e=e,
        )
        raise LLMException(f"总结生成失败: {e!s}") from e


async def generate_image(
    summary: str, user_info_cache: dict[str, str] | None = None
) -> bytes:
    if html_to_pic is None:
        raise ValueError("图片生成功能未启用或 htmlrender 未正确加载")
    try:
        html_from_md = markdown.markdown(
            summary,
            extensions=[
                "pymdownx.tasklist",
                "tables",
                "fenced_code",
                "codehilite",
                "mdx_math",
                "pymdownx.tilde",
            ],
            extension_configs={"mdx_math": {"enable_dollar_delimiter": True}},
        )

        enhanced_html = html_from_md
        if user_info_cache:
            from .message_processing import avatar_enhancer

            use_avatars = base_config.get("ENABLE_AVATAR_ENHANCEMENT", False)

            await avatar_enhancer.enhance_summary_with_avatars(summary, user_info_cache)

            if not use_avatars:
                enhanced_html = avatar_enhancer.enhance_html_with_markup(
                    html_from_md, user_info_cache, mode="mention"
                )
            else:
                enhanced_html = avatar_enhancer.enhance_html_with_markup(
                    html_from_md, user_info_cache, mode="avatar"
                )
        logger.debug(f"final_html: {enhanced_html}")
        css_file = "dark.css"
        theme = base_config.get("summary_theme")
        if theme == "light":
            css_file = "light.css"
        elif theme == "dark":
            css_file = "dark.css"
        elif theme == "cyber":
            css_file = "cyber.css"

        css_path = (Path(__file__).parent.parent / "assert" / css_file).resolve()

        async with aiofiles.open(css_path, encoding="utf-8") as f:
            css_content = await f.read()

        final_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta charset="utf-8">
            <style type="text/css">
                {css_content}
            </style>
            <style>
                .markdown-body {{
                    box-sizing: border-box;
                    min-width: 200px;
                    max-width: 980px;
                    margin: 0 auto;
                    padding: 45px;
                }}
                @media (max-width: 767px) {{
                    .markdown-body {{
                        padding: 15px;
                    }}
                }}
            </style>
        </head>
        <body>
            <article class="markdown-body">
                {enhanced_html}
            </article>
        </body>
        </html>
        """

        logger.debug(f"使用主题 {theme or '默认'} 生成图片", command="图片生成")

        img = await html_to_pic(
            html=final_html,
            viewport={"width": 850, "height": 10},
        )
        return img
    except Exception as e:
        logger.error(f"生成图片过程中发生意外错误: {e}", command="图片生成", e=e)
        raise SummaryException(
            f"图片生成失败: {e!s}", code=ErrorCode.IMAGE_GENERATION_FAILED
        ) from e


async def send_summary(
    bot: Bot,
    target: MsgTarget,
    summary: str,
    user_info_cache: dict[str, str] | None = None,
    group_id: int | None = None,
    model_name: str | None = None,
    reply_to_message_id: str | None = None,
) -> bool:
    try:
        reply_msg = None
        output_type = base_config.get("summary_output_type", "image")
        fallback_enabled = base_config.get("summary_fallback_enabled", False)

        if output_type == "image":
            try:
                img_bytes = await generate_image(summary, user_info_cache)

                # 持久化保存图片
                if img_bytes and group_id:
                    await _save_summary_image(img_bytes, group_id)

                reply_msg = UniMessage.image(raw=img_bytes)
                if model_name:
                    reply_msg += (
                        "\n"
                        f"本次总结由【{model_name}】生成，"
                        "准确度会随上下文增加而降低，仅供参考"
                    )
            except (SummaryException, ValueError) as e:
                if not fallback_enabled:
                    logger.error(
                        f"图片生成失败且未启用文本回退: {e}",
                        command="send_summary",
                        e=e,
                    )
                    return False

                logger.warning(
                    f"图片生成失败，已启用文本回退: {e}",
                    command="send_summary",
                )

        if reply_msg is None:
            error_prefix = ""
            if output_type == "image" and fallback_enabled:
                error_prefix = "⚠️ 图片生成失败，降级为文本输出：\n\n"

            plain_summary = summary.strip()

            if "<" in plain_summary and ">" in plain_summary:
                import re

                plain_summary = re.sub(r"<[^>]+>", "", plain_summary)

            max_text_length = 4500
            full_text = f"{error_prefix}{plain_summary}"

            if len(full_text) > max_text_length:
                full_text = full_text[:max_text_length] + "...(内容过长已截断)"
            reply_msg = UniMessage.text(full_text)

        if reply_msg:
            # 手动总结会显式透传触发消息 ID，定时总结则保持普通发送。
            await reply_msg.send(
                target,
                bot,
                reply_to=reply_to_message_id or False,
            )

            logger.info(
                f"总结已发送，类型: {output_type or 'text'}",
                command="send_summary",
            )
            return True

        logger.error(
            "无法发送总结：回复消息为空",
            command="send_summary",
        )
        return False

    except Exception as e:
        logger.error(
            f"发送总结失败: {e}",
            command="send_summary",
            e=e,
        )
        return False


async def _save_summary_image(img_bytes: bytes, group_id: int) -> None:
    """持久化保存总结图片到 data/summary_group/"""
    try:
        from zhenxun.configs.path_config import DATA_PATH

        save_dir = DATA_PATH / "summary_group"
        save_dir.mkdir(parents=True, exist_ok=True)

        import time as _time

        ts = int(_time.time())
        filename = f"summary_{group_id}_{ts}.png"
        filepath = save_dir / filename

        async with aiofiles.open(filepath, "wb") as f:
            await f.write(img_bytes)

        logger.info(
            f"总结图片已保存: {filepath}",
            command="send_summary",
        )
    except Exception as e:
        logger.warning(
            f"保存总结图片失败: {e}",
            command="send_summary",
        )


async def read_tpl(path: str) -> str:
    from nonebot_plugin_htmlrender.data_source import TEMPLATES_PATH

    async with aiofiles.open(f"{TEMPLATES_PATH}/{path}") as f:
        return await f.read()
