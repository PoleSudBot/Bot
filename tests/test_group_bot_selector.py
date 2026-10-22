"""多账号按群选号服务（``zhenxun.services.group_bot_selector``）单元测试。

用假 Bot + 伪造群列表覆盖：单账号零影响、按群归属选号、优先级与兜底、
缓存与重建节流、群并集、群级开关判定。
"""

from __future__ import annotations

from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services import group_bot_selector as selector
from zhenxun.services.cache.runtime_cache import GroupMemoryCache
from zhenxun.utils.platform import PlatformUtils


class _FakeBot:
    """只提供选号需要的两个属性：self_id 与适配器名。"""

    def __init__(self, bot_id: str, adapter_name: str = "OneBot V11") -> None:
        self.self_id = bot_id
        self.adapter = SimpleNamespace(get_name=lambda: adapter_name)
        self.group_list: list[str] = []

    def __repr__(self) -> str:  # 便于失败信息定位
        return f"<FakeBot {self.self_id}>"


@pytest.fixture(autouse=True)
def _reset_state():
    selector.invalidate()
    yield
    selector.invalidate()


def _patch_config(monkeypatch: pytest.MonkeyPatch, **values) -> None:
    """按 key 覆盖配置读取，避免依赖真实 config.yaml。"""
    monkeypatch.setattr(
        selector, "_config_bool", lambda key, default: values.get(key, default)
    )
    monkeypatch.setattr(
        selector,
        "_config_int",
        lambda key, default, *, minimum=0: values.get(key, default),
    )
    monkeypatch.setattr(selector, "_config_list", lambda key: values.get(key, []))


def _patch_bots(monkeypatch: pytest.MonkeyPatch, *bots: _FakeBot) -> None:
    bot_map = {str(bot.self_id): bot for bot in bots}
    monkeypatch.setattr(nonebot, "get_bots", lambda: bot_map)


def _patch_group_lists(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, list[str]],
    calls: list[str],
    *,
    fail: tuple[str, ...] = (),
) -> None:
    async def _fake_get_group_list(bot, only_group: bool = False):
        bot_id = str(bot.self_id)
        calls.append(bot_id)
        if bot_id in fail:
            raise RuntimeError(f"group list unavailable: {bot_id}")
        groups = [
            SimpleNamespace(group_id=group_id)
            for group_id in mapping.get(bot_id, [])
        ]
        return groups, "qq"

    monkeypatch.setattr(PlatformUtils, "get_group_list", _fake_get_group_list)


async def test_single_bot_returns_fallback_without_api_call(monkeypatch):
    """单账号部署必须零影响：不介入、不产生任何额外 API 调用。"""
    bot = _FakeBot("10001")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot)
    _patch_group_lists(monkeypatch, {"10001": ["111"]}, calls)

    assert await selector.select_group_bot("222", fallback=bot) is bot
    assert await selector.all_group_ids(only_adapter_of=bot) == set()
    assert calls == []


async def test_selects_account_that_is_in_group(monkeypatch):
    """只有 B 在的群由 B 发送，只有 A 在的群由 A 发送。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(
        monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls
    )

    assert await selector.select_group_bot("222", fallback=bot_a) is bot_b
    assert await selector.select_group_bot("111", fallback=bot_a) is bot_a


async def test_prefers_fallback_when_both_accounts_present(monkeypatch):
    """两个账号都在群里时，未配置优先级则沿用当前 Bot（身份稳定）。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["111"]}, calls)

    assert await selector.select_group_bot("111", fallback=bot_b) is bot_b


async def test_priority_list_overrides_fallback(monkeypatch):
    """配置了优先级列表时，靠前的账号优先。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    bot_c = _FakeBot("10003")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b, bot_c)
    _patch_config(monkeypatch, PRIORITY=["10003", "10002"])
    _patch_group_lists(
        monkeypatch,
        {"10001": ["111"], "10002": ["111"], "10003": ["111"]},
        calls,
    )

    assert await selector.select_group_bot("111", fallback=bot_a) is bot_c


async def test_priority_falls_back_when_priority_account_absent(monkeypatch):
    """优先级账号不在该群时，退回当前 Bot，不会串群。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_config(monkeypatch, PRIORITY=["10002"])
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    assert await selector.select_group_bot("111", fallback=bot_a) is bot_a
    assert await selector.select_group_bot("222", fallback=bot_a) is bot_b


async def test_group_without_any_account_returns_none(monkeypatch):
    """群列表可用但没有任何在线账号在该群 → 跳过该目标。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    assert await selector.select_group_bot("333", fallback=bot_a) is None


async def test_all_group_lists_failed_returns_fallback(monkeypatch):
    """所有账号都拉不到群列表时，回退默认 Bot 试发，不丢推送。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(
        monkeypatch, {}, calls, fail=("10001", "10002")
    )

    assert await selector.select_group_bot("333", fallback=bot_b) is bot_b


async def test_cache_avoids_repeated_group_list_calls(monkeypatch):
    """TTL 内复用快照，不重复拉取群列表。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    await selector.select_group_bot("111", fallback=bot_a)
    await selector.select_group_bot("222", fallback=bot_a)
    await selector.all_group_ids()

    assert calls == ["10001", "10002"]


async def test_miss_triggers_single_throttled_rebuild(monkeypatch):
    """未命中会主动重建一次，但受最小重建间隔节流。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    assert await selector.select_group_bot("333", fallback=bot_a) is None
    assert calls == ["10001", "10002", "10001", "10002"]

    # 第二次未命中仍在节流窗口内，不再重建
    assert await selector.select_group_bot("444", fallback=bot_a) is None
    assert calls == ["10001", "10002", "10001", "10002"]


async def test_new_group_visible_after_miss_rebuild(monkeypatch):
    """账号刚进群时，未命中的主动重建能把新群纳入（关闭节流以观察每一次重建）。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    mapping = {"10001": ["111"], "10002": []}
    _patch_config(monkeypatch, MIN_REBUILD_INTERVAL_SECONDS=0)
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, mapping, calls)

    assert await selector.select_group_bot("222", fallback=bot_a) is None
    mapping["10002"] = ["222"]
    assert await selector.select_group_bot("222", fallback=bot_a) is bot_b


async def test_online_bot_change_invalidates_topology(monkeypatch):
    """在线账号集合变化后必须重建，避免继续沿用旧拓扑。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    bot_c = _FakeBot("10003")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(
        monkeypatch, {"10001": ["111"], "10002": ["222"], "10003": ["333"]}, calls
    )

    assert await selector.select_group_bot("111", fallback=bot_a) is bot_a
    assert calls == ["10001", "10002"]

    _patch_bots(monkeypatch, bot_a, bot_b, bot_c)
    assert await selector.select_group_bot("333", fallback=bot_a) is bot_c
    assert calls == ["10001", "10002", "10001", "10002", "10003"]


async def test_disabled_switch_returns_fallback(monkeypatch):
    """熔断开关关闭时完全回到旧行为。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_config(monkeypatch, ENABLED=False)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    assert await selector.select_group_bot("222", fallback=bot_a) is bot_a
    assert await selector.all_group_ids() == set()
    assert calls == []


async def test_other_adapter_accounts_are_ignored(monkeypatch):
    """只统计 OneBot V11 账号；同群但其它适配器不参与。"""
    bot_a = _FakeBot("10001")
    other = _FakeBot("20001", adapter_name="OneBot V12")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, other)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "20001": ["111"]}, calls)

    assert await selector.select_group_bot("111", fallback=bot_a) is bot_a
    assert calls == []


async def test_all_group_ids_unions_accounts(monkeypatch):
    """@all 口径：所有在线账号群并集。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    assert await selector.all_group_ids(only_adapter_of=bot_a) == {"111", "222"}
    other_adapter_bot = _FakeBot("30001", "Telegram")
    assert await selector.all_group_ids(only_adapter_of=other_adapter_bot) == set()


def test_group_plugin_blocked_reads_group_snapshot(monkeypatch):
    """群级普通/超管禁用都应判为关闭；缓存未就绪时放行。"""
    snapshot = SimpleNamespace(
        block_plugin_set=frozenset({"nonebot_plugin_rollpig"}),
        superuser_block_plugin_set=frozenset(),
    )
    monkeypatch.setattr(
        GroupMemoryCache,
        "get_if_ready",
        staticmethod(lambda group_id, channel_id=None: snapshot),
    )
    assert selector.group_plugin_blocked("111", "nonebot_plugin_rollpig") is True
    assert selector.group_plugin_blocked("111", "other_plugin") is False

    snapshot.block_plugin_set = frozenset()
    snapshot.superuser_block_plugin_set = frozenset({"nonebot_plugin_rollpig"})
    assert selector.group_plugin_blocked("111", "nonebot_plugin_rollpig") is True

    monkeypatch.setattr(
        GroupMemoryCache,
        "get_if_ready",
        staticmethod(lambda group_id, channel_id=None: None),
    )
    assert selector.group_plugin_blocked("111", "nonebot_plugin_rollpig") is False
