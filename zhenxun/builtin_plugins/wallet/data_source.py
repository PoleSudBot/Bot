import asyncio
from pathlib import Path

from nonebot_plugin_uninfo import Uninfo
from tortoise.expressions import Subquery

from zhenxun import ui
from zhenxun.configs.path_config import IMAGE_PATH
from zhenxun.models.friend_user import FriendUser
from zhenxun.models.group_member_info import GroupInfoUser
from zhenxun.models.user_console import UserConsole
from zhenxun.services import avatar_service
from zhenxun.ui.models import ImageCell, TextCell
from zhenxun.utils.platform import PlatformUtils

ICON_PATH = IMAGE_PATH / "_icon"
_AVATAR_FETCH_CONCURRENCY = 8

PLATFORM_PATH = {
    "dodo": ICON_PATH / "dodo.png",
    "discord": ICON_PATH / "discord.png",
    "kaiheila": ICON_PATH / "kook.png",
    "qq": ICON_PATH / "qq.png",
}


async def get_balance(user_id: str, platform: str | None = None) -> int:
    """获取用户钱包余额"""
    user = await UserConsole.get_user(user_id, platform)
    return user.gold


async def wallet_rank(session: Uninfo, group_id: str | None, num: int) -> bytes | str:
    """生成钱包排行榜"""
    platform = PlatformUtils.get_platform(session)
    current_user = await UserConsole.get_user(session.user.id, platform)
    query = UserConsole.all()
    user_in_scope = True
    if group_id:
        member_query = GroupInfoUser.filter(group_id=group_id)
        user_in_scope = await member_query.filter(user_id=session.user.id).exists()
        query = query.filter(
            user_id__in=Subquery(member_query.values_list("user_id", flat=True))
        )

    user_list = await query.order_by("-gold", "uid").limit(num).values_list(
        "user_id", "gold", "platform", "uid"
    )
    current_rank: int | None = None
    if user_in_scope:
        current_rank = await query.filter(gold__gt=current_user.gold).count() + 1

    user_id_list = [user[0] for user in user_list]
    lookup_user_ids = list(dict.fromkeys([*user_id_list, session.user.id]))
    friend_user = await FriendUser.filter(user_id__in=lookup_user_ids).values_list(
        "user_id", "user_name"
    )
    uid2name = {user[0]: user[1] for user in friend_user}
    if diff_id := set(lookup_user_ids).difference(set(uid2name.keys())):
        group_query = GroupInfoUser.filter(user_id__in=diff_id)
        if group_id:
            group_query = group_query.filter(group_id=group_id)
        group_user = await group_query.values_list("user_id", "user_name")
        for group_member in group_user:
            uid2name[group_member[0]] = group_member[1]

    column_name = ["排名", "-", "名称", "金币", "平台"]

    avatar_keys = list(
        dict.fromkeys(
            [
                *((user[2] or "qq", user[0]) for user in user_list),
                (current_user.platform or "qq", session.user.id),
            ]
        )
    )
    semaphore = asyncio.Semaphore(min(_AVATAR_FETCH_CONCURRENCY, len(avatar_keys)))

    async def fetch_avatar(
        platform: str, user_id: str
    ) -> tuple[tuple[str, str], Path | None]:
        async with semaphore:
            avatar_path = await avatar_service.get_avatar_path(platform, user_id)
        return (platform, user_id), avatar_path

    avatar_paths = dict(
        await asyncio.gather(
            *(fetch_avatar(platform, user_id) for platform, user_id in avatar_keys)
        )
    )

    def build_row(
        rank: int | str, user_id: str, gold: int, user_platform: str | None
    ) -> list[TextCell | ImageCell]:
        resolved_platform = user_platform or "qq"
        avatar_path = avatar_paths.get((resolved_platform, user_id))
        platform_path = PLATFORM_PATH.get(resolved_platform)
        return [
            TextCell(content=str(rank)),
            ImageCell(
                src=avatar_path.as_uri() if avatar_path else "", shape="circle"
            )
            if avatar_path
            else TextCell(content=""),
            TextCell(content=uid2name.get(user_id) or user_id),
            TextCell(content=str(gold), bold=True),
            ImageCell(src=platform_path.resolve().as_uri())
            if platform_path
            else TextCell(content=""),
        ]

    data_list = []
    previous_gold: int | None = None
    display_rank = 0
    for index, user in enumerate(user_list, start=1):
        if user[1] != previous_gold:
            display_rank = index
            previous_gold = user[1]
        data_list.append(build_row(display_rank, user[0], user[1], user[2]))

    current_user_row = build_row(
        current_rank or "未上榜",
        session.user.id,
        current_user.gold,
        current_user.platform,
    )

    if group_id:
        title = "金币群组排行"
    else:
        title = "金币总排行"

    table = ui.table(title)
    table.set_headers(column_name).add_rows(data_list).add_divider().add_row(
        current_user_row
    )
    return await ui.render(table)
