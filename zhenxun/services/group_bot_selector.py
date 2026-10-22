"""多账号按群选号：把主动推送交给真正在该群里的账号发出。

对齐 ``nonebot_plugin_saa`` 的自动选号思路：每个在线 OneBot V11 账号各自上报自己的
群列表，发送前按目标群挑出确定在该群里的账号，避免继续用"第一个连上的号"给所有群发送。

本模块只负责两件事：

1. 该由哪个账号来发（:func:`select_group_bot`、:func:`all_group_ids`）
2. 该不该发（:func:`group_plugin_blocked`，群级开关）

默认只在多账号（在线 OneBot V11 账号 >= 2 个）且 ``ENABLED`` 时介入；单账号、熔断或
任何异常场景都回退到调用方给出的 Bot，保证老行为不变。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
import time

import nonebot
from nonebot.adapters import Bot

from zhenxun.configs.config import Config
from zhenxun.services.cache.runtime_cache import GroupMemoryCache
from zhenxun.services.log import logger
from zhenxun.utils.platform import PlatformUtils

LOGGER_COMMAND = "GroupBotSelector"
CONFIG_MODULE = "GroupBotSelector"
SUPPORTED_ADAPTER = "OneBot V11"


Config.add_plugin_config(
    CONFIG_MODULE,
    "ENABLED",
    True,
    help="多账号下按群选择发送账号；关闭后回到用同一个 Bot 发所有群的旧行为",
    default_value=True,
    type=bool,
)
Config.add_plugin_config(
    CONFIG_MODULE,
    "PRIORITY",
    [],
    help="选号优先级列表，靠前的账号优先；为空时优先沿用当前 Bot，其次按 self_id 升序",
    default_value=[],
    type=list[str],
)
Config.add_plugin_config(
    CONFIG_MODULE,
    "CACHE_TTL_SECONDS",
    1800,
    help="账号群列表拓扑缓存的有效期（秒）",
    default_value=1800,
    type=int,
)
Config.add_plugin_config(
    CONFIG_MODULE,
    "MIN_REBUILD_INTERVAL_SECONDS",
    60,
    help="群列表缓存的最小重建间隔（秒），用于抑制重新拉取风暴",
    default_value=60,
    type=int,
)
Config.add_plugin_config(
    CONFIG_MODULE,
    "ALL_GROUPS_ACROSS_ACCOUNTS",
    True,
    help="@all 是否解析为所有在线账号的群并集；关闭后只按当前 Bot 的群列表解析",
    default_value=True,
    type=bool,
)


@dataclass(slots=True, frozen=True)
class GroupTopology:
    """一次账号群列表快照。"""

    built_at: float
    bot_ids: frozenset[str]
    group_to_bots: dict[str, tuple[str, ...]]
    queried_bot_ids: frozenset[str]
    failed_bot_ids: frozenset[str]

    @property
    def usable(self) -> bool:
        """是否至少有一个账号成功上报了群列表。"""
        return bool(self.queried_bot_ids)


_TOPOLOGY: GroupTopology | None = None
_LOCK: asyncio.Lock = asyncio.Lock()
_LAST_MISS_REBUILD_AT = 0.0
"""上一次"未命中群"触发的重建时间，用于抑制拉取风暴。"""


def _config_bool(key: str, default: bool) -> bool:
    value = Config.get_config(CONFIG_MODULE, key, default, build_model=False)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _config_int(key: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(Config.get_config(CONFIG_MODULE, key, default, build_model=False))
    except (TypeError, ValueError):
        value = default
    return value if value >= minimum else default


def _config_list(key: str) -> list[str]:
    value = Config.get_config(CONFIG_MODULE, key, [], build_model=False)
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list | tuple | set):
        items = value
    else:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def enabled() -> bool:
    """多账号按群选号是否开启（熔断开关）。"""
    return _config_bool("ENABLED", True)


def all_groups_across_accounts() -> bool:
    """``@all`` 是否按所有在线账号的群并集解析。"""
    return _config_bool("ALL_GROUPS_ACROSS_ACCOUNTS", True)


def _adapter_name(bot: Bot) -> str:
    adapter = getattr(bot, "adapter", None)
    get_name = getattr(adapter, "get_name", None)
    return str(get_name()) if get_name else str(adapter or "")


def _bot_sort_key(bot_id: str) -> tuple[int, int, str]:
    """数字账号按数值排序，保证同一候选集合的选择稳定可预测。"""
    value = str(bot_id)
    if value.isdigit():
        return (0, int(value), "")
    return (1, 0, value)


def _online_bots(*, only_adapter_of: Bot | None = None) -> list[Bot]:
    """当前在线的 OneBot V11 账号，按适配器过滤可选。"""
    bots = [
        bot
        for bot in nonebot.get_bots().values()
        if _adapter_name(bot) == SUPPORTED_ADAPTER
    ]
    if only_adapter_of is not None:
        adapter_name = _adapter_name(only_adapter_of)
        bots = [bot for bot in bots if _adapter_name(bot) == adapter_name]
    return sorted(bots, key=lambda bot: _bot_sort_key(str(bot.self_id)))


async def _build(bots: Iterable[Bot]) -> GroupTopology:
    """逐个账号拉取群列表，单个账号失败不影响其它账号。"""
    group_to_bots: dict[str, set[str]] = {}
    queried_bot_ids: set[str] = set()
    failed_bot_ids: set[str] = set()
    for bot in bots:
        bot_id = str(bot.self_id)
        try:
            groups, _ = await PlatformUtils.get_group_list(bot, only_group=True)
        except Exception as exc:
            failed_bot_ids.add(bot_id)
            logger.warning(
                f"账号 {bot_id} 的群列表获取失败，本次选号忽略该账号",
                LOGGER_COMMAND,
                target=bot_id,
                e=exc,
            )
            continue
        queried_bot_ids.add(bot_id)
        for group in groups:
            group_id = str(getattr(group, "group_id", "") or "")
            if group_id:
                group_to_bots.setdefault(group_id, set()).add(bot_id)
    return GroupTopology(
        built_at=time.monotonic(),
        bot_ids=frozenset(str(bot.self_id) for bot in bots),
        group_to_bots={
            group_id: tuple(sorted(bot_ids, key=_bot_sort_key))
            for group_id, bot_ids in group_to_bots.items()
        },
        queried_bot_ids=frozenset(queried_bot_ids),
        failed_bot_ids=frozenset(failed_bot_ids),
    )


def _is_fresh(topology: GroupTopology, bots: list[Bot]) -> bool:
    ttl = _config_int("CACHE_TTL_SECONDS", 1800, minimum=1)
    if time.monotonic() - topology.built_at >= ttl:
        return False
    return topology.bot_ids == frozenset(str(bot.self_id) for bot in bots)


def _miss_rebuild_allowed() -> bool:
    """未命中触发的重建是否放行（按最小间隔节流）。"""
    interval = _config_int("MIN_REBUILD_INTERVAL_SECONDS", 60, minimum=0)
    if not interval:
        return True
    return time.monotonic() - _LAST_MISS_REBUILD_AT >= interval


def _mark_miss_rebuild() -> None:
    global _LAST_MISS_REBUILD_AT
    _LAST_MISS_REBUILD_AT = time.monotonic()


def _candidate_order(
    candidate_ids: tuple[str, ...], *, fallback: Bot | None
) -> list[str]:
    """优先级列表 → 当前 Bot → self_id 升序，去重后只保留真正在群里的账号。"""
    preferred: list[str] = _config_list("PRIORITY")
    if fallback is not None:
        preferred.append(str(fallback.self_id))
    preferred.extend(sorted(candidate_ids, key=_bot_sort_key))
    ordered: list[str] = []
    for bot_id in preferred:
        if bot_id in candidate_ids and bot_id not in ordered:
            ordered.append(bot_id)
    return ordered


async def _topology_for(bots: list[Bot], *, force: bool = False) -> GroupTopology:
    """取（必要时重建）群列表快照；重建在锁内串行，避免并发重复拉取。"""
    global _TOPOLOGY
    async with _LOCK:
        current = _TOPOLOGY
        if current is not None and not force and _is_fresh(current, bots):
            return current
        topology = await _build(bots)
        _TOPOLOGY = topology
        return topology


def invalidate() -> None:
    """丢弃群列表缓存（重连、测试或手工刷新时使用）。"""
    global _LAST_MISS_REBUILD_AT, _TOPOLOGY
    _TOPOLOGY = None
    _LAST_MISS_REBUILD_AT = 0.0


async def select_group_bot(
    group_id: str | int | None, *, fallback: Bot | None = None
) -> Bot | None:
    """挑选负责该群的在线账号。

    返回 ``None`` 表示该群当前没有任何在线账号（调用方应跳过该目标）；
    多账号条件不满足、缓存不可用等场景返回 ``fallback``，保持旧行为。
    """
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return fallback

    bots = _online_bots(only_adapter_of=fallback)
    if not enabled() or len(bots) < 2:
        return fallback

    topology = await _topology_for(bots)
    candidate_ids = topology.group_to_bots.get(normalized_group_id)
    if not candidate_ids and _miss_rebuild_allowed():
        # 未命中通常是"账号刚进群"或缓存已过期，主动重建一次再判定（带节流）。
        _mark_miss_rebuild()
        topology = await _topology_for(bots, force=True)
        candidate_ids = topology.group_to_bots.get(normalized_group_id)
    if not candidate_ids:
        if topology.usable:
            logger.warning(
                f"群 {normalized_group_id} 没有任何在线账号，已跳过该目标",
                LOGGER_COMMAND,
                target=normalized_group_id,
            )
            return None
        logger.warning(
            f"所有账号的群列表都获取失败，群 {normalized_group_id} 回退默认 Bot 试发",
            LOGGER_COMMAND,
            target=normalized_group_id,
        )
        return fallback

    bot_map = {str(bot.self_id): bot for bot in bots}
    for bot_id in _candidate_order(candidate_ids, fallback=fallback):
        bot = bot_map.get(bot_id)
        if bot is not None:
            return bot

    # 候选账号均已下线：交给调用方兜底，避免直接丢掉这条推送。
    logger.debug(
        f"群 {normalized_group_id} 的候选账号都不在线，回退默认 Bot 试发",
        LOGGER_COMMAND,
        target=normalized_group_id,
    )
    return fallback


async def all_group_ids(*, only_adapter_of: Bot | None = None) -> set[str]:
    """所有在线账号可见群的并集。

    不满足多账号条件（单账号、熔断关闭）时返回空集合，调用方据此走旧逻辑。
    """
    if not enabled():
        return set()
    bots = _online_bots(only_adapter_of=only_adapter_of)
    if len(bots) < 2:
        return set()
    topology = await _topology_for(bots)
    if topology.failed_bot_ids:
        logger.warning(
            "部分账号群列表获取失败，群并集可能不完整: "
            f"{sorted(topology.failed_bot_ids)}",
            LOGGER_COMMAND,
        )
    return set(topology.group_to_bots)


def group_plugin_blocked(group_id: str | int | None, module: str) -> bool:
    """群级开关是否禁用了该模块（普通禁用或超管禁用）。

    只读群内存快照；缓存未就绪时按"未禁用"处理，与既有鉴权层保持一致。
    """
    if not module:
        return False
    snapshot = GroupMemoryCache.get_if_ready(str(group_id or "").strip(), None)
    if snapshot is None:
        return False
    return (
        module in snapshot.block_plugin_set
        or module in snapshot.superuser_block_plugin_set
    )
