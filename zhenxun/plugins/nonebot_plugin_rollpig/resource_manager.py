from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import httpx
from nonebot.log import logger
import nonebot_plugin_localstore as localstore
from PIL import Image, UnidentifiedImageError

from .config import (
    get_official_gif_resource_enabled,
    get_official_gif_resource_manifest_url,
    get_private_resource_manifest_url,
    get_private_resource_manifests,
    get_private_resource_token,
    get_proxy,
    get_resource_manifest_url,
    get_resource_max_file_size,
    get_resource_sync_enabled,
    get_resource_sync_timeout,
)

PLUGIN_DIR = Path(__file__).parent
BUILTIN_RESOURCE_DIR = PLUGIN_DIR / "resource"
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_RULES_JSON = BUILTIN_RESOURCE_DIR / "pig_rules.json"
BUILTIN_EX_VARIANTS_JSON = BUILTIN_RESOURCE_DIR / "pig_ex_variants.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"
LEGACY_EX_VARIANTS_FILENAME = "pig_ex_variants_legacy.json"

CACHE_ROOT = localstore.get_plugin_data_dir() / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
ACTIVE_IMAGE_DIR = ACTIVE_RESOURCE_DIR / "images"
PREVIOUS_RESOURCE_DIR = CACHE_ROOT / "previous"
STATE_FILE = CACHE_ROOT / "state.json"
PRIVATE_RESOURCE_DIR = CACHE_ROOT / "private_active"
PRIVATE_IMAGE_DIR = PRIVATE_RESOURCE_DIR / "images"
PRIVATE_PREVIOUS_RESOURCE_DIR = CACHE_ROOT / "private_previous"
PRIVATE_STATE_FILE = CACHE_ROOT / "private_state.json"
PRIVATE_OVERLAY_ROOT = CACHE_ROOT / "private_overlays"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
IMAGE_SUFFIX_PRIORITY = (".gif", ".png", ".webp", ".jpg", ".jpeg")
ALLOWED_IMAGE_SUFFIXES = set(IMAGE_SUFFIX_PRIORITY)
VARIANT_IMAGE_MANIFEST_FIELDS = frozenset(
    {"pig_id", "level", "filename", "path", "size", "sha256"}
)
RESOURCE_EX_VARIANTS_JSON_MAX_SIZE = 512 * 1024
RESOURCE_MAX_EX_VARIANTS = 500
RESOURCE_MAX_VARIANT_IMAGES = 500
RESOURCE_MAX_VARIANT_LEVEL = 5
IMAGE_FORMAT_SUFFIXES = {
    "GIF": {".gif"},
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}
RULE_KEYS = (
    "food_pigs",
    "human_pigs",
    "eaten_pigs",
    "sold_pigs",
    "roast_excluded_pigs",
)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ResourceSyncResult:
    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


@dataclass
class ImageSyncReport:
    accepted_mismatches: list[str]
    skipped: list[str]

    def extend(self, other: "ImageSyncReport") -> None:
        self.accepted_mismatches.extend(other.accepted_mismatches)
        self.skipped.extend(other.skipped)


@dataclass(frozen=True)
class ResourceOverlaySource:
    name: str
    manifest_url: str
    token: str | None
    active_dir: Path
    previous_dir: Path
    state_file: Path


@dataclass(frozen=True)
class _PigExVariantSpec:
    pig_id: str
    level: int
    filename: str | None = None
    description: str | None = None
    analysis: str | None = None


@dataclass(frozen=True)
class PigExVariant:
    pig_id: str
    level: int
    image_path: Path | None = None
    description: str | None = None
    analysis: str | None = None


@dataclass(frozen=True)
class ResolvedPigAppearance:
    pig_data: dict[str, Any]
    image_path: Path | None
    base_image_path: Path | None
    requested_level: int
    applied_level: int
    resource_version: str


def _resource_overlay_sources() -> list[ResourceOverlaySource]:
    """按由低到高的覆盖优先级构建远程 overlay 列表。"""

    sources: list[ResourceOverlaySource] = []
    official_url = get_official_gif_resource_manifest_url()
    if get_official_gif_resource_enabled() and official_url:
        root = PRIVATE_OVERLAY_ROOT / "official-gif"
        sources.append(
            ResourceOverlaySource(
                name="official-gif",
                manifest_url=official_url,
                token=None,
                active_dir=root / "active",
                previous_dir=root / "previous",
                state_file=root / "state.json",
            )
        )

    legacy_url = get_private_resource_manifest_url()
    configured = get_private_resource_manifests()
    configured_urls = {item.manifest_url for item in configured}
    if legacy_url and legacy_url not in configured_urls:
        sources.append(
            ResourceOverlaySource(
                name="legacy-private",
                manifest_url=legacy_url,
                token=get_private_resource_token(),
                active_dir=PRIVATE_RESOURCE_DIR,
                previous_dir=PRIVATE_PREVIOUS_RESOURCE_DIR,
                state_file=PRIVATE_STATE_FILE,
            )
        )

    for item in configured:
        root = PRIVATE_OVERLAY_ROOT / item.name
        sources.append(
            ResourceOverlaySource(
                name=item.name,
                manifest_url=item.manifest_url,
                token=item.token,
                active_dir=root / "active",
                previous_dir=root / "previous",
                state_file=root / "state.json",
            )
        )
    return sources


class RollPigResourceManager:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()
        self.pig_list: list[dict[str, Any]] = []
        self.pig_map: dict[str, dict[str, Any]] = {}
        self.rules: dict[str, list[str]] = {}
        self.image_dirs: list[Path] = []
        self.ex_variants: dict[str, dict[int, PigExVariant]] = {}
        self.variant_blocked_pig_ids: set[str] = set()
        self.resource_version = "builtin"

    def reload(self) -> None:
        """加载当前可用资源快照；缓存坏掉时回退内置资源，避免阻断 Bot 启动。"""
        if (ACTIVE_RESOURCE_DIR / "pig.json").exists():
            try:
                self._load_from_dir(
                    ACTIVE_RESOURCE_DIR,
                    resource_version=self._read_state_version(),
                    image_dirs=[ACTIVE_IMAGE_DIR, BUILTIN_IMAGE_DIR],
                )
                self._load_private_overlay()
                return
            except Exception as error:
                logger.warning(f"rollpig 资源缓存读取失败，回退内置资源: {error}")

        self._load_from_dir(
            BUILTIN_RESOURCE_DIR,
            resource_version="builtin",
            image_dirs=[BUILTIN_IMAGE_DIR],
        )
        self._load_private_overlay()

    def _load_from_dir(
        self,
        resource_dir: Path,
        *,
        resource_version: str,
        image_dirs: list[Path],
    ) -> None:
        pig_list = self._read_pig_json(resource_dir / "pig.json")
        rules = self._normalize_rules(
            self._read_rules_json(resource_dir / "pig_rules.json")
        )
        self._validate_pig_list(pig_list)
        self.pig_list = pig_list
        self.pig_map = {str(item["id"]): item for item in pig_list}
        self.rules = rules
        self.image_dirs = image_dirs
        legacy_variants = self._load_ex_variants(
            resource_dir / LEGACY_EX_VARIANTS_FILENAME,
            pig_ids={str(item["id"]) for item in pig_list},
            image_dir=image_dirs[0],
            strict=False,
        )
        current_variants = self._load_ex_variants(
            resource_dir / "pig_ex_variants.json",
            pig_ids={str(item["id"]) for item in pig_list},
            image_dir=image_dirs[0],
            strict=False,
        )
        for pig_id, levels in current_variants.items():
            legacy_variants.setdefault(pig_id, {}).update(levels)
        self.ex_variants = legacy_variants
        self.variant_blocked_pig_ids = set()
        self.resource_version = resource_version or "cache"
        logger.info(
            f"rollpig 资源已加载: version={self.resource_version}, pigs={len(pig_list)}"
        )

    def _load_private_overlay(self) -> None:
        """依优先级叠加所有可用缓存；单包损坏不影响其余资源。"""

        for source in _resource_overlay_sources():
            if not (source.active_dir / "pig.json").exists():
                continue
            try:
                self._apply_private_overlay(
                    source.active_dir,
                    resource_version=self._read_private_state_version(source),
                )
            except Exception as error:
                logger.warning(
                    "rollpig 资源包缓存读取失败，已忽略: "
                    f"pack={source.name}, error={error}"
                )

    def _apply_private_overlay(
        self,
        resource_dir: Path,
        *,
        resource_version: str,
    ) -> None:
        """私有包默认只追加新 ID；覆盖公有 ID 必须放在 pig_overrides.json。"""
        private_pigs = self._read_pig_json(resource_dir / "pig.json")
        private_rules = self._normalize_rules(
            self._read_rules_json(resource_dir / "pig_rules.json")
        )
        pig_overrides = self._read_pig_overrides_json(
            resource_dir / "pig_overrides.json"
        )
        self._validate_pig_list(private_pigs)

        base_ids = set(self.pig_map)
        duplicate_ids = [
            str(item["id"])
            for item in private_pigs
            if str(item["id"]) in base_ids
        ]
        if duplicate_ids:
            raise ValueError(
                "私有 pig.json 不能重复公有 ID，请改用 pig_overrides.json: "
                + ", ".join(duplicate_ids[:10])
            )

        merged_map = {pig_id: dict(item) for pig_id, item in self.pig_map.items()}
        overridden_ids: set[str] = set()
        for override in pig_overrides:
            pig_id = str(override["id"])
            if pig_id not in merged_map:
                raise ValueError(f"pig_overrides 指向不存在的公有 ID: {pig_id}")
            updated_item = dict(merged_map[pig_id])
            updated_item.update(
                {key: value for key, value in override.items() if key != "id"}
            )
            updated_item["id"] = pig_id
            merged_map[pig_id] = updated_item
            overridden_ids.add(pig_id)

        merged_pigs = [merged_map[str(item["id"])] for item in self.pig_list]
        merged_pigs.extend(private_pigs)
        self._validate_pig_list(merged_pigs)
        self.pig_list = merged_pigs
        self.pig_map = {str(item["id"]): item for item in merged_pigs}
        self.rules = self._merge_rules(private_rules, self.rules)
        self.image_dirs = [resource_dir / "images", *self.image_dirs]
        self.variant_blocked_pig_ids.update(overridden_ids)
        self.resource_version = (
            f"{self.resource_version}+{resource_version or 'private'}"
        )
        logger.info(
            "rollpig 私有资源已叠加: "
            f"version={resource_version}, private_pigs={len(private_pigs)}"
        )

    def get_pig_by_id(self, pig_id: str | None) -> dict[str, Any] | None:
        if not pig_id:
            return None
        return self.pig_map.get(str(pig_id))

    def find_image_file(self, pig_id: str) -> Path | None:
        for image_dir in self.image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    def resolve_pig_appearance(
        self,
        pig_data: Mapping[str, Any],
        ex_level: int,
    ) -> ResolvedPigAppearance:
        resolved_data = dict(pig_data)
        pig_id = str(resolved_data.get("id") or "")
        requested_level = min(
            max(int(ex_level or 0), 0),
            RESOURCE_MAX_VARIANT_LEVEL,
        )
        base_image_path = self.find_image_file(pig_id) if pig_id else None
        image_path = base_image_path
        applied_level = 0

        if pig_id and pig_id not in self.variant_blocked_pig_ids:
            for candidate_level in range(1, requested_level + 1):
                variant = self.ex_variants.get(pig_id, {}).get(candidate_level)
                if variant is None:
                    continue
                if variant.image_path is not None and (
                    not variant.image_path.is_file() or variant.image_path.is_symlink()
                ):
                    logger.warning(
                        "rollpig EX 差分图片运行时缺失，已跳过该等级: "
                        f"pig_id={pig_id} level={candidate_level} "
                        f"file={variant.image_path}"
                    )
                    continue
                if variant.image_path is not None:
                    image_path = variant.image_path
                if variant.description is not None:
                    resolved_data["description"] = variant.description
                if variant.analysis is not None:
                    resolved_data["analysis"] = variant.analysis
                applied_level = candidate_level

        return ResolvedPigAppearance(
            pig_data=resolved_data,
            image_path=image_path,
            base_image_path=base_image_path,
            requested_level=requested_level,
            applied_level=applied_level,
            resource_version=self.resource_version,
        )

    def available_variant_levels(self, pig_id: str) -> tuple[int, ...]:
        if not pig_id or pig_id in self.variant_blocked_pig_ids:
            return ()
        return tuple(sorted(self.ex_variants.get(pig_id, {})))

    def newly_unlocked_variant_levels(
        self,
        pig_id: str,
        previous_level: int,
        current_level: int,
    ) -> tuple[int, ...]:
        previous = min(max(int(previous_level or 0), 0), RESOURCE_MAX_VARIANT_LEVEL)
        current = min(max(int(current_level or 0), 0), RESOURCE_MAX_VARIANT_LEVEL)
        if current <= previous:
            return ()
        return tuple(
            level
            for level in self.available_variant_levels(pig_id)
            if previous < level <= current
        )

    def variant_change_fields(self, pig_id: str, level: int) -> frozenset[str]:
        if not pig_id or pig_id in self.variant_blocked_pig_ids:
            return frozenset()
        variant = self.ex_variants.get(pig_id, {}).get(int(level or 0))
        if variant is None:
            return frozenset()

        fields: set[str] = set()
        if variant.image_path is not None:
            if not variant.image_path.is_file() or variant.image_path.is_symlink():
                return frozenset()
            fields.add("image")
        if variant.description is not None or variant.analysis is not None:
            fields.add("text")
        return frozenset(fields)

    def get_rule_ids(self, key: str) -> list[str]:
        return list(self.rules.get(key, []))

    async def sync_all(
        self,
        *,
        force: bool = False,
        wait_if_busy: bool = True,
    ) -> tuple[ResourceSyncResult, ResourceSyncResult]:
        """串行同步公有资源与私有 overlay；后台任务忙时可直接跳过。"""
        if not wait_if_busy and self._sync_lock.locked():
            return (
                ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    message="已有资源同步任务运行中",
                ),
                ResourceSyncResult(updated=False, skipped=True, message=""),
            )

        async with self._sync_lock:
            public_result = await self._sync_from_remote_unlocked(force=force)
            try:
                private_result = await self._sync_private_from_remote_unlocked(
                    force=force
                )
            except Exception as error:
                # 私有 overlay 是附加包，同步失败要报告，但不能让公有资源更新失效。
                logger.warning(
                    f"rollpig 私有资源同步失败，继续使用当前私有缓存: {error}"
                )
                private_result = ResourceSyncResult(
                    updated=False,
                    skipped=False,
                    message=f"私有资源同步失败：{error}",
                )
            if public_result.updated or private_result.updated:
                self.reload()
            return public_result, private_result

    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """兼容旧调用：单独同步公有包时也进入同一把锁。"""
        async with self._sync_lock:
            result = await self._sync_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def _sync_from_remote_unlocked(
        self,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        """从静态 manifest 同步资源；同步成功前不替换当前 active 快照。"""
        if not get_resource_sync_enabled() and not force:
            return ResourceSyncResult(
                updated=False,
                skipped=True,
                message="资源同步未启用",
            )

        manifest_url = get_resource_manifest_url()
        if not manifest_url:
            return ResourceSyncResult(
                updated=False,
                skipped=True,
                message="未配置资源 manifest URL",
            )

        timeout = get_resource_sync_timeout()
        max_size = get_resource_max_file_size()
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=get_proxy(),
        ) as client:
            manifest = await self._download_json(
                client,
                manifest_url,
                max_size=max_size,
            )
            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")
            if (
                not force
                and resource_version == self._read_state_version()
                and self._active_variant_assets_complete(manifest)
            ):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message="资源已是最新版本",
                )

            staging_dir = CACHE_ROOT / f".incoming_{int(time.time())}"
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)
            try:
                image_report = await self._download_manifest_files(
                    client,
                    manifest_url=manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_size,
                )
                self._merge_with_existing_snapshot(staging_dir)
                self._activate_staging_dir(
                    staging_dir,
                    resource_version,
                    image_report=image_report,
                )
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=self._build_sync_message(
                "资源",
                resource_version,
                image_report,
            ),
        )

    async def sync_private_from_remote(
        self,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        """兼容旧调用：单独同步私有包时也进入同一把锁。"""
        async with self._sync_lock:
            result = await self._sync_private_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def _sync_private_from_remote_unlocked(
        self,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        """依次同步所有 overlay；单包失败不阻断后续资源包。"""
        if not get_resource_sync_enabled() and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="")
        sources = _resource_overlay_sources()
        if not sources:
            return ResourceSyncResult(updated=False, skipped=True, message="")

        results: list[ResourceSyncResult] = []
        for source in sources:
            try:
                results.append(
                    await self._sync_overlay_source_unlocked(source, force=force)
                )
            except Exception as error:
                logger.warning(
                    "rollpig 资源包同步失败，继续使用当前缓存: "
                    f"pack={source.name}, error={error}"
                )
                results.append(
                    ResourceSyncResult(
                        updated=False,
                        skipped=False,
                        message=f"{source.name} 同步失败：{error}",
                    )
                )
        return ResourceSyncResult(
            updated=any(item.updated for item in results),
            skipped=all(item.skipped for item in results),
            resource_version="+".join(
                item.resource_version for item in results if item.resource_version
            ),
            message="\n".join(item.message for item in results if item.message),
        )

    async def _sync_overlay_source_unlocked(
        self,
        source: ResourceOverlaySource,
        *,
        force: bool,
    ) -> ResourceSyncResult:
        """同步单个 overlay 到自己的原子缓存目录。"""

        timeout = get_resource_sync_timeout()
        max_size = get_resource_max_file_size()
        headers: dict[str, str] = {}
        if source.token:
            headers["Authorization"] = f"Bearer {source.token}"

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=get_proxy(),
            headers=headers,
        ) as client:
            manifest = await self._download_json(
                client,
                source.manifest_url,
                max_size=max_size,
            )
            if manifest.get("overlay") is not True:
                raise ValueError("私有资源 manifest 必须标记 overlay=true")
            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("私有资源 manifest 缺少 resource_version")
            if (
                not force
                and resource_version == self._read_private_state_version(source)
            ):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"{source.name} 已是最新版本",
                )

            staging_dir = source.active_dir.parent / f".incoming_{int(time.time())}"
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)
            try:
                image_report = await self._download_private_manifest_files(
                    client,
                    manifest_url=source.manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_size,
                )
                self._validate_private_staging_dir(staging_dir)
                self._activate_private_staging_dir(
                    staging_dir,
                    resource_version,
                    source=source,
                    image_report=image_report,
                )
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=self._build_sync_message(
                source.name,
                resource_version,
                image_report,
            ),
        )

    async def _download_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> ImageSyncReport:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("manifest 缺少 pig_json")
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=max_size,
        )

        optional_files = manifest.get("optional_files") or {}
        if not isinstance(optional_files, dict):
            raise ValueError("manifest optional_files 必须是 object")
        rules_meta = (
            optional_files.get("pig_rules")
        )
        if isinstance(rules_meta, dict):
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=rules_meta,
                target=staging_dir / "pig_rules.json",
                max_size=max_size,
            )

        pig_list = self._read_pig_json(staging_dir / "pig.json")
        pig_ids = {str(item["id"]) for item in pig_list}
        variant_specs: dict[tuple[str, int], _PigExVariantSpec] = {}
        variants_meta = optional_files.get("pig_ex_variants")
        if "pig_ex_variants" in optional_files:
            if not isinstance(variants_meta, dict):
                raise ValueError("optional_files.pig_ex_variants 必须是 object")
            self._validate_required_manifest_meta(
                variants_meta,
                label="optional_files.pig_ex_variants",
                expected_path="pig_ex_variants.json",
            )
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=variants_meta,
                target=staging_dir / "pig_ex_variants.json",
                max_size=min(max_size, RESOURCE_EX_VARIANTS_JSON_MAX_SIZE),
            )
            variant_specs = self._read_ex_variant_specs(
                staging_dir / "pig_ex_variants.json",
                pig_ids=pig_ids,
                strict=True,
            )

        image_items = manifest.get("images") or []
        if not isinstance(image_items, list):
            raise ValueError("manifest images 必须是 list")
        image_report = ImageSyncReport(accepted_mismatches=[], skipped=[])
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("manifest images 存在非法条目")
            filename = str(image_meta.get("filename") or image_meta.get("path") or "")
            image_filename = Path(filename).name
            try:
                self._validate_image_filename(image_filename)
            except ValueError as error:
                logger.warning(
                    "rollpig 图片文件名非法，已跳过: "
                    f"{filename or '<empty>'} error={error}"
                )
                image_report.skipped.append(filename or "<empty>")
                continue
            image_report.extend(
                await self._download_image_by_meta(
                    client,
                    manifest_url=manifest_url,
                    meta=image_meta,
                    target=staging_dir / "images" / image_filename,
                    max_size=max_size,
                )
            )

        image_variant_specs = {
            key: spec.filename
            for key, spec in variant_specs.items()
            if spec.filename is not None
        }
        raw_variant_items = manifest.get("variant_images", [])
        if not isinstance(raw_variant_items, list):
            raise ValueError("manifest variant_images 必须是 list")
        if len(raw_variant_items) > RESOURCE_MAX_VARIANT_IMAGES:
            raise ValueError(
                "manifest variant_images 数量超过上限: "
                f"{len(raw_variant_items)}/{RESOURCE_MAX_VARIANT_IMAGES}"
            )
        if raw_variant_items and not isinstance(variants_meta, dict):
            raise ValueError(
                "manifest 声明了 variant_images，但缺少 pig_ex_variants.json"
            )

        seen_variant_keys: set[tuple[str, int]] = set()
        for index, image_meta in enumerate(raw_variant_items):
            if not isinstance(image_meta, dict):
                raise ValueError(f"manifest variant_images[{index}] 必须是 object")
            unknown_fields = sorted(set(image_meta) - VARIANT_IMAGE_MANIFEST_FIELDS)
            if unknown_fields:
                raise ValueError(
                    f"manifest variant_images[{index}] 含未支持字段: "
                    + ", ".join(unknown_fields)
                )
            pig_id = str(image_meta.get("pig_id") or "")
            raw_level = image_meta.get("level")
            if isinstance(raw_level, bool) or not isinstance(raw_level, int):
                raise ValueError(f"manifest variant_images[{index}].level 必须是整数")
            level = int(raw_level)
            filename = str(image_meta.get("filename") or "")
            self._validate_variant_image_filename(
                filename,
                pig_id=pig_id,
                level=level,
            )
            self._validate_required_manifest_meta(
                image_meta,
                label=f"variant_images[{index}]",
                expected_path=f"images/{filename}",
            )
            key = (pig_id, level)
            if key in seen_variant_keys:
                raise ValueError(f"manifest 重复声明差分图片: {pig_id}/EX{level}")
            seen_variant_keys.add(key)
            if image_variant_specs.get(key) != filename:
                raise ValueError(f"manifest 差分图片未与 JSON 对应: {pig_id}/EX{level}")
            target = staging_dir / "images" / filename
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=target,
                max_size=max_size,
            )
            self._validate_image_content(target.read_bytes(), target.suffix)

        missing_variant_images = sorted(set(image_variant_specs) - seen_variant_keys)
        if missing_variant_images:
            pig_id, level = missing_variant_images[0]
            raise ValueError(f"差分 JSON 引用的图片未写入 manifest: {pig_id}/EX{level}")
        return image_report

    async def _download_private_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> ImageSyncReport:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("私有资源 manifest 缺少 pig_json")
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=max_size,
        )

        optional_files = manifest.get("optional_files") or {}
        if not isinstance(optional_files, dict):
            raise ValueError("私有资源 optional_files 必须是 object")
        if optional_files.get("pig_ex_variants") is not None:
            raise ValueError("当前版本暂不支持私有 Overlay 提供 EX 等级差分")
        private_variant_images = manifest.get("variant_images")
        if private_variant_images is not None:
            if not isinstance(private_variant_images, list):
                raise ValueError("私有资源 variant_images 必须是 list 或 null")
            if private_variant_images:
                raise ValueError("当前版本暂不支持私有 Overlay 提供 EX 等级差分")
        for key, filename in (
            ("pig_rules", "pig_rules.json"),
            ("pig_overrides", "pig_overrides.json"),
        ):
            file_meta = optional_files.get(key)
            if not isinstance(file_meta, dict):
                continue
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=file_meta,
                target=staging_dir / filename,
                max_size=max_size,
            )

        image_items = manifest.get("images") or []
        if not isinstance(image_items, list):
            raise ValueError("私有资源 manifest images 必须是 list")
        image_report = ImageSyncReport(accepted_mismatches=[], skipped=[])
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("私有资源 images 存在非法条目")
            filename = str(image_meta.get("filename") or image_meta.get("path") or "")
            image_filename = Path(filename).name
            try:
                self._validate_image_filename(image_filename)
            except ValueError as error:
                logger.warning(
                    "rollpig 私有图片文件名非法，已跳过: "
                    f"{filename or '<empty>'} error={error}"
                )
                image_report.skipped.append(filename or "<empty>")
                continue
            image_report.extend(
                await self._download_image_by_meta(
                    client,
                    manifest_url=manifest_url,
                    meta=image_meta,
                    target=staging_dir / "images" / image_filename,
                    max_size=max_size,
                )
            )
        return image_report

    def _validate_private_staging_dir(self, staging_dir: Path) -> None:
        private_pigs = self._read_pig_json(staging_dir / "pig.json")
        self._normalize_rules(self._read_rules_json(staging_dir / "pig_rules.json"))
        self._validate_pig_list(private_pigs)
        self._read_pig_overrides_json(staging_dir / "pig_overrides.json")

    def _merge_with_existing_snapshot(self, staging_dir: Path) -> None:
        """云端缺失旧 ID 时继续保留旧资源，避免本地账本历史引用失效。"""
        new_pigs = self._read_pig_json(staging_dir / "pig.json")
        new_rules = self._normalize_rules(
            self._read_rules_json(staging_dir / "pig_rules.json")
        )
        self._validate_pig_list(new_pigs)

        base_pigs, base_rules, base_image_dirs = self._read_current_snapshot()
        merged_pigs = self._merge_pig_lists(new_pigs, base_pigs)
        merged_rules = self._merge_rules(new_rules, base_rules)
        self._copy_legacy_images(
            staging_dir / "images",
            new_pigs=new_pigs,
            merged_pigs=merged_pigs,
            base_image_dirs=base_image_dirs,
        )
        self._preserve_legacy_variants(
            staging_dir,
            new_pig_ids={str(item["id"]) for item in new_pigs},
            merged_pig_ids={str(item["id"]) for item in merged_pigs},
        )

        (staging_dir / "pig.json").write_text(
            json.dumps(merged_pigs, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        (staging_dir / "pig_rules.json").write_text(
            json.dumps(merged_rules, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_current_snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[Path]]:
        if (ACTIVE_RESOURCE_DIR / "pig.json").exists():
            try:
                pigs = self._read_pig_json(ACTIVE_RESOURCE_DIR / "pig.json")
                rules = self._normalize_rules(
                    self._read_rules_json(ACTIVE_RESOURCE_DIR / "pig_rules.json")
                )
                return pigs, rules, [ACTIVE_IMAGE_DIR, BUILTIN_IMAGE_DIR]
            except Exception as error:
                logger.warning(f"rollpig 旧缓存读取失败，合并时仅使用内置资源: {error}")

        pigs = self._read_pig_json(BUILTIN_PIG_JSON)
        rules = self._normalize_rules(self._read_rules_json(BUILTIN_RULES_JSON))
        return pigs, rules, [BUILTIN_IMAGE_DIR]

    def _merge_pig_lists(
        self,
        new_pigs: list[dict[str, Any]],
        base_pigs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged_map = {str(item["id"]): dict(item) for item in base_pigs}
        for item in new_pigs:
            merged_map[str(item["id"])] = dict(item)

        ordered_ids: list[str] = []
        for item in new_pigs:
            ordered_ids.append(str(item["id"]))
        for item in base_pigs:
            pig_id = str(item["id"])
            if pig_id not in ordered_ids:
                ordered_ids.append(pig_id)
        return [merged_map[pig_id] for pig_id in ordered_ids]

    def _merge_rules(
        self,
        new_rules: dict[str, list[str]],
        base_rules: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        for key in RULE_KEYS:
            merged[key] = list(
                dict.fromkeys([*base_rules.get(key, []), *new_rules.get(key, [])])
            )
        return merged

    def _copy_legacy_images(
        self,
        target_image_dir: Path,
        *,
        new_pigs: list[dict[str, Any]],
        merged_pigs: list[dict[str, Any]],
        base_image_dirs: list[Path],
    ) -> None:
        new_ids = {str(item["id"]) for item in new_pigs}
        for item in merged_pigs:
            pig_id = str(item["id"])
            if pig_id in new_ids:
                continue
            source = self._find_image_in_dirs(pig_id, base_image_dirs)
            if not source:
                continue
            target = target_image_dir / source.name
            if not target.exists():
                shutil.copy2(source, target)

    def _preserve_legacy_variants(
        self,
        staging_dir: Path,
        *,
        new_pig_ids: set[str],
        merged_pig_ids: set[str],
    ) -> None:
        legacy_pig_ids = merged_pig_ids - new_pig_ids
        if not legacy_pig_ids:
            return

        sources = [
            (
                BUILTIN_EX_VARIANTS_JSON,
                BUILTIN_IMAGE_DIR,
            ),
            (
                ACTIVE_RESOURCE_DIR / LEGACY_EX_VARIANTS_FILENAME,
                ACTIVE_IMAGE_DIR,
            ),
            (
                ACTIVE_RESOURCE_DIR / "pig_ex_variants.json",
                ACTIVE_IMAGE_DIR,
            ),
        ]
        preserved: dict[tuple[str, int], _PigExVariantSpec] = {}
        source_dirs: dict[tuple[str, int], Path] = {}
        for variants_path, image_dir in sources:
            specs = self._read_ex_variant_specs(
                variants_path,
                pig_ids=merged_pig_ids,
                strict=False,
            )
            for key, spec in specs.items():
                if spec.pig_id not in legacy_pig_ids:
                    continue
                preserved[key] = spec
                source_dirs[key] = image_dir

        if not preserved:
            return

        payload: dict[str, Any] = {"schema_version": 1, "pigs": {}}
        for key, spec in sorted(preserved.items()):
            level_data: dict[str, str] = {}
            if spec.filename is not None:
                source = source_dirs[key] / spec.filename
                if not source.is_file() or source.is_symlink():
                    continue
                target = staging_dir / "images" / spec.filename
                if not target.exists():
                    shutil.copy2(source, target)
                level_data["image"] = spec.filename
            if spec.description is not None:
                level_data["description"] = spec.description
            if spec.analysis is not None:
                level_data["analysis"] = spec.analysis
            if not level_data:
                continue
            pig_entry = payload["pigs"].setdefault(spec.pig_id, {"levels": {}})
            pig_entry["levels"][str(spec.level)] = level_data

        if payload["pigs"]:
            (staging_dir / LEGACY_EX_VARIANTS_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _find_image_in_dirs(self, pig_id: str, image_dirs: list[Path]) -> Path | None:
        for image_dir in image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    async def _download_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        max_size: int,
    ) -> dict[str, Any]:
        content = await self._download_bytes(client, url, max_size=max_size)
        data = json.loads(content.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON object")
        return data

    async def _download_file_by_meta(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
    ) -> None:
        path = str(meta.get("path") or meta.get("filename") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        content = await self._download_bytes(
            client,
            urljoin(manifest_url, path),
            max_size=max_size,
        )

        expected_size = meta.get("size")
        if expected_size is not None and int(expected_size) != len(content):
            raise ValueError(f"文件大小校验失败: {path}")

        expected_hash = str(meta.get("sha256") or "").lower()
        actual_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(f"sha256 校验失败: {path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    async def _download_image_by_meta(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
    ) -> ImageSyncReport:
        path = str(meta.get("path") or meta.get("filename") or "").strip()
        if not path:
            raise ValueError("manifest 图片条目缺少 path")

        try:
            content = await self._download_bytes(
                client,
                urljoin(manifest_url, path),
                max_size=max_size,
            )
            mismatch_reasons = self._get_file_mismatch_reasons(
                meta,
                content,
            )
            if mismatch_reasons:
                # 远端 manifest 可能落后于图片文件；图片能解码时保留可用性，
                # 但仍记录失配，避免把资源源头问题悄悄吃掉。
                self._validate_image_content(content, target.suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                logger.warning(
                    "rollpig 图片校验失配但可解码，已接受: "
                    f"{path} ({'; '.join(mismatch_reasons)})"
                )
                return ImageSyncReport(accepted_mismatches=[path], skipped=[])

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return ImageSyncReport(accepted_mismatches=[], skipped=[])
        except Exception as error:
            logger.warning(f"rollpig 图片下载失败，已跳过: {path} error={error}")
            return ImageSyncReport(accepted_mismatches=[], skipped=[path])

    def _get_file_mismatch_reasons(
        self,
        meta: dict[str, Any],
        content: bytes,
    ) -> list[str]:
        reasons: list[str] = []
        expected_size = meta.get("size")
        if expected_size is not None:
            try:
                size = int(expected_size)
            except (TypeError, ValueError):
                reasons.append(f"size 非法: {expected_size}")
            else:
                if size != len(content):
                    reasons.append(f"size {size}!={len(content)}")

        expected_hash = str(meta.get("sha256") or "").lower()
        actual_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            reasons.append("sha256 不一致")
        return reasons

    def _validate_image_content(self, content: bytes, suffix: str) -> None:
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
                image_format = str(image.format or "").upper()
        except (OSError, UnidentifiedImageError) as error:
            raise ValueError("图片内容无法解码") from error

        allowed_suffixes = IMAGE_FORMAT_SUFFIXES.get(image_format)
        if not allowed_suffixes or suffix.lower() not in allowed_suffixes:
            raise ValueError(
                f"图片格式与文件扩展名不匹配: format={image_format}, suffix={suffix}"
            )

    async def _download_bytes(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        max_size: int,
    ) -> bytes:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > max_size:
            raise ValueError(f"文件超过大小上限: {url}")
        return content

    def _activate_staging_dir(
        self,
        staging_dir: Path,
        resource_version: str,
        *,
        image_report: ImageSyncReport | None = None,
    ) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        if PREVIOUS_RESOURCE_DIR.exists():
            shutil.rmtree(PREVIOUS_RESOURCE_DIR)
        if ACTIVE_RESOURCE_DIR.exists():
            ACTIVE_RESOURCE_DIR.rename(PREVIOUS_RESOURCE_DIR)
        staging_dir.rename(ACTIVE_RESOURCE_DIR)
        image_report = image_report or ImageSyncReport([], [])
        STATE_FILE.write_text(
            json.dumps(
                {
                    "resource_version": resource_version,
                    "synced_at": int(time.time()),
                    "partial": bool(image_report.skipped),
                    "accepted_image_mismatches": image_report.accepted_mismatches,
                    "skipped_images": image_report.skipped,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _read_state_version(self) -> str:
        try:
            state = json.loads(self._read_json_text(STATE_FILE))
            if state.get("partial") is True:
                return "cache"
            return str(state.get("resource_version") or "cache")
        except Exception:
            return "cache"

    def _active_variant_assets_complete(self, manifest: Mapping[str, Any]) -> bool:
        optional_files = manifest.get("optional_files") or {}
        raw_variant_items = manifest.get("variant_images", [])
        if not isinstance(optional_files, dict) or not isinstance(
            raw_variant_items, list
        ):
            return False
        variants_meta = optional_files.get("pig_ex_variants")
        if variants_meta is None and not raw_variant_items:
            return True
        if not isinstance(variants_meta, dict):
            return False
        try:
            self._validate_required_manifest_meta(
                variants_meta,
                label="optional_files.pig_ex_variants",
                expected_path="pig_ex_variants.json",
            )
            if not self._local_file_matches_meta(
                ACTIVE_RESOURCE_DIR / "pig_ex_variants.json",
                variants_meta,
            ):
                return False
            pig_ids = {
                str(item["id"])
                for item in self._read_pig_json(ACTIVE_RESOURCE_DIR / "pig.json")
            }
            specs = self._read_ex_variant_specs(
                ACTIVE_RESOURCE_DIR / "pig_ex_variants.json",
                pig_ids=pig_ids,
                strict=True,
            )
            expected_images = {
                key: spec.filename
                for key, spec in specs.items()
                if spec.filename is not None
            }
            seen_keys: set[tuple[str, int]] = set()
            for index, image_meta in enumerate(raw_variant_items):
                if not isinstance(image_meta, dict):
                    return False
                pig_id = str(image_meta.get("pig_id") or "")
                level = image_meta.get("level")
                if isinstance(level, bool) or not isinstance(level, int):
                    return False
                filename = str(image_meta.get("filename") or "")
                self._validate_variant_image_filename(
                    filename,
                    pig_id=pig_id,
                    level=level,
                )
                self._validate_required_manifest_meta(
                    image_meta,
                    label=f"variant_images[{index}]",
                    expected_path=f"images/{filename}",
                )
                key = (pig_id, level)
                if key in seen_keys or expected_images.get(key) != filename:
                    return False
                seen_keys.add(key)
                if not self._local_file_matches_meta(
                    ACTIVE_IMAGE_DIR / filename,
                    image_meta,
                ):
                    return False
            return set(expected_images) == seen_keys
        except (OSError, TypeError, ValueError):
            return False

    def _local_file_matches_meta(
        self,
        path: Path,
        meta: Mapping[str, Any],
    ) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        expected_size = meta.get("size")
        expected_hash = meta.get("sha256")
        if expected_size is not None and path.stat().st_size != int(expected_size):
            return False
        if expected_hash:
            return _hash_file(path) == str(expected_hash).lower()
        return True

    def _read_private_state_version(
        self,
        source: ResourceOverlaySource | None = None,
    ) -> str:
        state_file = source.state_file if source else PRIVATE_STATE_FILE
        try:
            state = json.loads(self._read_json_text(state_file))
            if state.get("partial") is True:
                return "private"
            return str(state.get("resource_version") or "private")
        except Exception:
            return "private"

    def _activate_private_staging_dir(
        self,
        staging_dir: Path,
        resource_version: str,
        *,
        source: ResourceOverlaySource | None = None,
        image_report: ImageSyncReport | None = None,
    ) -> None:
        active_dir = source.active_dir if source else PRIVATE_RESOURCE_DIR
        previous_dir = source.previous_dir if source else PRIVATE_PREVIOUS_RESOURCE_DIR
        state_file = source.state_file if source else PRIVATE_STATE_FILE
        active_dir.parent.mkdir(parents=True, exist_ok=True)
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
        if active_dir.exists():
            active_dir.rename(previous_dir)
        staging_dir.rename(active_dir)
        image_report = image_report or ImageSyncReport([], [])
        state_file.write_text(
            json.dumps(
                {
                    "resource_version": resource_version,
                    "synced_at": int(time.time()),
                    "partial": bool(image_report.skipped),
                    "accepted_image_mismatches": image_report.accepted_mismatches,
                    "skipped_images": image_report.skipped,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _read_json_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    def _read_pig_json(self, path: Path) -> list[dict[str, Any]]:
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        return data

    def _read_rules_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, dict):
            raise ValueError(f"pig_rules.json 必须是 object: {path}")
        return data

    def _read_pig_overrides_json(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig_overrides.json 必须是 list: {path}")

        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig_overrides.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"pig_overrides.json 存在非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"pig_overrides.json 存在重复 pig_id: {pig_id}")
            seen_ids.add(pig_id)
        return data

    def _report_variant_problem(
        self,
        path: Path,
        message: str,
        *,
        strict: bool,
    ) -> None:
        if strict:
            raise ValueError(message)
        logger.warning(f"rollpig EX 差分已忽略: file={path} error={message}")

    def _read_ex_variant_specs(
        self,
        path: Path,
        *,
        pig_ids: set[str],
        strict: bool,
    ) -> dict[tuple[str, int], _PigExVariantSpec]:
        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise ValueError("pig_ex_variants.json 不能是符号链接")
            if path.stat().st_size > RESOURCE_EX_VARIANTS_JSON_MAX_SIZE:
                raise ValueError("pig_ex_variants.json 超过大小上限")
            data = json.loads(self._read_json_text(path))
        except Exception as error:
            self._report_variant_problem(path, str(error), strict=strict)
            return {}

        if not isinstance(data, dict) or data.get("schema_version") != 1:
            self._report_variant_problem(
                path,
                "pig_ex_variants.json 必须是 schema_version=1 的 object",
                strict=strict,
            )
            return {}
        unknown_root_fields = sorted(set(data) - {"schema_version", "pigs"})
        if unknown_root_fields:
            self._report_variant_problem(
                path,
                "pig_ex_variants.json 含未支持字段: "
                + ", ".join(unknown_root_fields),
                strict=strict,
            )
            if not strict:
                return {}
        raw_pigs = data.get("pigs")
        if not isinstance(raw_pigs, dict):
            self._report_variant_problem(
                path,
                "pig_ex_variants.pigs 必须是 object",
                strict=strict,
            )
            return {}

        specs: dict[tuple[str, int], _PigExVariantSpec] = {}
        for raw_pig_id, raw_pig in raw_pigs.items():
            pig_id = str(raw_pig_id)
            if not PIG_ID_PATTERN.fullmatch(pig_id) or pig_id not in pig_ids:
                self._report_variant_problem(
                    path,
                    f"差分指向不存在或非法的小猪 ID: {pig_id}",
                    strict=strict,
                )
                continue
            if not isinstance(raw_pig, dict) or set(raw_pig) != {"levels"}:
                self._report_variant_problem(
                    path,
                    f"差分猪条目只能包含 levels: {pig_id}",
                    strict=strict,
                )
                continue
            raw_levels = raw_pig.get("levels")
            if not isinstance(raw_levels, dict) or not raw_levels:
                self._report_variant_problem(
                    path,
                    f"差分猪 {pig_id}.levels 必须是非空 object",
                    strict=strict,
                )
                continue
            for raw_level, raw_variant in raw_levels.items():
                if (
                    not isinstance(raw_level, str)
                    or not raw_level.isdigit()
                    or not 1 <= int(raw_level) <= RESOURCE_MAX_VARIANT_LEVEL
                ):
                    self._report_variant_problem(
                        path,
                        f"差分等级非法: {pig_id}/{raw_level}",
                        strict=strict,
                    )
                    continue
                level = int(raw_level)
                if not isinstance(raw_variant, dict):
                    self._report_variant_problem(
                        path,
                        f"差分等级条目必须是 object: {pig_id}/EX{level}",
                        strict=strict,
                    )
                    continue
                unknown_fields = sorted(
                    set(raw_variant) - {"image", "description", "analysis"}
                )
                if unknown_fields or not raw_variant:
                    self._report_variant_problem(
                        path,
                        f"差分 {pig_id}/EX{level} 字段非法",
                        strict=strict,
                    )
                    continue

                filename: str | None = None
                if "image" in raw_variant:
                    raw_filename = raw_variant.get("image")
                    try:
                        self._validate_variant_image_filename(
                            raw_filename,
                            pig_id=pig_id,
                            level=level,
                        )
                    except ValueError as error:
                        self._report_variant_problem(path, str(error), strict=strict)
                        continue
                    filename = str(raw_filename)

                text_values: dict[str, str | None] = {}
                invalid_text = False
                for field in ("description", "analysis"):
                    value = raw_variant.get(field)
                    if field in raw_variant and (
                        not isinstance(value, str) or not value.strip()
                    ):
                        self._report_variant_problem(
                            path,
                            f"差分 {pig_id}/EX{level}.{field} 必须是非空字符串",
                            strict=strict,
                        )
                        invalid_text = True
                        break
                    text_values[field] = value if isinstance(value, str) else None
                if invalid_text:
                    continue
                specs[(pig_id, level)] = _PigExVariantSpec(
                    pig_id=pig_id,
                    level=level,
                    filename=filename,
                    description=text_values["description"],
                    analysis=text_values["analysis"],
                )
                if len(specs) > RESOURCE_MAX_EX_VARIANTS:
                    self._report_variant_problem(
                        path,
                        "差分条目数量超过上限: "
                        f"{len(specs)}/{RESOURCE_MAX_EX_VARIANTS}",
                        strict=strict,
                    )
                    return {}
        return specs

    def _load_ex_variants(
        self,
        path: Path,
        *,
        pig_ids: set[str],
        image_dir: Path,
        strict: bool,
    ) -> dict[str, dict[int, PigExVariant]]:
        specs = self._read_ex_variant_specs(path, pig_ids=pig_ids, strict=strict)
        variants: dict[str, dict[int, PigExVariant]] = {}
        for spec in specs.values():
            image_path = image_dir / spec.filename if spec.filename else None
            if image_path is not None and (
                not image_path.is_file() or image_path.is_symlink()
            ):
                self._report_variant_problem(
                    path,
                    f"差分图片不存在或非法: {spec.filename}",
                    strict=strict,
                )
                continue
            variants.setdefault(spec.pig_id, {})[spec.level] = PigExVariant(
                pig_id=spec.pig_id,
                level=spec.level,
                image_path=image_path,
                description=spec.description,
                analysis=spec.analysis,
            )
        return variants

    def _normalize_rules(self, rules: dict[str, Any]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for key in RULE_KEYS:
            values = rules.get(key) or []
            if not isinstance(values, list):
                raise ValueError(f"pig_rules.{key} 必须是 list")
            normalized_values: list[str] = []
            for value in values:
                pig_id = str(value)
                if not pig_id:
                    continue
                if not PIG_ID_PATTERN.match(pig_id):
                    raise ValueError(f"pig_rules.{key} 存在非法 pig_id: {pig_id}")
                normalized_values.append(pig_id)
            normalized[key] = normalized_values
        return normalized

    def _validate_pig_list(self, pig_list: list[dict[str, Any]]) -> None:
        seen_ids: set[str] = set()
        for item in pig_list:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            if not item.get("name"):
                raise ValueError(f"pig 缺少 name: {pig_id}")
            seen_ids.add(pig_id)

    def _validate_image_filename(self, filename: str) -> None:
        path = Path(filename)
        if path.name != filename:
            raise ValueError(f"图片文件名不能包含路径: {filename}")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"不支持的图片格式: {filename}")
        if not PIG_ID_PATTERN.match(path.stem):
            raise ValueError(f"图片文件名非法: {filename}")

    def _validate_variant_image_filename(
        self,
        filename: Any,
        *,
        pig_id: str,
        level: int,
    ) -> None:
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"差分 {pig_id}/EX{level} 缺少 image")
        if (
            not PIG_ID_PATTERN.fullmatch(pig_id)
            or not 1 <= level <= RESOURCE_MAX_VARIANT_LEVEL
        ):
            raise ValueError(f"差分图片的猪 ID 或等级非法: {pig_id}/EX{level}")
        path = Path(filename)
        if path.name != filename or "\\" in filename:
            raise ValueError(f"差分图片文件名不能包含路径: {filename}")
        if (
            path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES
            or path.suffix != path.suffix.lower()
        ):
            raise ValueError(f"差分图片格式不受支持: {filename}")
        if path.stem != f"{pig_id}_ex{level}":
            raise ValueError(
                f"差分图片文件名必须为 {pig_id}_ex{level}.png 或 .gif: {filename}"
            )

    def _validate_required_manifest_meta(
        self,
        meta: Mapping[str, Any],
        *,
        label: str,
        expected_path: str,
    ) -> None:
        path = str(meta.get("path") or "")
        if path != expected_path:
            raise ValueError(f"{label}.path 必须为 {expected_path}")
        sha256 = str(meta.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"{label}.sha256 必须是 64 位小写十六进制")
        size = meta.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{label}.size 必须是非负整数")

    def _build_sync_message(
        self,
        label: str,
        resource_version: str,
        image_report: ImageSyncReport,
    ) -> str:
        status = "部分同步完成" if image_report.skipped else "同步完成"
        message = f"{label}{status}：{resource_version}"
        details: list[str] = []
        if image_report.accepted_mismatches:
            details.append(
                "接受校验失配图片 "
                f"{len(image_report.accepted_mismatches)} 张"
                f"（{self._format_path_sample(image_report.accepted_mismatches)}）"
            )
        if image_report.skipped:
            details.append(
                "跳过失败图片 "
                f"{len(image_report.skipped)} 张"
                f"（{self._format_path_sample(image_report.skipped)}）"
            )
        if details:
            message += "；" + "；".join(details)
        return message

    def _format_path_sample(self, paths: list[str]) -> str:
        sample = paths[:3]
        suffix = "..." if len(paths) > len(sample) else ""
        return ", ".join(sample) + suffix


pig_resource_manager = RollPigResourceManager()
pig_resource_manager.reload()
