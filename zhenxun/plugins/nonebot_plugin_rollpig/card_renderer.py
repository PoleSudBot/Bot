from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import threading
import time
from typing import Any, Literal
import uuid
import weakref

from nonebot.log import logger
import nonebot_plugin_localstore as localstore
import PIL
from PIL import Image, ImageDraw

from zhenxun.utils._build_image import BuildImage

from .config import get_card_disk_cache_max_bytes, get_gif_render_concurrency

CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
AVATAR_SIZE = 240
NAME_FONT_SIZE = 48
DESC_FONT_SIZE = 30
ANALYSIS_FONT_SIZE = 28
ANALYSIS_MIN_FONT_SIZE = 20
GIF_TARGET_FRAMES = 60
GIF_MAX_DECODE_WORK_PIXELS = 16_000_000
GIF_ABSOLUTE_MAX_SOURCE_FRAMES = 600
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100
GIF_PALETTE_SAMPLE_FRAMES = 16
GIF_PALETTE_SAMPLE_SIZE = 200
GIF_SOURCE_CACHE_MAX_ENTRIES = 8
GIF_SOURCE_CACHE_MAX_BYTES = 24 * 1024 * 1024
CARD_RENDER_REVISION = "rollpig-psb-card-v3"
CARD_CACHE_MAGIC = b"ROLLPIG-CARD\x00"
CARD_CACHE_SCHEMA = 1
PLUGIN_DIR = Path(__file__).parent
NEW_ICON_FILE = PLUGIN_DIR / "resource" / "assets" / "new.png"
CARD_CACHE_DIR = localstore.get_plugin_cache_dir() / "card_results"
FONT_DIR = (
    Path(__file__).resolve().parents[3]
    / "resources"
    / "font"
    / "HarmonyOS_Sans_SC"
)
REGULAR_FONT = FONT_DIR / "HarmonyOS_SansSC_Regular.ttf"
BOLD_FONT = FONT_DIR / "HarmonyOS_SansSC_Bold.ttf"


@dataclass(frozen=True)
class PigCardRenderResult:
    data: bytes
    image_format: Literal["png", "gif"]
    renderer: Literal["pillow", "pillow-gif"]


@dataclass(frozen=True)
class _PreparedCard:
    canvas: Image.Image
    avatar_y: int


@dataclass(frozen=True)
class _CardFingerprint:
    card_key: str
    source_key: str | None


@dataclass
class _GifSourceCacheEntry:
    frames: list[tuple[Image.Image, int]]
    decoded_bytes: int


_gif_source_cache: OrderedDict[str, _GifSourceCacheEntry] = OrderedDict()
_gif_source_cache_lock = threading.Lock()
_static_asset_hash_cache: dict[tuple[str, int, int, int, int], str] = {}
_static_asset_hash_lock = threading.Lock()
_card_disk_cache_lock = threading.Lock()
_card_disk_cache_initialized_limit: int | None = None
_gif_render_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_card_render_tasks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Task[PigCardRenderResult]]
] = weakref.WeakKeyDictionary()


def _font(path: Path, size: int):
    return BuildImage.load_font(path, size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text or " ", font=font)
    return box[2] - box[0]


def _wrap_text(text: str, draw: ImageDraw.ImageDraw, font) -> list[str]:
    """按实际像素宽度换行，避免中文长文案溢出卡片。"""

    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and _text_width(draw, candidate, font) > CONTENT_WIDTH:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _layout_text(pig_data: Mapping[str, Any]) -> tuple[list[str], list[str], int]:
    probe = Image.new("RGB", (1, 1), "white")
    draw = ImageDraw.Draw(probe)
    desc_lines = _wrap_text(
        str(pig_data.get("description") or ""),
        draw,
        _font(REGULAR_FONT, DESC_FONT_SIZE),
    )
    analysis_text = str(pig_data.get("analysis") or "你今天是只神秘小猪。")
    analysis_size = ANALYSIS_FONT_SIZE
    while analysis_size > ANALYSIS_MIN_FONT_SIZE:
        analysis_lines = _wrap_text(
            analysis_text,
            draw,
            _font(REGULAR_FONT, analysis_size),
        )
        total_height = (
            AVATAR_SIZE
            + 20
            + 58
            + (20 + len(desc_lines) * 40 if any(desc_lines) else 0)
            + 30
            + len(analysis_lines) * int(analysis_size * 1.5)
        )
        if total_height <= CONTENT_WIDTH:
            return desc_lines, analysis_lines, analysis_size
        analysis_size -= 2

    analysis_lines = _wrap_text(
        analysis_text,
        draw,
        _font(REGULAR_FONT, ANALYSIS_MIN_FONT_SIZE),
    )
    max_lines = 8
    if len(analysis_lines) > max_lines:
        analysis_lines = analysis_lines[:max_lines]
        analysis_lines[-1] = analysis_lines[-1].rstrip("。…") + "…"
    return desc_lines, analysis_lines, ANALYSIS_MIN_FONT_SIZE


def _draw_centered_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font,
    fill: tuple[int, int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text or " ", font=font)
    width = box[2] - box[0]
    draw.text(((CANVAS_SIZE[0] - width) // 2, y), text, font=font, fill=fill)


def _prepare_card(pig_data: Mapping[str, Any]) -> _PreparedCard:
    """生成不含头像的静态卡片层，供 PNG 和每一帧 GIF 复用。"""

    desc_lines, analysis_lines, analysis_size = _layout_text(pig_data)
    desc_height = len(desc_lines) * 40 if any(desc_lines) else 0
    analysis_line_height = int(analysis_size * 1.5)
    total_height = (
        AVATAR_SIZE
        + 20
        + 58
        + (20 + desc_height if desc_height else 0)
        + 30
        + len(analysis_lines) * analysis_line_height
    )
    avatar_y = max(20, (CANVAS_SIZE[1] - total_height) // 2)
    build_image = BuildImage(
        *CANVAS_SIZE,
        color=(255, 255, 255, 255),
        font=_font(REGULAR_FONT, ANALYSIS_FONT_SIZE),
    )
    canvas = build_image.markImg
    draw = ImageDraw.Draw(canvas)
    y = avatar_y + AVATAR_SIZE + 20
    _draw_centered_line(
        draw,
        str(pig_data.get("name") or "未知小猪"),
        y,
        _font(BOLD_FONT, NAME_FONT_SIZE),
        (0, 0, 0, 255),
    )
    y += 58
    if desc_height:
        y += 20
        desc_font = _font(REGULAR_FONT, DESC_FONT_SIZE)
        for line in desc_lines:
            _draw_centered_line(draw, line, y, desc_font, (85, 85, 85, 255))
            y += 40
    y += 30
    analysis_font = _font(REGULAR_FONT, analysis_size)
    for line in analysis_lines:
        _draw_centered_line(draw, line, y, analysis_font, (51, 51, 51, 255))
        y += analysis_line_height

    return _PreparedCard(canvas=canvas, avatar_y=avatar_y)


def _paste_new_icon(canvas: Image.Image, avatar_y: int) -> None:
    if NEW_ICON_FILE.exists():
        with Image.open(NEW_ICON_FILE) as opened:
            with opened.convert("RGBA") as new_icon:
                new_icon.thumbnail((180, 180), Image.Resampling.LANCZOS)
                canvas.alpha_composite(new_icon, (390, max(0, avatar_y - 50)))


def _paste_avatar(canvas: Image.Image, avatar: Image.Image, avatar_y: int) -> None:
    """按资源原始画布居中合成头像，不额外裁切、缩放或补留白。"""

    with avatar.convert("RGBA") as source:
        canvas.alpha_composite(
            source,
            ((CANVAS_SIZE[0] - source.width) // 2, avatar_y),
        )


def _normalize_duration(value: object) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


def _close_frames(frames: list[tuple[Image.Image, int]]) -> None:
    for frame, _duration in frames:
        with suppress(Exception):
            frame.close()


def _clone_frames(
    frames: list[tuple[Image.Image, int]],
) -> list[tuple[Image.Image, int]]:
    cloned: list[tuple[Image.Image, int]] = []
    try:
        for frame, duration in frames:
            cloned.append((frame.copy(), duration))
    except Exception:
        _close_frames(cloned)
        raise
    return cloned


def _decoded_frame_bytes(frames: list[tuple[Image.Image, int]]) -> int:
    return sum(frame.width * frame.height * 4 for frame, _duration in frames)


def _get_cached_gif_frames(source_key: str) -> list[tuple[Image.Image, int]] | None:
    with _gif_source_cache_lock:
        cached = _gif_source_cache.get(source_key)
        if cached is None:
            return None
        _gif_source_cache.move_to_end(source_key)
        return _clone_frames(cached.frames)


def _store_gif_frames(
    source_key: str,
    frames: list[tuple[Image.Image, int]],
) -> None:
    decoded_bytes = _decoded_frame_bytes(frames)
    if decoded_bytes > GIF_SOURCE_CACHE_MAX_BYTES:
        return

    cached_frames = _clone_frames(frames)
    with _gif_source_cache_lock:
        previous = _gif_source_cache.pop(source_key, None)
        if previous is not None:
            _close_frames(previous.frames)
        _gif_source_cache[source_key] = _GifSourceCacheEntry(
            frames=cached_frames,
            decoded_bytes=decoded_bytes,
        )

        total_bytes = sum(entry.decoded_bytes for entry in _gif_source_cache.values())
        while (
            len(_gif_source_cache) > GIF_SOURCE_CACHE_MAX_ENTRIES
            or total_bytes > GIF_SOURCE_CACHE_MAX_BYTES
        ):
            _key, evicted = _gif_source_cache.popitem(last=False)
            total_bytes -= evicted.decoded_bytes
            _close_frames(evicted.frames)


def _gif_frame_groups(frame_count: int) -> list[tuple[int, int, int]]:
    output_count = min(frame_count, GIF_TARGET_FRAMES)
    groups: list[tuple[int, int, int]] = []
    for output_index in range(output_count):
        start = output_index * frame_count // output_count
        end = (output_index + 1) * frame_count // output_count
        representative = (start + end - 1) // 2
        groups.append((start, end, representative))
    return groups


def _decode_gif_frames(image_file: Path) -> list[tuple[Image.Image, int]]:
    images: list[Image.Image | None] = []
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1))
            if frame_count <= 1:
                return []
            if (
                frame_count > GIF_ABSOLUTE_MAX_SOURCE_FRAMES
                or opened.width * opened.height * frame_count
                > GIF_MAX_DECODE_WORK_PIXELS
            ):
                logger.warning(
                    "rollpig GIF 超出安全解码预算，降级为静态卡片: "
                    f"image={image_file}, frames={frame_count}, "
                    f"size={opened.width}x{opened.height}"
                )
                return []

            groups = _gif_frame_groups(frame_count)
            representative_to_group = {
                representative: group_index
                for group_index, (_start, _end, representative) in enumerate(groups)
            }
            durations = [0] * len(groups)
            images = [None] * len(groups)
            group_index = 0
            for frame_index in range(frame_count):
                opened.seek(frame_index)
                while frame_index >= groups[group_index][1]:
                    group_index += 1
                durations[group_index] += _normalize_duration(
                    opened.info.get("duration")
                )
                selected_index = representative_to_group.get(frame_index)
                if selected_index is not None:
                    images[selected_index] = opened.convert("RGBA")

            if any(image is None for image in images):
                for image in images:
                    if image is not None:
                        image.close()
                return []
            return [
                (image, durations[index])
                for index, image in enumerate(images)
                if image is not None
            ]
    except Exception as error:
        for image in images:
            if image is not None:
                with suppress(Exception):
                    image.close()
        logger.warning(f"rollpig GIF 解码失败，降级为静态卡片: image={image_file}, error={error}")
        return []


def _load_gif_frames(
    image_file: Path | None,
    source_key: str | None,
) -> list[tuple[Image.Image, int]]:
    if image_file is None or image_file.suffix.lower() != ".gif":
        return []
    if source_key:
        cached = _get_cached_gif_frames(source_key)
        if cached is not None:
            return cached
    frames = _decode_gif_frames(image_file)
    if frames and source_key:
        _store_gif_frames(source_key, frames)
    return frames


def _encode_png(
    prepared: _PreparedCard,
    image_file: Path | None,
    *,
    is_new: bool,
) -> PigCardRenderResult:
    canvas = prepared.canvas.copy()
    try:
        if image_file is not None:
            with Image.open(image_file) as opened:
                _paste_avatar(canvas, opened, prepared.avatar_y)
        if is_new:
            _paste_new_icon(canvas, prepared.avatar_y)
        output = BytesIO()
        with canvas.convert("RGB") as rgb:
            rgb.save(output, format="PNG", optimize=True)
        return PigCardRenderResult(output.getvalue(), "png", "pillow")
    finally:
        canvas.close()


def _compose_gif_frame(
    prepared: _PreparedCard,
    avatar: Image.Image,
    *,
    is_new: bool,
) -> Image.Image:
    frame = prepared.canvas.copy()
    _paste_avatar(frame, avatar, prepared.avatar_y)
    if is_new:
        _paste_new_icon(frame, prepared.avatar_y)
    return frame


def _build_gif_palette(
    prepared: _PreparedCard,
    avatar_frames: list[tuple[Image.Image, int]],
    *,
    is_new: bool,
) -> Image.Image:
    sample_count = min(len(avatar_frames), GIF_PALETTE_SAMPLE_FRAMES)
    sample_indices = sorted(
        {
            round(index * (len(avatar_frames) - 1) / max(sample_count - 1, 1))
            for index in range(sample_count)
        }
    )
    palette_source = Image.new(
        "RGB",
        (GIF_PALETTE_SAMPLE_SIZE, GIF_PALETTE_SAMPLE_SIZE * len(sample_indices)),
    )
    try:
        for output_index, frame_index in enumerate(sample_indices):
            avatar, _duration = avatar_frames[frame_index]
            frame = _compose_gif_frame(prepared, avatar, is_new=is_new)
            try:
                with frame.convert("RGB") as rgb:
                    with rgb.resize(
                        (GIF_PALETTE_SAMPLE_SIZE, GIF_PALETTE_SAMPLE_SIZE),
                        Image.Resampling.LANCZOS,
                    ) as sample:
                        palette_source.paste(
                            sample,
                            (0, output_index * GIF_PALETTE_SAMPLE_SIZE),
                        )
            finally:
                frame.close()
        return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    finally:
        palette_source.close()


def _encode_gif(
    prepared: _PreparedCard,
    avatar_frames: list[tuple[Image.Image, int]],
    *,
    is_new: bool,
) -> PigCardRenderResult:
    palette = _build_gif_palette(prepared, avatar_frames, is_new=is_new)
    output_frames: list[Image.Image] = []
    durations: list[int] = []
    try:
        for avatar, duration in avatar_frames:
            frame = _compose_gif_frame(prepared, avatar, is_new=is_new)
            try:
                with frame.convert("RGB") as rgb:
                    output_frames.append(
                        rgb.quantize(palette=palette, dither=Image.Dither.NONE)
                    )
            finally:
                frame.close()
            durations.append(duration)

        output = BytesIO()
        output_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=output_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        return PigCardRenderResult(output.getvalue(), "gif", "pillow-gif")
    finally:
        palette.close()
        for frame in output_frames:
            frame.close()


def _render_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    is_new: bool,
    source_key: str | None,
) -> PigCardRenderResult:
    prepared = _prepare_card(pig_data)
    gif_frames: list[tuple[Image.Image, int]] = []
    try:
        gif_frames = _load_gif_frames(image_file, source_key)
        if gif_frames:
            return _encode_gif(prepared, gif_frames, is_new=is_new)
        return _encode_png(prepared, image_file, is_new=is_new)
    finally:
        _close_frames(gif_frames)
        prepared.canvas.close()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _static_asset_signature(path: Path) -> dict[str, str | int]:
    stat = path.stat()
    cache_key = (
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
    )
    with _static_asset_hash_lock:
        digest = _static_asset_hash_cache.get(cache_key)
    if digest is None:
        digest = _hash_file(path)
        with _static_asset_hash_lock:
            _static_asset_hash_cache[cache_key] = digest
    return {"path": cache_key[0], "size": stat.st_size, "sha256": digest}


def _source_signature(path: Path | None) -> dict[str, str | int] | None:
    if path is None:
        return None
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "sha256": _hash_file(path),
    }


def _build_card_fingerprint(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    is_new: bool,
    resource_version: str,
) -> _CardFingerprint:
    source = _source_signature(image_file)
    payload = {
        "revision": CARD_RENDER_REVISION,
        "pillow": PIL.__version__,
        "layout": {
            "canvas": CANVAS_SIZE,
            "content_width": CONTENT_WIDTH,
            "avatar_size": AVATAR_SIZE,
            "name_font_size": NAME_FONT_SIZE,
            "description_font_size": DESC_FONT_SIZE,
            "analysis_font_size": ANALYSIS_FONT_SIZE,
            "analysis_min_font_size": ANALYSIS_MIN_FONT_SIZE,
        },
        "pig": {
            key: str(pig_data.get(key) or "")
            for key in ("id", "name", "description", "analysis")
        },
        "is_new": is_new,
        "resource_version": str(resource_version or ""),
        "source": source,
        "fonts": [
            _static_asset_signature(REGULAR_FONT),
            _static_asset_signature(BOLD_FONT),
        ],
        "new_icon": (
            _static_asset_signature(NEW_ICON_FILE)
            if is_new and NEW_ICON_FILE.exists()
            else None
        ),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    source_key = None
    if source is not None:
        source_key = f"{CARD_RENDER_REVISION}:{source['path']}:{source['sha256']}"
    return _CardFingerprint(
        card_key=hashlib.sha256(serialized).hexdigest(),
        source_key=source_key,
    )


def _card_cache_path(card_key: str) -> Path:
    return CARD_CACHE_DIR / f"{card_key}.card"


def _encode_cache_container(result: PigCardRenderResult) -> bytes:
    metadata = json.dumps(
        {
            "schema": CARD_CACHE_SCHEMA,
            "format": result.image_format,
            "renderer": result.renderer,
            "payload_size": len(result.data),
            "payload_sha256": hashlib.sha256(result.data).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CARD_CACHE_MAGIC + struct.pack(">I", len(metadata)) + metadata + result.data


def _decode_cache_container(data: bytes) -> PigCardRenderResult:
    prefix_size = len(CARD_CACHE_MAGIC) + 4
    if len(data) < prefix_size or not data.startswith(CARD_CACHE_MAGIC):
        raise ValueError("invalid cache magic")
    metadata_size = struct.unpack(">I", data[len(CARD_CACHE_MAGIC) : prefix_size])[0]
    metadata_end = prefix_size + metadata_size
    if metadata_size <= 0 or metadata_end > len(data):
        raise ValueError("invalid cache metadata size")
    metadata = json.loads(data[prefix_size:metadata_end].decode("utf-8"))
    payload = data[metadata_end:]
    image_format = metadata.get("format")
    renderer = metadata.get("renderer")
    if metadata.get("schema") != CARD_CACHE_SCHEMA:
        raise ValueError("unsupported cache schema")
    if image_format not in {"png", "gif"}:
        raise ValueError("invalid cache image format")
    if renderer not in {"pillow", "pillow-gif"}:
        raise ValueError("invalid cache renderer")
    if metadata.get("payload_size") != len(payload):
        raise ValueError("invalid cache payload size")
    if metadata.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("invalid cache payload digest")
    return PigCardRenderResult(payload, image_format, renderer)


def _trim_card_disk_cache(max_bytes: int, *, protected: Path | None = None) -> None:
    if not CARD_CACHE_DIR.exists():
        return
    now = time.time()
    for temp_file in CARD_CACHE_DIR.glob("*.tmp"):
        try:
            if now - temp_file.stat().st_mtime > 3600:
                temp_file.unlink(missing_ok=True)
        except OSError:
            continue

    entries: list[tuple[int, int, Path]] = []
    for cache_file in CARD_CACHE_DIR.glob("*.card"):
        try:
            stat = cache_file.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime_ns, stat.st_size, cache_file))
    total_bytes = sum(size for _mtime, size, _path in entries)
    for _mtime, size, cache_file in sorted(entries):
        if total_bytes <= max_bytes:
            break
        if protected is not None and cache_file == protected:
            continue
        try:
            cache_file.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size


def _ensure_card_disk_cache_limit(max_bytes: int) -> None:
    global _card_disk_cache_initialized_limit
    if _card_disk_cache_initialized_limit == max_bytes:
        return
    _trim_card_disk_cache(max_bytes)
    _card_disk_cache_initialized_limit = max_bytes


def _get_fixed_card(card_key: str, max_bytes: int) -> PigCardRenderResult | None:
    if max_bytes <= 0:
        return None
    target = _card_cache_path(card_key)
    with _card_disk_cache_lock:
        try:
            _ensure_card_disk_cache_limit(max_bytes)
            if not target.is_file():
                return None
            result = _decode_cache_container(target.read_bytes())
            os.utime(target, None)
            return result
        except Exception as error:
            with suppress(OSError):
                target.unlink(missing_ok=True)
            logger.warning(f"rollpig 固定卡片缓存读取失败，已旁路: file={target}, error={error}")
            return None


def _store_fixed_card(
    card_key: str,
    result: PigCardRenderResult,
    max_bytes: int,
) -> None:
    if max_bytes <= 0:
        return
    container = _encode_cache_container(result)
    if len(container) > max_bytes:
        return
    target = _card_cache_path(card_key)
    temp_file = CARD_CACHE_DIR / (
        f".{card_key}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    with _card_disk_cache_lock:
        try:
            CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp_file.write_bytes(container)
            os.replace(temp_file, target)
            _trim_card_disk_cache(max_bytes, protected=target)
        except Exception as error:
            logger.warning(f"rollpig 固定卡片缓存写入失败，已旁路: file={target}, error={error}")
        finally:
            with suppress(OSError):
                temp_file.unlink(missing_ok=True)


def _get_gif_render_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _gif_render_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(get_gif_render_concurrency())
        _gif_render_semaphores[loop] = semaphore
    return semaphore


async def _render_card_once(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    is_new: bool,
    resource_version: str,
    fingerprint: _CardFingerprint,
    cache_final_card: bool,
    max_cache_bytes: int,
) -> PigCardRenderResult:
    async def render() -> PigCardRenderResult:
        return await asyncio.to_thread(
            _render_sync,
            pig_data,
            image_file,
            is_new=is_new,
            source_key=fingerprint.source_key,
        )

    if image_file is not None and image_file.suffix.lower() == ".gif":
        async with _get_gif_render_semaphore():
            result = await render()
    else:
        result = await render()

    if cache_final_card:
        fresh_fingerprint = await asyncio.to_thread(
            _build_card_fingerprint,
            pig_data,
            image_file,
            is_new=is_new,
            resource_version=resource_version,
        )
        if fresh_fingerprint.card_key == fingerprint.card_key:
            await asyncio.to_thread(
                _store_fixed_card,
                fingerprint.card_key,
                result,
                max_cache_bytes,
            )
    return result


def clear_card_renderer_runtime_cache() -> None:
    global _card_disk_cache_initialized_limit
    with _gif_source_cache_lock:
        for cached in _gif_source_cache.values():
            _close_frames(cached.frames)
        _gif_source_cache.clear()
    with _static_asset_hash_lock:
        _static_asset_hash_cache.clear()
    _card_disk_cache_initialized_limit = None


async def shutdown_card_renderer() -> None:
    loop = asyncio.get_running_loop()
    tasks = [
        task
        for task in _card_render_tasks.get(loop, {}).values()
        if not task.done()
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(clear_card_renderer_runtime_cache)


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    is_new: bool = False,
    cache_final_card: bool = True,
    resource_version: str = "",
) -> PigCardRenderResult:
    """在线程中生成普通小猪卡片，动态头像输出 GIF。"""

    fingerprint = await asyncio.to_thread(
        _build_card_fingerprint,
        pig_data,
        image_file,
        is_new=is_new,
        resource_version=resource_version,
    )
    max_cache_bytes = get_card_disk_cache_max_bytes()
    if cache_final_card:
        cached = await asyncio.to_thread(
            _get_fixed_card,
            fingerprint.card_key,
            max_cache_bytes,
        )
        if cached is not None:
            return cached

    loop = asyncio.get_running_loop()
    tasks = _card_render_tasks.setdefault(loop, {})
    task_key = f"{'fixed' if cache_final_card else 'dynamic'}:{fingerprint.card_key}"
    task = tasks.get(task_key)
    if task is None:
        task = asyncio.create_task(
            _render_card_once(
                pig_data,
                image_file,
                is_new=is_new,
                resource_version=resource_version,
                fingerprint=fingerprint,
                cache_final_card=cache_final_card,
                max_cache_bytes=max_cache_bytes,
            )
        )
        tasks[task_key] = task

        def cleanup(done: asyncio.Task[PigCardRenderResult]) -> None:
            if tasks.get(task_key) is done:
                tasks.pop(task_key, None)

        task.add_done_callback(cleanup)
    return await asyncio.shield(task)
