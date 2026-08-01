from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me
from nonebot_plugin_alconna import Alconna, on_alconna

from zhenxun import ui
from zhenxun.configs.utils import PluginExtraData
from zhenxun.services.log import logger
from zhenxun.utils.enum import PluginType
from zhenxun.utils.message import MessageUtils

from .data_source import get_status_info

__plugin_meta__ = PluginMetadata(
    name="服务器自我检查",
    description="查看服务器当前状态",
    usage="""
    查看服务器当前状态
    指令:
        自检
    """.strip(),
    extra=PluginExtraData(
        author="HibiKier",
        version="0.3",
        plugin_type=PluginType.SUPERUSER,
    ).to_dict(),
)


_self_check_matcher = on_alconna(
    Alconna("自检"),
    rule=to_me(),
    permission=SUPERUSER,
    block=True,
    priority=1,
)


async def handle_self_check():
    try:
        data_dict = await get_status_info()

        image_bytes = await ui.render_template(
            "pages/builtin/check",
            data=data_dict,
        )

        await MessageUtils.build_message(image_bytes).send()
        logger.info("自检成功", "自检")
    except Exception as e:
        await MessageUtils.build_message(f"自检失败: {e}").send()
        logger.error("自检失败", "自检", e=e)


@_self_check_matcher.handle()
async def handle_message_check():
    await handle_self_check()
