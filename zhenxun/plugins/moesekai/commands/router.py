from __future__ import annotations

from nonebot import on_message
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule
from nonebot.typing import T_State
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_uninfo import Uninfo

from ..adapters.results import (
    MoeForwardMessage,
    MoeImageTextMessage,
    build_image_message,
)
from ..adapters.runtime import MessageUtils, PlatformUtils, logger
from ..command_parser import ParsedCommand, parse_command
from ..constants import format_binding_notice
from ..services import (
    handle_admin_blacklist,
    handle_admin_query_binding,
    handle_bind,
    handle_character,
    handle_default_server,
    handle_live_subscription,
    handle_live_toggle,
    handle_manga_by_id,
    handle_new_card_toggle,
    handle_query_archive,
    handle_random_manga,
    handle_story,
    handle_test_live_reminder,
    handle_test_new_card_reminder,
    handle_unbind,
    handle_update,
    handle_visibility,
)

_BINDING_ACTIONS = {
    "admin_query_binding",
    "bind",
    "default_server",
    "unbind",
    "visibility",
}


def _format_action_result(action: str, result: object) -> object:
    if action in _BINDING_ACTIONS and isinstance(result, str):
        return format_binding_notice(result)
    return result


async def _command_rule(event: Event, state: T_State) -> bool:
    if not isinstance(event, MessageEvent):
        return False
    parsed = parse_command(event)
    if not parsed:
        return False
    state["moesekai_parsed_command"] = parsed
    return True


matcher = on_message(priority=5, block=True, rule=Rule(_command_rule))


async def _send_result(result: object, *, bot: Bot | None = None, event: MessageEvent | None = None) -> None:
    if isinstance(result, list):
        for item in result:
            await _send_result(item, bot=bot, event=event)
        return
    if isinstance(result, MoeForwardMessage):
        for item in result.nodes:
            await item.send(reply_to=True)
        return
    if isinstance(result, MoeImageTextMessage):
        if bot and event:
            message = Message()
            message.append(MessageSegment.reply(event.message_id))
            message.append(MessageSegment.image(result.image_bytes))
            if result.text:
                message.append(MessageSegment.text(f"\n{result.text}"))
            await bot.send(event, message)
            return
        await build_image_message(result.image_bytes, result.text).send(reply_to=True)
        return
    if isinstance(result, UniMessage):
        await result.send(reply_to=True)
        return
    await MessageUtils.build_message(result).send(reply_to=True)


def _is_group_admin(event: MessageEvent, session: Uninfo) -> bool:
    if not session.group:
        return False
    sender = getattr(event, "sender", None)
    role = getattr(sender, "role", None)
    return role in {"admin", "owner"}


@matcher.handle()
async def _(
    bot: Bot,
    event: MessageEvent,
    session: Uninfo,
    state: T_State,
):
    parsed: ParsedCommand = state["moesekai_parsed_command"]
    is_superuser = await SUPERUSER(bot, event)
    can_manage_group = _is_group_admin(event, session)
    if parsed.action.startswith("admin_") and not is_superuser:
        await _send_result(
            _format_action_result(parsed.action, "该指令仅超级用户可用"),
            bot=bot,
            event=event,
        )
        return
    if parsed.action in {"test_live", "test_new_card"} and not (is_superuser or can_manage_group):
        await _send_result("该测试指令仅群管理员或超级用户可用", bot=bot, event=event)
        return
    if parsed.error:
        await _send_result(
            _format_action_result(parsed.action, parsed.error),
            bot=bot,
            event=event,
        )
        return

    platform = PlatformUtils.get_platform(session)
    user_id = session.user.id
    group_id = session.group.id if session.group else None

    if parsed.action == "bind":
        result = await handle_bind(
            platform,
            user_id,
            parsed.server,
            parsed.game_id or "",
            is_superuser=is_superuser,
        )
    elif parsed.action == "unbind":
        result = await handle_unbind(
            platform,
            user_id,
            parsed.server,
            is_superuser=is_superuser,
        )
    elif parsed.action == "default_server":
        result = await handle_default_server(
            platform,
            user_id,
            parsed.server,
            is_superuser=is_superuser,
        )
    elif parsed.action == "visibility":
        result = await handle_visibility(
            platform,
            user_id,
            allow_share_profile=parsed.admin_value == "allow",
        )
    elif parsed.action == "query_archive":
        result = await handle_query_archive(
            platform,
            user_id,
            server=parsed.server,
            game_id=parsed.game_id,
            target_user_id=parsed.target_user_id,
            is_superuser=is_superuser,
        )
    elif parsed.action == "update":
        result = await handle_update(
            platform,
            user_id,
            parsed.server,
            update_all=parsed.all_servers,
            is_superuser=is_superuser,
            bot=bot,
        )
    elif parsed.action == "story":
        result = await handle_story(
            parsed.event_id or 0,
            force_refresh=parsed.force_refresh,
        )
    elif parsed.action == "random_manga":
        result = await handle_random_manga()
    elif parsed.action == "manga_by_id":
        result = await handle_manga_by_id(parsed.manga_id or 0)
    elif parsed.action == "character":
        result = await handle_character(
            parsed.query_text or "",
            platform=platform,
            group_id=group_id,
            force_refresh=parsed.force_refresh,
        )
    elif parsed.action == "live_toggle":
        result = await handle_live_toggle(
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            server=parsed.server,
            action=parsed.admin_subaction or "status",
            is_superuser=is_superuser,
            can_manage_group=can_manage_group,
        )
    elif parsed.action == "new_card_toggle":
        result = await handle_new_card_toggle(
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            server=parsed.server,
            action=parsed.admin_subaction or "status",
            is_superuser=is_superuser,
            can_manage_group=can_manage_group,
        )
    elif parsed.action == "live_subscribe":
        result = await handle_live_subscription(
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            server=parsed.server,
            subscribe=parsed.admin_subaction == "subscribe",
        )
    elif parsed.action == "test_live":
        result = await handle_test_live_reminder(
            platform=platform,
            user_id=user_id,
            server=parsed.server,
            live_id=parsed.live_id,
        )
    elif parsed.action == "test_new_card":
        result = await handle_test_new_card_reminder(
            bot=bot,
            group_id=group_id,
            platform=platform,
            user_id=user_id,
            server=parsed.server,
            card_ids=parsed.card_ids,
        )
    elif parsed.action == "admin_blacklist":
        result = await handle_admin_blacklist(parsed, user_id)
    elif parsed.action == "admin_query_binding":
        result = await handle_admin_query_binding(parsed)
    else:
        result = "暂不支持的指令"

    if result is None:
        logger.info(
            f"MoeSekai 执行命令: {parsed.action}",
            parsed.raw_text,
            session=session,
            platform=platform,
        )
        return
    await _send_result(
        _format_action_result(parsed.action, result),
        bot=bot,
        event=event,
    )
    logger.info(
        f"MoeSekai 执行命令: {parsed.action}",
        parsed.raw_text,
        session=session,
        platform=platform,
    )
