from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
from nonebot.adapters.onebot.v11 import Bot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from zhenxun.services.scheduler import lifecycle
from zhenxun.services.scheduler.engine import _execute_job
from zhenxun.services.scheduler.manager import SchedulerManager
from zhenxun.services.scheduler.types import ScheduleContext, Trigger


class _FakeAdapter:
    """仅满足 Bot 构造所需的最小适配器。"""

    def __init__(self, bot_id: str):
        self.config = SimpleNamespace(superusers=set())
        self.connections = {bot_id: object()}
        self.onebot_config = SimpleNamespace(onebot_api_roots={})
        self.adapter_name = "OneBot V11"

    def get_name(self) -> str:
        return self.adapter_name


def _make_bot() -> Bot:
    return Bot(adapter=_FakeAdapter("10001"), self_id="10001")


def _patch_plugin_lookup(monkeypatch, plugin_name: str = "demo_plugin") -> None:
    """只为本测试模块伪造已加载插件（其他模块保持原行为）。"""
    target = __name__

    def _lookup(module_name: str):
        if module_name == target:
            return SimpleNamespace(name=plugin_name, id_=plugin_name)
        return None

    monkeypatch.setattr(
        "zhenxun.services.scheduler.manager.nonebot.get_plugin_by_module_name",
        _lookup,
    )


@pytest.mark.asyncio
async def test_ephemeral_job_key_matches_lifecycle_context(monkeypatch):
    """声明式临时任务的注册键必须与启动调度时生成的查找键一致。

    回归：注册用 ephemeral:: 前缀而 lifecycle 生成 runtime:: 前缀的
    context.plugin_name，engine._execute_job 查 _registered_tasks 必然落空，
    任务体永不执行并每小时记一次 "函数 '...' 未注册。"。
    """
    manager = SchedulerManager()
    monkeypatch.setattr(lifecycle, "scheduler_manager", manager)
    _patch_plugin_lookup(monkeypatch)

    @manager.runtime_job(Trigger.interval(hours=1))
    async def _demo_job(bot, context) -> None:
        return None

    scheduled: list[dict] = []
    monkeypatch.setattr(
        lifecycle.APSchedulerAdapter,
        "add_ephemeral_job",
        lambda **kwargs: scheduled.append(kwargs),
    )
    monkeypatch.setattr(
        lifecycle.ScheduleRepository,
        "get_all_enabled",
        AsyncMock(return_value=[]),
    )

    await lifecycle._load_schedules_from_db()

    assert [item["job_id"] for item in scheduled] == [
        "ephemeral::demo_plugin::_demo_job"
    ]
    context = scheduled[0]["context"]
    assert context.plugin_name == "ephemeral::demo_plugin::_demo_job"
    assert context.plugin_name in manager._registered_tasks


@pytest.mark.asyncio
async def test_ephemeral_job_body_executes_through_engine(monkeypatch):
    """键名一致后，engine 的临时任务入口必须能真正执行任务体。"""
    manager = SchedulerManager()
    monkeypatch.setattr("zhenxun.services.scheduler.manager.scheduler_manager", manager)
    _patch_plugin_lookup(monkeypatch)

    executed: list[ScheduleContext] = []

    @manager.runtime_job(Trigger.interval(hours=1))
    async def _demo_job(bot: Bot, context: ScheduleContext) -> None:
        executed.append(context)

    monkeypatch.setattr(nonebot, "get_bot", _make_bot)
    context = ScheduleContext(
        schedule_id=0,
        plugin_name="ephemeral::demo_plugin::_demo_job",
        bot_id=None,
        group_id=None,
        job_kwargs={},
    )

    await _execute_job(None, context_override=context)

    assert executed == [context], "临时任务体未被执行（键名查找落空）"
