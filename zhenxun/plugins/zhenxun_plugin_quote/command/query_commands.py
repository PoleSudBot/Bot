import asyncio
from dataclasses import dataclass
import re

import aiofiles
from arclet.alconna import Alconna, Args, Arparma, MultiVar, Option, Subcommand
from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, Event, PokeNotifyEvent
from nonebot.exception import ActionFailed, AdapterException
from nonebot.rule import Rule, to_me
from nonebot.typing import T_State
from nonebot_plugin_alconna import At, on_alconna
from nonebot_plugin_alconna.uniseg import Image, UniMessage

from zhenxun.services.log import logger
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.rules import notice_rule

from ..config import (
    safe_file_exists,
    resolve_quote_image_path,
)
from ..model import Quote
from ..services.quote_service import QuoteService

_LIAN_COUNT_PATTERN = re.compile(
    r"^(?P<count>(?:10|[1-9]|[一二两三四五六七八九十]))连$"
)
_SUFFIX_LIAN_COUNT_PATTERN = re.compile(
    r"^(?P<keyword>.+?)(?P<count>(?:10|[1-9]|[一二两三四五六七八九十]))连$"
)
_MULTIPLIER_COUNT_PATTERN = re.compile(
    r"^(?P<operator>[xX*])(?P<count>10|[1-9])$"
)
_SUFFIX_MULTIPLIER_COUNT_PATTERN = re.compile(
    r"^(?P<keyword>.+?)(?P<operator>[xX*])(?P<count>10|[1-9])$"
)
_HISTORY_INDEX_PATTERN = re.compile(r"^-(?P<index>[1-9]\d*)$")
_SUFFIX_HISTORY_INDEX_PATTERN = re.compile(
    r"^(?P<keyword>.+?)-(?P<index>[1-9]\d*)$"
)
_CN_LIAN_COUNT_MAP = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class QuoteQueryParams:
    """语录查询参数，区分随机数量请求与倒序指定请求。"""

    keyword: str
    count: int
    history_index: int | None = None


quote_alc = Alconna(
    "语录",
    Option("-n|--num", Args["count", int, 1], help_text="一次获取的语录数量"),
    Args["target_user?", At]["search_keywords?", MultiVar(str)],
)
record_pool = on_alconna(quote_alc, priority=2, block=True)
# 不能直接给“语录”开 compact=True，否则会吞掉“语录统计/语录主题/语录管理”。
# 这里用负前瞻只补“语录xxx”这种普通查询粘连写法，并保留管理/统计命令原路由。
record_pool.shortcut(
    r"语录(?!(?:统计|主题|管理)(?:\s|$))(?P<query>.+)",
    {"args": ["{query}"], "fuzzy": False},
)

stats_alc = Alconna(
    "quote",
    Subcommand(
        "stats",
        Subcommand("hot", Args["limit?", int, 10], alias={"热门"}),
        Subcommand("top-uploaders", Args["limit?", int, 10], alias={"高产上传"}),
        Subcommand("top-quoted", Args["limit?", int, 10], alias={"高产被录"}),
    ),
)
quote_stats_cmd = on_alconna(stats_alc, priority=5, block=True)
quote_stats_cmd.shortcut("语录统计", {"args": ["stats"]})


def _is_group_poke(event: PokeNotifyEvent) -> bool:
    return event.group_id is not None


quote_poke = on_notice(
    priority=5,
    block=False,
    rule=notice_rule(PokeNotifyEvent) & to_me() & Rule(_is_group_poke),
)


def _normalize_quote_count(count: int | None) -> int:
    if count is None:
        return 1
    return max(1, min(int(count), 10))


def _parse_lian_count(token: str) -> int | None:
    stripped = token.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        return _normalize_quote_count(int(stripped))
    return _CN_LIAN_COUNT_MAP.get(stripped)


def _parse_multiplier_count(token: str) -> int | None:
    stripped = token.strip()
    if not stripped:
        return None
    if match := _MULTIPLIER_COUNT_PATTERN.fullmatch(stripped):
        return _normalize_quote_count(int(match.group("count")))
    return None


def _can_parse_suffix_multiplier(keyword: str, operator: str) -> bool:
    if operator == "*":
        return True

    # xN 粘连写法存在英文关键词歧义；至少包含数字或中文时才拆后缀，
    # 避免把 xxxx3 误判成“xxx x3”。
    return bool(re.search(r"[\d\u4e00-\u9fff]", keyword))


def _extract_suffix_count(token: str) -> tuple[str, int] | None:
    if match := _SUFFIX_LIAN_COUNT_PATTERN.fullmatch(token):
        return (
            match.group("keyword").strip(),
            _parse_lian_count(match.group("count")) or 1,
        )

    if match := _SUFFIX_MULTIPLIER_COUNT_PATTERN.fullmatch(token):
        keyword = match.group("keyword").strip()
        operator = match.group("operator")
        if keyword and _can_parse_suffix_multiplier(keyword, operator):
            return keyword, _normalize_quote_count(int(match.group("count")))

    return None


def _extract_history_index(
    normalized_keywords: list[str],
) -> tuple[str, int] | None:
    if not normalized_keywords:
        return None

    if len(normalized_keywords) == 1:
        token = normalized_keywords[0]
        if match := _HISTORY_INDEX_PATTERN.fullmatch(token):
            return "", int(match.group("index"))
        if match := _SUFFIX_HISTORY_INDEX_PATTERN.fullmatch(token):
            return match.group("keyword").strip(), int(match.group("index"))

    last_token = normalized_keywords[-1]
    if match := _HISTORY_INDEX_PATTERN.fullmatch(last_token):
        return (
            " ".join(normalized_keywords[:-1]).strip(),
            int(match.group("index")),
        )

    return None


def _extract_quote_query_params(
    search_keywords: list[str], option_count: int | None
) -> QuoteQueryParams:
    normalized_keywords = [
        keyword.strip() for keyword in search_keywords if str(keyword).strip()
    ]
    if option_count is not None:
        # 显式 -n/--num 的优先级最高，避免“语录 -n 1 五连”这类写法被误拆成数量请求。
        return QuoteQueryParams(
            " ".join(normalized_keywords), _normalize_quote_count(option_count)
        )

    if not normalized_keywords:
        return QuoteQueryParams("", 1)

    if history_result := _extract_history_index(normalized_keywords):
        keyword, history_index = history_result
        return QuoteQueryParams(keyword, 1, history_index)

    if len(normalized_keywords) == 1:
        token = normalized_keywords[0]
        if match := _LIAN_COUNT_PATTERN.fullmatch(token):
            return QuoteQueryParams("", _parse_lian_count(match.group("count")) or 1)
        if multiplier_count := _parse_multiplier_count(token):
            return QuoteQueryParams("", multiplier_count)
        if suffix_count := _extract_suffix_count(token):
            # 仅支持“整条数量”或“末尾数量”，避免把正常关键词中间的数字误解释成数量。
            keyword, count = suffix_count
            return QuoteQueryParams(keyword, count)

    last_token = normalized_keywords[-1]
    if match := _LIAN_COUNT_PATTERN.fullmatch(last_token):
        return QuoteQueryParams(
            " ".join(normalized_keywords[:-1]).strip(),
            _parse_lian_count(match.group("count")) or 1,
        )
    if multiplier_count := _parse_multiplier_count(last_token):
        return QuoteQueryParams(
            " ".join(normalized_keywords[:-1]).strip(), multiplier_count
        )

    return QuoteQueryParams(" ".join(normalized_keywords), 1)


def _extract_quote_query_and_count(
    search_keywords: list[str], option_count: int | None
) -> tuple[str, int]:
    params = _extract_quote_query_params(search_keywords, option_count)
    return params.keyword, params.count


def _build_random_memory_key(group_id: str, user_id_filter: str | None = None) -> str:
    return f"{group_id}_{user_id_filter or 'all'}"


def _build_search_memory_key(
    group_id: str, keyword: str, user_id_filter: str | None = None
) -> str:
    return f"{group_id}_{user_id_filter or 'all'}_{keyword}"


async def _get_empty_quote_library_message(group_id: str) -> str:
    """区分数据库真空和有记录但图片均不可用两种状态。"""
    if await QuoteService.active_quotes(group_id=group_id).exists():
        return "语录记录存在，但图片暂时不可用，请联系管理员检查。"
    return "当前无语录库"


def _build_multi_quote_message(images: list[bytes]) -> UniMessage:
    message = UniMessage()
    for image_bytes in images:
        message += Image(raw=image_bytes)
    return message


async def _collect_valid_quotes_from_candidates(
    memory_key: str, candidate_quotes: list[Quote], target_count: int
) -> list[Quote]:
    valid_quotes: list[Quote] = []
    remaining_candidates = list(candidate_quotes)

    while remaining_candidates and len(valid_quotes) < target_count:
        # 每轮多拿 5 条候选，是为了在存在坏文件时尽量一次补齐，而不是频繁重跑检索。
        batch_size = min(len(remaining_candidates), (target_count - len(valid_quotes)) + 5)
        provisional_quotes = QuoteService.select_quotes_without_record(
            memory_key, remaining_candidates, batch_size
        )
        if not provisional_quotes:
            break

        provisional_ids = {quote.id for quote in provisional_quotes}
        remaining_candidates = [
            quote for quote in remaining_candidates if quote.id not in provisional_ids
        ]

        for quote in provisional_quotes:
            if safe_file_exists(quote.image_path):
                if len(valid_quotes) < target_count:
                    valid_quotes.append(quote)
            else:
                # 查询只跳过不可用图片，避免临时挂载或 IO 故障导致数据库记录被误删。
                logger.warning(
                    f"数据库中的语录 (ID: {quote.id}, 群: {quote.group_id}) "
                    f"对应图片暂时不可用: {quote.image_path}",
                    "群聊语录",
                )

    return valid_quotes


async def _collect_valid_quotes_from_random_ids(
    memory_key: str, candidate_ids: list[int], target_count: int
) -> list[Quote]:
    valid_quotes: list[Quote] = []
    remaining_ids = list(candidate_ids)

    while remaining_ids and len(valid_quotes) < target_count:
        batch_size = min(len(remaining_ids), (target_count - len(valid_quotes)) + 5)
        provisional_ids = QuoteService.select_quote_ids_without_record(
            memory_key, remaining_ids, batch_size
        )
        if not provisional_ids:
            break

        provisional_id_set = set(provisional_ids)
        remaining_ids = [
            quote_id for quote_id in remaining_ids if quote_id not in provisional_id_set
        ]
        provisional_quotes = await QuoteService.get_quotes_by_ids_in_order(
            provisional_ids
        )

        for quote in provisional_quotes:
            if safe_file_exists(quote.image_path):
                if len(valid_quotes) < target_count:
                    valid_quotes.append(quote)
            else:
                # ID 随机池同样保持只读，文件恢复后原记录仍可继续使用。
                logger.warning(
                    f"数据库中的语录 (ID: {quote.id}, 群: {quote.group_id}) "
                    f"对应图片暂时不可用: {quote.image_path}",
                    "群聊语录",
                )

    return valid_quotes


async def _get_valid_random_quotes(
    group_id: str,
    target_count: int,
    user_id_filter: str | None = None,
) -> tuple[list[Quote], str, bool]:
    """返回可用语录、随机记忆键和是否存在匹配的数据库记录。"""
    memory_key = _build_random_memory_key(group_id, user_id_filter)

    if user_id_filter:
        candidate_ids = await QuoteService.get_quote_ids_for_user(
            group_id, user_id_filter
        )
        random_candidates = await QuoteService.get_quotes_by_ids_in_order(
            candidate_ids
        )
        return (
            await _collect_valid_quotes_from_candidates(
                memory_key, random_candidates, target_count
            ),
            memory_key,
            bool(random_candidates),
        )

    random_quote_ids = await QuoteService.get_random_quote_ids(group_id)
    return (
        await _collect_valid_quotes_from_random_ids(
            memory_key, random_quote_ids, target_count
        ),
        memory_key,
        bool(random_quote_ids),
    )


async def _send_quote_batch(
    bot: Bot,
    target,
    group_id: str,
    quotes: list[Quote],
    *,
    silent_on_unavailable: bool = False,
) -> list[Quote]:
    # 发送前再次过滤可用文件，缩小存在性检查与实际读取之间的竞态窗口。
    available_quotes = [
        quote for quote in quotes if safe_file_exists(quote.image_path)
    ]
    if not available_quotes:
        if not silent_on_unavailable:
            await MessageUtils.build_message(
                "语录记录存在，但图片暂时不可用，请联系管理员检查。"
            ).send(target=target, bot=bot)
        return []

    # 所有发送模式先逐图读取，确保单张在复查后失效时仍能发送其余图片。
    image_payloads: list[bytes] = []
    sent_quotes: list[Quote] = []
    for quote in available_quotes:
        try:
            absolute_path = resolve_quote_image_path(quote.image_path)
            async with aiofiles.open(absolute_path, "rb") as file:
                image_bytes = await file.read()
        except (OSError, ValueError) as e:
            # 合并转发逐张读取，单图失效时保留其余可用结果。
            logger.warning(
                f"读取语录图片失败 - ID: {quote.id}, 群: {quote.group_id}, "
                f"路径: {quote.image_path}, 错误: {e}",
                "群聊语录",
            )
            continue
        image_payloads.append(image_bytes)
        sent_quotes.append(quote)

    if not image_payloads:
        if not silent_on_unavailable:
            await MessageUtils.build_message(
                "语录记录存在，但图片暂时不可用，请联系管理员检查。"
            ).send(target=target, bot=bot)
        return []

    if len(sent_quotes) <= 5:
        await _build_multi_quote_message(image_payloads).send(target=target, bot=bot)
        return sent_quotes

    forward_messages = [
        UniMessage([Image(raw=image_bytes)]) for image_bytes in image_payloads
    ]
    try:
        await bot.call_api(
            "send_group_forward_msg",
            group_id=int(group_id),
            messages=MessageUtils.template2forward(forward_messages, bot.self_id),
        )
        return sent_quotes
    except (ActionFailed, AdapterException, asyncio.TimeoutError) as e:
        logger.error(
            f"发送语录合并转发失败 - 群组: {group_id}, "
            f"数量: {len(sent_quotes)}, 错误: {e}",
            "群聊语录",
            e=e,
        )
        await MessageUtils.build_message("合并转发发送失败，请稍后重试。").send(
            target=target, bot=bot
        )
        return []

async def _send_history_quote(
    bot: Bot,
    target,
    group_id: str,
    keyword: str,
    history_index: int,
    user_id_filter: str | None = None,
    *,
    tag_only: bool = False,
) -> None:
    """处理 -N 倒序指定请求，保持结果确定且不参与随机去重。"""
    quote = await QuoteService.get_quote_by_history_index(
        group_id,
        history_index,
        keyword,
        user_id_filter,
        tag_only=tag_only,
    )
    if not quote:
        keyword_hint = f"匹配 '{keyword}' 的" if keyword else "本群"
        await MessageUtils.build_message(
            f"未找到{keyword_hint}倒数第 {history_index} 条语录。"
        ).send(target=target, bot=bot)
        return

    if not safe_file_exists(quote.image_path):
        logger.warning(
            f"倒序指定的语录 (ID: {quote.id}, 群: {quote.group_id}) "
            f"对应图片暂时不可用: {quote.image_path}",
            "群聊语录",
        )
        await MessageUtils.build_message(
            "这条语录图片暂时不可用，请联系管理员检查。"
        ).send(target=target, bot=bot)
        return

    sent_quotes = await _send_quote_batch(bot, target, group_id, [quote])
    if not sent_quotes:
        return

    await QuoteService.increment_view_counts([item.id for item in sent_quotes])


@record_pool.handle()
async def record_pool_handle(bot: Bot, event: Event, arp: Arparma, state: T_State):
    """语录查询处理函数。"""
    session_id = event.get_session_id()
    if "group" not in session_id:
        await MessageUtils.build_message("请在群聊中使用语录查询。").send(
            target=event,
            bot=bot,
        )
        return

    group_id = session_id.split("_")[1]
    target = PlatformUtils.get_target(group_id=group_id)

    at_user_info: At | None = arp.all_matched_args.get("target_user")
    search_keywords: list[str] = arp.all_matched_args.get("search_keywords", [])
    query_params = _extract_quote_query_params(
        search_keywords, arp.query("num.count")
    )
    search_key_processed = query_params.keyword
    tag_only = search_key_processed.startswith("#") and len(search_key_processed) > 1
    if tag_only:
        search_key_processed = search_key_processed[1:].strip()
    request_count = query_params.count
    user_id_filter: str | None = str(at_user_info.target) if at_user_info else None

    if query_params.history_index is not None:
        await _send_history_quote(
            bot,
            target,
            group_id,
            search_key_processed,
            query_params.history_index,
            user_id_filter,
            tag_only=tag_only,
        )
        return

    quotes: list[Quote] = []
    memory_key: str | None = None
    fallback_message: UniMessage | None = None

    if search_key_processed:
        search_memory_key = _build_search_memory_key(
            group_id,
            f"#{search_key_processed}" if tag_only else search_key_processed,
            user_id_filter,
        )
        matched_quotes = await QuoteService.search_quotes(
            group_id,
            search_key_processed,
            user_id_filter,
            tag_only=tag_only,
        )
        quotes = await _collect_valid_quotes_from_candidates(
            search_memory_key, matched_quotes, request_count
        )
        if quotes:
            memory_key = search_memory_key
        elif matched_quotes:
            await MessageUtils.build_message(
                "语录记录存在，但图片暂时不可用，请联系管理员检查。"
            ).send(target=target, bot=bot)
            return
        elif tag_only:
            await MessageUtils.build_message(
                f"未找到 tag「{search_key_processed}」对应的语录。"
            ).send(target=target, bot=bot)
            return
        else:
            # 只有关键词有效命中为 0 时才回退随机，命中了但数量不足时只发命中的结果，
            # 避免把“查询结果”悄悄掺入无关随机图，破坏搜索语义。
            quotes, memory_key, has_candidates = await _get_valid_random_quotes(
                group_id,
                request_count,
                user_id_filter=user_id_filter,
            )
            if quotes:
                if user_id_filter:
                    count_text = f"{len(quotes)} 条" if len(quotes) > 1 else "一条"
                    fallback_message = MessageUtils.build_message(
                        [
                            At(target=user_id_filter, flag="user"),
                            f" 关于 '{search_key_processed}' 的语录没找到哦，这是TA的{count_text}随机语录：\n",
                        ]
                    )
                else:
                    fallback_message = MessageUtils.build_message(
                        [f"当前查询无结果, 为您随机发送 {len(quotes)} 条语录。"]
                    )
            elif has_candidates:
                await MessageUtils.build_message(
                    "语录记录存在，但图片暂时不可用，请联系管理员检查。"
                ).send(target=target, bot=bot)
                return
            elif user_id_filter:
                await MessageUtils.build_message(
                    [At(target=user_id_filter, flag="user"), " 没有任何语录哦~"]
                ).send(target=target, bot=bot)
                return
            else:
                await MessageUtils.build_message(
                    await _get_empty_quote_library_message(group_id)
                ).send(target=target, bot=bot)
                return
    else:
        quotes, memory_key, has_candidates = await _get_valid_random_quotes(
            group_id, request_count, user_id_filter=user_id_filter
        )
        if not quotes:
            if has_candidates:
                await MessageUtils.build_message(
                    "语录记录存在，但图片暂时不可用，请联系管理员检查。"
                ).send(target=target, bot=bot)
            elif user_id_filter:
                await MessageUtils.build_message(
                    [At(target=user_id_filter, flag="user"), " 没有任何语录哦~"]
                ).send(target=target, bot=bot)
            else:
                await MessageUtils.build_message(
                    await _get_empty_quote_library_message(group_id)
                ).send(target=target, bot=bot)
            return

    if fallback_message:
        await fallback_message.send(target=target, bot=bot)

    sent_quotes = await _send_quote_batch(bot, target, group_id, quotes)
    if not sent_quotes:
        return
    quotes = sent_quotes

    if memory_key:
        QuoteService.record_recent_quote_ids(memory_key, [quote.id for quote in quotes])
    await QuoteService.increment_view_counts([quote.id for quote in quotes])


@quote_poke.handle()
async def handle_quote_poke(bot: Bot, event: PokeNotifyEvent):
    group_id = event.group_id
    if group_id is None:
        return

    group_id_str = str(group_id)
    target = PlatformUtils.get_target(group_id=group_id_str)
    quotes, memory_key, _ = await _get_valid_random_quotes(group_id_str, 1)
    if not quotes:
        return

    sent_quotes = await _send_quote_batch(
        bot,
        target,
        group_id_str,
        quotes,
        silent_on_unavailable=True,
    )
    if not sent_quotes:
        return

    quote_ids = [quote.id for quote in sent_quotes]
    QuoteService.record_recent_quote_ids(memory_key, quote_ids)
    await QuoteService.increment_view_counts(quote_ids)


@quote_stats_cmd.handle()
async def handle_quote_stats(bot: Bot, event: Event, arp: Arparma):
    """语录统计处理函数"""
    session_id = event.get_session_id()
    current_user_id = str(event.get_user_id())

    group_id_to_query: str | None = None

    if "group" in session_id:
        group_id_to_query = session_id.split("_")[1]

    if not group_id_to_query:
        await quote_stats_cmd.finish("请在群聊中执行此命令。")
        return

    reply_target = PlatformUtils.get_target(
        group_id=session_id.split("_")[1] if "group" in session_id else None,
        user_id=current_user_id if "private" in session_id else None,
    )
    if not reply_target:
        reply_target = PlatformUtils.get_target(user_id=current_user_id)

    result_message: str | bytes | None = None

    path_to_check = ""
    if arp.find("stats.hot"):
        path_to_check = "hot"
    elif arp.find("stats.top-uploaders"):
        path_to_check = "top-uploaders"
    elif arp.find("stats.top-quoted"):
        path_to_check = "top-quoted"

    if path_to_check == "hot":
        limit = arp.query("stats.hot.limit", 10)
        logger.debug(f"执行 '热门' 统计，limit={limit}", "群聊语录")

        import time

        start_time = time.time()

        hottest_quotes = await QuoteService.get_hottest_quotes(group_id_to_query, limit)

        fetch_time = time.time() - start_time
        logger.debug(f"获取热门语录耗时: {fetch_time:.3f}秒", "群聊语录")

        if hottest_quotes:
            image_quotes_count = sum(
                1
                for q in hottest_quotes
                if q.image_path and not (q.ocr_text or q.recorded_text)
            )
            text_quotes_count = len(hottest_quotes) - image_quotes_count
            logger.debug(
                f"热门语录类型统计 - 总数: {len(hottest_quotes)}, "
                f"图片语录: {image_quotes_count}, 文本语录: {text_quotes_count}",
                "群聊语录",
            )

            gen_start_time = time.time()

            result_message = await QuoteService.generate_hottest_quotes_image(
                group_id_to_query, hottest_quotes, bot.self_id
            )

            gen_time = time.time() - gen_start_time
            logger.debug(f"生成热门语录图片耗时: {gen_time:.3f}秒", "群聊语录")

            total_time = time.time() - start_time
            logger.debug(f"热门语录统计总耗时: {total_time:.3f}秒", "群聊语录")
        else:
            logger.warning(f"群组 {group_id_to_query} 没有热门语录数据", "群聊语录")
            result_message = f"群组 {group_id_to_query} 暂时没有热门语录。"

    elif path_to_check == "top-uploaders":
        limit = arp.query("stats.top-uploaders.limit", 10)
        logger.info(f"执行 '高产上传' 统计，limit={limit}", "群聊语录")
        prolific_uploaders = await QuoteService.get_most_prolific_uploaders(
            group_id_to_query, limit
        )
        if prolific_uploaders:
            result_message = await QuoteService.generate_bar_chart_for_prolific_users(
                group_id_to_query, prolific_uploaders, "语录上传"
            )
        else:
            result_message = f"群组 {group_id_to_query} 暂时没有高产上传用户数据。"

    elif path_to_check == "top-quoted":
        limit = arp.query("stats.top-quoted.limit", 10)
        logger.info(f"执行 '高产被录' 统计，limit={limit}", "群聊语录")
        prolific_quoted = await QuoteService.get_most_quoted_users(
            group_id_to_query, limit
        )
        if prolific_quoted:
            result_message = await QuoteService.generate_bar_chart_for_prolific_users(
                group_id_to_query, prolific_quoted, "被记录语录"
            )
        else:
            result_message = f"群组 {group_id_to_query} 暂时没有高产被记录用户数据。"

    else:
        await MessageUtils.build_message(
            "请指定统计类型：热门、高产上传、高产被录。\n例如：语录统计 热门"
        ).send(target=reply_target, bot=bot)
        return

    try:
        if result_message:
            if isinstance(result_message, str):
                await MessageUtils.build_message(result_message).send(
                    target=reply_target, bot=bot
                )
            elif isinstance(result_message, bytes):
                await MessageUtils.build_message(result_message).send(
                    target=reply_target, bot=bot
                )
        else:
            await MessageUtils.build_message("无法获取统计数据或数据为空。").send(
                target=reply_target, bot=bot
            )
    except Exception as e:
        logger.error(f"语录统计过程中发送消息发生错误: {e}", "群聊语录", e=e)
        await MessageUtils.build_message(f"统计过程中发生错误: {e}").send(
            target=reply_target, bot=bot
        )
