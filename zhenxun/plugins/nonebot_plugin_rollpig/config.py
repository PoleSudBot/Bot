from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from zhenxun.configs.config import Config as ZhenxunConfig

MODULE_NAME = "nonebot_plugin_rollpig"
DEFAULT_RESOURCE_MANIFEST_URL = (
    "https://pig.felislab.cc/resources/rollpig/manifest.json"
)
DEFAULT_PRIVATE_RESOURCE_MANIFEST_URL = (
    "https://pig.felislab.cc/resources/rollpig-pjsk/manifest.json"
)
DEFAULT_OFFICIAL_GIF_RESOURCE_MANIFEST_URL = (
    "https://pig.felislab.cc/resources/rollpig-gif/manifest.json"
)
RESOURCE_PACK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
base_config = ZhenxunConfig.get(MODULE_NAME)


class GroupSettings(BaseModel):
    """rollpig 分群配置"""

    daily_summary_enabled: bool = Field(
        True,
        description="是否为当前群发送猪圈日报",
    )


class PrivateResourceManifestConfig(BaseModel):
    """远程私有资源包配置。"""

    name: str
    manifest_url: str
    token: Optional[str] = None


class Config(BaseModel):
    """兼容 NoneBot 插件配置导入；运行时有效配置以 zhenxun 配置为准。"""

    AI_ENABLED: bool = False
    LLM_MODEL_NAME: Optional[str] = None
    ROAST_COOLDOWN_HOURS: float = 8.0
    ROAST_CHARGE_MAX: int = 2
    STORAGE_BACKEND: str = "local"
    CLOUD_API_URL: Optional[str] = None
    CLOUD_TOKEN: Optional[str] = None
    CLOUD_TIMEOUT: float = 3.0
    CLOUD_STRICT_MODE: bool = True
    PROXY: Optional[str] = None
    GROWTH_MAX_EXPERT_LEVEL: int = 5
    GROWTH_PITY_WEIGHT_STEP: float = 0.5
    GROWTH_PITY_WEIGHT_CAP: float = 4.0
    RESOURCE_SYNC_ENABLED: bool = True
    RESOURCE_MANIFEST_URL: Optional[str] = DEFAULT_RESOURCE_MANIFEST_URL
    RESOURCE_SYNC_ON_STARTUP: bool = True
    RESOURCE_SYNC_INTERVAL_HOURS: int = 24
    RESOURCE_SYNC_TIMEOUT: float = 10.0
    RESOURCE_MAX_FILE_SIZE: int = 10 * 1024 * 1024
    PRIVATE_RESOURCE_MANIFEST_URL: Optional[str] = DEFAULT_PRIVATE_RESOURCE_MANIFEST_URL
    PRIVATE_RESOURCE_TOKEN: Optional[str] = None
    OFFICIAL_GIF_RESOURCE_ENABLED: bool = True
    OFFICIAL_GIF_RESOURCE_MANIFEST_URL: Optional[str] = (
        DEFAULT_OFFICIAL_GIF_RESOURCE_MANIFEST_URL
    )
    PRIVATE_RESOURCE_MANIFESTS: list[PrivateResourceManifestConfig] = Field(
        default_factory=list
    )
    CATALOG_ENABLED: bool = True
    CATALOG_CACHE_SECONDS: int = 300
    CATALOG_RENDER_TIMEOUT: float = 8.0
    HTML_RENDER_CONCURRENCY: int = 2
    GIF_RENDER_CONCURRENCY: int = 2
    CARD_DISK_CACHE_MAX_MB: int = 64

    # Legacy compatibility only. Runtime no longer uses these fields.
    rollpig_ai_enabled: Optional[bool] = None
    rollpig_deepseek_key: Optional[str] = None
    rollpig_deepseek_base: Optional[str] = None
    rollpig_model: Optional[str] = None
    rollpig_roast_cooldown_hours: Optional[float] = None
    rollpig_roast_charge_max: Optional[int] = None
    rollpig_storage_backend: Optional[str] = None
    rollpig_cloud_api_url: Optional[str] = None
    rollpig_cloud_token: Optional[str] = None
    rollpig_cloud_timeout: Optional[float] = None
    rollpig_cloud_strict_mode: Optional[bool] = None
    rollpig_proxy: Optional[str] = None
    rollpig_growth_max_expert_level: Optional[int] = None
    rollpig_growth_pity_weight_step: Optional[float] = None
    rollpig_growth_pity_weight_cap: Optional[float] = None
    rollpig_resource_sync_enabled: Optional[bool] = None
    rollpig_resource_manifest_url: Optional[str] = None
    rollpig_resource_sync_on_startup: Optional[bool] = None
    rollpig_resource_sync_interval_hours: Optional[int] = None
    rollpig_resource_sync_timeout: Optional[float] = None
    rollpig_resource_max_file_size: Optional[int] = None
    rollpig_private_resource_manifest_url: Optional[str] = None
    rollpig_private_resource_token: Optional[str] = None
    rollpig_official_gif_resource_enabled: Optional[bool] = None
    rollpig_official_gif_resource_manifest_url: Optional[str] = None
    rollpig_private_resource_manifests: Optional[
        list[PrivateResourceManifestConfig]
    ] = None
    rollpig_catalog_enabled: Optional[bool] = None
    rollpig_catalog_cache_seconds: Optional[int] = None
    rollpig_catalog_render_timeout: Optional[float] = None
    rollpig_html_render_concurrency: Optional[int] = None


def _get_str(key: str, default: Optional[str] = None) -> Optional[str]:
    value = base_config.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _get_float(key: str, default: float) -> float:
    raw_value = base_config.get(key, default)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    raw_value = base_config.get(key, default)
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    value = base_config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def get_ai_enabled() -> bool:
    return _get_bool("AI_ENABLED", False)


def get_llm_model_name() -> Optional[str]:
    return _get_str("LLM_MODEL_NAME")


def get_roast_cooldown_hours() -> float:
    return _get_float("ROAST_COOLDOWN_HOURS", 8.0)


def get_roast_charge_max() -> int:
    return _get_int("ROAST_CHARGE_MAX", 2)


def get_storage_backend() -> str:
    return (_get_str("STORAGE_BACKEND", "local") or "local").strip().lower()


def get_cloud_api_url() -> Optional[str]:
    return _get_str("CLOUD_API_URL")


def get_cloud_token() -> Optional[str]:
    return _get_str("CLOUD_TOKEN")


def get_cloud_timeout() -> float:
    return _get_float("CLOUD_TIMEOUT", 3.0)


def get_cloud_strict_mode() -> bool:
    return _get_bool("CLOUD_STRICT_MODE", True)


def get_proxy() -> Optional[str]:
    return _get_str("PROXY")


def get_growth_max_expert_level() -> int:
    return max(0, _get_int("GROWTH_MAX_EXPERT_LEVEL", 5))


def get_growth_pity_weight_step() -> float:
    return max(0.0, _get_float("GROWTH_PITY_WEIGHT_STEP", 0.5))


def get_growth_pity_weight_cap() -> float:
    return max(0.0, _get_float("GROWTH_PITY_WEIGHT_CAP", 4.0))


def get_resource_sync_enabled() -> bool:
    return _get_bool("RESOURCE_SYNC_ENABLED", True)


def get_resource_manifest_url() -> Optional[str]:
    raw_value = base_config.get("RESOURCE_MANIFEST_URL", DEFAULT_RESOURCE_MANIFEST_URL)
    if raw_value is None:
        return DEFAULT_RESOURCE_MANIFEST_URL

    # 公有资源默认走上游全量包；显式配置为空字符串时表示关闭公有同步 URL。
    text = str(raw_value).strip()
    return text or None


def get_resource_sync_on_startup() -> bool:
    return _get_bool("RESOURCE_SYNC_ON_STARTUP", True)


def get_resource_sync_interval_hours() -> int:
    return max(1, _get_int("RESOURCE_SYNC_INTERVAL_HOURS", 24))


def get_resource_sync_timeout() -> float:
    return max(1.0, _get_float("RESOURCE_SYNC_TIMEOUT", 10.0))


def get_resource_max_file_size() -> int:
    return max(1024, _get_int("RESOURCE_MAX_FILE_SIZE", 10 * 1024 * 1024))


def get_private_resource_manifest_url() -> Optional[str]:
    raw_value = base_config.get(
        "PRIVATE_RESOURCE_MANIFEST_URL",
        DEFAULT_PRIVATE_RESOURCE_MANIFEST_URL,
    )
    if raw_value is None:
        return DEFAULT_PRIVATE_RESOURCE_MANIFEST_URL

    # 私有包默认跟随上游 PJSK；显式配置为空字符串时表示关闭 overlay。
    text = str(raw_value).strip()
    return text or None


def get_private_resource_token() -> Optional[str]:
    return _get_str("PRIVATE_RESOURCE_TOKEN")


def get_official_gif_resource_enabled() -> bool:
    return _get_bool("OFFICIAL_GIF_RESOURCE_ENABLED", True)


def get_official_gif_resource_manifest_url() -> Optional[str]:
    raw_value = base_config.get(
        "OFFICIAL_GIF_RESOURCE_MANIFEST_URL",
        DEFAULT_OFFICIAL_GIF_RESOURCE_MANIFEST_URL,
    )
    if raw_value is None:
        return DEFAULT_OFFICIAL_GIF_RESOURCE_MANIFEST_URL
    return str(raw_value).strip() or None


def get_private_resource_manifests() -> list[PrivateResourceManifestConfig]:
    """按配置顺序返回合法且名称唯一的私有资源包。"""

    raw_items = base_config.get("PRIVATE_RESOURCE_MANIFESTS", []) or []
    if not isinstance(raw_items, list):
        return []

    manifests: list[PrivateResourceManifestConfig] = []
    names: set[str] = set()
    for raw_item in raw_items:
        try:
            item = PrivateResourceManifestConfig.model_validate(raw_item)
        except (TypeError, ValueError):
            continue
        item.name = item.name.strip()
        item.manifest_url = item.manifest_url.strip()
        item.token = item.token.strip() if item.token else None
        if (
            not RESOURCE_PACK_NAME_PATTERN.fullmatch(item.name)
            or not item.manifest_url
            or item.name in names
        ):
            continue
        names.add(item.name)
        manifests.append(item)
    return manifests


def get_catalog_enabled() -> bool:
    return _get_bool("CATALOG_ENABLED", True)


def get_catalog_cache_seconds() -> int:
    return max(0, _get_int("CATALOG_CACHE_SECONDS", 300))


def get_catalog_render_timeout() -> float:
    return max(1.0, _get_float("CATALOG_RENDER_TIMEOUT", 8.0))


def get_html_render_concurrency() -> int:
    return max(1, min(6, _get_int("HTML_RENDER_CONCURRENCY", 2)))


def get_gif_render_concurrency() -> int:
    return max(1, min(2, _get_int("GIF_RENDER_CONCURRENCY", 2)))


def get_card_disk_cache_max_mb() -> int:
    return max(0, min(64, _get_int("CARD_DISK_CACHE_MAX_MB", 64)))


def get_card_disk_cache_max_bytes() -> int:
    return get_card_disk_cache_max_mb() * 1024 * 1024
