from __future__ import annotations

from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import httpx
import nonebot
import pytest

nonebot.init()

from zhenxun.services.sekai_resource import master_data as masterdata_module
from zhenxun.services.sekai_resource.asset_cache import AssetCacheProvider
from zhenxun.services.sekai_resource.assets import AssetProvider
import zhenxun.services.sekai_resource.config as resource_config
from zhenxun.services.sekai_resource.config import (
    MASTER_DATASET_KEYS,
    MasterSourceConfig,
    _merge_master_sources_config,
)
from zhenxun.services.sekai_resource.constants import RESOURCE_DATA_DIR
from zhenxun.services.sekai_resource.master_data import (
    MasterDataService,
    RegionUpdateResult,
    SourceVersionInfo,
    master_data_service,
)


class _FakeResourceConfig:
    def __init__(self, payload: dict[str, dict[str, object]]):
        self.payload = payload
        self._data = {
            module: SimpleNamespace(
                configs={
                    key: SimpleNamespace(value=value) for key, value in values.items()
                }
            )
            for module, values in payload.items()
        }
        self._simple_data = {}

    def get_config(self, module: str, key: str, default=None, **_kwargs):
        return self.payload.get(module, {}).get(key, default)


def test_sekai_resource_uses_dedicated_data_dir():
    assert RESOURCE_DATA_DIR.name == "sekai_resource"


def test_master_dataset_keys_include_required_datasets():
    assert "gachas" in MASTER_DATASET_KEYS
    assert "musicDifficulties" in MASTER_DATASET_KEYS


def test_card_cutout_asset_paths_are_supported():
    cache_paths = AssetCacheProvider.canonical_relative_paths(
        "jp",
        kind="card_cutout_normal",
        assetbundle="card_test",
    )
    assert cache_paths == [
        "jp/startapp/character/member_cutout/card_test/normal.png"
    ]

    provider = AssetProvider()
    urls = provider.build_candidate_urls(
        "jp",
        kind="card_cutout_after_training",
        assetbundle="card_test",
    )
    assert urls[0].endswith(
        "/startapp/character/member_cutout/card_test/after_training.png"
    )


def test_resource_settings_prefers_new_namespace(monkeypatch: pytest.MonkeyPatch):
    fake_config = _FakeResourceConfig(
        {
            "sekai_resource": {
                "SEKAI_RESOURCE_ASSET_SOURCE_ORDER": ["legacy-viewer"],
            },
            "moesekai": {
                "MOESEKAI_ASSET_SOURCE_ORDER": ["uni"],
            },
        }
    )

    monkeypatch.setattr(resource_config, "Config", fake_config)
    resource_config.get_settings.cache_clear()
    try:
        assert resource_config.get_settings().asset_source_order == ["legacy-viewer"]
    finally:
        resource_config.get_settings.cache_clear()


def test_resource_settings_falls_back_to_moesekai_legacy_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_config = _FakeResourceConfig(
        {
            "moesekai": {
                "MOESEKAI_AUDIO_FORMAT_PRIORITY": ["flac"],
            },
        }
    )

    monkeypatch.setattr(resource_config, "Config", fake_config)
    resource_config.get_settings.cache_clear()
    try:
        assert resource_config.get_settings().audio_format_priority == ["flac"]
    finally:
        resource_config.get_settings.cache_clear()


def test_resource_settings_falls_back_when_new_namespace_is_registered_default(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_config = _FakeResourceConfig(
        {
            "sekai_resource": {
                "SEKAI_RESOURCE_ASSET_SOURCE_ORDER": list(
                    resource_config.REGISTER_DEFAULTS.asset_source_order
                ),
            },
            "moesekai": {
                "MOESEKAI_ASSET_SOURCE_ORDER": ["legacy-viewer"],
            },
        }
    )

    monkeypatch.setattr(resource_config, "Config", fake_config)
    resource_config.get_settings.cache_clear()
    try:
        assert resource_config.get_settings().asset_source_order == ["legacy-viewer"]
    finally:
        resource_config.get_settings.cache_clear()


def test_resource_settings_reads_asset_fetch_concurrency(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_config = _FakeResourceConfig(
        {
            "sekai_resource": {
                "SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY": 16,
                "SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY": 6,
                "SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL": True,
            },
        }
    )

    monkeypatch.setattr(resource_config, "Config", fake_config)
    resource_config.get_settings.cache_clear()
    try:
        settings = resource_config.get_settings()
        assert settings.asset_batch_fetch_concurrency == 16
        assert settings.asset_source_fetch_concurrency == 6
        assert settings.asset_source_fetch_all is True
    finally:
        resource_config.get_settings.cache_clear()


def _full_master_payload(
    *,
    card_ids: list[int] | None = None,
    stamp_ids: list[int] | None = None,
) -> dict[str, list[dict[str, int]]]:
    payload = {dataset: [] for dataset in MASTER_DATASET_KEYS}
    payload["cards"] = [{"id": item_id} for item_id in card_ids or []]
    payload["stamps"] = [{"id": item_id} for item_id in stamp_ids or []]
    return payload


def _write_master_payloads(
    tmp_path,
    server: str,
    payloads: dict[str, list[dict[str, int]]],
) -> None:
    for dataset, payload in payloads.items():
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_get_current_event_next_first(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now()
    previous_event = {
        "id": 1,
        "startAt": int((now - timedelta(days=4)).timestamp() * 1000),
        "aggregateAt": int((now - timedelta(days=3)).timestamp() * 1000),
    }
    next_event = {
        "id": 2,
        "startAt": int((now + timedelta(days=1)).timestamp() * 1000),
        "aggregateAt": int((now + timedelta(days=2)).timestamp() * 1000),
    }

    async def fake_get_events(_cls: type[MasterDataService], server: str):
        assert server == "cn"
        return [previous_event, next_event]

    monkeypatch.setattr(
        MasterDataService,
        "get_events",
        classmethod(fake_get_events),
    )

    event = await master_data_service.get_current_event("cn", fallback="next_first")
    assert event is not None
    assert event["id"] == 2


def test_master_source_config_accepts_legacy_urls():
    config = MasterSourceConfig.model_validate(
        {
            "name": "legacy-jp",
            "region": "jp",
            "version_url": "https://example.com/master/versions.json",
            "events_url": "https://example.com/master/events.json",
            "version_field": "dataVersion",
        }
    )
    assert config.base_url == ""
    assert config.version_path == "https://example.com/master/versions.json"
    assert config.events_path == "https://example.com/master/events.json"
    assert config.version_url == "https://example.com/master/versions.json"
    assert config.events_url == "https://example.com/master/events.json"


def test_merge_master_sources_config_uses_new_default_order():
    merged = _merge_master_sources_config(
        [
            {
                "name": "haruki-jp",
                "region": "jp",
                "version_url": "https://example.com/haruki/versions/current_version.json",
                "events_url": "https://example.com/haruki/master/events.json",
                "version_field": "dataVersion",
            },
            {
                "name": "8823-jp",
                "region": "jp",
                "version_url": "https://example.com/8823/versions.json",
                "events_url": "https://example.com/8823/events.json",
                "version_field": "data_version",
            },
        ]
    )
    assert [item.name for item in merged[:6]] == [
        "8823-cn",
        "8823-jp",
        "8823-tw",
        "haruki-cn",
        "haruki-jp",
        "haruki-tw",
    ]
    assert any(item.name == "sekai-viewer-jp" for item in merged)


def test_merge_master_sources_config_backfills_new_default_datasets():
    legacy_datasets = {
        dataset: f"{dataset}.json"
        for dataset in MASTER_DATASET_KEYS
        if dataset != "gachas"
    }
    merged = _merge_master_sources_config(
        [
            {
                "name": "8823-jp",
                "family": "8823",
                "region": "jp",
                "owner": "custom-owner",
                "repo": "custom-repo",
                "branch": "custom-branch",
                "version_path": "versions.json",
                "version_field": "data_version",
                "datasets": legacy_datasets,
            }
        ]
    )

    source = next(item for item in merged if item.name == "8823-jp")

    assert source.owner == "custom-owner"
    assert source.dataset_path("gachas") == "gachas.json"
    assert source.dataset_url("gachas").endswith(
        "/custom-owner/custom-repo/custom-branch/gachas.json"
    )


@pytest.mark.asyncio
async def test_select_source_prefers_config_order_on_equal_versions(
    monkeypatch: pytest.MonkeyPatch,
):
    default_datasets = {key: f"{key}.json" for key in MASTER_DATASET_KEYS}
    settings = SimpleNamespace(
        master_source_order=["8823", "haruki", "sekai-viewer"],
        master_sources=[
            MasterSourceConfig(
                name="8823-jp",
                region="jp",
                base_url="https://example.com/8823",
                version_path="versions.json",
                version_field="data_version",
                datasets=default_datasets,
            ),
            MasterSourceConfig(
                name="haruki-jp",
                region="jp",
                base_url="https://example.com/haruki",
                version_path="versions/current_version.json",
                version_field="dataVersion",
                datasets={key: f"master/{key}.json" for key in MASTER_DATASET_KEYS},
            ),
            MasterSourceConfig(
                name="sekai-viewer-jp",
                region="jp",
                base_url="https://example.com/viewer",
                version_path="versions.json",
                version_field="dataVersion",
                datasets=default_datasets,
                auto_probe=False,
            ),
        ],
    )

    async def fake_fetch_source_snapshot(
        _cls: type[MasterDataService], source: MasterSourceConfig
    ) -> SourceVersionInfo:
        return SourceVersionInfo(
            source=source,
            version="6.0.0.1",
            success=True,
        )

    async def fake_fetch_selected_revision(
        _cls: type[MasterDataService], source: MasterSourceConfig
    ) -> str:
        assert source.name == "8823-jp"
        return "abc123"

    monkeypatch.setattr(masterdata_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_source_snapshot",
        classmethod(fake_fetch_source_snapshot),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_revision",
        classmethod(fake_fetch_selected_revision),
    )

    selected_source, source_versions = await MasterDataService._select_source(
        "jp",
        include_lazy=True,
    )

    assert selected_source is not None
    assert selected_source.name == "8823-jp"
    assert [item.source.name for item in source_versions] == [
        "8823-jp",
        "haruki-jp",
        "sekai-viewer-jp",
    ]


def test_probe_interval_seconds_uses_full_interval(monkeypatch: pytest.MonkeyPatch):
    settings = SimpleNamespace(
        master_check_interval_seconds=600,
        master_source_order=["8823", "haruki", "sekai-viewer"],
        master_sources=[],
    )
    monkeypatch.setattr(masterdata_module, "get_settings", lambda: settings)

    assert master_data_service.get_probe_interval_seconds() == 600


@pytest.mark.asyncio
async def test_fetch_selected_revision_uses_github_token(
    monkeypatch: pytest.MonkeyPatch,
):
    source = MasterSourceConfig(
        name="haruki-jp",
        region="jp",
        owner="Team-Haruki",
        repo="haruki-sekai-master",
        branch="main",
        version_path="versions/current_version.json",
        version_field="dataVersion",
        datasets={"events": "master/events.json"},
    )

    settings = SimpleNamespace(github_token="ghp_test_token")
    requests: list[dict[str, str]] = []
    updated_states: list[dict[str, str | None]] = []

    async def fake_get_probe_source_state(_cls, _name: str):
        return {"last_revision": "old-sha"}

    async def fake_update_probe_source_state(_cls, _name: str, **updates):
        updated_states.append(updates)
        return updates

    async def fake_get(url: str, **kwargs):
        requests.append(
            {
                "url": url,
                "Authorization": kwargs["headers"]["Authorization"],
                "Accept": kwargs["headers"]["Accept"],
                "X-GitHub-Api-Version": kwargs["headers"]["X-GitHub-Api-Version"],
                "accept_status_codes": str(kwargs.get("accept_status_codes")),
            }
        )
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"sha": "new-sha"},
        )

    monkeypatch.setattr(masterdata_module, "get_settings", lambda: settings)
    monkeypatch.setattr(masterdata_module.AsyncHttpx, "get", fake_get)
    monkeypatch.setattr(
        MasterDataService,
        "_get_probe_source_state",
        classmethod(fake_get_probe_source_state),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_update_probe_source_state",
        classmethod(fake_update_probe_source_state),
    )

    revision = await MasterDataService._fetch_selected_revision(source)

    assert revision == "new-sha"
    assert requests
    assert requests[0]["Authorization"] == "Bearer ghp_test_token"
    assert requests[0]["Accept"] == "application/vnd.github+json"
    assert requests[0]["X-GitHub-Api-Version"] == "2022-11-28"
    assert requests[0]["accept_status_codes"] == "(403, 429)"
    assert updated_states


@pytest.mark.asyncio
async def test_fetch_selected_revision_falls_back_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        owner="kotori8823",
        repo="sekai-master-db",
        branch="master",
        version_path="versions.json",
        version_field="data_version",
        datasets={"events": "events.json"},
    )

    settings = SimpleNamespace(github_token="")
    updated = False

    async def fake_get_probe_source_state(_cls, _name: str):
        return {"last_revision": "old-sha"}

    async def fake_update_probe_source_state(_cls, _name: str, **_updates):
        nonlocal updated
        updated = True
        return {}

    async def fake_get(url: str, **_kwargs):
        return httpx.Response(
            429,
            request=httpx.Request("GET", url),
            text="secondary rate limit",
        )

    monkeypatch.setattr(masterdata_module, "get_settings", lambda: settings)
    monkeypatch.setattr(masterdata_module.AsyncHttpx, "get", fake_get)
    monkeypatch.setattr(
        MasterDataService,
        "_get_probe_source_state",
        classmethod(fake_get_probe_source_state),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_update_probe_source_state",
        classmethod(fake_update_probe_source_state),
    )

    revision = await MasterDataService._fetch_selected_revision(source)

    assert revision == "old-sha"
    assert updated is False


@pytest.mark.asyncio
async def test_fetch_version_reports_context_for_non_json_body(
    monkeypatch: pytest.MonkeyPatch,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        owner="kotori8823",
        repo="sekai-master-db",
        branch="master",
        version_path="versions.json",
        version_field="data_version",
        datasets={"events": "events.json"},
    )

    async def fake_get(url: str, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="<html>bad gateway</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr(masterdata_module.AsyncHttpx, "get", fake_get)

    with pytest.raises(TypeError) as exc_info:
        await MasterDataService._fetch_version(source)

    message = str(exc_info.value)
    assert "version 接口返回的不是有效 JSON 对象" in message
    assert "source=8823-jp" in message
    assert "status=200" in message
    assert "content-type=text/html; charset=utf-8" in message
    assert "<html>bad gateway</html>" in message


@pytest.mark.asyncio
async def test_fetch_version_reports_context_for_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
):
    source = MasterSourceConfig(
        name="haruki-jp",
        region="jp",
        owner="Team-Haruki",
        repo="haruki-sekai-master",
        branch="main",
        version_path="versions/current_version.json",
        version_field="dataVersion",
        datasets={"events": "master/events.json"},
    )

    async def fake_get(url: str, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=["not", "an", "object"],
        )

    monkeypatch.setattr(masterdata_module.AsyncHttpx, "get", fake_get)

    with pytest.raises(TypeError) as exc_info:
        await MasterDataService._fetch_version(source)

    message = str(exc_info.value)
    assert "version 接口返回的不是 JSON 对象" in message
    assert "source=haruki-jp" in message
    assert "status=200" in message
    assert "content-type=application/json" in message
    assert 'body=["not","an","object"]' in message


@pytest.mark.asyncio
async def test_get_dataset_does_not_cache_missing_payload_on_failed_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    update_calls = 0
    original_cache = MasterDataService._cache
    MasterDataService._cache = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_update_region(
        _cls: type[MasterDataService],
        server: str,
        *,
        force: bool = False,
    ):
        nonlocal update_calls
        assert force is False
        update_calls += 1
        return RegionUpdateResult(server=server, error="下载失败")

    monkeypatch.setattr(
        MasterDataService,
        "_dataset_path",
        classmethod(fake_dataset_path),
    )
    monkeypatch.setattr(
        MasterDataService,
        "update_region",
        classmethod(fake_update_region),
    )

    try:
        first = await MasterDataService.get_dataset("jp", "events")
        second = await MasterDataService.get_dataset("jp", "events")
    finally:
        MasterDataService._cache = original_cache

    assert first == []
    assert second == []
    assert update_calls == 2
    assert ("jp", "events") not in MasterDataService._cache


@pytest.mark.asyncio
async def test_get_music_difficulties_uses_dedicated_dataset(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = [
        {
            "musicId": 1,
            "musicDifficulty": "master",
            "playLevel": 26,
            "totalNoteCount": 1147,
        }
    ]

    async def fake_get_dataset(
        _cls: type[MasterDataService], server: str, dataset: str
    ):
        assert (server, dataset) == ("jp", "musicDifficulties")
        return payload

    monkeypatch.setattr(
        MasterDataService,
        "get_dataset",
        classmethod(fake_get_dataset),
    )

    assert await MasterDataService.get_music_difficulties("jp") == payload


@pytest.mark.asyncio
async def test_update_region_first_landing_does_not_report_added_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    saved_state: dict[str, dict[str, object]] = {}
    original_cache = MasterDataService._cache
    MasterDataService._cache = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        _server: str,
        *,
        include_lazy: bool,
    ):
        assert include_lazy is False
        return source, [
            SourceVersionInfo(
                source=source,
                revision="rev-1",
                version="1.0.0",
                success=True,
            )
        ]

    async def fake_fetch_selected_payloads(
        _cls: type[MasterDataService],
        _source: MasterSourceConfig,
    ):
        return _full_master_payload(card_ids=[1], stamp_ids=[10]), "1.0.0"

    monkeypatch.setattr(
        MasterDataService, "_dataset_path", classmethod(fake_dataset_path)
    )
    monkeypatch.setattr(
        MasterDataService, "_select_source", classmethod(fake_select_source)
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fake_fetch_selected_payloads),
    )
    monkeypatch.setattr(MasterDataService, "_load_state", classmethod(lambda _cls: {}))
    monkeypatch.setattr(
        MasterDataService,
        "_save_state",
        classmethod(lambda _cls, payload: saved_state.update(payload)),
    )
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(master_check_mode="version"),
    )

    try:
        result = await MasterDataService.update_region("jp")
    finally:
        MasterDataService._cache = original_cache

    assert result.updated is True
    assert result.download_success is True
    assert result.added_records == {}
    assert result.changed_datasets == []
    assert saved_state["jp"]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_update_region_uses_legacy_moesekai_baseline_on_first_resource_landing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    current_root = tmp_path / "sekai_resource_master"
    legacy_master_root = tmp_path / "moesekai_master"
    legacy_state_root = tmp_path / "moesekai_state"
    _write_master_payloads(
        legacy_master_root,
        "jp",
        _full_master_payload(card_ids=[1], stamp_ids=[]),
    )
    legacy_state_root.mkdir(parents=True)
    (legacy_state_root / "master_state.json").write_text(
        json.dumps(
            {
                "jp": {
                    "source_name": "8823-jp",
                    "revision": "rev-1",
                    "version": "1.0.0",
                    "datasets": list(MASTER_DATASET_KEYS),
                }
            }
        ),
        encoding="utf-8",
    )
    saved_state: dict[str, dict[str, object]] = {}
    original_cache = MasterDataService._cache
    MasterDataService._cache = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = current_root / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        _server: str,
        *,
        include_lazy: bool,
    ):
        assert include_lazy is False
        return source, [
            SourceVersionInfo(
                source=source,
                revision="rev-2",
                version="1.0.1",
                success=True,
            )
        ]

    async def fake_fetch_selected_payloads(
        _cls: type[MasterDataService],
        _source: MasterSourceConfig,
    ):
        return _full_master_payload(card_ids=[1, 2], stamp_ids=[10]), "1.0.1"

    monkeypatch.setattr(
        MasterDataService, "_dataset_path", classmethod(fake_dataset_path)
    )
    monkeypatch.setattr(
        MasterDataService, "_select_source", classmethod(fake_select_source)
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fake_fetch_selected_payloads),
    )
    monkeypatch.setattr(MasterDataService, "_load_state", classmethod(lambda _cls: {}))
    monkeypatch.setattr(
        MasterDataService,
        "_save_state",
        classmethod(lambda _cls, payload: saved_state.update(payload)),
    )
    monkeypatch.setattr(masterdata_module, "LEGACY_MASTER_DATA_DIR", legacy_master_root)
    monkeypatch.setattr(masterdata_module, "LEGACY_STATE_DIR", legacy_state_root)
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(master_check_mode="version"),
    )

    try:
        result = await MasterDataService.update_region("jp")
    finally:
        MasterDataService._cache = original_cache

    assert result.updated is True
    assert result.added_records == {
        "cards": [{"id": 2}],
        "stamps": [{"id": 10}],
    }
    assert result.changed_datasets == ["cards", "stamps"]
    assert saved_state["jp"]["version"] == "1.0.1"


@pytest.mark.asyncio
async def test_update_region_reports_added_records_with_trusted_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    previous_payloads = _full_master_payload(card_ids=[1], stamp_ids=[])
    _write_master_payloads(tmp_path, "jp", previous_payloads)
    saved_state: dict[str, dict[str, object]] = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        _server: str,
        *,
        include_lazy: bool,
    ):
        assert include_lazy is False
        return source, [
            SourceVersionInfo(
                source=source,
                revision="rev-2",
                version="1.0.1",
                success=True,
            )
        ]

    async def fake_fetch_selected_payloads(
        _cls: type[MasterDataService],
        _source: MasterSourceConfig,
    ):
        return _full_master_payload(card_ids=[1, 2], stamp_ids=[10]), "1.0.1"

    monkeypatch.setattr(
        MasterDataService, "_dataset_path", classmethod(fake_dataset_path)
    )
    monkeypatch.setattr(
        MasterDataService, "_select_source", classmethod(fake_select_source)
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fake_fetch_selected_payloads),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_load_state",
        classmethod(
            lambda _cls: {
                "jp": {
                    "source_name": "8823-jp",
                    "revision": "rev-1",
                    "version": "1.0.0",
                    "datasets": list(MASTER_DATASET_KEYS),
                }
            }
        ),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_save_state",
        classmethod(lambda _cls, payload: saved_state.update(payload)),
    )
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(master_check_mode="version"),
    )

    result = await MasterDataService.update_region("jp")

    assert result.updated is True
    assert result.added_records == {
        "cards": [{"id": 2}],
        "stamps": [{"id": 10}],
    }
    assert result.changed_datasets == ["cards", "stamps"]
    assert saved_state["jp"]["version"] == "1.0.1"


@pytest.mark.asyncio
async def test_update_region_invalid_payload_does_not_write_partial_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    previous_payloads = _full_master_payload(card_ids=[1], stamp_ids=[10])
    _write_master_payloads(tmp_path, "jp", previous_payloads)
    saved_state: dict[str, dict[str, object]] = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        _server: str,
        *,
        include_lazy: bool,
    ):
        return source, [
            SourceVersionInfo(
                source=source,
                revision="rev-2",
                version="1.0.1",
                success=True,
            )
        ]

    async def fake_fetch_selected_payloads(
        _cls: type[MasterDataService],
        _source: MasterSourceConfig,
    ):
        payload = _full_master_payload(card_ids=[1, 2], stamp_ids=[10])
        payload["cards"] = {"id": 2}  # type: ignore[assignment]
        return payload, "1.0.1"

    monkeypatch.setattr(
        MasterDataService, "_dataset_path", classmethod(fake_dataset_path)
    )
    monkeypatch.setattr(
        MasterDataService, "_select_source", classmethod(fake_select_source)
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fake_fetch_selected_payloads),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_load_state",
        classmethod(
            lambda _cls: {
                "jp": {
                    "source_name": "8823-jp",
                    "revision": "rev-1",
                    "version": "1.0.0",
                    "datasets": list(MASTER_DATASET_KEYS),
                }
            }
        ),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_save_state",
        classmethod(lambda _cls, payload: saved_state.update(payload)),
    )
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(master_check_mode="version"),
    )

    result = await MasterDataService.update_region("jp")

    assert result.updated is False
    assert result.error is not None
    assert "TypeError" in result.error
    assert saved_state == {}
    for dataset, expected_payload in previous_payloads.items():
        path = fake_dataset_path(MasterDataService, "jp", dataset)
        assert json.loads(path.read_text(encoding="utf-8")) == expected_payload


@pytest.mark.asyncio
async def test_update_region_rolls_back_files_when_state_save_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    previous_payloads = _full_master_payload(card_ids=[1], stamp_ids=[10])
    _write_master_payloads(tmp_path, "jp", previous_payloads)

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        _server: str,
        *,
        include_lazy: bool,
    ):
        return source, [
            SourceVersionInfo(
                source=source,
                revision="rev-2",
                version="1.0.1",
                success=True,
            )
        ]

    async def fake_fetch_selected_payloads(
        _cls: type[MasterDataService],
        _source: MasterSourceConfig,
    ):
        return _full_master_payload(card_ids=[1, 2], stamp_ids=[10]), "1.0.1"

    def fail_save_state(_cls: type[MasterDataService], _payload):
        raise OSError("state save failed")

    monkeypatch.setattr(
        MasterDataService, "_dataset_path", classmethod(fake_dataset_path)
    )
    monkeypatch.setattr(
        MasterDataService, "_select_source", classmethod(fake_select_source)
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fake_fetch_selected_payloads),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_load_state",
        classmethod(
            lambda _cls: {
                "jp": {
                    "source_name": "8823-jp",
                    "revision": "rev-1",
                    "version": "1.0.0",
                    "datasets": list(MASTER_DATASET_KEYS),
                }
            }
        ),
    )
    monkeypatch.setattr(MasterDataService, "_save_state", classmethod(fail_save_state))
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(master_check_mode="version"),
    )

    result = await MasterDataService.update_region("jp")

    assert result.updated is False
    assert result.error is not None
    assert "OSError" in result.error
    for dataset, expected_payload in previous_payloads.items():
        path = fake_dataset_path(MasterDataService, "jp", dataset)
        assert json.loads(path.read_text(encoding="utf-8")) == expected_payload


@pytest.mark.asyncio
async def test_update_region_skips_auto_downgrade_when_local_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    source = MasterSourceConfig(
        name="8823-jp",
        region="jp",
        base_url="https://example.com/8823",
        version_path="versions.json",
        version_field="dataVersion",
        datasets={key: f"{key}.json" for key in MASTER_DATASET_KEYS},
    )
    original_cache = MasterDataService._cache
    MasterDataService._cache = {}
    saved_state: dict[str, dict[str, str]] = {}

    def fake_dataset_path(_cls: type[MasterDataService], server: str, dataset: str):
        path = tmp_path / server / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def fake_select_source(
        _cls: type[MasterDataService],
        server: str,
        *,
        include_lazy: bool,
    ):
        assert server == "jp"
        assert include_lazy is False
        return source, [
            SourceVersionInfo(
                source=source,
                revision="older-rev",
                version="6.0.0.1",
                success=True,
            )
        ]

    async def fail_fetch_selected_payloads(*_args, **_kwargs):
        raise AssertionError("自动路径遇到更低版本时不应因为缺文件而回退下载")

    for dataset in MASTER_DATASET_KEYS:
        if dataset == "events":
            continue
        fake_dataset_path(MasterDataService, "jp", dataset).write_text(
            "[]", encoding="utf-8"
        )

    monkeypatch.setattr(
        MasterDataService,
        "_dataset_path",
        classmethod(fake_dataset_path),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_select_source",
        classmethod(fake_select_source),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_fetch_selected_payloads",
        classmethod(fail_fetch_selected_payloads),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_load_state",
        classmethod(
            lambda _cls: {
                "jp": {
                    "source_name": "sekai-viewer-jp",
                    "revision": "newer-rev",
                    "version": "6.0.0.2",
                }
            }
        ),
    )
    monkeypatch.setattr(
        MasterDataService,
        "_save_state",
        classmethod(lambda _cls, payload: saved_state.update(payload)),
    )
    monkeypatch.setattr(
        masterdata_module,
        "get_settings",
        lambda: SimpleNamespace(
            master_check_mode="version",
            master_source_order=["8823", "haruki", "sekai-viewer"],
            master_sources=[source],
            github_token="",
            master_check_interval_seconds=180,
        ),
    )

    try:
        result = await MasterDataService.update_region("jp", force=False)
    finally:
        MasterDataService._cache = original_cache

    assert result.updated is False
    assert result.download_success is True
    assert result.current_version == "6.0.0.1"
    assert result.previous_version == "6.0.0.2"
    assert result.selection_note == "候选版本低于当前已落地版本，已跳过自动回退"
    assert not saved_state
    assert not fake_dataset_path(MasterDataService, "jp", "events").exists()


def test_region_update_result_message_includes_all_sources():
    result = RegionUpdateResult(
        server="jp",
        selected_source_name="8823-jp",
        previous_revision="old-rev",
        current_revision="new-rev",
        previous_version="6.3.5.10",
        current_version="6.3.5.11",
        updated=True,
        source_versions=[
            SourceVersionInfo(
                source=MasterSourceConfig(
                    name="8823-jp",
                    region="jp",
                    base_url="https://example.com/8823",
                    version_path="versions.json",
                    version_field="data_version",
                    datasets={"events": "events.json"},
                ),
                version="6.3.5.11",
                success=True,
            ),
            SourceVersionInfo(
                source=MasterSourceConfig(
                    name="haruki-jp",
                    region="jp",
                    base_url="https://example.com/haruki",
                    version_path="versions/current_version.json",
                    version_field="dataVersion",
                    datasets={"events": "master/events.json"},
                ),
                version="6.3.5.11",
                success=True,
            ),
        ],
    )
    message = result.to_message()
    assert "来源: 8823" in message
    assert "Revision: old-rev -> new-rev" in message
    assert "日服 MasterData 数据源" in message
    assert "[8823] 6.3.5.11" in message
    assert "[Haruki] 6.3.5.11" in message
