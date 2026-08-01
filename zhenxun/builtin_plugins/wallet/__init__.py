from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import (
    Alconna,
    AlconnaQuery,
    Args,
    Arparma,
    At,
    CommandMeta,
    Field,
    Match,
    MultiVar,
    Option,
    Query,
    Subcommand,
    on_alconna,
    store_true,
)
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.utils import BaseBlock, Command, PluginExtraData
from zhenxun.models.user_console import UserConsole
from zhenxun.services.log import logger
from zhenxun.utils.enum import BlockType, PluginType
from zhenxun.utils.exception import InsufficientGold
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils

from .data_source import get_balance, wallet_rank

__plugin_meta__ = PluginMetadata(
    name="钱包",
    description="查看金币余额、排行与转账",
    usage="""
    指令：
        钱包
        金币排行 [数量=10]
        金币总排行 [数量=10]
        转账<金额>@用户
        转账@用户<金额>
    """.strip(),
    extra=PluginExtraData(
        author="HibiKier",
        version="0.1",
        plugin_type=PluginType.NORMAL,
        menu_type="钱包",
        commands=[
            Command(command="钱包"),
            Command(command="金币排行"),
            Command(command="金币总排行"),
            Command(command="转账"),
        ],
        limits=[
            BaseBlock(
                check_type=BlockType.GROUP,
                result="钱包操作处理中，请稍候。",
            )
        ],
    ).to_dict(),
)

_command = Alconna(
    "钱包",
    Option("--global", action=store_true),
    Subcommand("balance", Args["at_user?", At], help_text="查看钱包余额"),
    Subcommand(
        "rank",
        Args[
            "num?",
            int,
            Field(unmatch_tips=lambda _: "排行榜人数必须为 1 到 50 之间的整数。"),
        ],
        help_text="查看金币排名",
    ),
)

_matcher = on_alconna(
    _command,
    skip_for_unmatch=False,
    priority=5,
    block=True,
)

_transfer_command = Alconna(
    "转账",
    Args["parts?", MultiVar(At | int, "*")],
    meta=CommandMeta(compact=True, hide_shortcut=True),
)

_transfer_matcher = on_alconna(
    _transfer_command,
    skip_for_unmatch=True,
    priority=5,
    block=False,
)

_matcher.shortcut(
    "我的金币",
    command="钱包",
    arguments=["balance"],
    prefix=True,
)

_matcher.shortcut(
    "查看我的钱包",
    command="钱包",
    arguments=["balance"],
    prefix=True,
)

_matcher.shortcut(
    "查看钱包余额",
    command="钱包",
    arguments=["balance"],
    prefix=True,
)

_matcher.shortcut(
    "金币排行",
    command="钱包",
    arguments=["rank"],
    prefix=True,
)

_matcher.shortcut(
    "金币总排行",
    command="钱包",
    arguments=["--global", "rank"],
    prefix=True,
)

_matcher.shortcut(
    "查看钱包排名",
    command="钱包",
    arguments=["rank"],
    prefix=True,
)

_transfer_matcher.shortcut(
    "钱包转账",
    command="转账",
    prefix=True,
)


@_matcher.assign("$main")
async def _(session: Uninfo, arparma: Arparma):
    gold = await get_balance(session.user.id, PlatformUtils.get_platform(session))
    logger.info("查看钱包", arparma.header_result, session=session)
    await MessageUtils.build_message(f"你的钱包余额: {gold}").send(reply_to=True)


@_matcher.assign("balance")
async def _(session: Uninfo, arparma: Arparma, at_user: Match[At]):
    target_id = at_user.result.target if at_user.available else session.user.id
    gold = await get_balance(target_id, PlatformUtils.get_platform(session))
    logger.info("查看钱包余额", arparma.header_result, session=session)
    if at_user.available:
        await MessageUtils.build_message(
            ["用户", at_user.result, f"的钱包余额: {gold}"]
        ).send(reply_to=True)
    else:
        await MessageUtils.build_message(f"你的钱包余额: {gold}").send(reply_to=True)


@_matcher.assign("rank")
async def _(
    session: Uninfo, arparma: Arparma, num: Query[int] = AlconnaQuery("num", 10)
):
    if not 1 <= num.result <= 50:
        await MessageUtils.build_message("排行榜人数必须在 1 到 50 之间。").finish(
            reply_to=True
        )
    group_id = session.group.id if session.group else None
    if not arparma.find("global") and not group_id:
        await MessageUtils.build_message(
            "私聊中无法查看 '金币排行'，请发送 '金币总排行'"
        ).finish()
    if arparma.find("global"):
        group_id = None
    result = await wallet_rank(session, group_id, num.result)
    logger.info("查看钱包排名", arparma.header_result, session=session)
    await MessageUtils.build_message(result).send(reply_to=True)


def _resolve_transfer_parts(values: tuple[At | int, ...]) -> tuple[int, At] | None:
    if len(values) != 2:
        return None
    gold_values = [value for value in values if isinstance(value, int)]
    at_users = [value for value in values if isinstance(value, At)]
    if len(gold_values) != 1 or len(at_users) != 1:
        return None
    return gold_values[0], at_users[0]


@_transfer_matcher.handle()
async def _handle_transfer(
    matcher: Matcher,
    session: Uninfo,
    arparma: Arparma,
    parts: Match[tuple[At | int, ...]],
):
    values = parts.result if parts.available else ()
    if not (transfer := _resolve_transfer_parts(values)):
        return
    gold, at_user = transfer
    matcher.stop_propagation()
    platform = PlatformUtils.get_platform(session)
    try:
        await UserConsole.transfer_gold(
            session.user.id,
            at_user.target,
            gold,
            platform,
        )
    except ValueError as e:
        await MessageUtils.build_message(str(e)).finish(reply_to=True)
        return
    except InsufficientGold:
        await MessageUtils.build_message("钱包余额不足，转账失败。").finish(
            reply_to=True
        )
        return

    logger.info(
        f"钱包转账 {gold} -> {at_user.target}",
        arparma.header_result,
        session=session,
    )
    await MessageUtils.build_message(
        ["转账成功，已给", at_user, f"转账 {gold} 金币。"]
    ).send(reply_to=True)
