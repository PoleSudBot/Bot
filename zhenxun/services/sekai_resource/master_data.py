from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import uuid4

import httpx

from .config import MASTER_DATASET_KEYS, MasterSourceConfig, get_settings
from .constants import (
    LEGACY_MASTER_DATA_DIR,
    LEGACY_STATE_DIR,
    MASTER_DATA_DIR,
    MODULE_NAME,
    SERVER_SET,
    SERVERS,
    STATE_DIR,
    server_label,
)
from .runtime import AsyncHttpx, logger
from .storage import JsonStateStore

_PROBE_STATE_DEFAULT = {"sources": {}}
_VERSIONLESS_FALLBACK_NOTE = "所有版本接口均失败，已回退到可下载源"


@dataclass
class SourceRevisionInfo:
    source: MasterSourceConfig
    revision: str | None = None
    version: str | None = None
    success: bool = False
    error: str | None = None

    @property
    def display(self) -> str:
        return self.version or self.revision or self.error or "获取失败"


@dataclass
class RegionUpdateResult:
    server: str
    selected_source_name: str | None = None
    previous_source_name: str | None = None
    previous_revision: str | None = None
    current_revision: str | None = None
    previous_version: str | None = None
    current_version: str | None = None
    checked_at: str | None = None
    updated: bool = False
    download_success: bool = False
    error: str | None = None
    selection_note: str | None = None
    changed_datasets: list[str] = field(default_factory=list)
    source_versions: list[SourceRevisionInfo] = field(default_factory=list)
    added_records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @staticmethod
    def _display_source_name(source_name: str | None) -> str:
        if not source_name:
            return "未选中"
        name = source_name.removesuffix("-jp").removesuffix("-cn").removesuffix("-tw")
        return {
            "8823": "8823",
            "haruki": "Haruki",
            "sekai-viewer": "Sekai Viewer",
        }.get(name, name)

    def _source_versions_message(self) -> str:
        lines = [f"{server_label(self.server)} MasterData 数据源"]
        for item in self.source_versions:
            lines.append(
                f"[{self._display_source_name(item.source.name)}] {item.display}"
            )
        return "\n".join(lines)

    def to_message(self) -> str:
        if self.error:
            base = f"{server_label(self.server)} 更新失败: {self.error}"
            if self.source_versions:
                return f"{base}\n\n{self._source_versions_message()}"
            return base
        changed = "已更新" if self.updated else "无变化"
        source_name = self._display_source_name(self.selected_source_name)
        revision_line = (
            f"Revision: {self.previous_revision or '-'} -> "
            f"{self.current_revision or '-'}"
        )
        version_line = (
            f"版本: {self.previous_version or '-'} -> {self.current_version or '-'}"
        )
        lines = [
            f"{server_label(self.server)} {changed}",
            f"来源: {source_name}",
        ]
        if self.selection_note:
            lines.append(f"说明: {self.selection_note}")
        lines.extend([revision_line, version_line])
        base = "\n".join(lines)
        if self.source_versions:
            return f"{base}\n\n{self._source_versions_message()}"
        return base


class MasterDataProvider:
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _cache: ClassVar[dict[tuple[str, str], Any]] = {}
    _state_store: ClassVar[JsonStateStore] = JsonStateStore(
        STATE_DIR / "master_state.json"
    )
    _probe_state_store: ClassVar[JsonStateStore] = JsonStateStore(
        STATE_DIR / "master_probe_state.json"
    )
    _probe_state_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _legacy_check_mode_warned: ClassVar[bool] = False

    @classmethod
    def _server_dir(cls, server: str) -> Path:
        path = MASTER_DATA_DIR / server
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _dataset_path(cls, server: str, dataset: str) -> Path:
        return cls._server_dir(server) / f"{dataset}.json"

    @classmethod
    def _load_state(cls) -> dict[str, dict[str, Any]]:
        return cls._state_store.load({})

    @classmethod
    def _save_state(cls, payload: dict[str, dict[str, Any]]) -> None:
        cls._state_store.save(payload)

    @classmethod
    def _normalize_probe_state(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return dict(_PROBE_STATE_DEFAULT)
        sources = payload.get("sources", {})
        if not isinstance(sources, dict):
            sources = {}
        return {
            "sources": {
                str(name): {
                    "last_version": (
                        str(value.get("last_version"))
                        if value.get("last_version") is not None
                        else None
                    ),
                    "last_revision": (
                        str(value.get("last_revision"))
                        if value.get("last_revision") is not None
                        else None
                    ),
                    "last_checked_at": (
                        str(value.get("last_checked_at"))
                        if value.get("last_checked_at") is not None
                        else None
                    ),
                    "error": (
                        str(value.get("error"))
                        if value.get("error") is not None
                        else None
                    ),
                }
                for name, value in sources.items()
                if isinstance(value, dict)
            },
        }

    @classmethod
    async def _load_probe_state(cls) -> dict[str, Any]:
        async with cls._probe_state_lock:
            return cls._normalize_probe_state(
                cls._probe_state_store.load(_PROBE_STATE_DEFAULT)
            )

    @classmethod
    async def _save_probe_state(cls, payload: dict[str, Any]) -> None:
        async with cls._probe_state_lock:
            cls._probe_state_store.save(cls._normalize_probe_state(payload))

    @classmethod
    async def _get_probe_source_state(cls, source_name: str) -> dict[str, Any]:
        state = await cls._load_probe_state()
        source_state = state["sources"].get(source_name, {})
        return source_state if isinstance(source_state, dict) else {}

    @classmethod
    async def _update_probe_source_state(
        cls,
        source_name: str,
        **updates: Any,
    ) -> dict[str, Any]:
        async with cls._probe_state_lock:
            state = cls._normalize_probe_state(
                cls._probe_state_store.load(_PROBE_STATE_DEFAULT)
            )
            source_state = state["sources"].get(source_name, {})
            if not isinstance(source_state, dict):
                source_state = {}
            source_state.update(updates)
            state["sources"][source_name] = source_state
            cls._probe_state_store.save(state)
            return source_state.copy()

    @classmethod
    def _load_dataset_from_disk(cls, server: str, dataset: str) -> Any:
        path = cls._dataset_path(server, dataset)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(
                f"SekaiResource 主数据解析失败: {server}/{dataset}",
                MODULE_NAME,
                e=exc,
            )
            return []

    @classmethod
    def _normalize_update_payloads(
        cls,
        payloads: dict[str, Any],
    ) -> dict[str, list[Any]]:
        # 更新前统一校验全部数据集，避免部分文件先落盘后才发现格式错误。
        normalized: dict[str, list[Any]] = {}
        for dataset in MASTER_DATASET_KEYS:
            if dataset not in payloads:
                raise KeyError(f"{dataset}.json 未下载")
            payload = payloads[dataset]
            if not isinstance(payload, list):
                raise TypeError(f"{dataset}.json 格式无效")
            normalized[dataset] = payload
        return normalized

    @classmethod
    def _load_baseline_payloads(
        cls,
        server: str,
        dataset_path: Callable[[str], Path],
        *,
        source_label: str,
    ) -> dict[str, list[Any]] | None:
        baseline: dict[str, list[Any]] = {}
        for dataset in MASTER_DATASET_KEYS:
            path = dataset_path(dataset)
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                logger.warning(
                    f"SekaiResource 主数据基线解析失败，将跳过新增记录比对: "
                    f"{server}/{dataset} ({source_label})",
                    MODULE_NAME,
                    e=exc,
                )
                return None
            if not isinstance(payload, list):
                return None
            baseline[dataset] = payload
        return baseline

    @classmethod
    def _load_legacy_diff_baseline(
        cls,
        server: str,
    ) -> dict[str, list[Any]] | None:
        legacy_state = JsonStateStore(LEGACY_STATE_DIR / "master_state.json").load({})
        legacy_region_state = (
            legacy_state.get(server, {}) if isinstance(legacy_state, dict) else {}
        )
        if not legacy_region_state:
            return None
        baseline = cls._load_baseline_payloads(
            server,
            lambda dataset: LEGACY_MASTER_DATA_DIR / server / f"{dataset}.json",
            source_label="legacy-moesekai",
        )
        if baseline is not None:
            logger.info(
                f"SekaiResource 使用旧 MoeSekai 主数据作为首次差异基线: {server}",
                MODULE_NAME,
            )
        return baseline

    @classmethod
    def _load_diff_baseline(
        cls,
        server: str,
        region_state: dict[str, Any],
    ) -> dict[str, list[Any]] | None:
        # 已存在的本地全量文件是最可靠的差异基线；state 只用来区分完全首次落地。
        current_baseline = cls._load_baseline_payloads(
            server,
            lambda dataset: cls._dataset_path(server, dataset),
            source_label="current",
        )
        if current_baseline is not None:
            return current_baseline
        if region_state:
            return None
        # sekai_resource 拆出后数据根从 moesekai 迁走；首次落新目录时用旧目录兜底，
        # 否则真实更新会因为缺少新 state 被当作首次落地而没有 added_records。
        return cls._load_legacy_diff_baseline(server)

    @classmethod
    def _diff_update_added_records(
        cls,
        server: str,
        region_state: dict[str, Any],
        payloads: dict[str, list[Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        added_by_dataset: dict[str, list[dict[str, Any]]] = {}
        changed_datasets: list[str] = []
        baseline = cls._load_diff_baseline(server, region_state)
        if baseline is None:
            return added_by_dataset, changed_datasets
        for dataset, payload in payloads.items():
            added_records = cls._diff_added_records(baseline[dataset], payload)
            if added_records:
                added_by_dataset[dataset] = added_records
                changed_datasets.append(dataset)
        return added_by_dataset, changed_datasets

    @staticmethod
    def _state_entry(
        *,
        source: MasterSourceConfig,
        revision: str | None,
        version: str | None,
        checked_at: str | None,
    ) -> dict[str, Any]:
        return {
            "source_name": source.name,
            "family": source.family,
            "revision": revision,
            "version": version,
            "checked_at": checked_at,
            "updated_at": checked_at,
            "datasets": list(MASTER_DATASET_KEYS),
        }

    @classmethod
    def _commit_region_payloads(
        cls,
        server: str,
        payloads: dict[str, list[Any]],
        state: dict[str, dict[str, Any]],
        state_entry: dict[str, Any],
    ) -> None:
        # 数据文件和 state 分两阶段提交；失败时用备份恢复旧文件。
        transaction_id = uuid4().hex
        tmp_paths: list[Path] = []
        replaced_paths: list[tuple[Path, Path | None]] = []

        try:
            for dataset, payload in payloads.items():
                path = cls._dataset_path(server, dataset)
                tmp_path = path.with_name(f".{path.name}.{transaction_id}.tmp")
                tmp_paths.append(tmp_path)
                tmp_path.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            for dataset in MASTER_DATASET_KEYS:
                path = cls._dataset_path(server, dataset)
                tmp_path = path.with_name(f".{path.name}.{transaction_id}.tmp")
                backup_path: Path | None = None
                if path.exists():
                    backup_path = path.with_name(f".{path.name}.{transaction_id}.bak")
                    path.replace(backup_path)
                try:
                    tmp_path.replace(path)
                except OSError:
                    if backup_path and backup_path.exists():
                        backup_path.replace(path)
                    raise
                replaced_paths.append((path, backup_path))

            # 状态文件最后提交；如果这里失败，前面的数据文件会按备份回滚。
            new_state = dict(state)
            new_state[server] = state_entry
            cls._save_state(new_state)
        except Exception:
            # 提交阶段任意一步失败都必须回滚，避免多份主数据文件停在不同版本。
            for path, backup_path in reversed(replaced_paths):
                path.unlink(missing_ok=True)
                if backup_path and backup_path.exists():
                    backup_path.replace(path)
            raise
        finally:
            for tmp_path in tmp_paths:
                tmp_path.unlink(missing_ok=True)
            for _path, backup_path in replaced_paths:
                if backup_path:
                    backup_path.unlink(missing_ok=True)

    @classmethod
    def _clear_region_cache(cls, server: str) -> None:
        keys = [key for key in cls._cache if key[0] == server]
        for key in keys:
            cls._cache.pop(key, None)

    @classmethod
    def _dataset_ids(cls, payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id is None:
                continue
            result[str(item_id)] = item
        return result

    @classmethod
    def _diff_added_records(
        cls, old_payload: Any, new_payload: Any
    ) -> list[dict[str, Any]]:
        old_ids = cls._dataset_ids(old_payload)
        new_map = cls._dataset_ids(new_payload)
        return [item for key, item in new_map.items() if key not in old_ids]

    @staticmethod
    def _response_excerpt(response: httpx.Response, *, limit: int = 240) -> str:
        text = response.text.replace("\n", " ").replace("\r", " ").strip()
        if not text:
            return "<empty>"
        if len(text) > limit:
            return f"{text[:limit]}...(truncated)"
        return text

    @classmethod
    def _response_debug_context(
        cls,
        response: httpx.Response,
        *,
        source: MasterSourceConfig,
        kind: Literal["version", "revision"],
    ) -> str:
        content_type = response.headers.get("content-type", "<missing>")
        return (
            f"{kind} source={source.name} "
            f"url={response.request.url!s} "
            f"status={response.status_code} "
            f"content-type={content_type} "
            f"body={cls._response_excerpt(response)}"
        )

    @classmethod
    def _parse_json_object_response(
        cls,
        response: httpx.Response,
        *,
        source: MasterSourceConfig,
        kind: Literal["version", "revision"],
    ) -> dict[str, Any]:
        context = cls._response_debug_context(response, source=source, kind=kind)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TypeError(f"{kind} 接口返回的不是有效 JSON 对象: {context}") from exc
        if not isinstance(payload, dict):
            raise TypeError(f"{kind} 接口返回的不是 JSON 对象: {context}")
        return payload

    @classmethod
    async def _fetch_version(cls, source: MasterSourceConfig) -> str | None:
        if not source.version_path:
            return None
        response = await AsyncHttpx.get(
            source.version_url,
            timeout=20,
        )
        payload = cls._parse_json_object_response(
            response,
            source=source,
            kind="version",
        )
        version = payload.get(source.version_field)
        if version is None:
            context = cls._response_debug_context(
                response,
                source=source,
                kind="version",
            )
            raise ValueError(f"版本字段 {source.version_field} 不存在: {context}")
        return str(version)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code in {403, 429}:
            return True
        text = response.text.lower()
        return "rate limit" in text or "secondary rate limit" in text

    @classmethod
    def _build_github_headers(cls) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = get_settings().github_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @classmethod
    def _warn_legacy_check_mode(cls) -> None:
        if cls._legacy_check_mode_warned:
            return
        mode = getattr(get_settings(), "master_check_mode", "version")
        if mode in {"revision", "hybrid"}:
            logger.warning(
                "SekaiResource 主数据自动更新现已统一按 version 判定，"
                "MASTER_CHECK_MODE 仅保留兼容语义",
                MODULE_NAME,
            )
            cls._legacy_check_mode_warned = True

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        text = str(version or "").strip()
        if not text:
            raise ValueError("版本号为空")
        parts = tuple(int(item) for item in text.split("."))
        if not parts:
            raise ValueError("版本号为空")
        return parts

    @classmethod
    def _compare_versions(cls, left: str | None, right: str | None) -> int:
        if left is None and right is None:
            return 0
        if left is None:
            return -1
        if right is None:
            return 1
        left_key = cls._version_key(left)
        right_key = cls._version_key(right)
        if left_key == right_key:
            return 0
        return 1 if left_key > right_key else -1

    @classmethod
    def _source_priority(
        cls,
        source: MasterSourceConfig,
        position: int,
    ) -> int:
        order = {
            family: index
            for index, family in enumerate(get_settings().master_source_order)
        }
        family_index = order.get(source.family)
        if family_index is not None:
            return family_index
        return len(order) + position

    @classmethod
    def _sources_for_server(
        cls,
        server: str,
        *,
        include_lazy: bool,
    ) -> list[MasterSourceConfig]:
        return [
            item
            for item in get_settings().master_sources
            if item.region == server and (include_lazy or item.auto_probe)
        ]

    @classmethod
    def _has_all_datasets(cls, source: MasterSourceConfig) -> bool:
        return all(dataset in source.datasets for dataset in MASTER_DATASET_KEYS)

    @classmethod
    def get_probe_interval_seconds(cls) -> float:
        return float(max(1, get_settings().master_check_interval_seconds))

    @classmethod
    async def _fetch_source_snapshot(
        cls, source: MasterSourceConfig
    ) -> SourceRevisionInfo:
        checked_at = datetime.now().isoformat(timespec="seconds")
        try:
            version = await cls._fetch_version(source)
            cls._version_key(version)
            await cls._update_probe_source_state(
                source.name,
                last_version=version,
                last_checked_at=checked_at,
                error=None,
            )
            return SourceRevisionInfo(
                source=source,
                version=version,
                success=True,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"获取 SekaiResource 主数据源版本失败: {source.name}",
                MODULE_NAME,
                e=exc,
            )
            await cls._update_probe_source_state(
                source.name,
                last_checked_at=checked_at,
                error=error,
            )
            return SourceRevisionInfo(
                source=source,
                success=False,
                error=error,
            )

    @classmethod
    async def _fetch_selected_revision(cls, source: MasterSourceConfig) -> str | None:
        previous_probe_state = await cls._get_probe_source_state(source.name)
        checked_at = datetime.now().isoformat(timespec="seconds")
        previous_revision = (
            str(previous_probe_state.get("last_revision"))
            if previous_probe_state.get("last_revision") is not None
            else None
        )
        if not source.revision_api_url:
            return previous_revision
        try:
            response = await AsyncHttpx.get(
                source.revision_api_url,
                headers=cls._build_github_headers(),
                timeout=20,
                accept_status_codes=(403, 429),
            )
            if cls._is_rate_limited(response):
                logger.warning(
                    "SekaiResource GitHub revision 获取触发限流，"
                    f"跳过 revision 刷新: {source.name}",
                    MODULE_NAME,
                )
                return previous_revision
            response.raise_for_status()
            payload = cls._parse_json_object_response(
                response,
                source=source,
                kind="revision",
            )
            revision_value = payload.get("sha")
            if revision_value is None:
                context = cls._response_debug_context(
                    response,
                    source=source,
                    kind="revision",
                )
                raise ValueError(f"revision 接口缺少 sha 字段: {context}")
            revision = str(revision_value)
            await cls._update_probe_source_state(
                source.name,
                last_revision=revision,
                last_checked_at=checked_at,
            )
            return revision
        except Exception as exc:
            logger.warning(
                f"获取 SekaiResource 主数据源 revision 失败: {source.name}",
                MODULE_NAME,
                e=exc,
            )
            return previous_revision

    @classmethod
    async def _select_source(
        cls,
        server: str,
        *,
        include_lazy: bool,
    ) -> tuple[MasterSourceConfig | None, list[SourceRevisionInfo]]:
        sources = cls._sources_for_server(server, include_lazy=include_lazy)
        if not sources:
            return None, []
        results = list(
            await asyncio.gather(
                *(cls._fetch_source_snapshot(source) for source in sources)
            )
        )
        candidates: list[tuple[tuple[int, ...], int, SourceRevisionInfo]] = []
        for position, item in enumerate(results):
            if (
                not item.success
                or not item.version
                or not cls._has_all_datasets(item.source)
            ):
                continue
            candidates.append(
                (
                    cls._version_key(item.version),
                    cls._source_priority(item.source, position),
                    item,
                )
            )
        if not candidates:
            return None, results
        candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected_info = candidates[0][2]
        selected_info.revision = await cls._fetch_selected_revision(
            selected_info.source
        )
        return selected_info.source, results

    @classmethod
    async def probe_next_updates(cls) -> list[RegionUpdateResult]:
        results: list[RegionUpdateResult] = []
        cls._warn_legacy_check_mode()
        for server in SERVERS:
            result = await cls.update_region(server, force=False)
            if result.updated:
                results.append(result)
        return results

    @classmethod
    async def _fetch_selected_payloads(
        cls, source: MasterSourceConfig
    ) -> tuple[dict[str, Any], str | None]:
        async def _fetch_dataset(dataset: str) -> tuple[str, Any]:
            return dataset, await AsyncHttpx.get_json(
                source.dataset_url(dataset),
                raise_on_failure=True,
            )

        dataset_results = await asyncio.gather(
            *(_fetch_dataset(dataset) for dataset in MASTER_DATASET_KEYS)
        )
        version: str | None = None
        try:
            version = await cls._fetch_version(source)
        except Exception as exc:
            logger.warning(
                f"SekaiResource 版本文件获取失败，将仅记录 revision: {source.name}",
                MODULE_NAME,
                e=exc,
            )
        return dict(dataset_results), version

    @classmethod
    async def update_region(
        cls,
        server: str,
        *,
        force: bool = False,
    ) -> RegionUpdateResult:
        server = server.lower()
        if server not in SERVER_SET:
            return RegionUpdateResult(server=server, error="不支持的区服")

        async with cls._lock:
            cls._warn_legacy_check_mode()
            state = cls._load_state()
            region_state = state.get(server, {})
            result = RegionUpdateResult(
                server=server,
                previous_source_name=region_state.get("source_name"),
                previous_revision=region_state.get("revision"),
                previous_version=region_state.get("version"),
                checked_at=datetime.now().isoformat(timespec="seconds"),
            )
            selected_source, source_versions = await cls._select_source(
                server,
                include_lazy=force,
            )
            result.source_versions = source_versions
            if not selected_source:
                if force:
                    fallback_sources = [
                        source
                        for source in cls._sources_for_server(server, include_lazy=True)
                        if cls._has_all_datasets(source)
                    ]
                    ordered_fallback_sources = sorted(
                        enumerate(fallback_sources),
                        key=lambda item: cls._source_priority(item[1], item[0]),
                    )
                    for _, source in ordered_fallback_sources:
                        try:
                            (
                                payloads,
                                fetched_version,
                            ) = await cls._fetch_selected_payloads(source)
                            result.selected_source_name = source.name
                            result.current_revision = (
                                await cls._fetch_selected_revision(source)
                            )
                            result.current_version = fetched_version
                            result.selection_note = _VERSIONLESS_FALLBACK_NOTE
                            normalized_payloads = cls._normalize_update_payloads(
                                payloads
                            )
                            (
                                added_records,
                                changed_datasets,
                            ) = cls._diff_update_added_records(
                                server,
                                region_state,
                                normalized_payloads,
                            )
                            cls._commit_region_payloads(
                                server,
                                normalized_payloads,
                                state,
                                cls._state_entry(
                                    source=source,
                                    revision=result.current_revision,
                                    version=result.current_version,
                                    checked_at=result.checked_at,
                                ),
                            )
                            cls._clear_region_cache(server)
                            result.added_records = added_records
                            result.changed_datasets = changed_datasets
                            result.updated = True
                            result.download_success = True
                            return result
                        except Exception as exc:
                            logger.warning(
                                f"SekaiResource 手动回退源下载失败: {source.name}",
                                MODULE_NAME,
                                e=exc,
                            )
                            continue
                result.error = "没有可用的主数据源"
                return result

            selected_info = next(
                item
                for item in source_versions
                if item.source.name == selected_source.name
            )
            result.selected_source_name = selected_source.name
            result.current_revision = selected_info.revision
            result.current_version = selected_info.version

            needs_update = force or not region_state
            version_compare = cls._compare_versions(
                result.current_version,
                result.previous_version,
            )
            if result.current_version and version_compare > 0:
                needs_update = True
            missing_datasets = [
                dataset
                for dataset in MASTER_DATASET_KEYS
                if not cls._dataset_path(server, dataset).exists()
            ]
            if region_state and result.previous_version and version_compare < 0:
                result.download_success = True
                result.selection_note = (
                    "候选版本低于当前已落地版本，已跳过更新"
                    if force
                    else "候选版本低于当前已落地版本，已跳过自动回退"
                )
                return result
            if missing_datasets:
                needs_update = True

            if not needs_update:
                result.download_success = True
                return result

            try:
                payloads, fetched_version = await cls._fetch_selected_payloads(
                    selected_source
                )
                if fetched_version:
                    result.current_version = fetched_version
                normalized_payloads = cls._normalize_update_payloads(payloads)
                added_records, changed_datasets = cls._diff_update_added_records(
                    server,
                    region_state,
                    normalized_payloads,
                )
                cls._commit_region_payloads(
                    server,
                    normalized_payloads,
                    state,
                    cls._state_entry(
                        source=selected_source,
                        revision=result.current_revision,
                        version=result.current_version,
                        checked_at=result.checked_at,
                    ),
                )
                cls._clear_region_cache(server)
                result.added_records = added_records
                result.changed_datasets = changed_datasets
                result.updated = True
                result.download_success = True
                return result
            except Exception as exc:
                logger.error(
                    f"SekaiResource 主数据下载失败: {server}",
                    MODULE_NAME,
                    e=exc,
                )
                result.error = f"{type(exc).__name__}: {exc}"
                return result

    @classmethod
    async def update_all(cls, *, force: bool = False) -> list[RegionUpdateResult]:
        results = []
        for server in SERVERS:
            results.append(await cls.update_region(server, force=force))
        return results

    @classmethod
    async def get_dataset(cls, server: str, dataset: str) -> Any:
        server = server.lower()
        key = (server, dataset)
        if key in cls._cache:
            return cls._cache[key]
        path = cls._dataset_path(server, dataset)
        if not path.exists():
            await cls.update_region(server)
        if not path.exists():
            return []
        payload = cls._load_dataset_from_disk(server, dataset)
        cls._cache[key] = payload
        return payload

    @classmethod
    async def get_events(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "events")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_virtual_lives(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "virtualLives")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_gachas(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "gachas")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_cards(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "cards")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_game_characters(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "gameCharacters")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_honors(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "honors")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_honor_groups(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "honorGroups")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_stamps(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "stamps")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_musics(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "musics")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_music_difficulties(cls, server: str) -> list[dict[str, Any]]:
        payload = await cls.get_dataset(server, "musicDifficulties")
        return payload if isinstance(payload, list) else []

    @classmethod
    async def get_current_event(
        cls,
        server: str,
        fallback: Literal[None, "prev", "next", "prev_first", "next_first"] = None,
    ) -> dict[str, Any] | None:
        events = sorted(
            await cls.get_events(server),
            key=lambda item: item.get("aggregateAt", 0),
        )
        if not events:
            return None

        now = datetime.now()
        prev_event = None
        cur_event = None
        next_event = None
        for event in events:
            try:
                start_time = datetime.fromtimestamp(event["startAt"] / 1000)
                end_time = datetime.fromtimestamp(event["aggregateAt"] / 1000 + 1)
            except (KeyError, TypeError, ValueError):
                continue
            if start_time <= now <= end_time:
                cur_event = event
            if end_time < now:
                prev_event = event
            if next_event is None and start_time > now:
                next_event = event

        if cur_event or fallback is None:
            return cur_event
        if fallback == "prev":
            return prev_event
        if fallback == "next":
            return next_event
        if fallback == "prev_first":
            return prev_event or next_event
        if fallback == "next_first":
            return next_event or prev_event
        return None


MasterDataService = MasterDataProvider
SourceVersionInfo = SourceRevisionInfo
master_data_provider = MasterDataProvider()
master_data_service = master_data_provider
