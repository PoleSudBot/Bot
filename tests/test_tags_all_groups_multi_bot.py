"""``@all``（调度器 ALL_GROUPS 目标）在多账号下的解析口径测试。

多账号时必须是所有在线账号的群并集，单账号或熔断时保持按单个 Bot 解析，
并且不能破坏"没有在线 Bot 时回退数据库群表"的老行为。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

# 必须先导入 zhenxun.services：直接以 zhenxun.models 进入会触发既有循环导入。
from zhenxun.services import group_bot_selector
from zhenxun.services.tags import tag_manager

# 模型层放在 services 之后惰性获取，避免反向改变上面的导入顺序。
GroupConsole = importlib.import_module("zhenxun.models.group_console").GroupConsole
PlatformUtils = importlib.import_module("zhenxun.utils.platform").PlatformUtils


class _FakeBot:
    def __init__(self, bot_id: str, adapter_name: str = "OneBot V11") -> None:
        self.self_id = bot_id
        self.adapter = SimpleNamespace(get_name=lambda: adapter_name)


class _FakeGroupQuery:
    """只实现 values_list 的假查询集，用于验证数据库兜底分支。"""

    def __init__(self, group_ids: list[str]) -> None:
        self._group_ids = group_ids

    async def values_list(self, _field: str, flat: bool = False):
        return list(self._group_ids)


@pytest.fixture(autouse=True)
async def _reset_caches():
    group_bot_selector.invalidate()
    await tag_manager._invalidate_cache()
    yield
    group_bot_selector.invalidate()
    await tag_manager._invalidate_cache()


def _patch_bots(monkeypatch: pytest.MonkeyPatch, *bots: _FakeBot) -> None:
    bot_map = {str(bot.self_id): bot for bot in bots}
    monkeypatch.setattr(nonebot, "get_bots", lambda: bot_map)


def _patch_group_lists(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, list[str]],
    calls: list[str],
) -> None:
    async def _fake_get_group_list(bot, only_group: bool = False):
        bot_id = str(bot.self_id)
        calls.append(bot_id)
        return [
            SimpleNamespace(group_id=group_id) for group_id in mapping.get(bot_id, [])
        ], "qq"

    monkeypatch.setattr(PlatformUtils, "get_group_list", _fake_get_group_list)


async def test_all_resolves_union_of_all_accounts(monkeypatch):
    """多账号：@all 覆盖所有账号的群（只有另一个账号在的群不再漏掉）。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)

    await tag_manager._invalidate_cache()
    assert await tag_manager.resolve_tag_to_group_ids("@all", bot=bot_a) == [
        "111",
        "222",
    ]
    assert sorted(calls) == ["10001", "10002"]


async def test_single_account_keeps_legacy_behavior(monkeypatch):
    """单账号：与改动前一致，只按当前 Bot 的群列表解析，且不产生额外拉取。"""
    bot_a = _FakeBot("10001")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a)
    _patch_group_lists(monkeypatch, {"10001": ["111"]}, calls)

    await tag_manager._invalidate_cache()
    assert await tag_manager.resolve_tag_to_group_ids("@all", bot=bot_a) == ["111"]
    assert calls == ["10001"]


async def test_switch_disables_union(monkeypatch):
    """熔断：ALL_GROUPS_ACROSS_ACCOUNTS=false 时回到单 Bot 口径。"""
    bot_a = _FakeBot("10001")
    bot_b = _FakeBot("10002")
    calls: list[str] = []
    _patch_bots(monkeypatch, bot_a, bot_b)
    _patch_group_lists(monkeypatch, {"10001": ["111"], "10002": ["222"]}, calls)
    monkeypatch.setattr(
        group_bot_selector,
        "_config_bool",
        lambda key, default: False if key == "ALL_GROUPS_ACROSS_ACCOUNTS" else default,
    )

    await tag_manager._invalidate_cache()
    assert await tag_manager.resolve_tag_to_group_ids("@all", bot=bot_a) == ["111"]


async def test_without_bot_falls_back_to_database(monkeypatch):
    """没有在线 Bot 时保持老行为：回退数据库群表。"""
    _patch_bots(monkeypatch, )
    monkeypatch.setattr(
        GroupConsole, "all", classmethod(lambda cls: _FakeGroupQuery(["999", "888"]))
    )

    await tag_manager._invalidate_cache()
    assert sorted(await tag_manager.resolve_tag_to_group_ids("@all")) == ["888", "999"]
