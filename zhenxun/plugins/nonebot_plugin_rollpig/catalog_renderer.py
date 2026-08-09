from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
from typing import Any

from nonebot.log import logger
import nonebot_plugin_localstore as localstore
from PIL import Image

from .config import (
    get_catalog_cache_seconds,
    get_catalog_render_timeout,
    get_growth_max_expert_level,
)
from .growth import expert_level_from_copies
from .render_budget import html_render_budget
from .resource_manager import pig_resource_manager
from .runtime import ROLLPIG_TIMEZONE, rollpig_today
from .store.models import CatalogSnapshot, DrawState, PigProgress

RES_DIR = Path(__file__).parent / "resource"
HARMONY_FONT_DIR = (
    Path(__file__).resolve().parents[3] / "resources" / "font" / "HarmonyOS_Sans_SC"
)
CATALOG_TEMPLATE = "catalog.html"
CATALOG_TEMPLATE_PATH = RES_DIR / CATALOG_TEMPLATE
THUMB_CACHE_DIR = localstore.get_plugin_cache_dir() / "catalog_thumbs"
CATALOG_CACHE_MAX_ENTRIES = 64
CATALOG_CACHE_MAX_BYTES = 64 * 1024 * 1024
NEW_BADGE_DAYS = 7
NEW_BADGE_URI = (RES_DIR / "assets" / "new.png").as_uri()
HARMONY_FONT_FILES = {
    400: "HarmonyOS_SansSC_Regular.ttf",
    500: "HarmonyOS_SansSC_Medium.ttf",
    600: "HarmonyOS_SansSC_Semibold.ttf",
    700: "HarmonyOS_SansSC_Bold.ttf",
    900: "HarmonyOS_SansSC_Black.ttf",
}


@dataclass
class _CachedCatalogImage:
    created_at: float
    payload: bytes


_catalog_cache: dict[str, _CachedCatalogImage] = {}
_catalog_cache_lock = asyncio.Lock()
_catalog_render_tasks: dict[str, asyncio.Task[bytes]] = {}


def clear_catalog_runtime_cache() -> None:
    """清空进程内图鉴缓存，供测试或资源热更新后的保守刷新使用。"""
    _catalog_cache.clear()
    for task in _catalog_render_tasks.values():
        if not task.done():
            task.cancel()
    _catalog_render_tasks.clear()


def get_harmony_font_faces() -> list[dict[str, str | int]]:
    """把项目共享鸿蒙字体转成 file URI，供 htmlrender 的独立模板直接加载。"""
    faces: list[dict[str, str | int]] = []
    for weight, filename in HARMONY_FONT_FILES.items():
        font_file = HARMONY_FONT_DIR / filename
        if font_file.exists():
            faces.append({"weight": weight, "uri": font_file.as_uri()})
    return faces


def _catalog_cache_bytes() -> int:
    return sum(len(cached.payload) for cached in _catalog_cache.values())


def _prune_catalog_cache(*, ttl: int, now: float) -> None:
    """按 TTL、条数和总字节三层上限清理图鉴缓存，防止大图长期占内存。"""
    if not _catalog_cache:
        return
    if ttl <= 0:
        _catalog_cache.clear()
        return

    expired_keys = [
        key for key, cached in _catalog_cache.items() if now - cached.created_at > ttl
    ]
    for key in expired_keys:
        _catalog_cache.pop(key, None)

    overflow = len(_catalog_cache) - CATALOG_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(
            _catalog_cache,
            key=lambda key: _catalog_cache[key].created_at,
        )[:overflow]
        for key in oldest_keys:
            _catalog_cache.pop(key, None)

    for key in sorted(_catalog_cache, key=lambda key: _catalog_cache[key].created_at):
        if _catalog_cache_bytes() <= CATALOG_CACHE_MAX_BYTES:
            break
        _catalog_cache.pop(key, None)


def _get_catalog_cache_locked(cache_key: str, *, ttl: int, now: float) -> bytes | None:
    """读取缓存时顺手执行淘汰；调用方需持有 `_catalog_cache_lock`。"""
    _prune_catalog_cache(ttl=ttl, now=now)
    cached = _catalog_cache.get(cache_key)
    if cached and ttl > 0 and now - cached.created_at <= ttl:
        return cached.payload
    return None


def _store_catalog_cache_locked(
    cache_key: str,
    payload: bytes,
    *,
    ttl: int,
    now: float,
) -> None:
    """写缓存后立即按上限淘汰，避免并发请求短时堆积大量 PNG。"""
    if ttl <= 0:
        return
    _catalog_cache[cache_key] = _CachedCatalogImage(created_at=now, payload=payload)
    _prune_catalog_cache(ttl=ttl, now=now)


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ROLLPIG_TIMEZONE).replace(tzinfo=None)
    return parsed


def _is_recent_new(first_obtained_at: str | None, *, today: datetime.date) -> bool:
    obtained_at = _parse_datetime(first_obtained_at)
    if obtained_at is None:
        return False
    return 0 <= (today - obtained_at.date()).days < NEW_BADGE_DAYS


def _thumbnail_uri(pig_id: str, image_file: Path) -> str | None:
    """生成小图缓存；源文件状态进入指纹，资源包更新后旧缩略图自然失效。"""
    try:
        stat = image_file.stat()
    except OSError:
        return None

    cache_key = hashlib.sha256(
        "|".join(
            [
                pig_resource_manager.resource_version,
                pig_id,
                str(image_file.resolve()),
                str(stat.st_size),
                str(stat.st_mtime_ns),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    target = THUMB_CACHE_DIR / f"{pig_id}_{cache_key}.png"
    if target.exists():
        return target.as_uri()

    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(image_file) as image:
            image = image.convert("RGBA")
            image.thumbnail((120, 120), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            # 缩略图只是性能缓存，写入失败时继续使用原图，不能阻断图鉴生成。
            target.write_bytes(output.getvalue())
    except Exception as error:
        logger.warning(f"rollpig 图鉴缩略图生成失败: pig_id={pig_id} error={error}")
        return None
    return target.as_uri()


def _image_uri(pig_data: dict[str, Any], level: int) -> str:
    pig_id = str(pig_data.get("id") or "")
    appearance = pig_resource_manager.resolve_pig_appearance(pig_data, level)
    if appearance.image_path is not None:
        image_uri = _thumbnail_uri(pig_id, appearance.image_path)
        if image_uri:
            return image_uri
    if (
        appearance.base_image_path is not None
        and appearance.base_image_path != appearance.image_path
    ):
        return _thumbnail_uri(pig_id, appearance.base_image_path) or ""
    return ""


def _calculate_checkin_streak(
    recent_rolls: dict[str, str],
    *,
    today: datetime.date,
) -> int:
    """从今天往前数连续抽猪天数；只看已有历史，不补写漏掉的日期。"""
    streak = 0
    for offset in range(60):
        date_str = (today - datetime.timedelta(days=offset)).isoformat()
        if date_str not in recent_rolls:
            break
        streak += 1
    return streak


def _sort_progress_items(draw_state: DrawState) -> list[tuple[str, PigProgress]]:
    """按成长强度排序摘要用小猪；展柜本身始终保持资源顺序。"""
    resource_order = {
        str(pig.get("id")): index
        for index, pig in enumerate(pig_resource_manager.pig_list)
    }
    return sorted(
        draw_state.progress.items(),
        key=lambda item: (
            -expert_level_from_copies(item[1].copies),
            -int(item[1].copies or 0),
            item[1].first_obtained_at or "",
            resource_order.get(item[0], 10**9),
            item[0],
        ),
    )


def _level_class(level: int, max_level: int) -> str:
    if level <= 0:
        return "level-0"
    if max_level <= 1:
        return "level-max"
    ratio = level / max_level
    if ratio >= 1:
        return "level-max"
    if ratio >= 0.75:
        return "level-high"
    if ratio >= 0.4:
        return "level-mid"
    return "level-low"


def _ranking_value(rank: int | None) -> str:
    return f"#{rank}" if rank else "未上榜"


def _build_template_payload(
    *,
    user_name: str,
    user_avatar: str = "",
    snapshot: CatalogSnapshot,
    group_rank: int | None = None,
    total_rank: int | None = None,
) -> dict[str, Any]:
    today = rollpig_today()
    progress_items = _sort_progress_items(snapshot.draw_state)
    total_pigs = len(pig_resource_manager.pig_list)
    unlocked = len(snapshot.draw_state.pig_ids)
    max_level = get_growth_max_expert_level()
    progress_map = snapshot.draw_state.progress
    unlocked_ids = set(snapshot.draw_state.pig_ids)

    cards: list[dict[str, Any]] = []
    for index, pig in enumerate(pig_resource_manager.pig_list, start=1):
        pig_id = str(pig.get("id") or "")
        progress = progress_map.get(pig_id)
        locked = pig_id not in unlocked_ids or progress is None
        level = expert_level_from_copies(progress.copies) if progress else 0
        is_max = (not locked) and level >= max_level
        is_new = (not locked) and _is_recent_new(
            progress.first_obtained_at if progress else None,
            today=today,
        )
        # 固定展柜需要稳定占位，未解锁项不泄露具体小猪图片与名称。
        display_name = str(pig.get("name") or pig_id) if not locked else "未解锁"
        level_class = _level_class(level, max_level)
        card_class = " locked" if locked else ""
        if is_max:
            card_class += " max"
        cards.append(
            {
                "id": pig_id,
                "index": index,
                "name": display_name,
                "image": "" if locked else _image_uri(pig, level),
                "level": level,
                "level_class": level_class,
                "copies": int(progress.copies or 0) if progress else 0,
                "locked": locked,
                "is_new": is_new,
                "is_max": is_max,
                "card_class": card_class,
            }
        )

    if progress_items:
        favorite_id, favorite_progress = progress_items[0]
        favorite_pig = pig_resource_manager.pig_map.get(favorite_id, {})
        favorite_level = expert_level_from_copies(favorite_progress.copies)
        favorite = {
            "name": str(favorite_pig.get("name") or favorite_id),
            "image": _image_uri(favorite_pig, favorite_level),
            "level": favorite_level,
            "level_class": _level_class(favorite_level, max_level),
            "copies": int(favorite_progress.copies or 0),
        }
    else:
        favorite = {
            "name": "暂无",
            "image": "",
            "level": 0,
            "level_class": "level-0",
            "copies": 0,
        }

    levels = [
        expert_level_from_copies(progress.copies) for _, progress in progress_items
    ]
    progress_percent = (
        round((unlocked / total_pigs) * 100, 1) if total_pigs > 0 else 0.0
    )
    stats = {
        "unlocked": unlocked,
        "total": total_pigs,
        "progress_percent": progress_percent,
        "maxed_count": sum(1 for level in levels if level >= max_level),
        "locked_count": max(0, total_pigs - unlocked),
        "max_level": max_level,
        "duplicate_streak": max(0, int(snapshot.draw_state.duplicate_streak or 0)),
        "recent_new_count": sum(
            1
            for _, progress in progress_items
            if _is_recent_new(progress.first_obtained_at, today=today)
        ),
        "checkin_streak": _calculate_checkin_streak(
            snapshot.recent_rolls,
            today=today,
        ),
        "roasted_7d": int(snapshot.roasted_7d or 0),
        "group_rank": _ranking_value(group_rank),
        "total_rank": _ranking_value(total_rank),
        "has_group_rank": group_rank is not None,
        "has_total_rank": total_rank is not None,
    }
    return {
        "user_name": user_name,
        "user_avatar": user_avatar,
        "stats": stats,
        "favorite": favorite,
        "cards": cards,
        "new_badge_uri": NEW_BADGE_URI,
        "font_faces": get_harmony_font_faces(),
    }


def _build_cache_key(payload: dict[str, Any], snapshot: CatalogSnapshot) -> str:
    """缓存指纹只放稳定摘要，避免把 HTML 或图片二进制塞进 key。"""
    key_payload = {
        "resource_version": pig_resource_manager.resource_version,
        "user_name": payload["user_name"],
        "user_avatar": payload["user_avatar"],
        "stats": payload["stats"],
        "favorite": payload["favorite"],
        "cards": [
            (
                card["id"],
                card["locked"],
                card["level"],
                card["copies"],
                card["is_new"],
                card["is_max"],
                card["image"],
            )
            for card in payload["cards"]
        ],
        "recent_rolls": snapshot.recent_rolls,
        "roasted_7d": snapshot.roasted_7d,
    }
    raw = json.dumps(
        key_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _render_catalog_image_uncached(
    *,
    payload: dict[str, Any],
    cache_key: str,
    ttl: int,
    started_at: float,
) -> bytes:
    """执行真实 HTML 截图；外层负责缓存命中和同 key 合流。"""
    render_started_at = time.perf_counter()
    timeout = get_catalog_render_timeout()
    async with html_render_budget("catalog"):
        from zhenxun import ui

        # 延迟导入 zhenxun 渲染入口，避免模块加载阶段提前触发 NoneBot 服务初始化。
        result = await asyncio.wait_for(
            ui.render_template(
                CATALOG_TEMPLATE_PATH,
                payload,
                use_cache=False,
                is_page=True,
                viewport={"width": 2580, "height": 10},
                wait=100,
            ),
            timeout=timeout,
        )
    finished_at = time.perf_counter()
    logger.info(
        "rollpig catalog rendered: "
        f"user={payload['user_name']} cards={len(payload['cards'])} "
        f"render={finished_at - render_started_at:.2f}s "
        f"total={finished_at - started_at:.2f}s bytes={len(result)}"
    )
    async with _catalog_cache_lock:
        _store_catalog_cache_locked(cache_key, result, ttl=ttl, now=time.time())
    return result


async def render_catalog_image(
    *,
    user_name: str,
    user_avatar: str = "",
    snapshot: CatalogSnapshot,
    group_rank: int | None = None,
    total_rank: int | None = None,
) -> bytes:
    """渲染图片版小猪图鉴；只消费快照，不修改抽猪状态或 copies。"""
    started_at = time.perf_counter()
    payload = _build_template_payload(
        user_name=user_name,
        user_avatar=user_avatar,
        snapshot=snapshot,
        group_rank=group_rank,
        total_rank=total_rank,
    )
    cache_key = _build_cache_key(payload, snapshot)
    ttl = get_catalog_cache_seconds()

    render_owner = False
    async with _catalog_cache_lock:
        cached_payload = _get_catalog_cache_locked(cache_key, ttl=ttl, now=time.time())
        if cached_payload is not None:
            logger.debug(
                "rollpig catalog cache hit: "
                f"user={user_name} cards={len(payload['cards'])} "
                f"bytes={len(cached_payload)}"
            )
            return cached_payload

        render_task = _catalog_render_tasks.get(cache_key)
        if render_task is None or render_task.done():
            render_task = asyncio.create_task(
                _render_catalog_image_uncached(
                    payload=payload,
                    cache_key=cache_key,
                    ttl=ttl,
                    started_at=started_at,
                )
            )
            _catalog_render_tasks[cache_key] = render_task
            render_owner = True

    if not render_owner:
        logger.debug(
            "rollpig catalog render coalesced: "
            f"user={user_name} cards={len(payload['cards'])}"
        )

    try:
        return await render_task
    finally:
        if render_owner:
            async with _catalog_cache_lock:
                if _catalog_render_tasks.get(cache_key) is render_task:
                    _catalog_render_tasks.pop(cache_key, None)
