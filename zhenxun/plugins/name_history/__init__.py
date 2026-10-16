from __future__ import annotations

# ruff: noqa: I001

import asyncio
from pathlib import Path

from nonebot import on_command, on_message, on_notice, require
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
)
from nonebot.exception import ActionFailed
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type
from zhenxun.utils.manager.priority_manager import PriorityLifecycle

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import template_to_pic

from zhenxun.configs.utils import Command, PluginExtraData
from zhenxun.models.group_member_info import GroupInfoUser
from zhenxun.models.group_name_history import GroupNameHistory
from zhenxun.services.avatar_service import avatar_service
from zhenxun.services.log import logger
from zhenxun.utils.enum import PluginType
from zhenxun.utils.message import MessageUtils

from ._logic import (
    GROUP_CARD,
    HISTORY_LIMIT,
    QQ_NAME,
    SenderNameSnapshot,
    build_history_display_data,
    build_group_info_defaults,
    extract_member_snapshot,
    extract_sender_snapshot,
    format_history_records,
    normalize_name_type_filter,
    record_sender_snapshot,
    resolve_query_target,
    should_sync_group_info,
)
from ._avatar import (
    AVATAR_HISTORY_LIMIT,
    build_avatar_uri,
    force_refresh_and_bind_avatar,
    get_avatar_history_items,
    import_existing_avatar_cache,
    persist_refreshed_avatar,
    refresh_avatar_history_on_query,
)

MODULE = "name_history"
TEMPLATE_DIR = (Path(__file__).parent / "templates").resolve()
_CACHE_MAX_SIZE = 20000
_HISTORY_ALIASES = {"曾用名", "过去昵称", "nickname"}
_GROUP_CARD_ALIASES = {"群名片历史"}
_QQ_NAME_ALIASES = {"历史qq名", "QQ名历史", "qq名历史"}

_record_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_latest_name_cache: dict[tuple[str, str, str, str], str | None] = {}
_group_info_synced: set[tuple[str, str, str]] = set()
_background_tasks: set[asyncio.Task[None]] = set()

__plugin_meta__ = PluginMetadata(
    name="历史昵称",
    description="查看群成员过去用过的群名片和 QQ 名称",
    usage="""
    历史昵称 ?[@用户|QQ号]      : 查看群名片和 QQ 名称变化
    历史群名片 ?[@用户|QQ号]    : 只查看群名片变化
    历史QQ名 ?[@用户|QQ号]      : 只查看 QQ 名称变化
    """.strip(),
    extra=PluginExtraData(
        author="k1yuyu",
        version="0.3",
        plugin_type=PluginType.NORMAL,
        menu_type="数据统计",
        commands=[
            Command(command="历史昵称 ?[@用户|QQ号]"),
            Command(command="历史群名片 ?[@用户|QQ号]"),
            Command(command="历史QQ名 ?[@用户|QQ号]"),
        ],
        aliases=_HISTORY_ALIASES | _GROUP_CARD_ALIASES | _QQ_NAME_ALIASES,
    ).to_dict(),
)


def _trim_cache(cache: dict | set, *, max_size: int = _CACHE_MAX_SIZE) -> None:
    """按插入顺序丢弃一批旧键，避免被动消息监听让进程缓存无限增长。"""
    overflow = len(cache) - max_size
    if overflow <= 0:
        return
    for key in list(cache)[: overflow + max_size // 10]:
        cache.pop(key, None) if isinstance(cache, dict) else cache.discard(key)


def _trim_record_locks() -> None:
    """只回收空闲的用户写入锁，避免并发记录时为同一用户创建两把锁。"""
    if len(_record_locks) <= _CACHE_MAX_SIZE:
        return
    for key, lock in list(_record_locks.items()):
        if len(_record_locks) <= _CACHE_MAX_SIZE:
            return
        if not lock.locked():
            _record_locks.pop(key, None)


def _track_background_task(task: asyncio.Task[None]) -> None:
    """保留后台任务引用，避免 fire-and-forget 任务异常被静默丢掉。"""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class _GroupNameHistoryRepository:
    async def get_latest_display_name(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
    ) -> str | None:
        """读取指定用户某类名称的最新记录。"""
        cache_key = (platform, group_id, user_id, name_type)
        if cache_key in _latest_name_cache:
            return _latest_name_cache[cache_key]
        latest = (
            await GroupNameHistory.filter(
                platform=platform,
                group_id=group_id,
                user_id=user_id,
                name_type=name_type,
            )
            .order_by("-record_time", "-id")
            .first()
        )
        display_name = latest.display_name if latest else None
        _latest_name_cache[cache_key] = display_name
        _trim_cache(_latest_name_cache)
        return display_name

    async def create_history(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
        display_name: str,
    ) -> int:
        """写入一次名称变化事件。"""
        record = await GroupNameHistory.create(
            platform=platform,
            group_id=group_id,
            user_id=user_id,
            name_type=name_type,
            display_name=display_name,
        )
        _latest_name_cache[(platform, group_id, user_id, name_type)] = display_name
        _trim_cache(_latest_name_cache)
        return int(record.id)


_repository = _GroupNameHistoryRepository()

_record_matcher = on_message(
    rule=is_type(GroupMessageEvent),
    priority=1,
    block=False,
)

_group_increase_matcher = on_notice(
    rule=is_type(GroupIncreaseNoticeEvent),
    priority=1,
    block=False,
)

_group_decrease_matcher = on_notice(
    rule=is_type(GroupDecreaseNoticeEvent),
    priority=1,
    block=False,
)

_history_matcher = on_command(
    "历史昵称",
    aliases=_HISTORY_ALIASES,
    rule=is_type(GroupMessageEvent),
    priority=5,
    block=True,
)

_group_card_matcher = on_command(
    "历史群名片",
    aliases=_GROUP_CARD_ALIASES,
    rule=is_type(GroupMessageEvent),
    priority=5,
    block=True,
)

_qq_name_matcher = on_command(
    "历史QQ名",
    aliases=_QQ_NAME_ALIASES,
    rule=is_type(GroupMessageEvent),
    priority=5,
    block=True,
)


async def _record_history_with_locks(
    *,
    platform: str,
    group_id: str,
    user_id: str,
    snapshot: SenderNameSnapshot,
) -> int:
    """按用户和名称类型串行写入，避免并发消息重复制造同一条变化。"""
    lock_key = (platform, group_id, user_id, "name_history")
    _trim_record_locks()
    lock = _record_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        created_ids = await record_sender_snapshot(
            _repository,
            platform=platform,
            group_id=group_id,
            user_id=user_id,
            snapshot=snapshot,
        )
    if created_ids:
        _track_background_task(
            asyncio.create_task(
                force_refresh_and_bind_avatar(
                    platform=platform,
                    user_id=user_id,
                    record_ids=created_ids,
                )
            )
        )
    return len(created_ids)


async def _sync_member_snapshot(
    *,
    platform: str,
    group_id: str,
    user_id: str,
    snapshot: SenderNameSnapshot,
) -> int:
    """记录名称并同步当前成员行，缓存命中时仍校验数据库行存在。"""
    created_count = await _record_history_with_locks(
        platform=platform,
        group_id=group_id,
        user_id=user_id,
        snapshot=snapshot,
    )
    sync_key = (platform, group_id, user_id)
    cache_hit = sync_key in _group_info_synced
    member_exists = cache_hit and await GroupInfoUser.exists(
        user_id=user_id,
        group_id=group_id,
    )
    if not should_sync_group_info(
        created_count=created_count,
        cache_hit=cache_hit,
        member_exists=member_exists,
    ):
        return 0

    await GroupInfoUser.update_or_create(
        user_id=user_id,
        group_id=group_id,
        defaults=build_group_info_defaults(snapshot, platform),
    )
    _group_info_synced.add(sync_key)
    _trim_cache(_group_info_synced)
    return created_count


async def _get_current_member_snapshot(
    bot: Bot,
    *,
    group_id: str,
    user_id: str,
) -> SenderNameSnapshot | None:
    """绕过适配器缓存读取当前成员；查不到时视为不在群。"""
    try:
        member_info = await bot.get_group_member_info(
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=True,
        )
    except ActionFailed:
        return None
    return extract_member_snapshot(member_info)


async def _query_history_records(
    *,
    group_id: str,
    user_id: str,
    name_type: str | None,
) -> list[GroupNameHistory]:
    """按名称类型分别查询，避免混合查询时某类历史挤掉另一类。"""
    if name_type:
        return await (
            GroupNameHistory.filter(
                platform="qq",
                group_id=group_id,
                user_id=user_id,
                name_type=name_type,
            )
            .order_by("-record_time", "-id")
            .limit(HISTORY_LIMIT + 1)
        )

    records: list[GroupNameHistory] = []
    for current_type in (GROUP_CARD, QQ_NAME):
        records.extend(
            await GroupNameHistory.filter(
                platform="qq",
                group_id=group_id,
                user_id=user_id,
                name_type=current_type,
            )
            .order_by("-record_time", "-id")
            .limit(HISTORY_LIMIT + 1)
        )
    return records


async def _get_avatar_uri(user_id: str) -> str:
    """获取目标用户头像 URI，失败时交给模板展示占位头像。"""
    try:
        avatar_path = await avatar_service.get_avatar_path("qq", user_id)
    except Exception as e:
        logger.warning(
            "历史昵称头像获取失败，使用占位头像", MODULE, target=user_id, e=e
        )
        return ""
    return avatar_path.as_uri() if avatar_path else ""


async def _build_history_picture(
    *,
    target_user_id: str,
    avatar_uri: str,
    records: list[GroupNameHistory],
    name_type: str | None,
    is_current_member: bool,
) -> bytes:
    """用 htmlrender 将历史昵称分区数据渲染成图片。"""
    avatar_history_items = await get_avatar_history_items(
        platform="qq",
        user_id=target_user_id,
        limit=None,
    )
    avatar_uri_map = {
        item.avatar_hash: item.avatar_uri for item in avatar_history_items
    }
    for record in records:
        if record.avatar_hash and record.avatar_hash not in avatar_uri_map:
            avatar_uri_map[record.avatar_hash] = build_avatar_uri(
                "qq", record.avatar_hash
            )
    display_data = build_history_display_data(
        records,
        target_user_id=target_user_id,
        name_type=name_type,
        limit=HISTORY_LIMIT,
        avatar_uri_map=avatar_uri_map,
        avatar_history=[
            item.avatar_uri for item in avatar_history_items[:AVATAR_HISTORY_LIMIT]
        ],
        avatar_records=avatar_history_items,
        is_current_member=is_current_member,
    )
    return await template_to_pic(
        template_path=TEMPLATE_DIR,
        template_name="history.html",
        templates={
            "title": display_data.profile_name,
            "target_user_id": display_data.target_user_id,
            "avatar_uri": avatar_uri,
            "avatar_history": display_data.avatar_history,
            "sections": display_data.sections,
            "empty_text": display_data.empty_text,
            "limit": HISTORY_LIMIT,
        },
        pages={"viewport": {"width": 860, "height": 10}},
        wait=80,
    )


@_record_matcher.handle()
async def _(event: GroupMessageEvent):
    """收到群消息时记录发送者当前群名片和 QQ 名称。"""
    platform = "qq"
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    snapshot = extract_sender_snapshot(event.sender)
    created_count = await _sync_member_snapshot(
        platform=platform,
        group_id=group_id,
        user_id=user_id,
        snapshot=snapshot,
    )
    logger.debug(
        f"记录名称变化 {created_count} 条",
        MODULE,
        group_id=group_id,
        session=user_id,
    )


@_group_increase_matcher.handle()
async def _(bot: Bot, event: GroupIncreaseNoticeEvent):
    """成员入群后立即采样名称，接口尚未就绪时由后续消息补偿。"""
    if str(event.user_id) == str(bot.self_id):
        return
    platform = "qq"
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    _group_info_synced.discard((platform, group_id, user_id))
    snapshot = await _get_current_member_snapshot(
        bot,
        group_id=group_id,
        user_id=user_id,
    )
    if snapshot is not None:
        await _sync_member_snapshot(
            platform=platform,
            group_id=group_id,
            user_id=user_id,
            snapshot=snapshot,
        )


@_group_decrease_matcher.handle()
async def _(event: GroupDecreaseNoticeEvent):
    """成员离群后失效同步状态，历史记录继续保留。"""
    _group_info_synced.discard(("qq", str(event.group_id), str(event.user_id)))


async def _send_history(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message,
    *,
    name_type: str | None,
):
    """查询当前群内指定用户的历史名称并回复。"""
    target = resolve_query_target(args, str(event.user_id))
    if target.error:
        await MessageUtils.build_message(target.error).finish(reply_to=True)

    group_id = str(event.group_id)
    snapshot = await _get_current_member_snapshot(
        bot,
        group_id=group_id,
        user_id=target.user_id,
    )
    is_current_member = snapshot is not None
    if snapshot is not None:
        await _sync_member_snapshot(
            platform="qq",
            group_id=group_id,
            user_id=target.user_id,
            snapshot=snapshot,
        )

    name_type = normalize_name_type_filter(name_type)
    records = await _query_history_records(
        group_id=group_id,
        user_id=target.user_id,
        name_type=name_type,
    )
    fallback_text = format_history_records(
        records,
        target_user_id=target.user_id,
        name_type=name_type,
        limit=HISTORY_LIMIT,
        is_current_member=is_current_member,
    )
    logger.info("查看历史昵称", MODULE, target=target.user_id)
    try:
        await refresh_avatar_history_on_query("qq", target.user_id)
        avatar_uri = await _get_avatar_uri(target.user_id)
        picture = await _build_history_picture(
            target_user_id=target.user_id,
            avatar_uri=avatar_uri,
            records=records,
            name_type=name_type,
            is_current_member=is_current_member,
        )
    except Exception as e:
        logger.warning(
            "历史昵称图片渲染失败，回退文本",
            MODULE,
            target=target.user_id,
            e=e,
        )
        await MessageUtils.build_message(fallback_text).finish(reply_to=True)
        return

    await MessageUtils.build_message(picture).finish(reply_to=True)


avatar_service.register_refresh_callback(persist_refreshed_avatar)


@PriorityLifecycle.on_startup(priority=6)
async def _startup_import_avatar_cache():
    """启动后后台导入已有头像缓存，兼容历史昵称插件首次启用场景。"""
    _track_background_task(asyncio.create_task(import_existing_avatar_cache()))


@_history_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    """查看群名片和 QQ 名称的混合变化时间线。"""
    await _send_history(bot, event, args, name_type=None)


@_group_card_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    """只查看群名片变化。"""
    await _send_history(bot, event, args, name_type=GROUP_CARD)


@_qq_name_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    """只查看 QQ 名称变化。"""
    await _send_history(bot, event, args, name_type=QQ_NAME)
