"""群聊总结定时任务的按群选号接线测试。

调度引擎注入的是"第一个连上的号"，任务内必须再按 ``context.group_id`` 选号，
保证读消息与发送都用真正在该群里的账号。
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

# 必须先导入 zhenxun.services 完成 require()：若插件模块先直接 import
# nonebot_plugin_alconna，zhenxun.services.__init__ 的 require() 会失败。
from zhenxun.services import group_bot_selector  # noqa: F401
from zhenxun.services.scheduler import ScheduleContext

_TASKS_MODULE = "zhenxun.plugins.zhenxun_plugin_summary_group.utils.scheduler_tasks"


def _load_plugin_modules():
    """在 services 包就绪后再导入插件模块，返回 (任务模块, 消息处理模块)。"""
    utils_package = importlib.import_module(
        "zhenxun.plugins.zhenxun_plugin_summary_group.utils"
    )
    tasks_module = importlib.import_module(_TASKS_MODULE)
    return tasks_module, utils_package.message_processing


class _FakeBot:
    def __init__(self, bot_id: str) -> None:
        self.self_id = bot_id

    def __repr__(self) -> str:
        return f"<FakeBot {self.self_id}>"


def _make_context(group_id: str) -> ScheduleContext:
    return ScheduleContext(
        schedule_id=1,
        plugin_name="summary_group",
        bot_id="10001",
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_scheduled_summary_uses_account_inside_group(monkeypatch):
    """注入号不在该群时，读消息与发送都必须改用被选中的账号。"""
    tasks, message_processing = _load_plugin_modules()
    bot_injected = _FakeBot("10001")
    bot_in_group = _FakeBot("10002")
    selector_calls: list[tuple[str, object]] = []
    used: dict[str, object] = {}

    async def fake_select_group_bot(group_id, *, fallback=None):
        selector_calls.append((str(group_id), fallback))
        return bot_in_group

    async def fake_get_group_messages(bot, group_id, _scope, use_db=False):
        used["fetch_bot"] = bot
        used["group_id"] = group_id
        return SimpleNamespace(
            messages=["message"],
            user_info_cache={},
            warning_message=None,
        )

    async def fake_messages_summary(**_kwargs):
        return SimpleNamespace(summary_text="summary", resolved_model_name=None)

    async def fake_send_summary(bot, _target, _text, *_args, **_kwargs):
        used["send_bot"] = bot
        return True

    monkeypatch.setattr(tasks, "select_group_bot", fake_select_group_bot)
    monkeypatch.setattr(
        tasks, "build_preset_scope", lambda _time_range: SimpleNamespace(name="today")
    )
    monkeypatch.setattr(
        message_processing, "get_group_messages", fake_get_group_messages
    )
    monkeypatch.setattr(tasks, "messages_summary", fake_messages_summary)
    monkeypatch.setattr(tasks, "send_summary", fake_send_summary)

    await tasks.scheduled_summary_task(
        bot=bot_injected,
        context=_make_context("222"),
        params=tasks.SummaryTaskParams(time_range_type="today"),
    )

    assert selector_calls == [("222", bot_injected)]
    assert used["fetch_bot"] is bot_in_group
    assert used["send_bot"] is bot_in_group
    assert used["group_id"] == 222


@pytest.mark.asyncio
async def test_scheduled_summary_skips_group_without_any_account(monkeypatch):
    """群内没有任何在线账号时直接跳过，不做无意义的总结与发送。"""
    tasks, message_processing = _load_plugin_modules()
    bot_injected = _FakeBot("10001")
    called: list[str] = []

    async def fake_select_group_bot(_group_id, *, fallback=None):
        return None

    async def fake_get_group_messages(*_args, **_kwargs):
        called.append("fetch")
        raise AssertionError("不应读取消息")

    async def fake_send_summary(*_args, **_kwargs):
        called.append("send")
        raise AssertionError("不应发送消息")

    monkeypatch.setattr(tasks, "select_group_bot", fake_select_group_bot)
    monkeypatch.setattr(
        message_processing, "get_group_messages", fake_get_group_messages
    )
    monkeypatch.setattr(tasks, "send_summary", fake_send_summary)

    await tasks.scheduled_summary_task(
        bot=bot_injected,
        context=_make_context("222"),
        params=tasks.SummaryTaskParams(time_range_type="today"),
    )

    assert called == []
