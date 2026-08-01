import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Literal, Union
from nonebot import on_message
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule
from nonebot.typing import T_State
from arclet.alconna import Alconna, Args, Arparma, MultiVar, Option, Subcommand
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    Bot as V11Bot,
    MessageEvent,
)
from nonebot_plugin_alconna import At, Text, on_alconna
from nonebot_plugin_alconna.uniseg import Image
from nonebot_plugin_alconna.uniseg.tools import reply_fetch
from nonebot_plugin_uninfo import Uninfo
from nonebot_plugin_waiter import waiter

from zhenxun.configs.config import Config
from zhenxun.services.log import logger
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.rules import admin_check

from ..model import Quote
from ..services.quote_service import QuoteService
from ..config import QUOTE_ASSETS_PATH
from ..utils.tag_utils import (
    extract_manual_tags,
    extract_manual_tags_with_mention_names,
)
from ..utils.exceptions import ImageProcessError
from ..utils.reply_images import extract_direct_images, extract_forward_images

_IMAGE_STEM_MD5_RE = re.compile(r"(?i)[0-9a-f]{32}")
_REPLY_QUOTE_STATE_KEY = "reply_quote"


@dataclass(frozen=True)
class ReplyQuoteResolution:
    quotes: list[Quote]
    image_count: int = 0
    unresolved_count: int = 0


async def _get_image_from_reply(event: Event, bot: Bot) -> Optional[Image]:
    """
    从回复消息中提取图片。
    此函数总是通过API获取消息详情，并直接解析返回的原始数据。
    """
    if not (reply := await reply_fetch(event, bot)):
        return None

    if not isinstance(bot, V11Bot):
        logger.warning(
            f"当前 Bot 类型 ({type(bot)}) 不支持通过API获取消息，无法从回复中提取图片。"
        )
        return None

    try:
        msg_info = await bot.get_msg(message_id=int(reply.id))
        raw_message_data = msg_info.get("message")
    except Exception as e:
        logger.error(
            f"通过 API get_msg(id={reply.id}) 获取回复消息失败: {e}", "群聊语录", e=e
        )
        return None

    if not raw_message_data:
        logger.debug(f"通过 API get_msg(id={reply.id}) 获取到的消息内容为空。")
        return None

    segment_dicts = []
    if isinstance(raw_message_data, dict):
        segment_dicts.append(raw_message_data)
    elif isinstance(raw_message_data, list):
        segment_dicts = raw_message_data
    else:
        logger.debug(f"获取到的消息内容为字符串，不含图片: {raw_message_data}")
        return None

    for seg_dict in segment_dicts:
        if isinstance(seg_dict, dict) and seg_dict.get("type") == "image":
            data = seg_dict.get("data", {})
            return Image(id=data.get("file"), url=data.get("url"))

    return None


def _extract_reply_image_identifiers(image_seg: Image) -> tuple[str | None, str | None]:
    """
    从回复图片里提取语录标识。

    这里不能继续依赖平台回传的完整文件名，因为 OneBot 发出的图片在被回复时
    可能会变成大写文件名或不同扩展名。我们只认文件名主干是不是完整的 32 位
    MD5，这样既能兼容 `.png/.jpg` 差异，也能避免把 URL 参数里的其他十六进制
    字符串误识别为语录图片标识。
    """

    fallback_basename: str | None = None
    sources = (
        ("id", image_seg.id),
        ("path", getattr(image_seg, "path", None)),
        ("url", image_seg.url),
    )

    for source_name, raw_value in sources:
        if not raw_value:
            continue

        value = str(raw_value)
        if "://" in value:
            value = urlparse(value).path or value

        image_basename = os.path.basename(value)
        if not image_basename:
            continue

        if fallback_basename is None:
            fallback_basename = image_basename

        image_stem = Path(image_basename).stem
        if _IMAGE_STEM_MD5_RE.fullmatch(image_stem):
            image_md5 = image_stem.lower()
            logger.debug(
                "回复图片标识提取成功 - 来源: %s, basename: %s, md5: %s"
                % (source_name, image_basename, image_md5),
                "群聊语录",
            )
            return image_md5, image_basename

        logger.debug(
            f"回复图片来源 {source_name} 未命中语录 md5，basename: {image_basename}",
            "群聊语录",
        )

    if fallback_basename:
        logger.debug(
            f"回复图片未提取到 md5，将回退到 basename 链路: {fallback_basename}",
            "群聊语录",
        )
    return None, fallback_basename


async def _get_images_from_reply(
    event: Event, bot: Bot
) -> tuple[list[Image], int]:
    if not (reply := await reply_fetch(event, bot)):
        return [], 0

    if not isinstance(bot, V11Bot):
        logger.warning(
            f"当前 Bot 类型 ({type(bot)}) 不支持通过API获取消息，无法从回复中提取图片。"
        )
        return [], 0

    try:
        msg_info = await bot.get_msg(message_id=int(reply.id))
        raw_message_data = msg_info.get("message")
    except Exception as e:
        logger.error(
            f"通过 API get_msg(id={reply.id}) 获取回复消息失败: {e}", "群聊语录", e=e
        )
        return [], 0

    direct_images = await extract_direct_images(bot, raw_message_data)
    unresolved_count = 0
    try:
        forward_images, has_forward = await extract_forward_images(
            bot, raw_message_data
        )
    except ImageProcessError as e:
        logger.warning(f"解析回复中的合并转发失败: {e}", "群聊语录", e=e)
        forward_images, has_forward = [], True

    if has_forward and not forward_images:
        unresolved_count += 1
    return direct_images + forward_images, unresolved_count


async def _get_quotes_from_reply(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    *,
    recycled_only: bool = False,
) -> ReplyQuoteResolution:
    if not session.group or not await is_reply_to_bot(event):
        return ReplyQuoteResolution([])

    image_segments, unresolved_count = await _get_images_from_reply(event, bot)
    quotes: list[Quote] = []
    seen_quote_ids: set[int] = set()

    for image_seg in image_segments:
        image_md5, image_basename = _extract_reply_image_identifiers(image_seg)
        if not image_md5 and not image_basename:
            unresolved_count += 1
            continue

        lookup_kwargs = {
            "reply_image_md5": image_md5,
            "reply_image_basename": image_basename,
        }
        if recycled_only:
            lookup_kwargs["recycled_only"] = True
        quote = await QuoteService.find_quote_by_reply_image(
            session.group.id,
            **lookup_kwargs,
        )
        if not quote:
            unresolved_count += 1
            continue
        if quote.id not in seen_quote_ids:
            seen_quote_ids.add(quote.id)
            quotes.append(quote)

    return ReplyQuoteResolution(
        quotes=quotes,
        image_count=len(image_segments),
        unresolved_count=unresolved_count,
    )


async def _get_quote_from_reply(
    bot: Bot, event: MessageEvent, session: Uninfo
) -> Quote | None:
    resolution = await _get_quotes_from_reply(bot, event, session)
    return resolution.quotes[0] if resolution.quotes else None


def _as_quotes(
    value: Quote | list[Quote] | ReplyQuoteResolution | None,
) -> list[Quote]:
    if isinstance(value, ReplyQuoteResolution):
        return value.quotes
    if isinstance(value, list):
        return value
    return [value] if value else []


async def uploader_or_admin_check(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    quote: Quote | None = None,
) -> bool:
    """
    检查执行删除操作的用户是否为语录上传者，或者是满足配置权限的管理员。
    """
    if await admin_check("quote", "DELETE_ADMIN_LEVEL")(bot, event, session):
        return True

    if session.group:
        user_id = session.user.id
        quote = quote or await _get_quote_from_reply(bot, event, session)
        if quote and quote.uploader_user_id == user_id:
            return True
    return False


async def deleter_or_admin_check(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    quote: Quote,
) -> bool:
    """仅允许原删除者或当前满足删除权限的管理员恢复语录。"""
    if await admin_check("quote", "DELETE_ADMIN_LEVEL")(bot, event, session):
        return True
    return str(getattr(quote, "deleted_by_user_id", "") or "") == str(
        session.user.id
    )


async def is_reply_to_bot(event: Event) -> bool:
    """检查消息是否为对机器人自身消息的回复"""
    if not isinstance(event, MessageEvent):
        return False

    reply = getattr(event, "reply", None)
    sender = getattr(reply, "sender", None) if reply else None
    sender_id = getattr(sender, "user_id", None) if sender else None

    return sender_id is not None and str(sender_id) == str(
        getattr(event, "self_id", "")
    )


def reply_to_quote_rule() -> Rule:
    """仅在回复机器人发出的语录图片时匹配命令"""

    async def _rule(bot: Bot, event: Event, session: Uninfo):
        if not isinstance(event, MessageEvent):
            return False
        return bool((await _get_quotes_from_reply(bot, event, session)).quotes)

    return Rule(_rule)


async def _match_reply_quote_delete(
    bot: Bot,
    event: Event,
    session: Uninfo,
    state: T_State,
) -> bool:
    """
    判断当前消息是否应由语录插件接管“回复删除”。

    这里必须先把纯文本严格收窄为“删除”，再确认回复目标确实能解析到语录，
    否则会误拦普通聊天里的“删除”，或者抢走其他插件自己的回复删除逻辑。
    """
    if not isinstance(event, MessageEvent):
        return False

    if event.get_plaintext().strip() != "删除":
        return False

    resolution = await _get_quotes_from_reply(bot, event, session)
    if resolution.quotes:
        # 规则阶段已完成解析，缓存结果避免重复请求历史消息。
        state[_REPLY_QUOTE_STATE_KEY] = resolution
        return True
    return False


def reply_to_quote_delete_rule() -> Rule:
    """仅在纯文本为“删除”且回复目标确实是语录图时匹配"""

    async def _rule(bot: Bot, event: Event, session: Uninfo, state: T_State):
        return await _match_reply_quote_delete(bot, event, session, state)

    return Rule(_rule)


async def _require_reply_quote(
    bot: Bot, event: MessageEvent, session: Uninfo
) -> Quote | None:
    quote = await _get_quote_from_reply(bot, event, session)
    if quote:
        return quote

    await MessageUtils.build_message("请回复 Bot 发出的语录图片后再使用该命令。").send(
        target=event, bot=bot
    )
    return None


async def _require_reply_quotes(
    bot: Bot, event: MessageEvent, session: Uninfo
) -> ReplyQuoteResolution | None:
    resolution = await _get_quotes_from_reply(bot, event, session)
    if resolution.quotes:
        return resolution

    await MessageUtils.build_message(
        "请回复 Bot 发出的语录图片或合并转发后再使用该命令。"
    ).send(target=event, bot=bot)
    return None


async def _build_tag_view_message(
    quotes: Quote | list[Quote],
    show_all: bool = True,
    *,
    bot: Bot | None = None,
) -> list[object]:
    quote_list = _as_quotes(quotes)
    message: list[object] = []
    uploader_names: dict[tuple[str, str], str] = {}

    async def resolve_uploader_name(quote: Quote) -> str:
        uploader_user_id = str(getattr(quote, "uploader_user_id", "") or "").strip()
        if not uploader_user_id:
            return "未知"

        group_id = str(getattr(quote, "group_id", "") or "").strip()
        cache_key = (group_id, uploader_user_id)
        if cache_key in uploader_names:
            return uploader_names[cache_key]

        display_name = uploader_user_id
        if bot and group_id:
            try:
                member_info = await bot.get_group_member_info(
                    group_id=int(group_id),
                    user_id=int(uploader_user_id),
                )
                if isinstance(member_info, dict):
                    display_name = str(
                        member_info.get("card")
                        or member_info.get("nickname")
                        or uploader_user_id
                    ).strip()
            except Exception as e:
                logger.debug(
                    f"获取语录上传者群名片失败，将回退 QQ 号: {e}", "群聊语录"
                )

        uploader_names[cache_key] = display_name
        return display_name

    for index, quote in enumerate(quote_list, start=1):
        if index > 1:
            message.append("\n")
        if len(quote_list) > 1:
            message.append(f"{index}. ")
        message.append("tag：")
        tag_groups = await QuoteService.get_quote_tag_groups(quote)
        if not tag_groups:
            message.append("无")
        else:
            for group_index, members in enumerate(tag_groups):
                if group_index:
                    message.append("；")
                message.extend(QuoteService.format_tags_for_message([members[0]]))
                if len(members) > 1:
                    message.append("（")
                    for member_index, member in enumerate(members[1:]):
                        if member_index:
                            message.append("、")
                        message.extend(QuoteService.format_tags_for_message([member]))
                    message.append("）")

        uploader_user_id = str(getattr(quote, "uploader_user_id", "") or "").strip()
        uploader_name = await resolve_uploader_name(quote)
        message.append("\n上传者：")
        message.append(
            f"{uploader_name}（{uploader_user_id}）"
            if uploader_user_id and uploader_name != uploader_user_id
            else uploader_name
        )
    return message or ["没有可显示的 tag。"]


async def _show_quote_tags(
    bot: Bot,
    event: MessageEvent,
    quotes: Quote | list[Quote],
    show_all: bool = True,
    unresolved_count: int = 0,
):
    message = await _build_tag_view_message(quotes, show_all=show_all, bot=bot)
    if unresolved_count:
        message.append(f"\n未识别图片/转发节点：{unresolved_count}")
    await MessageUtils.build_message(
        message
    ).send(target=event, bot=bot)


async def _update_quote_manual_tags(
    bot: Bot,
    event: MessageEvent,
    quotes: Quote | list[Quote],
    tags: list[str],
    action: Literal["add", "del"],
    unresolved_count: int = 0,
):
    normalized_tags = QuoteService.normalize_tags(tags)
    if not normalized_tags:
        await MessageUtils.build_message(
            f"请在命令后附带至少一个 tag 或 @用户，例如：tag {action} 标签名"
        ).send(target=event, bot=bot)
        return

    quote_list = _as_quotes(quotes)
    results = []
    for quote in quote_list:
        if action == "add":
            result = await QuoteService.add_manual_tags(quote, normalized_tags)
        else:
            result = await QuoteService.delete_manual_tags(quote, normalized_tags)
        results.append(result)

    if len(quote_list) == 1:
        result = results[0]
        if not result.success:
            await MessageUtils.build_message("修改 tag 失败，请稍后再试。").send(
                target=event, bot=bot
            )
            return
        changed_tags = result.changed_tags
        if action == "add":
            prefix = "已添加 tag：" if changed_tags else "这些 tag 已经都在了："
        else:
            prefix = (
                "已删除 tag："
                if changed_tags
                else "这些 tag 当前都不存在："
            )
        display_tags = changed_tags or normalized_tags
        message = [
            prefix,
            *QuoteService.format_tags_for_message(display_tags),
            "\n当前 tag：",
            *QuoteService.format_tags_for_message(result.tags),
        ]
    else:
        action_text = "添加" if action == "add" else "删除"
        success_count = sum(result.success for result in results)
        message = [
            f"已为 {success_count}/{len(quote_list)} 条语录{action_text} tag："
        ]
        message.extend(QuoteService.format_tags_for_message(normalized_tags))
        for index, result in enumerate(results, start=1):
            message.append(f"\n{index}. 当前 tag：")
            message.extend(
                QuoteService.format_tags_for_message(
                    result.tags if result.success else []
                )
            )
        failed_count = len(quote_list) - success_count
        if failed_count:
            message.append(f"\n修改失败：{failed_count} 条")

    if unresolved_count:
        message.append(f"\n未识别图片/转发节点：{unresolved_count}")

    await MessageUtils.build_message(message).send(target=event, bot=bot)


async def _handle_delete_reply_quote(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    quote: Quote | list[Quote] | ReplyQuoteResolution | None = None,
    state: T_State | None = None,
):
    """处理回复语录图片后的删除逻辑"""
    group_id = session.group.id
    user_id = session.user.id

    # 回复删除的规则阶段已经做过一次解析，这里优先复用缓存，避免重复请求历史消息。
    cached_quote = state.get(_REPLY_QUOTE_STATE_KEY) if state else None
    quote_value = quote or cached_quote
    if quote_value is None:
        quote_value = await _get_quotes_from_reply(bot, event, session)
    unresolved_count = (
        quote_value.unresolved_count
        if isinstance(quote_value, ReplyQuoteResolution)
        else 0
    )
    quotes = _as_quotes(quote_value)
    if not quotes:
        logger.info(
            f"尝试删除语录失败，回复内容未能定位到群组 {group_id} 中的语录。",
            "群聊语录",
        )
        return

    authorized = [
        await uploader_or_admin_check(bot, event, session, quote=item)
        for item in quotes
    ]
    if not all(authorized):
        unauthorized_count = authorized.count(False)
        await MessageUtils.build_message(
            [
                At(target=user_id, flag="user"),
                f" 其中 {unauthorized_count} 条语录无删除权限，已取消整批删除",
                *(
                    [f"；另有 {unresolved_count} 个图片/转发节点未识别"]
                    if unresolved_count
                    else []
                ),
            ]
        ).send(target=event, bot=bot)
        return

    deleted_count = 0
    for item in quotes:
        if await QuoteService.delete_quote_instance(
            item,
            deleted_by_user_id=str(user_id),
        ):
            deleted_count += 1

    if deleted_count == len(quotes):
        message = [
            At(target=user_id, flag="user"),
            (
                f" 已移入回收站，共 {deleted_count} 条；7 日后彻底删除；"
                "回复原语录发送「恢复语录」可恢复"
            ),
        ]
    else:
        status_text = (
            f" 删除完成：成功 {deleted_count} 条，"
            f"失败 {len(quotes) - deleted_count} 条"
        )
        if deleted_count:
            status_text += (
                "；成功项已移入回收站，"
                "回复原语录发送「恢复语录」可恢复"
            )
        message = [
            At(target=user_id, flag="user"),
            status_text,
        ]
    if unresolved_count:
        message.append(f"；未识别图片/转发节点：{unresolved_count}")
    await MessageUtils.build_message(message).send(target=event, bot=bot)


async def _handle_restore_reply_quote(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    resolution: ReplyQuoteResolution,
):
    user_id = str(session.user.id)
    quotes = resolution.quotes
    authorized = [
        await deleter_or_admin_check(bot, event, session, quote=item)
        for item in quotes
    ]
    if not all(authorized):
        unauthorized_count = authorized.count(False)
        await MessageUtils.build_message(
            [
                At(target=user_id, flag="user"),
                f" 其中 {unauthorized_count} 条语录无恢复权限，已取消整批恢复",
            ]
        ).send(target=event, bot=bot)
        return

    restored_count = 0
    for item in quotes:
        if await QuoteService.restore_quote_instance(item):
            restored_count += 1

    if restored_count == len(quotes):
        message: list[object] = [
            At(target=user_id, flag="user"),
            f" 恢复成功，共 {restored_count} 条",
        ]
    else:
        message = [
            At(target=user_id, flag="user"),
            (
                f" 恢复完成：成功 {restored_count} 条，"
                f"失败 {len(quotes) - restored_count} 条"
            ),
        ]
    if resolution.unresolved_count:
        message.append(f"；未识别图片/转发节点：{resolution.unresolved_count}")
    await MessageUtils.build_message(message).send(target=event, bot=bot)


async def _handle_delete_last_quote(bot: Bot, event: MessageEvent, session: Uninfo):
    """处理直接删除本群上一条语录的逻辑"""
    group_id = session.group.id
    user_id = session.user.id

    if not await admin_check("quote", "DELETE_ADMIN_LEVEL")(bot, event, session):
        await delete_quote_cmd.finish()

    quote = await QuoteService.get_last_quote(group_id)
    if not quote:
        await MessageUtils.build_message("本群语录库为空").send()
        return

    image_basename = os.path.basename(quote.image_path)
    is_deleted = await QuoteService.delete_quote(
        group_id,
        image_basename,
        deleted_by_user_id=str(user_id),
    )

    if is_deleted:
        await MessageUtils.build_message(
            [
                At(target=user_id, flag="user"),
                (
                    " 已将本群上一条语录移入回收站，7 日后彻底删除；"
                    "回复原语录发送「恢复语录」可恢复"
                ),
            ]
        ).send()
        return

    await MessageUtils.build_message("删除失败，可能语录已被手动删除").send()


# 这里不能继续用 Alconna("删除")，因为同名短命令已经被其他插件占用，
# 单纯提高优先级并不能保证一定进入语录插件自己的 handler。
# 改成纯消息 matcher 后，只有“纯文本恰好是删除 + 回复目标确实是语录图”才会接管，
# 这样既能绕开同名命令冲突，也不会误拦其他插件的回复删除。
delete_quote_reply_cmd = on_message(
    priority=0,
    block=True,
    rule=reply_to_quote_delete_rule(),
)
delete_quote_cmd = on_alconna(
    Alconna("删除语录"), aliases={"del"}, priority=11, block=True
)
restore_quote_cmd = on_alconna(
    Alconna("恢复语录"), priority=4, block=True
)
quote_tag_cmd = on_alconna(
    Alconna("tag", Args["action?", str]["parts?", MultiVar(At | Text)]),
    priority=4,
    block=True,
    rule=reply_to_quote_rule(),
)
quote_alltag_cmd = on_alconna(
    Alconna("alltag"), priority=4, block=True, rule=reply_to_quote_rule()
)
quote_addtag_cmd = on_alconna(
    Alconna("addtag", Args["parts?", MultiVar(At | Text)]),
    aliases={"tagadd"},
    priority=4,
    block=True,
    rule=reply_to_quote_rule(),
)
quote_deltag_cmd = on_alconna(
    Alconna("deltag", Args["parts?", MultiVar(At | Text)]),
    aliases={"tagdel"},
    priority=4,
    block=True,
    rule=reply_to_quote_rule(),
)
quote_taglink_cmd = on_alconna(
    Alconna("taglink", Args["parts", MultiVar(At | Text)]),
    aliases={"tag互通", "标签互通"},
    priority=4,
    block=True,
)
quote_tagunlink_cmd = on_alconna(
    Alconna("tagunlink", Args["parts", MultiVar(At | Text)]),
    aliases={"取消互通"},
    priority=4,
    block=True,
)
quote_taggroup_cmd = on_alconna(
    Alconna("taggroup", Args["parts", MultiVar(At | Text)]),
    aliases={"tag组", "tag别名", "互通组"},
    priority=4,
    block=True,
)


@delete_quote_reply_cmd.handle()
async def handle_delete_quote_reply(
    bot: Bot, event: MessageEvent, session: Uninfo, state: T_State
):
    """处理回复语录图片后发送“删除”的情况"""
    if not session.group:
        logger.debug("删除命令在非群聊环境中使用，已忽略。", "群聊语录")
        return

    await _handle_delete_reply_quote(bot, event, session, state=state)


@delete_quote_cmd.handle()
async def handle_delete_quote_standalone(
    bot: Bot, event: MessageEvent, session: Uninfo
):
    """处理“删除语录”/“del”命令"""
    if not session.group:
        logger.debug("删除命令在非群聊环境中使用，已忽略。", "群聊语录")
        return

    resolution = await _get_quotes_from_reply(bot, event, session)
    if resolution.quotes:
        await _handle_delete_reply_quote(
            bot,
            event,
            session,
            quote=resolution,
        )
        return

    await MessageUtils.build_message("请回复需要删除的语录图片后再使用此命令。").send(
        target=event,
        bot=bot,
    )


@restore_quote_cmd.handle()
async def handle_restore_quote(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
):
    if not session.group:
        await MessageUtils.build_message("恢复语录仅支持群聊。").send(
            target=event,
            bot=bot,
        )
        return

    resolution = await _get_quotes_from_reply(
        bot,
        event,
        session,
        recycled_only=True,
    )
    if not resolution.quotes:
        await MessageUtils.build_message(
            "请回复已移入回收站的语录图片或合并转发后再使用此命令。"
        ).send(target=event, bot=bot)
        return

    await _handle_restore_reply_quote(bot, event, session, resolution)


@quote_tag_cmd.handle()
async def handle_quote_tag(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    resolution = await _require_reply_quotes(bot, event, session)
    if not resolution:
        return

    action = str(arp.query("action", "") or "").strip().lower()

    if not action:
        await _show_quote_tags(
            bot,
            event,
            resolution.quotes,
            unresolved_count=resolution.unresolved_count,
        )
        return

    if action == "all":
        if extract_manual_tags(arp):
            await MessageUtils.build_message(
                "tag all 仅用于查看全部 tag，请不要额外附带参数。"
            ).send(target=event, bot=bot)
            return
        await _show_quote_tags(
            bot,
            event,
            resolution.quotes,
            unresolved_count=resolution.unresolved_count,
        )
        return

    if action in {"add", "del"}:
        await _update_quote_manual_tags(
            bot,
            event,
            resolution.quotes,
            await extract_manual_tags_with_mention_names(bot, session.group.id, arp),
            action,
            resolution.unresolved_count,
        )
        return

    await MessageUtils.build_message(
        "回复语录时仅支持 tag / tag all / tag add / tag del。"
    ).send(target=event, bot=bot)


@quote_alltag_cmd.handle()
async def handle_quote_alltag(bot: Bot, event: MessageEvent, session: Uninfo):
    resolution = await _require_reply_quotes(bot, event, session)
    if not resolution:
        return

    await _show_quote_tags(
        bot,
        event,
        resolution.quotes,
        unresolved_count=resolution.unresolved_count,
    )


@quote_addtag_cmd.handle()
async def handle_quote_addtag(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    resolution = await _require_reply_quotes(bot, event, session)
    if not resolution:
        return

    await _update_quote_manual_tags(
        bot,
        event,
        resolution.quotes,
        await extract_manual_tags_with_mention_names(bot, session.group.id, arp),
        action="add",
        unresolved_count=resolution.unresolved_count,
    )


@quote_deltag_cmd.handle()
async def handle_quote_deltag(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    resolution = await _require_reply_quotes(bot, event, session)
    if not resolution:
        return

    await _update_quote_manual_tags(
        bot,
        event,
        resolution.quotes,
        await extract_manual_tags_with_mention_names(bot, session.group.id, arp),
        action="del",
        unresolved_count=resolution.unresolved_count,
    )


def _format_alias_group_message(members: list[str]) -> list[object]:
    message: list[object] = []
    for index, member in enumerate(members):
        if index:
            message.append(" = ")
        message.extend(QuoteService.format_tags_for_message([member]))
    return message or ["无"]


async def _require_alias_admin(
    bot: Bot, event: MessageEvent, session: Uninfo
) -> bool:
    if not session.group:
        await MessageUtils.build_message("tag 互通命令仅支持群聊。").send(
            target=event, bot=bot
        )
        return False
    if not await admin_check("quote", "TAG_ALIAS_ADMIN_LEVEL")(bot, event, session):
        return False
    return True


@quote_taglink_cmd.handle()
async def handle_quote_taglink(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    if not await _require_alias_admin(bot, event, session):
        return
    group_id = str(session.group.id)
    values = extract_manual_tags(arp)
    preview = await QuoteService.preview_tag_alias_link(group_id, values)
    if not preview.success:
        await MessageUtils.build_message(
            "请提供至少两个 tag 或 @用户，例如：taglink xc 小葱 @用户"
        ).send(target=event, bot=bot)
        return

    confirm_merge = False
    if preview.requires_confirmation:
        original_session_id = event.get_session_id()
        original_user_id = str(event.get_user_id())
        message = ["将合并多个现有互通组：", *_format_alias_group_message(preview.members)]
        message.append("\n确认请在 30 秒内回复“是”。")
        await MessageUtils.build_message(message).send(target=event, bot=bot)

        @waiter(waits=["message"], keep_session=True)
        async def check_confirm(confirm_event: MessageEvent):
            if (
                confirm_event.get_session_id() != original_session_id
                or str(confirm_event.get_user_id()) != original_user_id
            ):
                return None
            return confirm_event.get_plaintext().strip()

        reply_text = await check_confirm.wait(timeout=30)
        if reply_text != "是":
            await MessageUtils.build_message("操作已取消。").send(
                target=event, bot=bot
            )
            return
        confirm_merge = True

    result = await QuoteService.link_tag_aliases(
        group_id, values, confirm_merge=confirm_merge
    )
    await MessageUtils.build_message(
        ["互通组：", *_format_alias_group_message(result.members)]
    ).send(target=event, bot=bot)


@quote_tagunlink_cmd.handle()
async def handle_quote_tagunlink(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    if not await _require_alias_admin(bot, event, session):
        return
    group_id = str(session.group.id)
    values = extract_manual_tags(arp)
    if len(values) != 1:
        await MessageUtils.build_message(
            "请只提供一个要取消互通的 tag 或 @用户。"
        ).send(target=event, bot=bot)
        return
    result = await QuoteService.unlink_tag_alias(group_id, values[0])
    if not result.success:
        await MessageUtils.build_message("该 tag 尚不存在。").send(
            target=event, bot=bot
        )
        return
    await MessageUtils.build_message(
        ["已取消互通：", *QuoteService.format_tags_for_message([values[0]])]
    ).send(target=event, bot=bot)


@quote_taggroup_cmd.handle()
async def handle_quote_taggroup(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    if not session.group:
        await MessageUtils.build_message("tag 互通命令仅支持群聊。").send(
            target=event, bot=bot
        )
        return
    values = extract_manual_tags(arp)
    if len(values) != 1:
        await MessageUtils.build_message("请只提供一个 tag 或 @用户。").send(
            target=event, bot=bot
        )
        return
    members = await QuoteService.get_tag_alias_group(str(session.group.id), values[0])
    if not members:
        await MessageUtils.build_message("该 tag 尚不存在。").send(
            target=event, bot=bot
        )
        return
    await MessageUtils.build_message(
        ["互通组：", *_format_alias_group_message(members)]
    ).send(target=event, bot=bot)


quote_manage_cmd = on_alconna(
    Alconna(
        "quote",
        Subcommand(
            "manager",
            Subcommand(
                "keyword",
                Args["keywords", MultiVar(str)],
                Option("--uploader", Args["user_id", Union[At, int]]),
                Option("--quoted", Args["user_id", Union[At, int]]),
                alias={"删除关键词"},
            ),
            Subcommand(
                "clear",
                Option("--uploader", Args["user_id", Union[At, int]]),
                Option("--quoted", Args["user_id", Union[At, int]]),
                Option("--group", Args["group_id", str]),
                alias={"清空全部"},
            ),
            Subcommand(
                "cleanup",
                Args["target?", Literal["退群用户"], "退群用户"],
                alias={"清理"},
            ),
            Subcommand(
                "check",
                Args["group_id?", str],
                alias={"检查"},
            ),
        ),
        Subcommand("theme", Args["theme_name?", str]),
    ),
    permission=SUPERUSER,
    block=True,
    priority=10,
)

quote_manage_cmd.shortcut("语录管理", {"args": ["manager"]})
quote_manage_cmd.shortcut("语录主题", {"args": ["theme"]})


@quote_manage_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo):
    if arp.find("manager.check"):
        await handle_storage_audit(bot, event, arp, session)
    elif arp.find("manager"):
        await handle_adv_delete(bot, event, arp, session)
    elif arp.find("theme"):
        await handle_theme(bot, event, arp, session)


def get_available_themes() -> list[str]:
    """获取所有可用的语录主题，并确保排序。"""
    available_themes_set = set()
    components_root = QUOTE_ASSETS_PATH / "components"
    if components_root.exists() and components_root.is_dir():
        for component_dir in components_root.iterdir():
            if component_dir.is_dir():
                available_themes_set.add(component_dir.name)

                skins_dir = component_dir / "skins"
                if skins_dir.exists() and skins_dir.is_dir():
                    for skin_dir in skins_dir.iterdir():
                        if skin_dir.is_dir():
                            available_themes_set.add(skin_dir.name)

    return sorted(list(available_themes_set))


async def handle_storage_audit(
    bot: Bot,
    event: MessageEvent,
    arp: Arparma,
    session: Uninfo,
) -> None:
    """执行不会修改数据库或图片文件的语录存储审计。"""
    requested_group_id: str | None = arp.query("manager.check.group_id")
    if requested_group_id in {"全部", "all"}:
        audit_group_id = None
        scope_text = "全部语录"
    elif requested_group_id:
        audit_group_id = requested_group_id
        scope_text = f"群 {requested_group_id}"
    elif session.group:
        audit_group_id = str(session.group.id)
        scope_text = f"当前群 {audit_group_id}"
    else:
        await MessageUtils.build_message(
            "私聊检查时请提供群号，或使用“语录管理 检查 全部”。"
        ).send(target=event, bot=bot)
        return

    result = await QuoteService.audit_storage(audit_group_id)
    message_lines = [
        "语录存储只读检查",
        f"范围：{scope_text}",
        f"数据库记录：{result.total}",
        f"正常：{result.valid}",
        f"缺失：{result.missing}",
        f"越界：{result.out_of_bounds}",
    ]
    if audit_group_id is None:
        message_lines.append(f"孤儿图片：{result.orphan_files}")

    if result.issues:
        reason_text = {"missing": "缺失", "out_of_bounds": "越界"}
        message_lines.append("异常明细（最多 20 条）：")
        message_lines.extend(
            f"ID {issue.id} | 群 {issue.group_id} | "
            f"{reason_text.get(issue.reason, issue.reason)} | {issue.image_path}"
            for issue in result.issues
        )

    message_lines.append("本操作只读，不会删除数据库或图片。")
    await MessageUtils.build_message("\n".join(message_lines)).send(
        target=event,
        bot=bot,
    )


async def handle_theme(bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo):
    """处理 'quote theme' 命令"""
    theme_name_or_index: str | None = arp.query("theme.theme_name")

    available_themes = get_available_themes()

    if not theme_name_or_index:
        message_parts = [
            "可用的语录主题列表 (使用 `quote theme [主题名/序号]` 切换):"
        ] + [f"{i + 1}. {th}" for i, th in enumerate(available_themes)]
        await quote_manage_cmd.finish("\n".join(message_parts))
        return

    theme_name = theme_name_or_index
    if theme_name and theme_name.isdigit():
        try:
            theme_index = int(theme_name)
            if 1 <= theme_index <= len(available_themes):
                theme_name = available_themes[theme_index - 1]
        except (ValueError, IndexError):
            pass

    if theme_name in available_themes:
        Config.set_config("quote", "THEME", theme_name, auto_save=True)
        await quote_manage_cmd.finish(f"语录主题已切换为: {theme_name}")
    else:
        await quote_manage_cmd.finish(
            f"主题 '{theme_name_or_index}' 不存在。可用主题有: {', '.join(sorted(available_themes))}"
        )


async def handle_adv_delete(
    bot: Bot, event: MessageEvent, arp: Arparma, session: Uninfo
):
    """高级删除命令处理函数"""
    current_user_id = str(event.get_user_id())

    if not session.group:
        await quote_manage_cmd.finish("高级管理命令必须在群聊中使用。")
        return
    group_id = session.group.id

    target_for_reply = PlatformUtils.get_target(
        group_id=group_id, user_id=current_user_id
    )
    if not target_for_reply:
        target_for_reply = PlatformUtils.get_target(user_id=current_user_id)

    matched_quotes = []
    confirm_msg_prefix = ""
    user_filter_id: str | None = None

    if arp.find("manager.keyword"):
        keywords: list[str] = arp.query("manager.keyword.keywords", [])
        if not keywords:
            await quote_manage_cmd.finish("请提供至少一个要删除的关键词")

        uploader_param: Union[At, int, None] = arp.query(
            "manager.keyword.uploader.user_id"
        )
        quoted_param: Union[At, int, None] = arp.query("manager.keyword.quoted.user_id")

        user_filter_id = None
        user_filter_type = None

        if uploader_param:
            user_filter_id = (
                str(uploader_param.target)
                if isinstance(uploader_param, At)
                else str(uploader_param)
            )
            user_filter_type = "uploader_user_id"
        elif quoted_param:
            user_filter_id = (
                str(quoted_param.target)
                if isinstance(quoted_param, At)
                else str(quoted_param)
            )
            user_filter_type = "quoted_user_id"

        user_filter_kwargs = (
            {user_filter_type: user_filter_id} if user_filter_type else {}
        )

        matched_quotes = await QuoteService.search_quotes_for_deletion(
            group_id, keywords, **user_filter_kwargs
        )
        confirm_msg_prefix = f"与关键词 '{' 或 '.join(keywords)}' 相关的"

    elif arp.find("manager.clear"):
        uploader_param: Union[At, int, None] = arp.query(
            "manager.clear.uploader.user_id"
        )
        quoted_param: Union[At, int, None] = arp.query("manager.clear.quoted.user_id")
        group_to_clear: str | None = arp.query("manager.clear.group.group_id")

        if uploader_param:
            user_filter_id = (
                str(uploader_param.target)
                if isinstance(uploader_param, At)
                else str(uploader_param)
            )
            matched_quotes = await QuoteService.search_quotes_for_deletion(
                group_id, keywords=None, uploader_user_id=user_filter_id
            )
            confirm_msg_prefix = f"由用户 {user_filter_id} 上传的全部"
        elif quoted_param:
            user_filter_id = (
                str(quoted_param.target)
                if isinstance(quoted_param, At)
                else str(quoted_param)
            )
            matched_quotes = await QuoteService.search_quotes_for_deletion(
                group_id, keywords=None, quoted_user_id=user_filter_id
            )
            confirm_msg_prefix = f"关于用户 {user_filter_id} 的全部"
        elif group_to_clear:
            matched_quotes = await QuoteService.search_quotes_for_deletion(
                group_to_clear
            )
            confirm_msg_prefix = f"群组 {group_to_clear} 的全部"
        else:
            await quote_manage_cmd.finish(
                "使用 '清空全部' 子命令时，必须提供 --uploader, --quoted, 或 --group 中的一个选项。"
            )

    elif arp.find("manager.cleanup"):
        matched_quotes = await QuoteService.find_quotes_from_left_users(group_id, bot)
        confirm_msg_prefix = "由已退群用户产生或记录的"

    if not matched_quotes:
        await MessageUtils.build_message(f"未找到{confirm_msg_prefix}语录").send(
            target=target_for_reply, bot=bot
        )
        return

    count = len(matched_quotes)
    confirm_msg_text = (
        f"在群 {group_id} 中找到 {count} 条{confirm_msg_prefix}语录，"
        f"确认删除请回复'是'，取消请回复其他内容"
    )

    await MessageUtils.build_message(confirm_msg_text).send(
        target=target_for_reply, bot=bot
    )

    @waiter(waits=["message"], keep_session=True)
    async def check_confirm(event: MessageEvent):
        if (
            event.get_session_id() != event.get_session_id()
            or str(event.get_user_id()) != current_user_id
        ):
            return None
        return event.get_plaintext().strip()

    try:
        reply_text = await check_confirm.wait(timeout=30)

        if reply_text is None or reply_text != "是":
            await MessageUtils.build_message("操作已取消").send(
                target=target_for_reply, bot=bot
            )
            return

        deleted_count = 0
        failed_count = 0

        for quote in matched_quotes:
            try:
                if await QuoteService.delete_quote_instance(
                    quote,
                    deleted_by_user_id=current_user_id,
                ):
                    deleted_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(
                    f"删除语录失败 - ID: {quote.id}, "
                    f"路径: {quote.image_path}, 错误: {e}",
                    "群聊语录",
                    e=e,
                )
                failed_count += 1

        result_msg = (
            f"语录已移入回收站，成功: {deleted_count}，失败: {failed_count}；"
            "7 日后彻底删除；回复原语录发送「恢复语录」可恢复"
        )
        await MessageUtils.build_message(result_msg).send(
            target=target_for_reply, bot=bot
        )
    except Exception as e:
        logger.error(f"删除语录过程中发生错误: {e}", "群聊语录", e=e)
        await MessageUtils.build_message(f"删除过程中发生错误: {e}").send(
            target=target_for_reply, bot=bot
        )
