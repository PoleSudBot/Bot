from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import nonebot
from nonebot.exception import ActionFailed
import pytest

from zhenxun.utils.exception import RenderingError

nonebot.init()

from zhenxun.plugins.moesekai.adapters.results import MoeImageTextMessage
from zhenxun.plugins.moesekai.application import service as service_module
from zhenxun.plugins.moesekai.b30 import ConstantsTable, parse_constants_csv
from zhenxun.plugins.moesekai.providers.aliases import (
    AliasProfile,
)
from zhenxun.plugins.moesekai.providers.suite import (
    SuiteApiError,
    SuiteB30Data,
    SuiteProfile,
)
from zhenxun.plugins.moesekai.screenshot import ScreenshotError

FUTURE_RELEASE_AT_MS = 4_102_444_800_000
PAST_RELEASE_AT_MS = 1_700_000_000_000


def _auto_card(
    card_id: int,
    release_at: object = FUTURE_RELEASE_AT_MS,
) -> dict[str, object]:
    return {"id": card_id, "releaseAt": release_at}


@pytest.mark.asyncio
async def test_handle_update_uses_default_server(monkeypatch: pytest.MonkeyPatch):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    class FakeResult:
        def to_message(self) -> str:
            return "JP 已更新"

    async def fake_update_region(server: str, *, force: bool):
        assert server == "jp"
        assert force is True
        return FakeResult()

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "update_region",
        fake_update_region,
    )

    result = await service_module.moesekai_app.handle_update(
        "qq",
        "123456",
        None,
        update_all=False,
        is_superuser=False,
    )
    assert result == "JP 已更新"


@pytest.mark.asyncio
async def test_handle_update_dispatches_new_card_notifications_when_bot_available(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_resolve_default_server(*_args, **_kwargs):
        return "jp", None

    update_result = service_module.RegionUpdateResult(
        server="jp",
        updated=True,
        download_success=True,
        current_revision="rev-2",
        added_records={"cards": [{"id": 100}], "stamps": []},
    )

    async def fake_update_region(server: str, *, force: bool):
        assert (server, force) == ("jp", True)
        return update_result

    dispatched: dict[str, object] = {}

    async def fake_dispatch(bot, results):
        dispatched["bot"] = bot
        dispatched["results"] = results

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "update_region",
        fake_update_region,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_dispatch_manual_new_card_notifications",
        fake_dispatch,
    )

    bot = SimpleNamespace()
    result = await service_module.moesekai_app.handle_update(
        "qq",
        "123456",
        None,
        update_all=False,
        is_superuser=False,
        bot=bot,
    )

    assert "日服 已更新" in result
    assert dispatched == {"bot": bot, "results": [update_result]}


@pytest.mark.asyncio
async def test_handle_best30_uses_explicit_uid_without_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_is_uid_blacklisted(server: str, game_id: str, _is_superuser: bool):
        assert (server, game_id) == ("jp", "1234567890123")
        return None

    async def fake_capture(server: str, game_id: str):
        assert (server, game_id) == ("jp", "1234567890123")
        return b"best30"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_uid_blacklisted",
        fake_is_uid_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_capture_best30_image",
        fake_capture,
    )

    result = await service_module.moesekai_app.handle_best30(
        "qq",
        "1163272259",
        server="jp",
        game_id="1234567890123",
        target_user_id=None,
        is_superuser=False,
    )

    assert result == b"best30"


@pytest.mark.asyncio
async def test_handle_best30_rejects_at_target(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_binding_for_user",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("B30 @用户查询不应解析目标绑定")
        ),
    )

    result = await service_module.moesekai_app.handle_best30(
        "qq",
        "1163272259",
        server=None,
        game_id=None,
        target_user_id="2233",
        is_superuser=False,
    )

    assert result == "B30 不支持 @用户查询，请使用游戏ID或查询自己的绑定账号"


@pytest.mark.asyncio
async def test_build_best30_avatar_uses_default_deck_leader(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_get_card_image(
        server: str,
        assetbundle_name: str,
        *,
        after_training: bool,
        thumbnail: bool,
        timeout: float,
    ):
        assert server == "jp"
        assert assetbundle_name == "card_1182"
        assert after_training is False
        assert thumbnail is True
        assert timeout == 8
        return b"avatar"

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image",
        fake_get_card_image,
    )

    suite_data = SuiteB30Data(
        profile=SuiteProfile("6540035398873094", "kiyu", 495, 1779774627),
        music_results=[],
        user_decks=[
            {"deckId": 1, "leader": 1},
            {"deckId": 10, "leader": 1182},
        ],
        user_cards=[{"cardId": 1182, "defaultImage": "original"}],
        default_deck_id=10,
        source_url="https://suite-api.haruki.seiunx.com/public/jp/suite/6540035398873094",
    )

    avatar_uri = await service_module.moesekai_app._build_best30_avatar_uri(
        "jp",
        suite_data,
        [{"id": 1182, "assetbundleName": "card_1182"}],
    )

    assert avatar_uri == "data:image/png;base64,YXZhdGFy"


@pytest.mark.asyncio
async def test_capture_best30_image_returns_haruki_public_api_hint(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_get_b30_data(_server: str, _game_id: str):
        raise SuiteApiError("missing suite data")

    monkeypatch.setattr(
        service_module.suite_provider,
        "get_b30_data",
        fake_get_b30_data,
    )

    result = await service_module.moesekai_app._capture_best30_image("jp", "123")

    assert result == (
        "未查询到suite数据，请前往Haruki工具箱上传数据，"
        "并确保游戏账号管理处勾选了“允许公开API访问”"
    )


@pytest.mark.asyncio
async def test_build_best30_image_uses_official_level_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    suite_data = SuiteB30Data(
        profile=SuiteProfile("6540035398873094", "kiyu", 495, 1779774627),
        music_results=[
            {
                "musicId": 671,
                "musicDifficultyType": "append",
                "playResult": "all_perfect",
                "fullComboFlg": True,
                "fullPerfectFlg": True,
            },
            {
                "musicId": 999,
                "musicDifficultyType": "master",
                "playResult": "full_combo",
                "fullComboFlg": True,
                "fullPerfectFlg": False,
            },
        ],
        user_decks=[],
        user_cards=[],
        default_deck_id=0,
        source_url="https://suite-api.haruki.seiunx.com/public/jp/suite/6540035398873094",
    )
    table = ConstantsTable(
        parse_constants_csv(
            "Song,JP Name,Constant,Level,Note Count,Difficulty,Song ID,Notes\n"
            "CSV Song,CSV JP,38.1,APD 38,2800,Append,671,help\n"
        )
    )

    async def fake_get_b30_data(server: str, game_id: str):
        assert (server, game_id) == ("jp", "6540035398873094")
        return suite_data

    async def fake_get_table():
        return table

    async def fake_get_musics(server: str):
        assert server == "jp"
        return [
            {
                "id": 671,
                "title": "怪獣になりたい",
                "assetbundleName": "jacket_671",
                "publishedAt": 1,
            },
            {
                "id": 999,
                "title": "官方等级回退歌曲",
                "assetbundleName": "jacket_999",
                "publishedAt": 2,
            },
        ]

    async def fake_get_cards(server: str):
        assert server == "jp"
        return []

    async def fake_get_music_difficulties(server: str):
        assert server == "jp"
        return [
            {
                "musicId": 671,
                "musicDifficulty": "append",
                "playLevel": 37,
                "totalNoteCount": 2700,
            },
            {
                "musicId": 999,
                "musicDifficulty": "master",
                "playLevel": 35,
                "totalNoteCount": 1500,
            },
            {
                "musicId": 1000,
                "musicDifficulty": "master",
                "playLevel": 0,
                "totalNoteCount": 0,
            },
        ]

    async def fake_avatar(*_args, **_kwargs):
        return ""

    async def fake_get_music_jackets(
        server: str,
        assetbundle_names: list[str],
        *,
        timeout: float,
    ):
        assert (server, assetbundle_names, timeout) == (
            "jp",
            ["jacket_671", "jacket_999"],
            8,
        )
        return [b"jacket", b"official-jacket"]

    async def fake_render_best30_image(**kwargs):
        result = kwargs["result"]
        assert kwargs["sources"] == [
            "suite数据来源：Haruki工具箱",
            "定数来源：社区定数",
        ]
        community_entry, official_entry = result.entries
        assert community_entry.title == "怪獣になりたい"
        assert community_entry.level == 38
        assert community_entry.level_label == "38"
        assert community_entry.constant == 38.1
        assert community_entry.note_count == 2800
        assert community_entry.jacket_uri == "data:image/png;base64,amFja2V0"
        assert official_entry.title == "官方等级回退歌曲"
        assert official_entry.level == 35
        assert official_entry.level_label == "35"
        assert official_entry.constant == 35.0
        assert official_entry.note_count == 1500
        assert official_entry.user_rating == 34.0
        assert official_entry.jacket_uri == "data:image/png;base64,b2ZmaWNpYWwtamFja2V0"
        return b"best30"

    monkeypatch.setattr(
        service_module.suite_provider,
        "get_b30_data",
        fake_get_b30_data,
    )
    monkeypatch.setattr(
        service_module.b30_constants_provider,
        "get_table",
        fake_get_table,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_musics",
        fake_get_musics,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_cards",
        fake_get_cards,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_music_difficulties",
        fake_get_music_difficulties,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_best30_avatar_uri",
        fake_avatar,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "get_music_jackets",
        fake_get_music_jackets,
    )
    monkeypatch.setattr(
        service_module,
        "render_best30_image",
        fake_render_best30_image,
    )

    result = await service_module.moesekai_app._build_best30_image(
        "jp",
        "6540035398873094",
    )

    assert result == b"best30"


@pytest.mark.asyncio
async def test_handle_update_prefers_explicit_server_over_jp_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] == "cn"
        assert kwargs["fallback_jp"] is True
        return "cn", None

    class FakeResult:
        def to_message(self) -> str:
            return "CN 已更新"

    async def fake_update_region(server: str, *, force: bool):
        assert server == "cn"
        assert force is True
        return FakeResult()

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(service_module.master_data_provider, "update_region", fake_update_region)

    result = await service_module.moesekai_app.handle_update(
        "qq",
        "123456",
        "cn",
        update_all=False,
        is_superuser=False,
    )

    assert result == "CN 已更新"


@pytest.mark.asyncio
async def test_handle_update_all_requires_superuser(monkeypatch: pytest.MonkeyPatch):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )

    result = await service_module.moesekai_app.handle_update(
        "qq",
        "123456",
        None,
        update_all=True,
        is_superuser=False,
    )
    assert result == "pjsk update all 仅超级用户可用"


@pytest.mark.asyncio
async def test_handle_live_toggle_status_defaults_to_jp_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    async def fake_get_group_feature_toggle(*, platform: str, group_id: str, feature_name: str, server: str):
        assert platform == "qq"
        assert group_id == "654321"
        assert server == "jp"
        return SimpleNamespace(enabled=True)

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module,
        "get_group_feature_toggle",
        fake_get_group_feature_toggle,
    )

    result = await service_module.moesekai_app.handle_live_toggle(
        platform="qq",
        user_id="123456",
        group_id="654321",
        server=None,
        action="status",
        is_superuser=False,
        can_manage_group=False,
    )

    assert result == "日服 live提醒当前为：开启"


@pytest.mark.asyncio
async def test_handle_new_card_toggle_status_defaults_to_jp_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    async def fake_get_group_feature_toggle(*, server: str, **_kwargs):
        assert server == "jp"
        return SimpleNamespace(enabled=False)

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module,
        "get_group_feature_toggle",
        fake_get_group_feature_toggle,
    )

    result = await service_module.moesekai_app.handle_new_card_toggle(
        platform="qq",
        user_id="123456",
        group_id="654321",
        server=None,
        action="status",
        is_superuser=False,
        can_manage_group=False,
    )

    assert result == "日服 新卡上线提醒当前为：关闭"


@pytest.mark.asyncio
async def test_handle_live_subscription_defaults_to_jp_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    async def fake_upsert_user_feature_subscription(*, server: str, **_kwargs):
        assert server == "jp"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module,
        "upsert_user_feature_subscription",
        fake_upsert_user_feature_subscription,
    )

    result = await service_module.moesekai_app.handle_live_subscription(
        platform="qq",
        user_id="123456",
        group_id="654321",
        server=None,
        subscribe=True,
    )

    assert result == "已订阅 日服 live提醒"


@pytest.mark.asyncio
async def test_handle_test_live_reminder_defaults_to_jp_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    async def fake_get_virtual_lives(server: str):
        assert server == "jp"
        return [{"id": 999, "name": "测试 Live"}]

    async def fake_build_live_message(
        server: str,
        live: dict,
        *,
        start_time: datetime,
        countdown_minutes: int,
        title: str,
        remaining_count: int | None = None,
    ):
        assert server == "jp"
        assert live["id"] == 999
        assert title == "Live 提醒测试"
        assert start_time > datetime.now()
        assert countdown_minutes == 3
        assert remaining_count is None
        return "live-message"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_virtual_lives",
        fake_get_virtual_lives,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_live_message",
        fake_build_live_message,
    )

    result = await service_module.moesekai_app.handle_test_live_reminder(
        platform="qq",
        user_id="123456",
        server=None,
    )

    assert result == "live-message"


@pytest.mark.asyncio
async def test_build_live_message_uses_new_start_time_copy_and_remaining_count(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_get_virtual_live_banner(server: str, assetbundle_name: str):
        assert server == "jp"
        assert assetbundle_name == "banner001"
        return b"banner"

    async def fake_render_reminder_card(view_model):
        captured["view_model"] = view_model
        return b"rendered"

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_virtual_live_banner",
        fake_get_virtual_live_banner,
    )
    monkeypatch.setattr(
        service_module,
        "render_reminder_card",
        fake_render_reminder_card,
    )

    result = await service_module.moesekai_app._build_live_message(
        "jp",
        {"name": "测试 Live", "assetbundleName": "banner001"},
        start_time=datetime(2026, 4, 14, 20, 0),
        countdown_minutes=3,
        title="Live 即将开始",
        remaining_count=2,
    )

    view_model = captured["view_model"]
    assert view_model.title == "Live 即将开始"
    assert view_model.subtitle == "测试 Live"
    assert view_model.lines == [
        "【开始时间】2026-4-14 20:00（3分钟后）",
        "【剩余场数】2",
    ]
    assert "区服" not in "".join(view_model.lines)
    assert "提醒时间" not in "".join(view_model.lines)
    segments = list(result)
    assert len(segments) == 1
    assert segments[0].type == "image"


@pytest.mark.asyncio
async def test_build_live_message_omits_remaining_count_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_render_reminder_card(view_model):
        captured["view_model"] = view_model
        return b"rendered"

    monkeypatch.setattr(
        service_module,
        "render_reminder_card",
        fake_render_reminder_card,
    )

    await service_module.moesekai_app._build_live_message(
        "jp",
        {"name": "测试 Live"},
        start_time=datetime(2026, 4, 14, 20, 0),
        countdown_minutes=3,
        title="Live 即将开始",
    )

    view_model = captured["view_model"]
    assert view_model.lines == ["【开始时间】2026-4-14 20:00（3分钟后）"]


@pytest.mark.asyncio
async def test_dispatch_live_reminders_sends_start_reminder_image_before_mentions(
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime.now()
    first_start = now + timedelta(minutes=2, seconds=10)
    second_start = now + timedelta(minutes=20)
    third_start = now + timedelta(minutes=40)
    expected_first_start = datetime.fromtimestamp(int(first_start.timestamp() * 1000) / 1000)
    sent: dict[str, object] = {}
    build_calls: list[dict[str, object]] = []
    records: list[str] = []

    async def fake_toggles(*, feature_name: str):
        assert feature_name == service_module.FEATURE_LIVE_REMINDER
        return [SimpleNamespace(platform="qq", group_id="group-a", server="jp")]

    async def fake_get_virtual_lives(server: str):
        assert server == "jp"
        return [
            {
                "id": 100,
                "name": "测试 Live",
                "virtualLiveSchedules": [
                    {"startAt": int(first_start.timestamp() * 1000)},
                    {"startAt": int(second_start.timestamp() * 1000)},
                    {"startAt": int(third_start.timestamp() * 1000)},
                ],
            }
        ]

    async def fake_has_notification_record(**_kwargs):
        return False

    async def fake_list_subscribers(**_kwargs):
        return [SimpleNamespace(user_id="1001"), SimpleNamespace(user_id="1002")]

    async def fake_build_live_message(
        server: str,
        live: dict,
        *,
        start_time: datetime,
        countdown_minutes: int,
        title: str,
        remaining_count: int | None = None,
    ):
        assert server == "jp"
        assert live["id"] == 100
        build_calls.append(
            {
                "start_time": start_time,
                "countdown_minutes": countdown_minutes,
                "title": title,
                "remaining_count": remaining_count,
            }
        )
        return service_module.UniMessage([service_module.Image(raw=b"image-bytes")])

    async def fake_send_message(_bot, _user_id, group_id: str, message):
        sent["group_id"] = group_id
        sent["message"] = message

    async def fake_create_notification_record(*, record_key: str, **_kwargs):
        records.append(record_key)

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_virtual_lives",
        fake_get_virtual_lives,
    )
    monkeypatch.setattr(
        service_module,
        "has_notification_record",
        fake_has_notification_record,
    )
    monkeypatch.setattr(
        service_module,
        "list_user_feature_subscriptions",
        fake_list_subscribers,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_live_message",
        fake_build_live_message,
    )
    monkeypatch.setattr(
        service_module.PlatformUtils,
        "send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        service_module,
        "create_notification_record",
        fake_create_notification_record,
    )

    await service_module.moesekai_app.dispatch_live_reminders(bot=SimpleNamespace())

    assert build_calls == [
        {
            "start_time": expected_first_start,
            "countdown_minutes": 3,
            "title": "Live 即将开始",
            "remaining_count": 3,
        }
    ]
    assert sent["group_id"] == "group-a"
    segments = list(sent["message"])
    assert [segment.type for segment in segments] == ["image", "text", "at", "text", "at"]
    assert segments[1].text == "\n"
    assert segments[2].target == "1001"
    assert segments[3].text == " "
    assert segments[4].target == "1002"
    assert records == [f"100:first:{expected_first_start.isoformat()}"]


@pytest.mark.asyncio
async def test_dispatch_live_reminders_sends_second_last_stage_as_end_reminder(
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime.now()
    first_start = now + timedelta(minutes=1)
    second_last_start = now + timedelta(minutes=9, seconds=20)
    last_start = now + timedelta(minutes=30)
    expected_second_last_start = datetime.fromtimestamp(
        int(second_last_start.timestamp() * 1000) / 1000
    )
    build_calls: list[dict[str, object]] = []
    records: list[str] = []

    async def fake_toggles(*, feature_name: str):
        assert feature_name == service_module.FEATURE_LIVE_REMINDER
        return [SimpleNamespace(platform="qq", group_id="group-a", server="jp")]

    async def fake_get_virtual_lives(_server: str):
        return [
            {
                "id": 200,
                "name": "结束提醒测试",
                "virtualLiveSchedules": [
                    {"startAt": int(first_start.timestamp() * 1000)},
                    {"startAt": int(second_last_start.timestamp() * 1000)},
                    {"startAt": int(last_start.timestamp() * 1000)},
                ],
            }
        ]

    async def fake_has_notification_record(**_kwargs):
        return False

    async def fake_list_subscribers(**_kwargs):
        return []

    async def fake_build_live_message(
        _server: str,
        _live: dict,
        *,
        start_time: datetime,
        countdown_minutes: int,
        title: str,
        remaining_count: int | None = None,
    ):
        build_calls.append(
            {
                "start_time": start_time,
                "countdown_minutes": countdown_minutes,
                "title": title,
                "remaining_count": remaining_count,
            }
        )
        return service_module.UniMessage([service_module.Image(raw=b"image-bytes")])

    async def fake_send_message(_bot, _user_id, _group_id: str, message):
        segments = list(message)
        assert [segment.type for segment in segments] == ["image"]

    async def fake_create_notification_record(*, record_key: str, **_kwargs):
        records.append(record_key)

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_virtual_lives",
        fake_get_virtual_lives,
    )
    monkeypatch.setattr(
        service_module,
        "has_notification_record",
        fake_has_notification_record,
    )
    monkeypatch.setattr(
        service_module,
        "list_user_feature_subscriptions",
        fake_list_subscribers,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_live_message",
        fake_build_live_message,
    )
    monkeypatch.setattr(
        service_module.PlatformUtils,
        "send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        service_module,
        "create_notification_record",
        fake_create_notification_record,
    )

    await service_module.moesekai_app.dispatch_live_reminders(bot=SimpleNamespace())

    assert build_calls == [
        {
            "start_time": expected_second_last_start,
            "countdown_minutes": 10,
            "title": "Live 即将结束",
            "remaining_count": 2,
        }
    ]
    assert records == [f"200:second_last:{expected_second_last_start.isoformat()}"]


@pytest.mark.asyncio
async def test_dispatch_live_reminders_single_schedule_only_emits_start_stage(
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime.now()
    schedule_start = now + timedelta(minutes=2, seconds=5)
    expected_start_time = datetime.fromtimestamp(int(schedule_start.timestamp() * 1000) / 1000)
    build_titles: list[str] = []

    async def fake_toggles(*, feature_name: str):
        assert feature_name == service_module.FEATURE_LIVE_REMINDER
        return [SimpleNamespace(platform="qq", group_id="group-a", server="jp")]

    async def fake_get_virtual_lives(_server: str):
        return [
            {
                "id": 300,
                "name": "单场 Live",
                "virtualLiveSchedules": [
                    {"startAt": int(schedule_start.timestamp() * 1000)},
                ],
            }
        ]

    async def fake_has_notification_record(**_kwargs):
        return False

    async def fake_list_subscribers(**_kwargs):
        return []

    async def fake_build_live_message(
        _server: str,
        _live: dict,
        *,
        start_time: datetime,
        countdown_minutes: int,
        title: str,
        remaining_count: int | None = None,
    ):
        assert start_time == expected_start_time
        assert countdown_minutes == 3
        assert remaining_count == 1
        build_titles.append(title)
        return service_module.UniMessage([service_module.Image(raw=b"image-bytes")])

    async def fake_send_message(_bot, _user_id, _group_id: str, _message):
        return None

    async def fake_create_notification_record(**_kwargs):
        return None

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_virtual_lives",
        fake_get_virtual_lives,
    )
    monkeypatch.setattr(
        service_module,
        "has_notification_record",
        fake_has_notification_record,
    )
    monkeypatch.setattr(
        service_module,
        "list_user_feature_subscriptions",
        fake_list_subscribers,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_live_message",
        fake_build_live_message,
    )
    monkeypatch.setattr(
        service_module.PlatformUtils,
        "send_message",
        fake_send_message,
    )
    monkeypatch.setattr(
        service_module,
        "create_notification_record",
        fake_create_notification_record,
    )

    await service_module.moesekai_app.dispatch_live_reminders(bot=SimpleNamespace())

    assert build_titles == ["Live 即将开始"]


@pytest.mark.asyncio
async def test_dispatch_live_reminders_skips_when_notification_record_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime.now()
    start_time = now + timedelta(minutes=2, seconds=10)

    async def fake_toggles(*, feature_name: str):
        assert feature_name == service_module.FEATURE_LIVE_REMINDER
        return [SimpleNamespace(platform="qq", group_id="group-a", server="jp")]

    async def fake_get_virtual_lives(_server: str):
        return [
            {
                "id": 400,
                "name": "已提醒 Live",
                "virtualLiveSchedules": [
                    {"startAt": int(start_time.timestamp() * 1000)},
                ],
            }
        ]

    async def fake_has_notification_record(**_kwargs):
        return True

    async def fail_list_subscribers(**_kwargs):
        raise AssertionError("已存在记录时不应再读取订阅")

    async def fail_build_message(*_args, **_kwargs):
        raise AssertionError("已存在记录时不应构建提醒消息")

    async def fail_send_message(_bot, _user_id, _group_id: str, _message):
        raise AssertionError("已存在记录时不应发送消息")

    async def fail_create_record(**_kwargs):
        raise AssertionError("已存在记录时不应重复写入记录")

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_virtual_lives",
        fake_get_virtual_lives,
    )
    monkeypatch.setattr(
        service_module,
        "has_notification_record",
        fake_has_notification_record,
    )
    monkeypatch.setattr(
        service_module,
        "list_user_feature_subscriptions",
        fail_list_subscribers,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_build_live_message",
        fail_build_message,
    )
    monkeypatch.setattr(
        service_module.PlatformUtils,
        "send_message",
        fail_send_message,
    )
    monkeypatch.setattr(
        service_module,
        "create_notification_record",
        fail_create_record,
    )

    await service_module.moesekai_app.dispatch_live_reminders(bot=SimpleNamespace())


@pytest.mark.asyncio
async def test_handle_test_new_card_reminder_defaults_to_jp_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is True
        return "jp", None

    async def fake_get_cards(server: str):
        assert server == "jp"
        return [{"id": 100, "assetbundleName": "res001_no001", "releaseAt": 1}]

    async def fake_get_stamps(server: str):
        assert server == "jp"
        return [{"id": 200, "assetbundleName": "stamp001", "seq": 1}]

    async def fake_send_test_batches(*, bot, group_id: str, server: str, revision: str | None, specs):
        assert server == "jp"
        assert group_id == "178732453"
        assert revision == "test"
        captured["keys"] = [spec.key for spec in specs]
        return True

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(service_module.master_data_provider, "get_cards", fake_get_cards)
    monkeypatch.setattr(service_module.master_data_provider, "get_stamps", fake_get_stamps)
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_test_new_card_batches",
        fake_send_test_batches,
    )

    result = await service_module.moesekai_app.handle_test_new_card_reminder(
        bot=SimpleNamespace(),
        group_id="178732453",
        platform="qq",
        user_id="123456",
        server=None,
    )

    assert result is None
    assert captured["keys"] == ["test:summary", "test:card:100", "test:stamp:200"]


@pytest.mark.asyncio
async def test_handle_personal_archive_wraps_query_archive(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_handle_query_archive(
        platform: str,
        requester_user_id: str,
        *,
        server: str | None,
        game_id: str | None,
        target_user_id: str | None,
        is_superuser: bool,
    ):
        captured["platform"] = platform
        captured["requester_user_id"] = requester_user_id
        captured["server"] = server
        captured["game_id"] = game_id
        captured["target_user_id"] = target_user_id
        captured["is_superuser"] = is_superuser
        return "profile"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "handle_query_archive",
        fake_handle_query_archive,
    )

    result = await service_module.moesekai_app.handle_personal_archive(
        "qq",
        "123456",
        None,
        is_superuser=False,
    )

    assert result == "profile"
    assert captured == {
        "platform": "qq",
        "requester_user_id": "123456",
        "server": None,
        "game_id": None,
        "target_user_id": "123456",
        "is_superuser": False,
    }


@pytest.mark.asyncio
async def test_handle_query_archive_without_target_defaults_to_self_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_resolve_default_server(*_args, **kwargs):
        assert kwargs["explicit_server"] is None
        assert kwargs["fallback_jp"] is False
        return "jp", None

    async def fake_get_user_binding(platform: str, user_id: str, server: str):
        assert platform == "qq"
        assert user_id == "123456"
        assert server == "jp"
        return SimpleNamespace(server="jp", game_id="1234567890123")

    async def fake_is_uid_blacklisted(*_args, **_kwargs):
        return None

    async def fake_capture_profile_image(server: str, game_id: str):
        assert server == "jp"
        assert game_id == "1234567890123"
        return b"profile-image"

    async def fail_resolve_binding_for_user(*_args, **_kwargs):
        raise AssertionError("查自己时不应走公开档案分享校验")

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(
        service_module,
        "get_user_binding",
        fake_get_user_binding,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_uid_blacklisted",
        fake_is_uid_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_capture_profile_image",
        fake_capture_profile_image,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_binding_for_user",
        fail_resolve_binding_for_user,
    )

    result = await service_module.moesekai_app.handle_query_archive(
        platform="qq",
        requester_user_id="123456",
        server=None,
        game_id=None,
        target_user_id=None,
        is_superuser=False,
    )

    assert result == b"profile-image"


@pytest.mark.asyncio
async def test_capture_profile_image_falls_back_to_screenshot_once(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class _FakeSettings:
        profile_render_mode = "internal_first"

    async def fake_render_profile_image(server: str, game_id: str):
        calls.append(f"render:{server}:{game_id}")
        raise RenderingError("render failed")

    async def fake_capture_profile(server: str, game_id: str):
        calls.append(f"screenshot:{server}:{game_id}")
        return b"legacy-profile"

    monkeypatch.setattr(service_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(service_module, "render_profile_image", fake_render_profile_image)
    monkeypatch.setattr(service_module.screenshot_service, "capture_profile", fake_capture_profile)

    result = await service_module.moesekai_app._capture_profile_image(
        "jp",
        "1234567890123",
    )

    assert result == b"legacy-profile"
    assert calls == [
        "render:jp:1234567890123",
        "screenshot:jp:1234567890123",
    ]


@pytest.mark.asyncio
async def test_handle_query_archive_uses_internal_profile_capture_for_explicit_uid(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_is_uid_blacklisted(*_args, **_kwargs):
        return None

    async def fake_capture_profile_image(server: str, game_id: str):
        assert server == "jp"
        assert game_id == "1234567890123"
        return b"profile-image"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_uid_blacklisted",
        fake_is_uid_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_capture_profile_image",
        fake_capture_profile_image,
    )

    result = await service_module.moesekai_app.handle_query_archive(
        platform="qq",
        requester_user_id="123456",
        server="jp",
        game_id="1234567890123",
        target_user_id=None,
        is_superuser=False,
    )

    assert result == b"profile-image"


@pytest.mark.asyncio
async def test_handle_default_server_still_requires_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_get_or_create_user_settings(*_args, **_kwargs):
        return SimpleNamespace(default_server=None)

    async def fake_list_user_bindings(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module,
        "get_or_create_user_settings",
        fake_get_or_create_user_settings,
    )
    monkeypatch.setattr(
        service_module,
        "list_user_bindings",
        fake_list_user_bindings,
    )

    result = await service_module.moesekai_app.handle_default_server(
        "qq",
        "123456",
        None,
        is_superuser=False,
    )

    assert result == "你还没有绑定任何账号"


@pytest.mark.asyncio
async def test_archive_and_best30_unbound_prompts_add_nickname(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_is_qq_blacklisted(*_args, **_kwargs):
        return None

    async def fake_resolve_default_server(*_args, **_kwargs):
        return None, "未绑定"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_is_qq_blacklisted",
        fake_is_qq_blacklisted,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )

    archive_result = await service_module.moesekai_app.handle_query_archive(
        platform="qq",
        requester_user_id="123456",
        server=None,
        game_id=None,
        target_user_id=None,
        is_superuser=False,
    )
    best30_result = await service_module.moesekai_app.handle_best30(
        platform="qq",
        requester_user_id="123456",
        server=None,
        game_id=None,
        target_user_id=None,
        is_superuser=False,
    )

    expected = "[南极] 你还没有绑定任何账号，请先使用“绑定 [区服] <游戏ID>”"
    assert archive_result == expected
    assert best30_result == expected


@pytest.mark.asyncio
async def test_handle_story_appends_bilibili_url(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_event_by_id(*_args, **_kwargs):
        return {"id": 199, "name": "剧情活动"}

    async def fake_capture_story(event_id: int):
        assert event_id == 199
        return b"story-image"

    async def fake_get_bvid(event_id: int):
        assert event_id == 199
        return "https://www.bilibili.com/video/BV1xx411c7mD"

    cache_set_calls: list[tuple[int, bytes]] = []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_get_event_by_id",
        fake_get_event_by_id,
    )
    monkeypatch.setattr(
        service_module.story_cache_provider,
        "get",
        lambda event_id: None,
    )
    monkeypatch.setattr(
        service_module.story_cache_provider,
        "set",
        lambda event_id, payload: cache_set_calls.append((event_id, payload)),
    )
    monkeypatch.setattr(service_module.screenshot_service, "capture_story", fake_capture_story)
    monkeypatch.setattr(service_module.hub_provider, "get_event_bilibili_url", fake_get_bvid)

    result = await service_module.moesekai_app.handle_story(199)
    assert isinstance(result, MoeImageTextMessage)
    assert result.image_bytes == b"story-image"
    assert result.text == "B站链接：https://www.bilibili.com/video/BV1xx411c7mD"
    assert cache_set_calls == [(199, b"story-image")]


@pytest.mark.asyncio
async def test_handle_story_uses_cache_when_available(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_event_by_id(*_args, **_kwargs):
        return {"id": 199, "name": "剧情活动"}

    async def fail_capture_story(_event_id: int):
        raise AssertionError("命中剧情缓存时不应再次截图")

    async def fake_get_bvid(event_id: int):
        assert event_id == 199
        return "https://www.bilibili.com/video/BV1xx411c7mD"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_get_event_by_id",
        fake_get_event_by_id,
    )
    monkeypatch.setattr(
        service_module.story_cache_provider,
        "get",
        lambda event_id: b"cached-story" if event_id == 199 else None,
    )
    monkeypatch.setattr(service_module.screenshot_service, "capture_story", fail_capture_story)
    monkeypatch.setattr(service_module.hub_provider, "get_event_bilibili_url", fake_get_bvid)

    result = await service_module.moesekai_app.handle_story(199)
    assert isinstance(result, MoeImageTextMessage)
    assert result.image_bytes == b"cached-story"
    assert result.text == "B站链接：https://www.bilibili.com/video/BV1xx411c7mD"


@pytest.mark.asyncio
async def test_handle_story_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_event_by_id(*_args, **_kwargs):
        return {"id": 199, "name": "剧情活动"}

    async def fake_capture_story(event_id: int):
        assert event_id == 199
        return b"fresh-story"

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_get_event_by_id",
        fake_get_event_by_id,
    )
    monkeypatch.setattr(
        service_module.story_cache_provider,
        "get",
        lambda event_id: b"cached-story" if event_id == 199 else None,
    )
    monkeypatch.setattr(
        service_module.story_cache_provider,
        "set",
        lambda _event_id, _payload: None,
    )
    monkeypatch.setattr(service_module.screenshot_service, "capture_story", fake_capture_story)

    async def fake_get_bvid_none(_event_id: int):
        return None

    monkeypatch.setattr(
        service_module.hub_provider,
        "get_event_bilibili_url",
        fake_get_bvid_none,
    )

    result = await service_module.moesekai_app.handle_story(199, force_refresh=True)
    assert isinstance(result, MoeImageTextMessage)
    assert result.image_bytes == b"fresh-story"
    assert result.text is None


@pytest.mark.asyncio
async def test_handle_story_deduplicates_inflight_capture(monkeypatch: pytest.MonkeyPatch):
    app = service_module.MoeSekaiApplication()
    release = asyncio.Event()
    capture_calls = 0

    async def fake_get_event_by_id(*_args, **_kwargs):
        return {"id": 199, "name": "剧情活动"}

    async def fake_capture_story(event_id: int):
        nonlocal capture_calls
        assert event_id == 199
        capture_calls += 1
        await release.wait()
        return b"shared-story"

    monkeypatch.setattr(app, "_get_event_by_id", fake_get_event_by_id)
    monkeypatch.setattr(service_module.story_cache_provider, "get", lambda _event_id: None)
    monkeypatch.setattr(service_module.story_cache_provider, "set", lambda *_args: None)
    monkeypatch.setattr(service_module.screenshot_service, "capture_story", fake_capture_story)

    async def fake_get_bvid_none(_event_id: int):
        return None

    monkeypatch.setattr(service_module.hub_provider, "get_event_bilibili_url", fake_get_bvid_none)

    first = asyncio.create_task(app.handle_story(199))
    second = asyncio.create_task(app.handle_story(199))
    await asyncio.sleep(0.05)
    release.set()
    left, right = await asyncio.gather(first, second)

    assert isinstance(left, MoeImageTextMessage)
    assert isinstance(right, MoeImageTextMessage)
    assert left.image_bytes == b"shared-story"
    assert right.image_bytes == b"shared-story"
    assert capture_calls == 1


@pytest.mark.asyncio
async def test_handle_random_manga_downloads_image(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_random_manga():
        return {
            "id": 351,
            "image_url": "https://moe.exmeaning.com/mangas/351.png",
            "url": "https://www.bilibili.com/opus/1183974551994761216",
        }

    async def fake_get_content(url: str):
        assert url.endswith("/351.png")
        return b"manga-image"

    monkeypatch.setattr(service_module.hub_provider, "get_random_manga", fake_get_random_manga)
    monkeypatch.setattr(service_module.AsyncHttpx, "get_content", fake_get_content)

    result = await service_module.moesekai_app.handle_random_manga()
    assert isinstance(result, MoeImageTextMessage)
    assert result.image_bytes == b"manga-image"
    assert "B站链接：" in (result.text or "")
    assert "1183974551994761216" in (result.text or "")


@pytest.mark.asyncio
async def test_handle_manga_by_id_returns_not_found(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_manga_by_id(_manga_id: int):
        return None

    monkeypatch.setattr(service_module.hub_provider, "get_manga_by_id", fake_get_manga_by_id)

    result = await service_module.moesekai_app.handle_manga_by_id(999999)
    assert result == "未找到第 999999 话四格漫画"


@pytest.mark.asyncio
async def test_handle_manga_by_id_returns_image_and_link(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_manga_by_id(manga_id: int):
        assert manga_id == 351
        return {
            "id": 351,
            "image_url": "https://moe.exmeaning.com/mangas/351.png",
            "url": "https://www.bilibili.com/opus/1183974551994761216",
        }

    async def fake_get_content(url: str):
        assert url.endswith("/351.png")
        return b"manga-image"

    monkeypatch.setattr(service_module.hub_provider, "get_manga_by_id", fake_get_manga_by_id)
    monkeypatch.setattr(service_module.AsyncHttpx, "get_content", fake_get_content)

    result = await service_module.moesekai_app.handle_manga_by_id(351)
    assert isinstance(result, MoeImageTextMessage)
    assert result.image_bytes == b"manga-image"
    assert "B站链接：" in (result.text or "")


@pytest.mark.asyncio
async def test_handle_character_uses_cache_when_available(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_character_id(*_args, **_kwargs):
        return "21"

    async def fake_capture_character(character_id: int):
        raise AssertionError(f"命中缓存后不应再次截图: {character_id}")

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_character_id",
        fake_resolve_character_id,
    )
    monkeypatch.setattr(
        service_module.character_cache_provider,
        "get",
        lambda character_id: b"cached-character" if character_id == 21 else None,
    )
    monkeypatch.setattr(
        service_module.screenshot_service,
        "capture_character",
        fake_capture_character,
    )

    result = await service_module.moesekai_app.handle_character("初音未来")
    assert result == b"cached-character"


@pytest.mark.asyncio
async def test_handle_character_returns_screenshot_and_updates_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_character_id(*_args, **_kwargs):
        return "21"

    async def fake_capture_character(character_id: int):
        assert character_id == 21
        return b"character-image"

    set_calls: list[tuple[int, bytes]] = []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_character_id",
        fake_resolve_character_id,
    )
    monkeypatch.setattr(service_module.character_cache_provider, "get", lambda _character_id: None)
    monkeypatch.setattr(
        service_module.character_cache_provider,
        "set",
        lambda character_id, payload: set_calls.append((character_id, payload)),
    )
    monkeypatch.setattr(
        service_module.screenshot_service,
        "capture_character",
        fake_capture_character,
    )

    result = await service_module.moesekai_app.handle_character("初音未来")
    assert result == b"character-image"
    assert set_calls == [(21, b"character-image")]


@pytest.mark.asyncio
async def test_handle_character_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_character_id(*_args, **_kwargs):
        return "21"

    async def fake_capture_character(character_id: int):
        assert character_id == 21
        return b"fresh-character"

    set_calls: list[tuple[int, bytes]] = []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_character_id",
        fake_resolve_character_id,
    )
    monkeypatch.setattr(
        service_module.character_cache_provider,
        "get",
        lambda _character_id: b"stale-character",
    )
    monkeypatch.setattr(
        service_module.character_cache_provider,
        "set",
        lambda character_id, payload: set_calls.append((character_id, payload)),
    )
    monkeypatch.setattr(
        service_module.screenshot_service,
        "capture_character",
        fake_capture_character,
    )

    result = await service_module.moesekai_app.handle_character(
        "初音未来",
        force_refresh=True,
    )
    assert result == b"fresh-character"
    assert set_calls == [(21, b"fresh-character")]


@pytest.mark.asyncio
async def test_handle_character_deduplicates_inflight_capture(
    monkeypatch: pytest.MonkeyPatch,
):
    app = service_module.MoeSekaiApplication()
    release = asyncio.Event()
    capture_calls = 0

    async def fake_resolve_character_id(*_args, **_kwargs):
        return "21"

    async def fake_capture_character(character_id: int):
        nonlocal capture_calls
        assert character_id == 21
        capture_calls += 1
        await release.wait()
        return b"shared-character"

    monkeypatch.setattr(app, "_resolve_character_id", fake_resolve_character_id)
    monkeypatch.setattr(service_module.character_cache_provider, "get", lambda _character_id: None)
    monkeypatch.setattr(service_module.character_cache_provider, "set", lambda *_args: None)
    monkeypatch.setattr(
        service_module.screenshot_service,
        "capture_character",
        fake_capture_character,
    )

    first = asyncio.create_task(app.handle_character("初音未来"))
    second = asyncio.create_task(app.handle_character("初音未来"))
    await asyncio.sleep(0.05)
    release.set()
    left, right = await asyncio.gather(first, second)

    assert left == b"shared-character"
    assert right == b"shared-character"
    assert capture_calls == 1


@pytest.mark.asyncio
async def test_handle_character_does_not_overwrite_cache_on_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_character_id(*_args, **_kwargs):
        return "21"

    async def fake_capture_character(_character_id: int):
        raise ScreenshotError("查角色", ["页面加载失败"])

    set_calls: list[tuple[int, bytes]] = []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_character_id",
        fake_resolve_character_id,
    )
    monkeypatch.setattr(service_module.character_cache_provider, "get", lambda _character_id: None)
    monkeypatch.setattr(
        service_module.character_cache_provider,
        "set",
        lambda character_id, payload: set_calls.append((character_id, payload)),
    )
    monkeypatch.setattr(
        service_module.screenshot_service,
        "capture_character",
        fake_capture_character,
    )

    result = await service_module.moesekai_app.handle_character("初音未来")
    assert result == "查角色截图失败，请稍后重试"
    assert set_calls == []


@pytest.mark.asyncio
async def test_sync_music_aliases_uses_bulk_index(monkeypatch: pytest.MonkeyPatch):
    saved_state: dict[str, object] = {}
    refresh_calls: list[list[dict[str, object]]] = []

    monkeypatch.setattr(service_module._ALIAS_STATE, "load", lambda default: {})
    monkeypatch.setattr(
        service_module._ALIAS_STATE,
        "save",
        lambda payload: saved_state.update(payload),
    )

    async def fake_get_music_alias_index():
        return [
            {
                "music_id": 1,
                "title": "Tell Your World",
                "aliases": ["tyw", "告诉你的世界"],
            },
            {
                "music_id": 2,
                "title": "ロキ",
                "aliases": [],
            },
        ]

    def fake_refresh_music_alias_snapshot(items):
        refresh_calls.append(items)
        return items

    async def fail_get_music_aliases(*_args, **_kwargs):
        raise AssertionError("同步歌曲别名时不应再逐曲请求 API")

    monkeypatch.setattr(
        service_module.hub_provider,
        "get_music_alias_index",
        fake_get_music_alias_index,
    )
    monkeypatch.setattr(
        service_module.hub_provider,
        "get_music_aliases",
        fail_get_music_aliases,
    )
    monkeypatch.setattr(
        service_module.alias_provider,
        "refresh_music_alias_snapshot",
        fake_refresh_music_alias_snapshot,
    )

    await service_module.moesekai_app.sync_music_aliases(force=True)

    assert refresh_calls == [
        [
            {
                "music_id": 1,
                "title": "Tell Your World",
                "aliases": ["tyw", "告诉你的世界"],
            },
            {
                "music_id": 2,
                "title": "ロキ",
                "aliases": [],
            },
        ]
    ]
    assert saved_state["music_alias_source"] == "MoeSekai-Hub/music_aliases.json"
    assert saved_state["music_alias_sync_count"] == 2
    assert "music_alias_sync_at" in saved_state


@pytest.mark.asyncio
async def test_seed_default_aliases_is_noop(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        service_module.hub_provider,
        "read_character_alias_seed",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("seed_default_aliases 不应再读取角色 seed 并写库")
        ),
    )

    async def fail_get_game_characters(*_args, **_kwargs):
        raise AssertionError("seed_default_aliases 不应读取角色 masterdata")

    async def fail_get_musics(*_args, **_kwargs):
        raise AssertionError("seed_default_aliases 不应读取歌曲 masterdata")

    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_game_characters",
        fail_get_game_characters,
    )
    monkeypatch.setattr(
        service_module.master_data_provider,
        "get_musics",
        fail_get_musics,
    )

    await service_module.MoeSekaiApplication().seed_default_aliases()


@pytest.mark.asyncio
async def test_handle_test_new_card_reminder_returns_clear_message_when_no_media(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **_kwargs):
        return "jp", None

    async def fake_get_cards(_server: str):
        return [
            {
                "id": 100,
                "assetbundleName": "res001_no001",
                "cardRarityType": "rarity_4",
                "specialTrainingCosts": [{"resourceId": 1}],
                "releaseAt": 100,
            },
            {
                "id": 101,
                "assetbundleName": "res001_no002",
                "cardRarityType": "rarity_3",
                "specialTrainingCosts": [{"resourceId": 1}],
                "releaseAt": 200,
            },
        ]

    async def fake_get_stamps(_server: str):
        return [
            {
                "id": 200,
                "assetbundleName": "stamp200",
                "seq": 200,
            }
        ]

    async def fake_get_card_image(*_args, **_kwargs):
        return None

    async def fake_get_stamp_image(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(service_module.master_data_provider, "get_cards", fake_get_cards)
    monkeypatch.setattr(service_module.master_data_provider, "get_stamps", fake_get_stamps)
    monkeypatch.setattr(service_module.asset_provider, "get_card_image", fake_get_card_image)
    monkeypatch.setattr(service_module.asset_provider, "get_stamp_image", fake_get_stamp_image)

    result = await service_module.moesekai_app.handle_test_new_card_reminder(
        bot=SimpleNamespace(),
        group_id="178732453",
        platform="qq",
        user_id="123456",
        server="jp",
    )

    assert result == "当前暂无可用的新卡上线提醒测试样本，请稍后重试或显式指定 card_id"


@pytest.mark.asyncio
async def test_handle_test_new_card_reminder_reports_missing_explicit_card_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_resolve_default_server(*_args, **_kwargs):
        return "jp", None

    async def fake_get_cards(_server: str):
        return [{"id": 100, "assetbundleName": "res001_no001"}]

    async def fake_get_stamps(_server: str):
        return []

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_resolve_default_server",
        fake_resolve_default_server,
    )
    monkeypatch.setattr(service_module.master_data_provider, "get_cards", fake_get_cards)
    monkeypatch.setattr(service_module.master_data_provider, "get_stamps", fake_get_stamps)

    result = await service_module.moesekai_app.handle_test_new_card_reminder(
        bot=SimpleNamespace(),
        group_id="178732453",
        platform="qq",
        user_id="123456",
        server="jp",
        card_ids=[947, 948],
    )

    assert result == "未找到指定的新卡测试数据"


@pytest.mark.asyncio
async def test_prepare_new_card_summary_item_uses_generic_summary_lines(
):
    item = await service_module.moesekai_app._prepare_new_card_summary_item(
        server="jp",
        revision="rev-summary",
        key="rev-summary:summary",
    )

    expected = "\n".join(
        [
            "检测到 日服 新卡/表情更新，上传中..",
        ]
    )
    assert str(item.message) == expected
    assert item.estimated_bytes == len(expected.encode("utf-8"))


@pytest.mark.asyncio
async def test_dispatch_new_card_notifications_replays_pending_snapshot_without_fresh_updates(
    monkeypatch: pytest.MonkeyPatch,
):
    base_key = "rev-pending"
    summary_key = f"{base_key}:summary"
    card_key = f"{base_key}:card:100"
    pending_payload = {
        "jp": {
            base_key: {
                "current_revision": base_key,
                "current_version": None,
                "cards": [_auto_card(100)],
                "stamps": [],
            }
        }
    }

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fake_toggles(*, feature_name: str, server: str):
        return [SimpleNamespace(platform="qq", group_id="group-a")]

    async def fake_sent_keys(**_kwargs):
        return {("qq", "group-a"): set()}

    async def fake_batches(*, server: str, specs):
        assert server == "jp"
        assert [spec.key for spec in specs] == [summary_key, card_key]
        yield [
            service_module._NewCardReminderPreparedItem(
                key=summary_key,
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=card_key,
                kind="card",
                message="card",
                estimated_bytes=1,
            ),
        ]

    plain_calls: list[tuple[str, list[str]]] = []

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        plain_calls.append((target_state.group_id, [item.key for item in items]))
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(service_module, "list_enabled_group_feature_toggles", fake_toggles)
    monkeypatch.setattr(service_module, "list_notification_record_keys_by_groups", fake_sent_keys)
    monkeypatch.setattr(service_module.moesekai_app, "_iter_new_card_batches", fake_batches)
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[],
    )

    assert plain_calls == [
        ("group-a", [summary_key]),
        ("group-a", [card_key]),
    ]
    assert pending_payload == {}


@pytest.mark.asyncio
async def test_dispatch_new_card_notifications_keeps_pending_snapshot_after_failed_round_and_replays_next_round(
    monkeypatch: pytest.MonkeyPatch,
):
    base_key = "rev-replay"
    summary_key = f"{base_key}:summary"
    card_key = f"{base_key}:card:100"
    result = service_module.RegionUpdateResult(
        server="jp",
        updated=True,
        download_success=True,
        current_revision=base_key,
        added_records={"cards": [_auto_card(100)], "stamps": []},
    )
    pending_payload: dict[str, dict[str, dict[str, object]]] = {}
    replay_round = {"value": 0}

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fake_toggles(*, feature_name: str, server: str):
        return [SimpleNamespace(platform="qq", group_id="group-a")]

    async def fake_sent_keys(**_kwargs):
        return {("qq", "group-a"): set()}

    async def fake_batches(*, server: str, specs):
        assert server == "jp"
        yield [
            service_module._NewCardReminderPreparedItem(
                key=summary_key,
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=card_key,
                kind="card",
                message="card",
                estimated_bytes=1,
            ),
        ]

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        if replay_round["value"] == 0:
            target_state.aborted = True
            return
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(service_module, "list_enabled_group_feature_toggles", fake_toggles)
    monkeypatch.setattr(service_module, "list_notification_record_keys_by_groups", fake_sent_keys)
    monkeypatch.setattr(service_module.moesekai_app, "_iter_new_card_batches", fake_batches)
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[result],
    )

    assert pending_payload == {
        "jp": {
            base_key: {
                "current_revision": base_key,
                "current_version": None,
                "cards": [_auto_card(100)],
                "stamps": [],
            }
        }
    }

    replay_round["value"] = 1
    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[],
    )

    assert pending_payload == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cards",
    [
        [_auto_card(100, PAST_RELEASE_AT_MS)],
        [_auto_card(100, PAST_RELEASE_AT_MS), _auto_card(101, FUTURE_RELEASE_AT_MS)],
        [_auto_card(100, "invalid")],
        [_auto_card(100, 0)],
        [{"id": 100}],
    ],
)
async def test_dispatch_new_card_notifications_drops_stale_pending_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    cards: list[dict[str, object]],
):
    base_key = "rev-expired"
    pending_payload = {
        "jp": {
            base_key: {
                "current_revision": base_key,
                "current_version": None,
                "cards": cards,
                "stamps": [{"id": 200}],
            }
        }
    }

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fail_toggles(**_kwargs):
        raise AssertionError("releaseAt 校验失败时不应继续加载群开关")

    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fail_toggles,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[],
    )

    assert pending_payload == {}


@pytest.mark.asyncio
async def test_dispatch_new_card_notifications_sends_stamps_with_unreleased_card_batch(
    monkeypatch: pytest.MonkeyPatch,
):
    base_key = "rev-stamp"
    summary_key = f"{base_key}:summary"
    card_key = f"{base_key}:card:100"
    stamp_key = f"{base_key}:stamp:200"
    result = service_module.RegionUpdateResult(
        server="jp",
        updated=True,
        download_success=True,
        current_revision=base_key,
        added_records={
            "cards": [_auto_card(100)],
            "stamps": [{"id": 200}],
        },
    )
    pending_payload: dict[str, dict[str, dict[str, object]]] = {}

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fake_toggles(*, feature_name: str, server: str):
        assert feature_name == service_module.FEATURE_NEW_CARD_REMINDER
        assert server == "jp"
        return [SimpleNamespace(platform="qq", group_id="group-a")]

    async def fake_sent_keys(**_kwargs):
        return {("qq", "group-a"): set()}

    async def fake_batches(*, server: str, specs):
        assert server == "jp"
        assert [spec.key for spec in specs] == [summary_key, card_key, stamp_key]
        yield [
            service_module._NewCardReminderPreparedItem(
                key=summary_key,
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=card_key,
                kind="card",
                message="card",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=stamp_key,
                kind="stamp",
                message="stamp",
                estimated_bytes=1,
            ),
        ]

    plain_calls: list[list[str]] = []

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        plain_calls.append([item.key for item in items])
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(
        service_module,
        "list_notification_record_keys_by_groups",
        fake_sent_keys,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_iter_new_card_batches",
        fake_batches,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[result],
    )

    assert plain_calls == [[summary_key], [card_key], [stamp_key]]
    assert pending_payload == {}


@pytest.mark.asyncio
async def test_handle_test_new_card_reminder_explicit_ids_do_not_include_stamps(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_get_cards(_server: str):
        return [{"id": 1006}, {"id": 1007}, {"id": 1008}]

    async def fake_get_stamps(_server: str):
        return [{"id": 501}]

    async def fake_send_test_batches(*, bot, group_id: str, server: str, revision: str | None, specs):
        captured["keys"] = [spec.key for spec in specs]
        return True

    monkeypatch.setattr(service_module.master_data_provider, "get_cards", fake_get_cards)
    monkeypatch.setattr(service_module.master_data_provider, "get_stamps", fake_get_stamps)
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_test_new_card_batches",
        fake_send_test_batches,
    )

    result = await service_module.moesekai_app.handle_test_new_card_reminder(
        bot=SimpleNamespace(),
        group_id="178732453",
        platform="qq",
        user_id="123456",
        server="jp",
        card_ids=[1006, 1007],
    )

    assert result is None
    assert captured["keys"] == ["test:summary", "test:card:1007", "test:card:1006"]


@pytest.mark.asyncio
async def test_send_test_new_card_batches_reuses_plain_sender_across_prepared_batches(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_auto_asset_timeout_seconds=8.0,
        new_card_media_fetch_concurrency=4,
        new_card_send_timeout_seconds=5.0,
        new_card_send_delay_min_seconds=0.2,
        new_card_send_delay_max_seconds=0.8,
        new_card_plain_card_max_estimated_bytes=10_485_760,
        new_card_plain_stamp_max_estimated_bytes=10_485_760,
        new_card_abort_after_consecutive_failures=2,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    specs = [
        service_module._NewCardReminderItemSpec(key="test:summary", kind="summary", payload={}),
        service_module._NewCardReminderItemSpec(key="test:card:1006", kind="card", payload={}),
        service_module._NewCardReminderItemSpec(key="test:card:1007", kind="card", payload={}),
    ]

    async def fake_batches(*, server: str, specs):
        yield [
            service_module._NewCardReminderPreparedItem(
                key="test:summary",
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
        ]
        yield [
            service_module._NewCardReminderPreparedItem(
                key="test:card:1006",
                kind="card",
                message="card-1006",
                estimated_bytes=1,
            ),
        ]
        yield [
            service_module._NewCardReminderPreparedItem(
                key="test:card:1007",
                kind="card",
                message="card-1007",
                estimated_bytes=1,
            ),
        ]

    plain_calls: list[tuple[list[str], bool]] = []

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        plain_calls.append(([item.key for item in items], mark_sent))
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(service_module.moesekai_app, "_iter_new_card_batches", fake_batches)
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    sent_content = await service_module.moesekai_app._send_test_new_card_batches(
        bot=SimpleNamespace(),
        group_id="178732453",
        server="jp",
        revision="test",
        specs=specs,
    )

    assert sent_content is True
    assert plain_calls == [
        (["test:summary"], False),
        (["test:card:1006", "test:card:1007"], False),
    ]


@pytest.mark.asyncio
async def test_build_new_card_segments_fetches_normal_and_trained_concurrently(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[bool] = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    monkeypatch.setattr(
        service_module.asset_provider,
        "only_has_after_training",
        lambda _card: False,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "has_after_training",
        lambda _card: True,
    )

    async def fake_get_card_image(
        _server: str,
        _assetbundle: str,
        *,
        after_training: bool = False,
        timeout: float = 20,
    ) -> bytes:
        assert timeout == 5
        started.append(after_training)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return b"trained-image" if after_training else b"normal-image"

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image",
        fake_get_card_image,
    )
    monkeypatch.setattr(
        service_module.alias_provider,
        "get_character_profile",
        lambda _target_id, **_kwargs: asyncio.sleep(0, result=AliasProfile(
            target_type="character",
            target_id="200",
            canonical_name="初音未来",
            display_title="初音未来",
            merged_aliases=[],
            group_aliases=[],
        )),
    )

    task = asyncio.create_task(
        service_module.moesekai_app._build_new_card_segments(
            "jp",
            {
                "id": 100,
                "name": "群青赞歌",
                "characterId": 200,
                "cardRarityType": "rarity_4",
                "assetbundleName": "card001",
            },
            asset_timeout=5,
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    segments, has_media, estimated_bytes = await task

    assert has_media is True
    assert len(segments) == 3
    assert str(segments[0]) == "100:🌟4[群青赞歌]初音未来"
    assert set(started) == {False, True}
    assert estimated_bytes >= len(b"normal-image") + len(b"trained-image")


@pytest.mark.asyncio
async def test_build_new_card_segments_prefers_prefix_and_cached_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    cached_image = tmp_path / "card_normal.png"
    cached_image.write_bytes(b"cached")

    monkeypatch.setattr(
        service_module.asset_provider,
        "only_has_after_training",
        lambda _card: False,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "has_after_training",
        lambda _card: False,
    )

    async def fake_get_card_image(
        _server: str,
        _assetbundle: str,
        *,
        after_training: bool = False,
        timeout: float = 20,
    ) -> bytes:
        assert after_training is False
        return b"normal-image"

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image",
        fake_get_card_image,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image_local_path",
        lambda *_args, **_kwargs: cached_image,
    )
    monkeypatch.setattr(
        service_module.alias_provider,
        "get_character_profile",
        lambda _target_id, **_kwargs: asyncio.sleep(0, result=AliasProfile(
            target_type="character",
            target_id="14",
            canonical_name="鳳えむ",
            display_title="鳳えむ",
            merged_aliases=[],
            group_aliases=[],
        )),
    )

    segments, has_media, _ = await service_module.moesekai_app._build_new_card_segments(
        "jp",
        {
            "id": 805,
            "name": "",
            "prefix": "feat.シナモロール",
            "characterId": 14,
            "cardRarityType": "rarity_4",
            "assetbundleName": "res014_no032",
        },
    )

    assert has_media is True
    assert str(segments[0]) == "805:🌟4[feat.シナモロール]鳳えむ"
    assert getattr(segments[1], "path", None) == str(cached_image)
    assert getattr(segments[1], "raw", None) is None


@pytest.mark.asyncio
async def test_build_new_card_segments_falls_back_to_raw_when_cached_path_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        service_module.asset_provider,
        "only_has_after_training",
        lambda _card: False,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "has_after_training",
        lambda _card: False,
    )

    async def fake_get_card_image(
        _server: str,
        _assetbundle: str,
        *,
        after_training: bool = False,
        timeout: float = 20,
    ) -> bytes:
        assert after_training is False
        return b"normal-image"

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image",
        fake_get_card_image,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image_local_path",
        lambda *_args, **_kwargs: None,
    )

    async def fake_get_character_profile(_target_id: str, **_kwargs):
        return None

    monkeypatch.setattr(
        service_module.alias_provider,
        "get_character_profile",
        fake_get_character_profile,
    )

    segments, has_media, _ = await service_module.moesekai_app._build_new_card_segments(
        "jp",
        {
            "id": 101,
            "name": "未知卡",
            "characterId": 999,
            "cardRarityType": "rarity_special",
            "assetbundleName": "card002",
        },
    )

    assert has_media is True
    assert str(segments[0]) == "101:rarity_special[未知卡]角色999"
    assert getattr(segments[1], "path", None) is None
    assert getattr(segments[1], "raw", None) == b"normal-image"


@pytest.mark.asyncio
async def test_build_new_card_segments_falls_back_for_unknown_rarity_and_character(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        service_module.asset_provider,
        "only_has_after_training",
        lambda _card: False,
    )
    monkeypatch.setattr(
        service_module.asset_provider,
        "has_after_training",
        lambda _card: False,
    )

    async def fake_get_card_image(
        _server: str,
        _assetbundle: str,
        *,
        after_training: bool = False,
        timeout: float = 20,
    ) -> bytes:
        assert after_training is False
        return b"normal-image"

    async def fake_get_character_profile(_target_id: str, **_kwargs):
        return None

    monkeypatch.setattr(
        service_module.asset_provider,
        "get_card_image",
        fake_get_card_image,
    )
    monkeypatch.setattr(
        service_module.alias_provider,
        "get_character_profile",
        fake_get_character_profile,
    )

    segments, has_media, _ = await service_module.moesekai_app._build_new_card_segments(
        "jp",
        {
            "id": 101,
            "name": "未知卡",
            "characterId": 999,
            "cardRarityType": "rarity_special",
            "assetbundleName": "card002",
        },
    )

    assert has_media is True
    assert str(segments[0]) == "101:rarity_special[未知卡]角色999"


@pytest.mark.asyncio
async def test_iter_new_card_batches_chunks_by_prepare_window(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_auto_asset_timeout_seconds=8.0,
        new_card_media_fetch_concurrency=2,
        new_card_plain_card_max_estimated_bytes=10,
        new_card_plain_stamp_max_estimated_bytes=10,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    async def fake_prepare(*, server: str, spec, asset_timeout: float, semaphore):
        assert server == "jp"
        assert asset_timeout == 8.0
        assert semaphore is not None
        return service_module._NewCardReminderPreparedItem(
            key=spec.key,
            kind=spec.kind,
            message=f"node-{spec.key}",
            estimated_bytes=6,
        )

    monkeypatch.setattr(
        service_module.moesekai_app,
        "_prepare_new_card_item",
        fake_prepare,
    )

    specs = [
        service_module._NewCardReminderItemSpec(key="item1", kind="card", payload={}),
        service_module._NewCardReminderItemSpec(key="item2", kind="card", payload={}),
        service_module._NewCardReminderItemSpec(key="item3", kind="card", payload={}),
    ]
    batches = [
        [item.key for item in batch]
        async for batch in service_module.moesekai_app._iter_new_card_batches(
            server="jp",
            specs=specs,
        )
    ]

    assert batches == [["item1", "item2"], ["item3"]]


@pytest.mark.asyncio
async def test_dispatch_new_card_notifications_filters_pending_keys_per_group(
    monkeypatch: pytest.MonkeyPatch,
):
    base_key = "rev-1"
    summary_key = f"{base_key}:summary"
    card_key = f"{base_key}:card:100"
    result = service_module.RegionUpdateResult(
        server="jp",
        updated=True,
        download_success=True,
        current_revision=base_key,
        added_records={"cards": [_auto_card(100)], "stamps": []},
    )
    pending_payload: dict[str, dict[str, dict[str, object]]] = {}

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fake_toggles(*, feature_name: str, server: str):
        assert feature_name == service_module.FEATURE_NEW_CARD_REMINDER
        assert server == "jp"
        return [
            SimpleNamespace(platform="qq", group_id="group-a"),
            SimpleNamespace(platform="qq", group_id="group-b"),
        ]

    async def fake_sent_keys(*, feature_name: str, server: str, record_key_prefix: str, groups):
        assert record_key_prefix == f"{base_key}:"
        return {
            ("qq", "group-a"): set(),
            ("qq", "group-b"): {summary_key},
        }

    async def fake_batches(*, server: str, specs):
        assert server == "jp"
        assert [spec.key for spec in specs] == [summary_key, card_key]
        yield [
            service_module._NewCardReminderPreparedItem(
                key=summary_key,
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=card_key,
                kind="card",
                message="card-100",
                estimated_bytes=1,
            ),
        ]

    plain_calls: list[tuple[str, list[str]]] = []

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        plain_calls.append((target_state.group_id, [item.key for item in items]))
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(
        service_module,
        "list_notification_record_keys_by_groups",
        fake_sent_keys,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_iter_new_card_batches",
        fake_batches,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[result],
    )

    assert plain_calls == [
        ("group-a", [summary_key]),
        ("group-a", [card_key]),
        ("group-b", [card_key]),
    ]


@pytest.mark.asyncio
async def test_dispatch_new_card_notifications_failed_group_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
):
    base_key = "rev-2"
    summary_key = f"{base_key}:summary"
    card_1_key = f"{base_key}:card:100"
    card_2_key = f"{base_key}:card:101"
    result = service_module.RegionUpdateResult(
        server="jp",
        updated=True,
        download_success=True,
        current_revision=base_key,
        added_records={"cards": [_auto_card(100), _auto_card(101)], "stamps": []},
    )
    pending_payload: dict[str, dict[str, dict[str, object]]] = {}

    class DummyPendingStore:
        def load(self, default):
            return copy.deepcopy(pending_payload or default)

        def save(self, payload):
            pending_payload.clear()
            pending_payload.update(copy.deepcopy(payload))

    async def fake_toggles(*, feature_name: str, server: str):
        return [
            SimpleNamespace(platform="qq", group_id="group-a"),
            SimpleNamespace(platform="qq", group_id="group-b"),
        ]

    async def fake_sent_keys(**_kwargs):
        return {
            ("qq", "group-a"): set(),
            ("qq", "group-b"): set(),
        }

    async def fake_batches(*, server: str, specs):
        assert [spec.key for spec in specs] == [summary_key, card_2_key, card_1_key]
        yield [
            service_module._NewCardReminderPreparedItem(
                key=summary_key,
                kind="summary",
                message="summary",
                estimated_bytes=1,
            ),
            service_module._NewCardReminderPreparedItem(
                key=card_2_key,
                kind="card",
                message="card-101",
                estimated_bytes=1,
            ),
        ]
        yield [
            service_module._NewCardReminderPreparedItem(
                key=card_1_key,
                kind="card",
                message="card-100",
                estimated_bytes=1,
            )
        ]

    plain_calls: list[tuple[str, list[str], int]] = []

    async def fake_send_plain_items(
        *,
        bot,
        server: str,
        revision: str | None,
        target_state,
        items,
        batch_index: int,
        mark_sent: bool = True,
    ):
        plain_calls.append((target_state.group_id, [item.key for item in items], batch_index))
        if target_state.group_id == "group-a" and any(item.kind == "card" for item in items):
            target_state.aborted = True
            return
        target_state.pending_keys.difference_update(item.key for item in items)

    monkeypatch.setattr(
        service_module,
        "list_enabled_group_feature_toggles",
        fake_toggles,
    )
    monkeypatch.setattr(service_module, "_NEW_CARD_PENDING_STATE", DummyPendingStore())
    monkeypatch.setattr(
        service_module,
        "list_notification_record_keys_by_groups",
        fake_sent_keys,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_iter_new_card_batches",
        fake_batches,
    )
    monkeypatch.setattr(
        service_module.moesekai_app,
        "_send_new_card_plain_items",
        fake_send_plain_items,
    )

    await service_module.moesekai_app.dispatch_new_card_notifications(
        bot=SimpleNamespace(),
        results=[result],
    )

    assert plain_calls == [
        ("group-a", [summary_key], 1),
        ("group-b", [summary_key], 1),
        ("group-a", [card_2_key, card_1_key], 2),
        ("group-b", [card_2_key, card_1_key], 2),
    ]


def test_build_new_card_plain_message_batches_separates_cards_and_stamps(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_plain_card_max_estimated_bytes=10,
        new_card_plain_stamp_max_estimated_bytes=10,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    items = [
        service_module._NewCardReminderPreparedItem(
            key="summary",
            kind="summary",
            message="summary",
            estimated_bytes=1,
        ),
        service_module._NewCardReminderPreparedItem(
            key="card-1",
            kind="card",
            message="card-1",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="card-2",
            kind="card",
            message="card-2",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="stamp-1",
            kind="stamp",
            message="stamp-1",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="stamp-2",
            kind="stamp",
            message="stamp-2",
            estimated_bytes=4,
        ),
    ]

    batches = service_module.moesekai_app._build_new_card_plain_message_batches(items)
    assert [[item.key for item in batch] for batch in batches] == [
        ["summary"],
        ["card-1", "card-2"],
        ["stamp-1", "stamp-2"],
    ]


def test_build_new_card_plain_message_batches_splits_cards_on_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_plain_card_max_estimated_bytes=7,
        new_card_plain_stamp_max_estimated_bytes=10,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    items = [
        service_module._NewCardReminderPreparedItem(
            key="card-1",
            kind="card",
            message="card-1",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="card-2",
            kind="card",
            message="card-2",
            estimated_bytes=4,
        ),
    ]

    batches = service_module.moesekai_app._build_new_card_plain_message_batches(items)
    assert [[item.key for item in batch] for batch in batches] == [["card-1"], ["card-2"]]


@pytest.mark.asyncio
async def test_send_new_card_plain_items_inserts_delay_and_marks_success(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_send_timeout_seconds=5.0,
        new_card_send_delay_min_seconds=1.5,
        new_card_send_delay_max_seconds=3.0,
        new_card_plain_card_max_estimated_bytes=10,
        new_card_plain_stamp_max_estimated_bytes=10,
        new_card_abort_after_consecutive_failures=2,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    send_calls: list[str] = []
    sleep_calls: list[float] = []
    marked: list[list[str]] = []

    async def fake_send_message(_bot, _user_id, group_id: str, _message):
        send_calls.append(group_id)
        return "ok"

    async def fake_mark(*, platform: str, group_id: str, server: str, keys: list[str]):
        marked.append(keys)

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(service_module.PlatformUtils, "send_message", fake_send_message)
    monkeypatch.setattr(service_module.moesekai_app, "_mark_new_card_keys_sent", fake_mark)
    monkeypatch.setattr(service_module.random, "uniform", lambda _a, _b: 2.25)
    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)

    target_state = service_module._NewCardReminderTargetState(
        platform="qq",
        group_id="group-a",
        pending_keys={"key-1", "key-2", "key-3", "key-4"},
    )
    items = [
        service_module._NewCardReminderPreparedItem(
            key="key-1",
            kind="summary",
            message="summary",
            estimated_bytes=1,
        ),
        service_module._NewCardReminderPreparedItem(
            key="key-2",
            kind="card",
            message="card-1",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="key-3",
            kind="card",
            message="card-2",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="key-4",
            kind="stamp",
            message="stamp-1",
            estimated_bytes=4,
        ),
    ]

    await service_module.moesekai_app._send_new_card_plain_items(
        bot=SimpleNamespace(),
        server="jp",
        revision="rev",
        target_state=target_state,
        items=items,
        batch_index=1,
    )

    assert send_calls == ["group-a", "group-a", "group-a"]
    assert sleep_calls == [2.25, 2.25]
    assert marked == [["key-1"], ["key-2", "key-3"], ["key-4"]]
    assert target_state.pending_keys == set()
    assert target_state.aborted is False


@pytest.mark.asyncio
async def test_send_new_card_plain_items_aborts_after_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = SimpleNamespace(
        new_card_send_timeout_seconds=5.0,
        new_card_send_delay_min_seconds=1.5,
        new_card_send_delay_max_seconds=3.0,
        new_card_plain_card_max_estimated_bytes=10,
        new_card_plain_stamp_max_estimated_bytes=10,
        new_card_abort_after_consecutive_failures=2,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    send_attempts: list[str] = []

    async def fake_send_message(_bot, _user_id, group_id: str, _message):
        send_attempts.append(group_id)
        raise ActionFailed("onebot", {"retcode": 123, "msg": "blocked"})

    async def fail_mark(**_kwargs):
        raise AssertionError("连续失败时不应写入成功记录")

    monkeypatch.setattr(service_module.PlatformUtils, "send_message", fake_send_message)
    monkeypatch.setattr(service_module.moesekai_app, "_mark_new_card_keys_sent", fail_mark)

    target_state = service_module._NewCardReminderTargetState(
        platform="qq",
        group_id="group-a",
        pending_keys={"key-1", "key-2", "key-3"},
    )
    items = [
        service_module._NewCardReminderPreparedItem(
            key="key-1",
            kind="summary",
            message="summary",
            estimated_bytes=1,
        ),
        service_module._NewCardReminderPreparedItem(
            key="key-2",
            kind="card",
            message="card-1",
            estimated_bytes=4,
        ),
        service_module._NewCardReminderPreparedItem(
            key="key-3",
            kind="card",
            message="card-2",
            estimated_bytes=4,
        ),
    ]

    await service_module.moesekai_app._send_new_card_plain_items(
        bot=SimpleNamespace(),
        server="jp",
        revision="rev",
        target_state=target_state,
        items=items,
        batch_index=1,
    )

    assert send_attempts == ["group-a", "group-a"]
    assert target_state.aborted is True
    assert target_state.pending_keys == {"key-1", "key-2", "key-3"}


def test_build_new_card_item_specs_skips_summary_when_only_retrying_missing_items():
    base_key = "rev-3"
    specs = service_module.moesekai_app._build_new_card_item_specs(
        server="jp",
        revision=base_key,
        base_key=base_key,
        cards=[{"id": 100}, {"id": 101}],
        stamps=[{"id": 200}],
        pending_keys={f"{base_key}:card:101", f"{base_key}:stamp:200"},
    )

    assert [spec.key for spec in specs] == [f"{base_key}:card:101", f"{base_key}:stamp:200"]
