from __future__ import annotations

from functools import lru_cache
import os
import sys
from typing import Any, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import LEGACY_MODULE_NAME, MODULE_NAME, SERVERS

_TEST_MODE = bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


class _CompatConfig:
    @staticmethod
    def get_config(_module: str, _key: str, default: Any = None, **_kwargs: Any) -> Any:
        return default


if not _TEST_MODE:
    try:
        from zhenxun.configs.config import Config
    except Exception:
        Config = _CompatConfig()  # type: ignore[assignment]
else:
    Config = _CompatConfig()  # type: ignore[assignment]

if not _TEST_MODE:
    try:
        from zhenxun.configs.utils.models import RegisterConfig
    except Exception:

        class RegisterConfig(BaseModel):
            key: str
            value: Any
            module: str | None = None
            help: str | None = None
            default_value: Any | None = None
            type: object = None
            arg_parser: Any = None
else:

    class RegisterConfig(BaseModel):
        key: str
        value: Any
        module: str | None = None
        help: str | None = None
        default_value: Any | None = None
        type: object = None
        arg_parser: Any = None


MASTER_DATASET_KEYS = (
    "events",
    "virtualLives",
    "gachas",
    "cards",
    "gameCharacters",
    "honors",
    "honorGroups",
    "stamps",
    "musics",
    "musicDifficulties",
)

DEFAULT_MASTER_SOURCE_ORDER = ["8823", "haruki", "sekai-viewer"]
DEFAULT_ASSET_SOURCE_ORDER = [
    "uni",
    "haruki-main",
    "haruki-jp-dedicated",
    "legacy-viewer",
]
DEFAULT_B30_CONSTANTS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1B8tX9VL2PcSJKyuHFVd2UT_8kYlY4ZdwHwg9MfWOPug/"
    "gviz/tq?tqx=out:csv&gid=1855810409"
)
DEFAULT_PROFILE_STATIC_ASSET_BASES = [
    "https://raw.githubusercontent.com/Exmeaning/Exmeaning-Image-hosting/main",
    "https://cdn.jsdelivr.net/gh/Exmeaning/Exmeaning-Image-hosting@main",
]
LEGACY_MOESEKAI_B30_CONSTANTS_URL = (
    "https://moe.exmeaning.com/data/pjskb30/merged_chart.csv"
)


def _dataset_paths(prefix: str = "") -> dict[str, str]:
    return {dataset: f"{prefix}{dataset}.json" for dataset in MASTER_DATASET_KEYS}


DEFAULT_MASTER_SOURCE_FAMILIES: dict[str, dict[str, Any]] = {
    "8823": {
        "auto_probe": True,
        "version_path": "versions.json",
        "version_field": "data_version",
        "datasets": _dataset_paths(),
        "regions": {
            "jp": ("kotori8823", "sekai-master-db", "master"),
            "cn": ("kotori8823", "sekai-sc-master-db", "master"),
            "tw": ("kotori8823", "sekai-tc-master-db", "master"),
        },
    },
    "haruki": {
        "auto_probe": True,
        "version_path": "versions/current_version.json",
        "version_field": "dataVersion",
        "datasets": _dataset_paths("master/"),
        "regions": {
            "jp": ("Team-Haruki", "haruki-sekai-master", "main"),
            "cn": ("Team-Haruki", "haruki-sekai-sc-master", "main"),
            "tw": ("Team-Haruki", "haruki-sekai-tc-master", "main"),
        },
    },
    "sekai-viewer": {
        "auto_probe": False,
        "version_path": "versions.json",
        "version_field": "dataVersion",
        "datasets": _dataset_paths(),
        "regions": {
            "jp": ("Sekai-World", "sekai-master-db-diff", "main"),
            "cn": ("Sekai-World", "sekai-master-db-cn-diff", "main"),
            "tw": ("Sekai-World", "sekai-master-db-tc-diff", "main"),
        },
    },
}


def _infer_datasets_from_events_path(events_path: str) -> dict[str, str]:
    normalized = events_path.strip().strip("/")
    prefix = ""
    if "/" in normalized:
        prefix = normalized.rsplit("/", 1)[0].strip("/")
        prefix = f"{prefix}/" if prefix else ""
    return {dataset: f"{prefix}{dataset}.json" for dataset in MASTER_DATASET_KEYS}


class MasterSourceConfig(BaseModel):
    name: str
    region: Literal["cn", "jp", "tw"]
    family: str = "custom"
    auto_probe: bool = True
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    base_url: str = ""
    version_path: str = ""
    version_field: str = "dataVersion"
    datasets: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = value.copy()

        # 旧配置曾直接保存完整 URL；这里转成新模型字段，保证历史配置仍可读取。
        if payload.get("version_url") and not payload.get("version_path"):
            payload["version_path"] = payload["version_url"]
        if payload.get("events_url") and not payload.get("datasets"):
            payload["datasets"] = _infer_datasets_from_events_path(
                payload["events_url"]
            )
        if payload.get("events_path") and not payload.get("datasets"):
            payload["datasets"] = _infer_datasets_from_events_path(
                payload["events_path"]
            )

        payload.setdefault("family", "custom")
        family = str(payload.get("family") or "").strip() or "custom"
        inferred_family = family
        if inferred_family == "custom":
            inferred_name = str(payload.get("name") or "").strip().rsplit("-", 1)[0]
            if inferred_name in DEFAULT_MASTER_SOURCE_FAMILIES:
                inferred_family = inferred_name
        if "auto_probe" not in payload or payload.get("auto_probe") is None:
            payload["auto_probe"] = DEFAULT_MASTER_SOURCE_FAMILIES.get(
                inferred_family,
                {},
            ).get("auto_probe", True)
        payload.setdefault("base_url", "")
        payload.setdefault("datasets", {})
        return payload

    @field_validator("datasets")
    @classmethod
    def _normalize_datasets(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(key): str(path).strip()
            for key, path in value.items()
            if key and str(path).strip()
        }

    @staticmethod
    def _build_url(base_url: str, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))

    @property
    def raw_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        if self.owner and self.repo:
            return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}"
        return ""

    @property
    def version_url(self) -> str:
        return self._build_url(self.raw_base_url, self.version_path)

    @property
    def revision_api_url(self) -> str | None:
        if self.owner and self.repo:
            return f"https://api.github.com/repos/{self.owner}/{self.repo}/commits/{self.branch}"
        return None

    def dataset_path(self, dataset: str) -> str | None:
        return self.datasets.get(dataset)

    def dataset_url(self, dataset: str) -> str:
        path = self.dataset_path(dataset)
        if not path:
            raise KeyError(f"{self.name} 未配置数据集 {dataset}")
        return self._build_url(self.raw_base_url, path)

    @property
    def events_url(self) -> str:
        return self.dataset_url("events")

    @property
    def events_path(self) -> str:
        return self.dataset_path("events") or ""


def build_default_master_sources(
    order: list[str] | tuple[str, ...] | None = None,
) -> list[MasterSourceConfig]:
    family_order = list(order or DEFAULT_MASTER_SOURCE_ORDER)
    sources: list[MasterSourceConfig] = []
    for family_name in family_order:
        family = DEFAULT_MASTER_SOURCE_FAMILIES.get(family_name)
        if not family:
            continue
        for region in SERVERS:
            owner, repo, branch = family["regions"][region]
            sources.append(
                MasterSourceConfig(
                    name=f"{family_name}-{region}",
                    family=family_name,
                    auto_probe=bool(family.get("auto_probe", True)),
                    region=region,
                    owner=owner,
                    repo=repo,
                    branch=branch,
                    version_path=family["version_path"],
                    version_field=family["version_field"],
                    datasets=family["datasets"].copy(),
                )
            )
    return sources


DEFAULT_MASTER_SOURCES = build_default_master_sources()


def _merge_master_sources_config(
    raw_sources: Any,
    *,
    default_sources: list[MasterSourceConfig] | None = None,
) -> list[MasterSourceConfig]:
    configured_sources = [
        item
        if isinstance(item, MasterSourceConfig)
        else MasterSourceConfig.model_validate(item)
        for item in (raw_sources or [])
    ]
    configured_map = {item.name: item for item in configured_sources}
    merged: list[MasterSourceConfig] = []
    for default_source in default_sources or DEFAULT_MASTER_SOURCES:
        configured_source = configured_map.pop(default_source.name, None)
        if not configured_source:
            merged.append(default_source)
            continue
        # 默认源会随版本新增 dataset；旧配置同名源只覆盖用户字段，缺失的
        # dataset 路径从当前默认值补齐，避免升级后新增数据集永远下载不到。
        merged.append(
            configured_source.model_copy(
                update={
                    "datasets": {
                        **default_source.datasets,
                        **configured_source.datasets,
                    }
                }
            )
        )
    merged.extend(configured_map.values())
    return merged


class SekaiResourceSettings(BaseModel):
    master_source_order: list[str] = Field(
        default_factory=lambda: DEFAULT_MASTER_SOURCE_ORDER.copy()
    )
    master_sources: list[MasterSourceConfig] = Field(
        default_factory=lambda: DEFAULT_MASTER_SOURCES.copy()
    )
    master_check_interval_seconds: int = 180
    master_check_mode: Literal["revision", "version", "hybrid"] = "revision"
    asset_miss_cache_ttl_seconds: int = 21_600
    asset_batch_fetch_concurrency: int = 12
    asset_source_fetch_concurrency: int = 4
    asset_source_fetch_all: bool = False
    github_token: str = ""
    asset_source_order: list[str] = Field(
        default_factory=lambda: DEFAULT_ASSET_SOURCE_ORDER.copy()
    )
    profile_static_asset_bases: list[str] = Field(
        default_factory=lambda: DEFAULT_PROFILE_STATIC_ASSET_BASES.copy()
    )
    audio_format_priority: list[str] = Field(default_factory=lambda: ["mp3", "flac"])
    b30_constants_url: str = DEFAULT_B30_CONSTANTS_URL
    b30_constants_timeout_seconds: float = 10.0
    b30_constants_refresh_interval_seconds: int = 86_400

    @field_validator(
        "master_source_order",
        "asset_source_order",
        "profile_static_asset_bases",
        "audio_format_priority",
    )
    @classmethod
    def _filter_empty_values(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if item and str(item).strip()]

    @field_validator("github_token")
    @classmethod
    def _normalize_github_token(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("asset_miss_cache_ttl_seconds")
    @classmethod
    def _normalize_ttl_seconds(cls, value: int) -> int:
        return max(0, value)

    @field_validator(
        "asset_batch_fetch_concurrency",
        "asset_source_fetch_concurrency",
    )
    @classmethod
    def _normalize_fetch_concurrency(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("master_check_interval_seconds")
    @classmethod
    def _normalize_master_interval(cls, value: int) -> int:
        return max(1, value)

    @field_validator("b30_constants_timeout_seconds")
    @classmethod
    def _normalize_timeout_seconds(cls, value: float) -> float:
        return max(1.0, float(value))

    @field_validator("b30_constants_refresh_interval_seconds")
    @classmethod
    def _normalize_b30_refresh_interval_seconds(cls, value: int) -> int:
        return max(60, int(value))

    @field_validator("master_sources")
    @classmethod
    def _normalize_master_sources(
        cls,
        value: list[MasterSourceConfig],
    ) -> list[MasterSourceConfig]:
        return _merge_master_sources_config(value)


REGISTER_DEFAULTS = SekaiResourceSettings()

REGISTER_CONFIGS = [
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_ASSET_MISS_CACHE_TTL_SECONDS",
        value=REGISTER_DEFAULTS.asset_miss_cache_ttl_seconds,
        default_value=REGISTER_DEFAULTS.asset_miss_cache_ttl_seconds,
        help="SekaiResource 资源 404 负缓存有效期，秒；0 表示不缓存缺失状态",
        type=int,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY",
        value=REGISTER_DEFAULTS.asset_batch_fetch_concurrency,
        default_value=REGISTER_DEFAULTS.asset_batch_fetch_concurrency,
        help="SekaiResource 批量静态资源下载并发数",
        type=int,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY",
        value=REGISTER_DEFAULTS.asset_source_fetch_concurrency,
        default_value=REGISTER_DEFAULTS.asset_source_fetch_concurrency,
        help="SekaiResource 单个静态资源多源竞速并发数",
        type=int,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL",
        value=REGISTER_DEFAULTS.asset_source_fetch_all,
        default_value=REGISTER_DEFAULTS.asset_source_fetch_all,
        help="SekaiResource 单个静态资源是否同时请求全部候选源",
        type=bool,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_MASTER_SOURCE_ORDER",
        value=list(REGISTER_DEFAULTS.master_source_order),
        default_value=list(REGISTER_DEFAULTS.master_source_order),
        help="SekaiResource MasterData 源优先级",
        type=list[str],
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_MASTER_SOURCES",
        value=[item.model_dump() for item in REGISTER_DEFAULTS.master_sources],
        default_value=[item.model_dump() for item in REGISTER_DEFAULTS.master_sources],
        help="SekaiResource MasterData 数据源列表",
        type=list[MasterSourceConfig],
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_MASTER_CHECK_INTERVAL_SECONDS",
        value=REGISTER_DEFAULTS.master_check_interval_seconds,
        default_value=REGISTER_DEFAULTS.master_check_interval_seconds,
        help="SekaiResource MasterData 自动检查间隔，秒",
        type=int,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_MASTER_CHECK_MODE",
        value=REGISTER_DEFAULTS.master_check_mode,
        default_value=REGISTER_DEFAULTS.master_check_mode,
        help="兼容旧语义：当前统一按 version 判定更新",
        type=str,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_GITHUB_TOKEN",
        value=REGISTER_DEFAULTS.github_token,
        default_value=REGISTER_DEFAULTS.github_token,
        help="访问 GitHub revision API 的 Token；留空时匿名访问",
        type=str,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_ASSET_SOURCE_ORDER",
        value=list(REGISTER_DEFAULTS.asset_source_order),
        default_value=list(REGISTER_DEFAULTS.asset_source_order),
        help="SekaiResource 静态资源源站优先级",
        type=list[str],
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_PROFILE_STATIC_ASSET_BASES",
        value=list(REGISTER_DEFAULTS.profile_static_asset_bases),
        default_value=list(REGISTER_DEFAULTS.profile_static_asset_bases),
        help="SekaiResource 个人档案静态资源源站列表",
        type=list[str],
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_AUDIO_FORMAT_PRIORITY",
        value=list(REGISTER_DEFAULTS.audio_format_priority),
        default_value=list(REGISTER_DEFAULTS.audio_format_priority),
        help="SekaiResource 音频资源格式优先级",
        type=list[str],
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_B30_CONSTANTS_URL",
        value=REGISTER_DEFAULTS.b30_constants_url,
        default_value=REGISTER_DEFAULTS.b30_constants_url,
        help="SekaiResource B30 社区定数 CSV 地址",
        type=str,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_B30_CONSTANTS_TIMEOUT_SECONDS",
        value=REGISTER_DEFAULTS.b30_constants_timeout_seconds,
        default_value=REGISTER_DEFAULTS.b30_constants_timeout_seconds,
        help="SekaiResource B30 社区定数请求超时秒数",
        type=float,
    ),
    RegisterConfig(
        module=MODULE_NAME,
        key="SEKAI_RESOURCE_B30_CONSTANTS_REFRESH_INTERVAL_SECONDS",
        value=REGISTER_DEFAULTS.b30_constants_refresh_interval_seconds,
        default_value=REGISTER_DEFAULTS.b30_constants_refresh_interval_seconds,
        help="SekaiResource B30 社区定数自动检查间隔，秒",
        type=int,
    ),
]

_SENTINEL = object()


def _normalize_compare_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_compare_value(value.model_dump())
    if isinstance(value, list | tuple):
        return [_normalize_compare_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_compare_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def _config_value_equals(left: Any, right: Any) -> bool:
    return _normalize_compare_value(left) == _normalize_compare_value(right)


def _is_explicit_config_value(module: str, key: str, default: Any) -> bool:
    # 显式配置通过 config.yaml 或非默认值识别，注册默认值不算用户配置。
    key = key.upper()
    simple_group = getattr(Config, "_simple_data", {}).get(module)
    if isinstance(simple_group, dict) and key in simple_group:
        return True

    module_group = getattr(Config, "_data", {}).get(module)
    config = module_group.configs.get(key) if module_group else None
    if not config:
        return False
    value = getattr(config, "value", _SENTINEL)
    if value is _SENTINEL or value is None:
        return False
    return not _config_value_equals(value, default)


def register_configs() -> None:
    if not hasattr(Config, "add_plugin_config"):
        return
    for config in REGISTER_CONFIGS:
        Config.add_plugin_config(
            config.module or MODULE_NAME,
            config.key,
            config.value,
            help=config.help,
            default_value=config.default_value,
            type=config.type,
        )


def _get_compat_config(
    key: str,
    default: Any,
    *,
    legacy_key: str | None = None,
    legacy_keys: list[str] | None = None,
    legacy_default: Any = _SENTINEL,
) -> Any:
    value = Config.get_config(MODULE_NAME, key, _SENTINEL)
    if value is not _SENTINEL and (
        not _config_value_equals(value, default)
        or _is_explicit_config_value(MODULE_NAME, key, default)
    ):
        return value

    # 新配置只是注册默认值时继续读取旧键，避免升级后用户配置被默认值遮蔽。
    candidates = ([legacy_key] if legacy_key else []) + (legacy_keys or [])
    for candidate in candidates:
        legacy_value = Config.get_config(LEGACY_MODULE_NAME, candidate, _SENTINEL)
        if legacy_value is _SENTINEL:
            continue
        if legacy_default is _SENTINEL:
            return legacy_value
        # 旧 B30 默认源已经过期；只有用户显式配置过旧键时才迁移旧值。
        if (
            not _config_value_equals(legacy_value, legacy_default)
            or _is_explicit_config_value(LEGACY_MODULE_NAME, candidate, legacy_default)
        ):
            return legacy_value
    if value is not _SENTINEL:
        return value
    return default


@lru_cache(maxsize=1)
def get_settings() -> SekaiResourceSettings:
    register_configs()
    defaults = SekaiResourceSettings()
    payload = {
        "master_source_order": _get_compat_config(
            "SEKAI_RESOURCE_MASTER_SOURCE_ORDER",
            defaults.master_source_order,
            legacy_key="MOESEKAI_MASTER_SOURCE_ORDER",
        ),
        "master_sources": _get_compat_config(
            "SEKAI_RESOURCE_MASTER_SOURCES",
            defaults.master_sources,
            legacy_key="MOESEKAI_MASTER_SOURCES",
        ),
        "master_check_interval_seconds": _get_compat_config(
            "SEKAI_RESOURCE_MASTER_CHECK_INTERVAL_SECONDS",
            defaults.master_check_interval_seconds,
            legacy_key="MOESEKAI_MASTER_CHECK_INTERVAL_SECONDS",
            legacy_keys=["MOESEKAI_MASTER_AUTO_CHECK_INTERVAL_SECONDS"],
        ),
        "master_check_mode": _get_compat_config(
            "SEKAI_RESOURCE_MASTER_CHECK_MODE",
            defaults.master_check_mode,
            legacy_key="MOESEKAI_MASTER_CHECK_MODE",
        ),
        "asset_miss_cache_ttl_seconds": _get_compat_config(
            "SEKAI_RESOURCE_ASSET_MISS_CACHE_TTL_SECONDS",
            defaults.asset_miss_cache_ttl_seconds,
            legacy_key="MOESEKAI_ASSET_MISS_CACHE_TTL_SECONDS",
        ),
        "asset_batch_fetch_concurrency": _get_compat_config(
            "SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY",
            defaults.asset_batch_fetch_concurrency,
        ),
        "asset_source_fetch_concurrency": _get_compat_config(
            "SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY",
            defaults.asset_source_fetch_concurrency,
        ),
        "asset_source_fetch_all": _get_compat_config(
            "SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL",
            defaults.asset_source_fetch_all,
        ),
        "github_token": _get_compat_config(
            "SEKAI_RESOURCE_GITHUB_TOKEN",
            defaults.github_token,
            legacy_key="MOESEKAI_GITHUB_TOKEN",
        ),
        "asset_source_order": _get_compat_config(
            "SEKAI_RESOURCE_ASSET_SOURCE_ORDER",
            defaults.asset_source_order,
            legacy_key="MOESEKAI_ASSET_SOURCE_ORDER",
        ),
        "profile_static_asset_bases": _get_compat_config(
            "SEKAI_RESOURCE_PROFILE_STATIC_ASSET_BASES",
            defaults.profile_static_asset_bases,
            legacy_key="MOESEKAI_PROFILE_STATIC_ASSET_BASES",
            legacy_default=defaults.profile_static_asset_bases,
        ),
        "audio_format_priority": _get_compat_config(
            "SEKAI_RESOURCE_AUDIO_FORMAT_PRIORITY",
            defaults.audio_format_priority,
            legacy_key="MOESEKAI_AUDIO_FORMAT_PRIORITY",
        ),
        "b30_constants_url": _get_compat_config(
            "SEKAI_RESOURCE_B30_CONSTANTS_URL",
            defaults.b30_constants_url,
            legacy_key="MOESEKAI_B30_CONSTANTS_URL",
            legacy_default=LEGACY_MOESEKAI_B30_CONSTANTS_URL,
        ),
        "b30_constants_timeout_seconds": _get_compat_config(
            "SEKAI_RESOURCE_B30_CONSTANTS_TIMEOUT_SECONDS",
            defaults.b30_constants_timeout_seconds,
            legacy_key="MOESEKAI_B30_CONSTANTS_TIMEOUT_SECONDS",
            legacy_default=10.0,
        ),
        "b30_constants_refresh_interval_seconds": _get_compat_config(
            "SEKAI_RESOURCE_B30_CONSTANTS_REFRESH_INTERVAL_SECONDS",
            defaults.b30_constants_refresh_interval_seconds,
            legacy_key="MOESEKAI_B30_CONSTANTS_REFRESH_INTERVAL_SECONDS",
            legacy_default=21_600,
        ),
    }
    settings = SekaiResourceSettings.model_validate(payload)
    ordered_defaults = build_default_master_sources(settings.master_source_order)
    settings.master_sources = _merge_master_sources_config(
        settings.master_sources or ordered_defaults,
        default_sources=ordered_defaults,
    )
    return settings


def refresh_settings() -> SekaiResourceSettings:
    get_settings.cache_clear()
    return get_settings()


register_configs()


__all__ = [
    "DEFAULT_ASSET_SOURCE_ORDER",
    "DEFAULT_B30_CONSTANTS_URL",
    "DEFAULT_MASTER_SOURCES",
    "DEFAULT_MASTER_SOURCE_ORDER",
    "DEFAULT_PROFILE_STATIC_ASSET_BASES",
    "MASTER_DATASET_KEYS",
    "REGISTER_CONFIGS",
    "MasterSourceConfig",
    "SekaiResourceSettings",
    "_merge_master_sources_config",
    "build_default_master_sources",
    "get_settings",
    "refresh_settings",
    "register_configs",
]
