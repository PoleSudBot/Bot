from decimal import Decimal
import re

from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import Alconna, Args, Arparma, At, Field, on_alconna
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.utils import PluginExtraData
from zhenxun.models.sign_user import SignUser
from zhenxun.models.user_console import UserConsole
from zhenxun.services.log import logger
from zhenxun.utils.enum import PluginType
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils

__plugin_meta__ = PluginMetadata(
    name="高贵的作弊器",
    description="这是一个作弊器，设置用户金币数量和好感度",
    usage="""
    金币设置 100 @用户（设置绝对余额，允许负数）
    金币增减 +100 @用户（按差额调整，允许负数）
    金币增减 -50 @用户
    好感度设置 100（好感度） @用户
    """.strip(),
    extra=PluginExtraData(
        author="HibiKier",
        version="0.1",
        plugin_type=PluginType.SUPERUSER,
    ).to_dict(),
)

_gold_matcher = on_alconna(
    Alconna(
        "金币设置",
        Args[
            "gold",
            int,
            Field(
                missing_tips=lambda: "请在命令后跟随金币数量！",
                unmatch_tips=lambda _: "金币数量必须为数字！",
            ),
        ][
            "at_user",
            At,
            Field(
                missing_tips=lambda: "必须要at一名指定用户！",
            ),
        ],
    ),
    skip_for_unmatch=False,
    permission=SUPERUSER,
    priority=5,
    block=True,
)

_gold_adjust_matcher = on_alconna(
    Alconna(
        "金币增减",
        Args[
            "delta",
            str,
            Field(
                missing_tips=lambda: "请在命令后跟随带正负号的金币数量！",
            ),
        ][
            "at_user",
            At,
            Field(
                missing_tips=lambda: "必须要at一名指定用户！",
            ),
        ],
    ),
    skip_for_unmatch=False,
    permission=SUPERUSER,
    priority=5,
    block=True,
)


_impression_matcher = on_alconna(
    Alconna(
        "好感度设置",
        Args[
            "impression",
            float,
            Field(
                missing_tips=lambda: "请在命令后跟随好感度！",
                unmatch_tips=lambda _: "好感度数量必须为数字！",
            ),
        ][
            "at_user",
            At,
            Field(
                missing_tips=lambda: "必须要at一名指定用户！",
            ),
        ],
    ),
    skip_for_unmatch=False,
    permission=SUPERUSER,
    priority=5,
    block=True,
)


def _parse_signed_delta(delta: str) -> int:
    if not re.fullmatch(r"[+-]\d+", delta):
        raise ValueError(
            "金币调整数量必须是带正负号的整数，例如 +100 或 -50。"
        )
    return int(delta)


@_gold_matcher.handle()
async def _(session: Uninfo, arparma: Arparma, gold: int, at_user: At):
    old_balance, new_balance = await UserConsole.set_gold(
        at_user.target,
        gold,
        f"superuser:set:{session.user.id}",
        PlatformUtils.get_platform(session),
    )
    if old_balance == new_balance:
        await MessageUtils.build_message(
            ["用户", at_user, f"的金币已经是 {new_balance}，未发生变化。"]
        ).send(reply_to=True)
        return
    await MessageUtils.build_message(
        [
            "已将用户",
            at_user,
            f"的金币从 {old_balance} 设置为 {new_balance}"
            f"（变化 {new_balance - old_balance:+d}）。",
        ]
    ).send(reply_to=True)
    logger.info(
        f"成功将用户{at_user.target}的金币设置为{gold}",
        arparma.header_result,
        session=session,
    )


@_gold_adjust_matcher.handle()
async def _(session: Uninfo, arparma: Arparma, delta: str, at_user: At):
    try:
        delta_value = _parse_signed_delta(delta)
        old_balance, new_balance = await UserConsole.adjust_gold(
            at_user.target,
            delta_value,
            f"superuser:adjust:{session.user.id}",
            PlatformUtils.get_platform(session),
        )
    except ValueError as e:
        await MessageUtils.build_message(str(e)).finish(reply_to=True)
        return
    await MessageUtils.build_message(
        [
            "已调整用户",
            at_user,
            f"的金币 {delta_value:+d}：{old_balance} → {new_balance}。",
        ]
    ).send(reply_to=True)
    logger.info(
        f"成功调整用户{at_user.target}的金币{delta_value:+d}，"
        f"余额{old_balance}->{new_balance}",
        arparma.header_result,
        session=session,
    )


@_impression_matcher.handle()
async def _(session: Uninfo, arparma: Arparma, impression: float, at_user: At):
    platform = PlatformUtils.get_platform(session)
    user_console = await UserConsole.get_user(at_user.target, platform)
    user, _ = await SignUser.get_or_create(
        user_id=at_user.target,
        defaults={"user_console": user_console, "platform": platform},
    )
    user.impression = Decimal(impression)
    await user.save(update_fields=["impression"])
    await MessageUtils.build_message(
        ["成功将用户", at_user, f"的好感度设置为 {impression}"]
    ).send(reply_to=True)
    logger.info(
        f"成功将用户{at_user.target}的好感度设置为{impression}",
        arparma.header_result,
        session=session,
    )
