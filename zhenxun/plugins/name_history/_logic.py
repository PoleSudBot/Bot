from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal, Protocol

NameType = Literal["group_card", "qq_name"]

GROUP_CARD = "group_card"
QQ_NAME = "qq_name"
GROUP_CARD_EMPTY_LABEL = "未设置群名片"
HISTORY_LIMIT = 50

NAME_TYPE_LABELS: dict[str, str] = {
    GROUP_CARD: "群名片",
    QQ_NAME: "QQ名称",
}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class SenderNameSnapshot:
    group_card: str | None
    qq_name: str | None


@dataclass(slots=True)
class QueryTarget:
    user_id: str
    error: str | None = None


@dataclass(slots=True)
class HistoryDisplayItem:
    time: str
    display_name: str
    is_current: bool
    avatar_uri: str = ""


@dataclass(slots=True)
class HistoryDisplaySection:
    name_type: str
    label: str
    items: list[HistoryDisplayItem]
    truncated: bool = False


@dataclass(slots=True)
class HistoryDisplayData:
    title: str
    target_user_id: str
    sections: list[HistoryDisplaySection]
    profile_name: str
    avatar_history: list[str]
    empty_text: str = "暂无记录。之后我看到名称变化时会慢慢记下来。"


class HistoryRecord(Protocol):
    name_type: str
    display_name: str
    record_time: datetime
    avatar_hash: str | None


class AvatarHistoryRecord(Protocol):
    avatar_hash: str
    avatar_uri: str
    record_time: datetime


class NameHistoryRepository(Protocol):
    async def get_latest_display_name(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
    ) -> str | None:
        """读取指定名称类型的最新记录。"""

    async def create_history(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
        display_name: str,
    ) -> int:
        """写入一次名称变化，并返回新记录主键。"""


def normalize_display_name(value: object) -> str | None:
    """清理消息事件中的名称，避免控制字符和换行破坏历史列表展示。"""
    if value is None:
        return None
    name = _CONTROL_CHAR_RE.sub(" ", str(value))
    name = _WHITESPACE_RE.sub(" ", name).strip()
    return name or None


def extract_sender_snapshot(sender: object) -> SenderNameSnapshot:
    """从 OneBot sender 中拆出群名片和 QQ 名称两个独立来源。"""
    return SenderNameSnapshot(
        group_card=normalize_display_name(getattr(sender, "card", None)),
        qq_name=normalize_display_name(getattr(sender, "nickname", None)),
    )


def extract_member_snapshot(member_info: Mapping[str, object]) -> SenderNameSnapshot:
    """从 OneBot 群成员信息接口结果中提取名称快照。"""
    return SenderNameSnapshot(
        group_card=normalize_display_name(member_info.get("card")),
        qq_name=normalize_display_name(member_info.get("nickname")),
    )


def choose_history_display_name(
    name_type: str,
    current_name: str | None,
    latest_display_name: str | None,
) -> str | None:
    """根据最新记录判断当前名称是否需要新增一条历史。"""
    if name_type == GROUP_CARD:
        if current_name:
            candidate = current_name
        elif latest_display_name is None:
            # 初始状态没有群名片时不记录，避免新群第一次见面就刷出无意义历史。
            return None
        else:
            candidate = GROUP_CARD_EMPTY_LABEL
    elif name_type == QQ_NAME:
        if not current_name:
            return None
        candidate = current_name
    else:
        return None

    return None if candidate == latest_display_name else candidate


async def record_sender_snapshot(
    repository: NameHistoryRepository,
    *,
    platform: str,
    group_id: str,
    user_id: str,
    snapshot: SenderNameSnapshot,
) -> list[int]:
    """按类型分别比较最新名称，只在真正变化时写入历史。"""
    created_ids: list[int] = []
    candidates = (
        (GROUP_CARD, snapshot.group_card),
        (QQ_NAME, snapshot.qq_name),
    )
    for name_type, current_name in candidates:
        latest_display_name = await repository.get_latest_display_name(
            platform=platform,
            group_id=group_id,
            user_id=user_id,
            name_type=name_type,
        )
        display_name = choose_history_display_name(
            name_type, current_name, latest_display_name
        )
        if display_name is None:
            continue
        record_id = await repository.create_history(
            platform=platform,
            group_id=group_id,
            user_id=user_id,
            name_type=name_type,
            display_name=display_name,
        )
        created_ids.append(record_id)
    return created_ids


def build_group_info_defaults(
    snapshot: SenderNameSnapshot, platform: str
) -> dict[str, str | None]:
    """只同步不会覆盖用户自定义昵称的群成员基础信息。"""
    defaults: dict[str, str | None] = {"platform": platform}
    if snapshot.qq_name is not None:
        defaults["user_name"] = snapshot.qq_name
    return defaults


def should_sync_group_info(
    *,
    created_count: int,
    cache_hit: bool,
    member_exists: bool,
) -> bool:
    """名称变化、未同步或成员行被删除时都需要刷新当前成员信息。"""
    return bool(created_count) or not cache_hit or not member_exists


def resolve_query_target(message: object, default_user_id: str) -> QueryTarget:
    """从命令参数中解析查询目标，优先使用 @，其次使用 QQ 号。"""
    for segment in message:
        segment_type = getattr(segment, "type", "")
        segment_data = getattr(segment, "data", {}) or {}
        if segment_type != "at":
            continue
        target = str(segment_data.get("qq") or "").strip()
        if target and target not in {"all", "0"}:
            return QueryTarget(user_id=target)

    plain_text = (
        message.extract_plain_text()
        if hasattr(message, "extract_plain_text")
        else str(message)
    )
    plain_text = str(plain_text).strip()
    if not plain_text:
        return QueryTarget(user_id=default_user_id)

    if plain_text.isdecimal():
        return QueryTarget(user_id=plain_text)
    return QueryTarget(
        user_id=default_user_id,
        error="我没看懂要查谁，可以直接 @ 对方，或者写 QQ 号。",
    )


def normalize_name_type_filter(value: str | None) -> str | None:
    """规范化查询筛选类型，只允许展示层支持的两类名称。"""
    if value in {GROUP_CARD, QQ_NAME}:
        return value
    return None


def _format_record_time(record_time: datetime) -> str:
    """把记录时间压缩成适合聊天窗口阅读的格式。"""
    return record_time.strftime("%m-%d %H:%M")


def _build_title(target_user_id: str, name_type: str | None) -> str:
    """根据筛选类型生成面向用户的标题。"""
    if name_type == GROUP_CARD:
        return f"{target_user_id} 的历史群名片"
    if name_type == QQ_NAME:
        return f"{target_user_id} 的历史QQ名称"
    return f"{target_user_id} 的历史昵称"


def _iter_display_name_types(name_type: str | None) -> tuple[str, ...]:
    """根据筛选条件决定需要展示哪些名称分区。"""
    if name_type == GROUP_CARD:
        return (GROUP_CARD,)
    if name_type == QQ_NAME:
        return (QQ_NAME,)
    return (GROUP_CARD, QQ_NAME)


def _resolve_profile_name(
    sections: Sequence[HistoryDisplaySection],
    target_user_id: str,
) -> str:
    """图片主名称优先显示当前群名片，其次用 QQ 名称，最后回退到 QQ 号。"""
    fallback_name = ""
    for section in sections:
        if not section.items:
            continue
        if section.name_type == GROUP_CARD:
            return section.items[0].display_name
        if section.name_type == QQ_NAME and not fallback_name:
            fallback_name = section.items[0].display_name
    return fallback_name or target_user_id


def _resolve_record_avatar_uri(
    record: HistoryRecord,
    avatar_uri_map: dict[str, str],
    avatar_records: Sequence[AvatarHistoryRecord],
) -> str:
    """优先用精确绑定头像，旧记录则按采样时间找最接近的历史头像。"""
    if record.avatar_hash:
        avatar_uri = avatar_uri_map.get(record.avatar_hash)
        if avatar_uri:
            return avatar_uri
    if not avatar_records:
        return ""

    def _distance_key(avatar_record: AvatarHistoryRecord):
        # 同距时优先更早的头像，避免把“未来才出现的头像”贴回旧昵称。
        diff = avatar_record.record_time - record.record_time
        is_future = diff.total_seconds() > 0
        return (abs(diff), is_future)

    return min(avatar_records, key=_distance_key).avatar_uri


def build_history_display_data(
    records: Sequence[HistoryRecord],
    *,
    target_user_id: str,
    name_type: str | None = None,
    limit: int = HISTORY_LIMIT,
    avatar_uri_map: dict[str, str] | None = None,
    avatar_history: Sequence[str] | None = None,
    avatar_records: Sequence[AvatarHistoryRecord] | None = None,
    is_current_member: bool = True,
) -> HistoryDisplayData:
    """把历史记录整理成图片模板和文本回退共用的分区展示数据。"""
    name_type = normalize_name_type_filter(name_type)
    avatar_uri_map = avatar_uri_map or {}
    avatar_records = avatar_records or []
    title = _build_title(target_user_id, name_type)
    sections: list[HistoryDisplaySection] = []

    for current_type in _iter_display_name_types(name_type):
        section_records = [
            record for record in records if record.name_type == current_type
        ]
        section_records.sort(
            key=lambda record: record.record_time,
            reverse=True,
        )
        items = [
            HistoryDisplayItem(
                time=_format_record_time(record.record_time),
                display_name=record.display_name,
                is_current=is_current_member and index == 0,
                avatar_uri=_resolve_record_avatar_uri(
                    record, avatar_uri_map, avatar_records
                ),
            )
            for index, record in enumerate(section_records[:limit])
        ]
        sections.append(
            HistoryDisplaySection(
                name_type=current_type,
                label=NAME_TYPE_LABELS[current_type],
                items=items,
                truncated=len(section_records) > limit,
            )
        )

    return HistoryDisplayData(
        title=title,
        target_user_id=target_user_id,
        sections=sections,
        profile_name=_resolve_profile_name(sections, target_user_id),
        avatar_history=list(avatar_history or []),
    )


def format_history_records(
    records: Sequence[HistoryRecord],
    *,
    target_user_id: str,
    name_type: str | None = None,
    limit: int = HISTORY_LIMIT,
    is_current_member: bool = True,
) -> str:
    """把分区展示数据格式化为渲染失败时可读的文本回退。"""
    display_data = build_history_display_data(
        records,
        target_user_id=target_user_id,
        name_type=name_type,
        limit=limit,
        is_current_member=is_current_member,
    )
    lines = [display_data.title]
    has_any_item = False
    for section in display_data.sections:
        lines.append("")
        lines.append(section.label)
        if not section.items:
            lines.append(display_data.empty_text)
            continue

        has_any_item = True
        for item in section.items:
            current_mark = "（当前）" if item.is_current else ""
            lines.append(f"{item.time}  「{item.display_name}」{current_mark}")
        if section.truncated:
            lines.append(f"只显示最近 {limit} 条。")

    if not has_any_item and len(display_data.sections) == 1:
        return f"{display_data.title}\n{display_data.empty_text}"
    return "\n".join(lines)
