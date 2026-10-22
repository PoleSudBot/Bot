from __future__ import annotations

import asyncio
import contextlib
import os
import sys

import nonebot
from nonebot.adapters import Bot
from nonebot.plugin import PluginMetadata

from zhenxun.services.sekai_resource.config import (
    REGISTER_CONFIGS as SEKAI_RESOURCE_REGISTER_CONFIGS,
)
from zhenxun.services.sekai_resource.config import (
    refresh_settings as refresh_resource_settings,
)
from zhenxun.services.sekai_resource.config import (
    register_configs as register_resource_configs,
)

_TEST_MODE = bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules

if not _TEST_MODE:
    from zhenxun.services.log import logger
    from zhenxun.utils.manager.priority_manager import PriorityLifecycle
    from zhenxun.utils.platform import PlatformUtils

    from .b30 import b30_constants_provider
    from .commands import matcher
    from .config import get_settings, refresh_settings
    from .config_migration import migrate_legacy_plugin_config
    from .constants import MODULE_NAME
    from .master_data import master_data_service
    from .services import (
        build_auto_update_notifications,
        dispatch_live_reminders,
        dispatch_new_card_notifications,
        migrate_legacy_bindings,
        seed_default_aliases,
        sync_music_aliases,
    )
else:
    matcher = None

from .config import REGISTER_CONFIGS

register_resource_configs()

PLUGIN_REGISTER_CONFIGS = [*SEKAI_RESOURCE_REGISTER_CONFIGS, *REGISTER_CONFIGS]

__plugin_meta__ = PluginMetadata(
    name="MoeSekai",
    description="Project Sekai 档案、提醒与资料助手",
    usage="""
## 🌟 快速开始
`个人档案 / 查询档案 / 档案查询 [区服可选] [游戏ID|@用户可选]`
查看自己的绑定档案，或查询指定游戏 ID / 指定用户的公开档案。
示例：`个人档案` / `个人档案 @用户` / `查询档案 1234567890123`

`b30 / pjskb30 [区服可选] [游戏ID可选]`
查看自己的 Best 30，或查询指定游戏 ID 的公开 B30。
示例：`b30` / `b30 jp` / `pjskb30 1234567890123`

`绑定 [区服可选] <游戏ID>`
绑定游戏账号，不写区服时默认绑定 `jp`。
示例：`绑定1234567890123` / `绑定 jp 1234567890123` / `cn绑定 1234567890123`

## 👤 档案相关
`个人档案 / 查询档案 / 档案查询 [区服可选] [游戏ID|@用户可选]`(当前仅日服可用)
不带参数时查询自己的绑定档案；带游戏 ID 或 `@用户` 时按查询档案语义处理。

`给看 / 不给看`
控制别人能否通过“个人档案 @你”或“查询档案 @你”查看你的绑定档案。

`b30 / pjskb30 [区服可选] [游戏ID可选]`
查看 Best 30；不支持通过 @ 用户查询他人 B30。

## 🌍 区服与绑定
`绑定 [区服可选] <游戏ID>`
支持 `绑定 jp 1234567890123` 和 `jp绑定 1234567890123` 两种写法。

`解绑 [区服可选]`
不写区服时解绑当前默认区服。

`默认区服 [cn|jp|tw]`
查看或切换默认服务器。
示例：`默认区服` / `默认区服jp`

## 📚 资料查询
`活动剧情 <活动ID> [强制刷新]`
查看活动剧情总览。
示例：`活动剧情199` / `活动剧情 强制刷新 199`

`随机四格`
随机发送一张四格漫画，并附上对应 B 站链接。

`四格 <话数>`
查询指定话数的四格漫画，并附上对应 B 站链接。
示例：`四格351`

## 🔔 提醒订阅

`订阅live提醒 [区服]`
订阅自己在本群的 live 提醒艾特。
未绑定时默认订阅日服。

`取消订阅live提醒 [区服]`
取消自己在本群的 live 提醒订阅。
未绑定时默认作用于日服。

`live提醒 <开启|关闭|状态> [区服]`
群管理员或超级用户可开关群内 live 提醒。
未绑定时默认作用于日服。

`新卡上线提醒 <开启|关闭|状态> [区服]`
群管理员或超级用户可开关群内新卡/表情提醒。
未绑定时默认作用于日服。

## 🛠️ 管理命令
`pjsk update [区服可选|all]`
刷新 MasterData；不写区服时优先使用默认区服，未绑定则默认日服。
示例：`pjsk update` / `cnpjsk update`

`pjsk blacklist <add|remove|check> <uid|qq> ...`
超级用户管理 UID / QQ 黑名单。

`pjsk 查询绑定 <uid|qq> ...`
超级用户查询 QQ 或 UID 的绑定信息。

`pjsk test live提醒 [区服可选] [live_id可选]`
管理员 / 超级用户测试 live 提醒发送效果。

`pjsk test 新卡上线提醒 [区服可选] [card_id可选...]`
管理员 / 超级用户测试新卡上线提醒合并转发效果。

## 💡 说明
- 未指定区服时，优先使用你的默认区服；
  若命令本身不依赖绑定且你未绑定账号，则默认使用日服。
- pjsk update all 仅超级用户可用。
""".strip(),
    extra={
        "author": "k1yuyu",
        "version": "3.3.5",
        "menu_type": "游戏相关",
        "configs": [
            c.model_dump() if hasattr(c, "model_dump") else c.dict()
            for c in PLUGIN_REGISTER_CONFIGS
        ],
        "commands": [
            {"command": "个人档案 / 查询档案 / 档案查询 [区服可选] [游戏ID|@用户可选]"},
            {"command": "b30 / pjskb30 [区服可选] [游戏ID可选]"},
            {"command": "绑定 [区服可选] <游戏ID>"},
            {"command": "解绑 [区服可选]"},
            {"command": "默认区服 [cn|jp|tw]"},
            {"command": "给看 / 不给看"},
            {"command": "活动剧情 <活动ID> [强制刷新]"},
            {"command": "随机四格"},
            {"command": "四格 <话数>"},
            {"command": "live提醒 <开启|关闭|状态> [区服]"},
            {"command": "新卡上线提醒 <开启|关闭|状态> [区服]"},
            {"command": "订阅live提醒 / 取消订阅live提醒 [区服]"},
            {"command": "pjsk update [区服可选|all]"},
        ],
        "superuser_help": """
pjsk update [区服可选|all]
pjsk blacklist <add|remove|check> <uid|qq> ...
pjsk 查询绑定 <uid|qq> ...
pjsk test live提醒 [区服] [live_id]
pjsk test 新卡上线提醒 [区服] [card_id...]
""".strip(),
    },
)

if not _TEST_MODE:
    driver = nonebot.get_driver()
    _auto_update_task: asyncio.Task | None = None
    _live_reminder_task: asyncio.Task | None = None
    _alias_sync_task: asyncio.Task | None = None

    @PriorityLifecycle.on_startup(priority=2)
    async def _bootstrap_moesekai() -> None:
        if migrate_legacy_plugin_config():
            refresh_settings()
            refresh_resource_settings()
        else:
            get_settings()
        await migrate_legacy_bindings()
        await seed_default_aliases()

    async def _auto_update_loop(bot: Bot) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                results = await master_data_service.probe_next_updates()
                await b30_constants_provider.refresh_if_needed()
                if get_settings().master_notify_superusers:
                    for message in await build_auto_update_notifications(results):
                        await PlatformUtils.send_superuser(bot, message)
                await dispatch_new_card_notifications(bot, results)
            except Exception as exc:
                logger.error("MoeSekai 自动更新循环失败", MODULE_NAME, e=exc)
            await asyncio.sleep(master_data_service.get_probe_interval_seconds())

    async def _live_reminder_loop(bot: Bot) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await dispatch_live_reminders(bot)
            except Exception as exc:
                logger.error("MoeSekai Live 提醒循环失败", MODULE_NAME, e=exc)
            await asyncio.sleep(60)

    async def _alias_sync_loop() -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await sync_music_aliases(force=False)
            except Exception as exc:
                logger.error("MoeSekai 歌曲别名同步失败", MODULE_NAME, e=exc)
            await asyncio.sleep(get_settings().alias_sync_interval_seconds)

    @driver.on_bot_connect
    async def _start_background_tasks(bot: Bot) -> None:
        global _auto_update_task, _live_reminder_task, _alias_sync_task
        if _auto_update_task is None or _auto_update_task.done():
            _auto_update_task = asyncio.create_task(_auto_update_loop(bot))
        if _live_reminder_task is None or _live_reminder_task.done():
            _live_reminder_task = asyncio.create_task(_live_reminder_loop(bot))
        if _alias_sync_task is None or _alias_sync_task.done():
            _alias_sync_task = asyncio.create_task(_alias_sync_loop())

    @driver.on_shutdown
    async def _stop_background_tasks() -> None:
        global _auto_update_task, _live_reminder_task, _alias_sync_task
        for task in (_auto_update_task, _live_reminder_task, _alias_sync_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        _auto_update_task = None
        _live_reminder_task = None
        _alias_sync_task = None


__all__ = ["__plugin_meta__", "matcher"]
