"""猪圈日报的按群推送。

日报的待发群来自云账本（所有账号的活动混在一起），必须按"这个群里有哪个账号"选号，
否则会被不在该群的账号尝试发送，整群推送直接失败。这里把"渲染 → 选号 → 发送"独立成
叶模块：既不再往插件入口里堆逻辑，也便于对每个群的选择/跳过行为做单测。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger

from zhenxun.services.group_bot_selector import select_group_bot

RenderSummary = Callable[[str, dict[str, Any]], Awaitable[Message]]
"""按 (group_id, summary) 渲染消息；调用方负责图片失败时的文本回退。"""


async def push_group_summaries(
    *,
    group_ids: Iterable[str],
    summaries: Mapping[str, dict[str, Any]],
    render: RenderSummary,
    summary_date: str,
    fallback_bot: Bot,
) -> tuple[int, int]:
    """按群选号推送日报，返回 (成功数, 未送达数)。

    - 某群没有任何在线账号：跳过该群，不再对着必然失败的目标空发。
    - 渲染或发送失败：只影响该群，其余群继续推送。
    """
    pending_groups = [str(group_id) for group_id in group_ids]
    sent_count = 0
    unsent_count = 0
    for group_id in pending_groups:
        try:
            message = await render(group_id, summaries[group_id])
        except Exception as render_error:
            logger.warning(
                f"[每日总结] 渲染失败，跳过该群: group={group_id} error={render_error}"
            )
            unsent_count += 1
            continue

        send_bot = await select_group_bot(group_id, fallback=fallback_bot)
        if send_bot is None:
            logger.warning(f"[每日总结] 群内没有可用账号，跳过推送: group={group_id}")
            unsent_count += 1
            continue

        try:
            await send_bot.send_group_msg(group_id=int(group_id), message=message)
            sent_count += 1
        except Exception as send_error:
            logger.warning(f"[每日总结] 推送失败: group={group_id} error={send_error}")
            unsent_count += 1

    logger.debug(
        f"[每日总结] 按群推送结束: date={summary_date} "
        f"pending={len(pending_groups)} sent={sent_count} unsent={unsent_count}"
    )
    return sent_count, unsent_count
