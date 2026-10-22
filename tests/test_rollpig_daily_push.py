"""烤猪日报"按群选号推送"（``nonebot_plugin_rollpig/daily_push.py``）单元测试。

日报待发群来自云账本（所有账号混在一起），这里验证每个群都由"在该群的账号"发送、
找不到归属账号时不空发、单群失败不影响其它群。选号模块被替换为按群路由的假实现，
真实选号逻辑由 ``tests/test_group_bot_selector.py`` 覆盖。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import uuid

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

DAILY_PUSH_PATH = (
    Path(__file__).resolve().parents[1]
    / "zhenxun"
    / "plugins"
    / "nonebot_plugin_rollpig"
    / "daily_push.py"
)


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def _record(self, level: str, message: object) -> None:
        self.messages.append((level, str(message)))

    def debug(self, message, *_args, **_kwargs) -> None:
        self._record("debug", message)

    def info(self, message, *_args, **_kwargs) -> None:
        self._record("info", message)

    def warning(self, message, *_args, **_kwargs) -> None:
        self._record("warning", message)

    def error(self, message, *_args, **_kwargs) -> None:
        self._record("error", message)

    def warnings(self) -> list[str]:
        return [message for level, message in self.messages if level == "warning"]


class FakeBot:
    """记录每个群由哪个 Bot 发出、以及可选的发送异常。"""

    def __init__(self, bot_id: str, *, fail_groups: set[int] | None = None) -> None:
        self.self_id = bot_id
        self.sent_groups: list[int] = []
        self.fail_groups = fail_groups or set()

    async def send_group_msg(self, *, group_id: int, message) -> None:
        if group_id in self.fail_groups:
            raise RuntimeError(f"send failed: {group_id}")
        self.sent_groups.append(group_id)


def load_module_from_path(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_daily_push_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    routes: dict[str, object],
    logger: FakeLogger,
    selector_calls: list[tuple[str, object]],
):
    """加载真实 daily_push 实现，只把选号模块替换成按群路由的假实现。

    ``routes`` 命中即为"该群有账号"（值为 ``None`` 表示群内没有任何在线账号，
    调用方应跳过）；未命中则退回 ``fallback``，与真实选号模块的兜底一致。
    """

    async def fake_select_group_bot(group_id, *, fallback=None):
        selector_calls.append((str(group_id), fallback))
        normalized_group_id = str(group_id)
        if normalized_group_id in routes:
            return routes[normalized_group_id]
        return fallback

    fake_selector = types.ModuleType("zhenxun.services.group_bot_selector")
    fake_selector.select_group_bot = fake_select_group_bot
    fake_selector.group_plugin_blocked = lambda *_args, **_kwargs: False

    fake_nonebot_log = types.ModuleType("nonebot.log")
    fake_nonebot_log.logger = logger

    fake_zhenxun = types.ModuleType("zhenxun")
    fake_zhenxun.__path__ = []
    fake_zhenxun_services = types.ModuleType("zhenxun.services")
    fake_zhenxun_services.__path__ = []

    for name, module in {
        "nonebot.log": fake_nonebot_log,
        "zhenxun": fake_zhenxun,
        "zhenxun.services": fake_zhenxun_services,
        "zhenxun.services.group_bot_selector": fake_selector,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return load_module_from_path(
        f"rollpig_daily_push_test_{uuid.uuid4().hex}",
        DAILY_PUSH_PATH,
    )


async def _render(_group_id: str, summary: dict):
    return summary["message"]


@pytest.mark.asyncio
async def test_each_group_is_sent_by_an_account_inside_it(monkeypatch):
    """A-only 群由 A 发、B-only 群由 B 发；没有任何账号的群跳过。"""
    logger = FakeLogger()
    selector_calls: list[tuple[str, object]] = []
    bot_a = FakeBot("10001")
    bot_b = FakeBot("10002")
    routes = {"111": bot_a, "222": bot_b, "333": None}
    module = load_daily_push_module(
        monkeypatch, routes=routes, logger=logger, selector_calls=selector_calls
    )

    sent, unsent = await module.push_group_summaries(
        group_ids=["111", "222", "333"],
        summaries={
            "111": {"message": "m111"},
            "222": {"message": "m222"},
            "333": {"message": "m333"},
        },
        render=_render,
        summary_date="2026-04-22",
        fallback_bot=bot_a,
    )

    assert bot_a.sent_groups == [111]
    assert bot_b.sent_groups == [222]
    assert (sent, unsent) == (2, 1)
    assert [group_id for group_id, _fallback in selector_calls] == ["111", "222", "333"]
    assert all(fallback is bot_a for _group_id, fallback in selector_calls)
    assert any("group=333" in message for message in logger.warnings())


@pytest.mark.asyncio
async def test_render_failure_skips_only_that_group(monkeypatch):
    logger = FakeLogger()
    selector_calls: list[tuple[str, object]] = []
    bot_a = FakeBot("10001")
    module = load_daily_push_module(
        monkeypatch,
        routes={"111": bot_a, "222": bot_a},
        logger=logger,
        selector_calls=selector_calls,
    )

    async def render(group_id: str, _summary: dict):
        if group_id == "222":
            raise RuntimeError("render boom")
        return f"message-{group_id}"

    sent, unsent = await module.push_group_summaries(
        group_ids=["111", "222"],
        summaries={"111": {}, "222": {}},
        render=render,
        summary_date="2026-04-22",
        fallback_bot=bot_a,
    )

    assert bot_a.sent_groups == [111]
    assert (sent, unsent) == (1, 1)
    assert any("group=222" in message for message in logger.warnings())


@pytest.mark.asyncio
async def test_send_failure_is_isolated(monkeypatch):
    logger = FakeLogger()
    selector_calls: list[tuple[str, object]] = []
    bot_a = FakeBot("10001", fail_groups={111})
    bot_b = FakeBot("10002")
    module = load_daily_push_module(
        monkeypatch,
        routes={"111": bot_a, "222": bot_b},
        logger=logger,
        selector_calls=selector_calls,
    )

    sent, unsent = await module.push_group_summaries(
        group_ids=["111", "222"],
        summaries={"111": {"message": "m111"}, "222": {"message": "m222"}},
        render=_render,
        summary_date="2026-04-22",
        fallback_bot=bot_b,
    )

    assert bot_a.sent_groups == []
    assert bot_b.sent_groups == [222]
    assert (sent, unsent) == (1, 1)
    assert any("group=111" in message for message in logger.warnings())


@pytest.mark.asyncio
async def test_empty_group_list_does_nothing(monkeypatch):
    logger = FakeLogger()
    selector_calls: list[tuple[str, object]] = []
    bot_a = FakeBot("10001")
    module = load_daily_push_module(
        monkeypatch, routes={}, logger=logger, selector_calls=selector_calls
    )

    sent, unsent = await module.push_group_summaries(
        group_ids=[],
        summaries={},
        render=_render,
        summary_date="2026-04-22",
        fallback_bot=bot_a,
    )

    assert (sent, unsent) == (0, 0)
    assert selector_calls == []
