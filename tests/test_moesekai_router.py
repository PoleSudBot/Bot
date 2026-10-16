from __future__ import annotations

from types import SimpleNamespace

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
import pytest

nonebot.init()

from zhenxun.plugins.moesekai.adapters.results import MoeImageTextMessage
from zhenxun.plugins.moesekai.command_parser import ParsedCommand
from zhenxun.plugins.moesekai.commands import router as router_module


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[object, Message]] = []

    async def send(self, event, message: Message):
        self.calls.append((event, message))
        return None


class FakeEvent:
    def __init__(self, message_id: int = 12345) -> None:
        self.message_id = message_id


@pytest.mark.parametrize(
    "action",
    ["bind", "unbind", "default_server", "visibility", "admin_query_binding"],
)
def test_binding_action_results_add_nickname_once(action: str):
    assert router_module._format_action_result(action, "提示") == "[南极] 提示"
    assert router_module._format_action_result(action, "[南极] 提示") == "[南极] 提示"


def test_non_binding_action_result_keeps_original_text():
    assert router_module._format_action_result("query_archive", "提示") == "提示"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parsed", "expected"),
    [
        (
            ParsedCommand(
                action="bind",
                raw_text="绑定",
                error="用法: 绑定 [区服] <游戏ID>",
            ),
            "[南极] 用法: 绑定 [区服] <游戏ID>",
        ),
        (
            ParsedCommand(action="admin_query_binding", raw_text="pjsk 查询绑定"),
            "[南极] 该指令仅超级用户可用",
        ),
    ],
)
async def test_binding_router_early_errors_add_nickname(
    monkeypatch: pytest.MonkeyPatch,
    parsed: ParsedCommand,
    expected: str,
):
    sent: list[object] = []

    async def fake_superuser(_bot, _event):
        return False

    async def fake_send_result(result, *, bot=None, event=None):
        sent.append(result)

    monkeypatch.setattr(router_module, "SUPERUSER", fake_superuser)
    monkeypatch.setattr(router_module, "_send_result", fake_send_result)

    await router_module._(
        bot=SimpleNamespace(),
        event=SimpleNamespace(),
        session=SimpleNamespace(user=SimpleNamespace(id="123456"), group=None),
        state={"moesekai_parsed_command": parsed},
    )

    assert sent == [expected]


@pytest.mark.asyncio
async def test_send_result_uses_single_native_onebot_message_for_image_text():
    bot = FakeBot()
    event = FakeEvent()

    await router_module._send_result(
        MoeImageTextMessage(
            image_bytes=b"manga-image",
            text="B站链接：https://www.bilibili.com/opus/1183974551994761216",
        ),
        bot=bot,
        event=event,
    )

    assert len(bot.calls) == 1
    sent_event, sent_message = bot.calls[0]
    assert sent_event is event
    assert isinstance(sent_message, Message)
    assert sent_message[0].type == "reply"
    assert sent_message[1].type == "image"
    assert sent_message[2].type == "text"
    assert sent_message[2].data["text"].startswith("\nB站链接：")
    assert MessageSegment.image(b"manga-image").data["file"] == sent_message[1].data["file"]


@pytest.mark.asyncio
async def test_query_archive_router_does_not_inject_requester_for_explicit_uid(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_superuser(_bot, _event):
        return False

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
        return None

    async def fake_send_result(_result, *, bot=None, event=None):
        raise AssertionError("handle_query_archive 返回 None 时不应回消息")

    monkeypatch.setattr(router_module, "SUPERUSER", fake_superuser)
    monkeypatch.setattr(
        router_module.PlatformUtils,
        "get_platform",
        lambda _session: "qq",
    )
    monkeypatch.setattr(router_module, "handle_query_archive", fake_handle_query_archive)
    monkeypatch.setattr(router_module, "_send_result", fake_send_result)

    parsed = ParsedCommand(
        action="query_archive",
        raw_text="个人档案26958722584772616",
        game_id="26958722584772616",
    )
    session = SimpleNamespace(user=SimpleNamespace(id="1163272259"), group=None)
    state = {"moesekai_parsed_command": parsed}

    await router_module._(
        bot=SimpleNamespace(),
        event=SimpleNamespace(),
        session=session,
        state=state,
    )

    assert captured == {
        "platform": "qq",
        "requester_user_id": "1163272259",
        "server": None,
        "game_id": "26958722584772616",
        "target_user_id": None,
        "is_superuser": False,
    }


@pytest.mark.asyncio
async def test_character_router_keeps_query_context(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    async def fake_superuser(_bot, _event):
        return False

    async def fake_handle_character(
        query: str,
        *,
        platform: str | None,
        group_id: str | None,
        force_refresh: bool,
    ):
        captured["query"] = query
        captured["platform"] = platform
        captured["group_id"] = group_id
        captured["force_refresh"] = force_refresh
        return None

    async def fake_send_result(_result, *, bot=None, event=None):
        raise AssertionError("handle_character 返回 None 时不应回消息")

    monkeypatch.setattr(router_module, "SUPERUSER", fake_superuser)
    monkeypatch.setattr(router_module.PlatformUtils, "get_platform", lambda _session: "qq")
    monkeypatch.setattr(router_module, "handle_character", fake_handle_character)
    monkeypatch.setattr(router_module, "_send_result", fake_send_result)

    parsed = ParsedCommand(
        action="character",
        raw_text="查角色 初音未来 强制刷新",
        query_text="初音未来",
        force_refresh=True,
    )
    session = SimpleNamespace(
        user=SimpleNamespace(id="1163272259"),
        group=SimpleNamespace(id="654321"),
    )
    state = {"moesekai_parsed_command": parsed}

    await router_module._(
        bot=SimpleNamespace(),
        event=SimpleNamespace(),
        session=session,
        state=state,
    )

    assert captured == {
        "query": "初音未来",
        "platform": "qq",
        "group_id": "654321",
        "force_refresh": True,
    }
