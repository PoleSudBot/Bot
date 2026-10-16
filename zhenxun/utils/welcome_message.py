from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nonebot_plugin_alconna import UniMessage


def _load_message_types():
    from nonebot_plugin_alconna import At, UniMessage

    return At, UniMessage


def strip_welcome_command(message: UniMessage, command: str) -> UniMessage:
    """从欢迎消息的文本段中移除设置命令。"""
    for segment in message:
        segment_type = getattr(segment, "type", segment.__class__.__name__.lower())
        if segment_type.lstrip("_") == "text" and command in segment.text:
            segment.text = segment.text.replace(command, "", 1).strip()
            break
    return message


def sanitize_welcome_message_data(data: list[dict]) -> list[dict]:
    """过滤不能跨事件长期复用的欢迎消息段。"""
    return [
        segment.copy()
        for segment in data
        if segment.get("type") != "reply"
        and not (
            segment.get("type") == "text" and not str(segment.get("text", "")).strip()
        )
    ]


def load_welcome_message(
    data: list[dict], *, user_id: str | None = None, at_user: bool = False
) -> UniMessage:
    """从持久化数据构建可安全重复发送的欢迎消息。"""
    At, UniMessage = _load_message_types()
    message = UniMessage().load(sanitize_welcome_message_data(data))
    if at_user and user_id:
        message.insert(0, At("user", user_id))
    return message


def select_welcome_message(data: dict, user_id: str) -> UniMessage:
    """随机选择一条启用的欢迎消息并构建发送内容。"""
    key = random.choice([key for key in data if data[key]["status"]])
    welcome_data = data[key]
    return load_welcome_message(
        welcome_data["message"],
        user_id=user_id,
        at_user=welcome_data["at"],
    )
