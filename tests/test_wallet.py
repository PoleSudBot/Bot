from __future__ import annotations

import asyncio
from types import SimpleNamespace

import nonebot
import pytest
from tortoise import Tortoise

nonebot.init()
nonebot.require("nonebot_plugin_alconna")
nonebot.require("nonebot_plugin_uninfo")

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot_plugin_alconna import At, Match, UniMessage
from nonebot_plugin_uninfo.model import Scene, SceneType, Session, User
import nonebot_plugin_uninfo.params as uninfo_params

from zhenxun.builtin_plugins.superuser.super_power import (
    _gold_adjust_matcher,
    _parse_signed_delta,
)
from zhenxun.builtin_plugins.wallet import (
    _command,
    _handle_transfer,
    _matcher,
    _resolve_transfer_parts,
    _transfer_command,
    _transfer_matcher,
)
from zhenxun.builtin_plugins.wallet import data_source as wallet_data_source
from zhenxun.models.friend_user import FriendUser
from zhenxun.models.group_member_info import GroupInfoUser
from zhenxun.models.user_console import UserConsole
from zhenxun.models.user_gold_log import UserGoldLog
from zhenxun.utils.enum import GoldHandle
from zhenxun.utils.exception import InsufficientGold


@pytest.mark.parametrize(
    ("message", "gold"),
    [
        (UniMessage.text("转账100").at("42"), 100),
        (UniMessage.text("转账").at("42").text("100"), 100),
        (UniMessage.text("转账 100 ").at("42"), 100),
        (UniMessage.text("转账 ").at("42").text(" 100"), 100),
        (UniMessage.text("钱包转账100").at("42"), 100),
        (UniMessage.text("钱包转账").at("42").text("100"), 100),
        (UniMessage.text("钱包转账 100 ").at("42"), 100),
        (UniMessage.text("钱包转账 ").at("42").text(" 100"), 100),
    ],
)
def test_transfer_command_accepts_both_argument_orders(message, gold):
    result = _transfer_command.parse(message)

    assert result.matched
    parts = result.all_matched_args["parts"]
    assert len(parts) == 2
    assert gold in parts
    assert At("user", "42") in parts


@pytest.mark.parametrize(
    "message",
    [
        UniMessage.text("转账100"),
        UniMessage.text("转账100 200").at("42"),
        UniMessage.text("转账100").at("42").at("43"),
    ],
)
def test_transfer_command_collects_invalid_parts_for_silent_rejection(message):
    result = _transfer_command.parse(message)

    assert result.matched
    values = result.all_matched_args.get("parts", ())
    assert _resolve_transfer_parts(values) is None


@pytest.mark.parametrize(
    "message",
    [
        UniMessage.text("转账一百").at("42"),
        UniMessage.text("转账").at("42").text("100 已收到"),
    ],
)
def test_transfer_command_parser_rejects_unmatched_text(message):
    assert not _transfer_command.parse(message).matched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        Match((), False),
        Match((), True),
        Match((100,), True),
        Match((100, 200), True),
        Match((At("user", "42"), At("user", "43")), True),
    ],
)
async def test_transfer_handler_silently_ignores_invalid_parts(monkeypatch, parts):
    called = []

    async def fail_transfer(*_args, **_kwargs):
        called.append("ledger")

    def fail_build_message(*_args, **_kwargs):
        called.append("message")

    monkeypatch.setattr(UserConsole, "transfer_gold", fail_transfer)
    monkeypatch.setattr(
        "zhenxun.builtin_plugins.wallet.MessageUtils.build_message",
        fail_build_message,
    )
    matcher = SimpleNamespace(stop_propagation=lambda: called.append("stop"))

    await _handle_transfer(
        matcher=matcher,
        session=SimpleNamespace(user=SimpleNamespace(id="sender")),
        arparma=SimpleNamespace(header_result="转账"),
        parts=parts,
    )

    assert called == []


@pytest.mark.asyncio
async def test_transfer_handler_calls_ledger_and_stops_propagation(monkeypatch):
    called = []
    sent = []

    async def fake_transfer(*args):
        called.append(("ledger", args))

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

        async def send(self, **kwargs):
            sent.append((self.content, kwargs))

    monkeypatch.setattr(UserConsole, "transfer_gold", fake_transfer)
    monkeypatch.setattr(
        "zhenxun.builtin_plugins.wallet.PlatformUtils.get_platform",
        lambda _session: "qq",
    )
    monkeypatch.setattr(
        "zhenxun.builtin_plugins.wallet.MessageUtils.build_message", _FakeMessage
    )
    matcher = SimpleNamespace(stop_propagation=lambda: called.append(("stop", ())))

    at_user = At("user", "42")
    await _handle_transfer(
        matcher=matcher,
        session=SimpleNamespace(user=SimpleNamespace(id="sender")),
        arparma=SimpleNamespace(header_result="转账"),
        parts=Match((at_user, 100), True),
    )

    assert called == [
        ("stop", ()),
        ("ledger", ("sender", "42", 100, "qq")),
    ]
    assert sent == [
        (["转账成功，已给", at_user, "转账 100 金币。"], {"reply_to": True})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (ValueError("转账金额必须为正整数"), "转账金额必须为正整数"),
        (InsufficientGold(), "钱包余额不足，转账失败。"),
    ],
)
async def test_transfer_handler_reports_expected_failures(
    monkeypatch, error, expected_message
):
    called = []
    finished = []
    sent = []

    async def fail_transfer(*_args):
        called.append("ledger")
        raise error

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

        async def finish(self, **kwargs):
            finished.append((self.content, kwargs))

        async def send(self, **kwargs):
            sent.append((self.content, kwargs))

    monkeypatch.setattr(UserConsole, "transfer_gold", fail_transfer)
    monkeypatch.setattr(
        "zhenxun.builtin_plugins.wallet.PlatformUtils.get_platform",
        lambda _session: "qq",
    )
    monkeypatch.setattr(
        "zhenxun.builtin_plugins.wallet.MessageUtils.build_message", _FakeMessage
    )
    matcher = SimpleNamespace(stop_propagation=lambda: called.append("stop"))

    await _handle_transfer(
        matcher=matcher,
        session=SimpleNamespace(user=SimpleNamespace(id="sender")),
        arparma=SimpleNamespace(header_result="转账"),
        parts=Match((At("user", "42"), 100), True),
    )

    assert called == ["stop", "ledger"]
    assert finished == [(expected_message, {"reply_to": True})]
    assert sent == []


@pytest.mark.parametrize(
    ("message", "component", "num"),
    [
        ("我的金币", "balance", None),
        ("查看我的钱包", "balance", None),
        ("查看钱包余额", "balance", None),
        ("金币排行 5", "rank", 5),
        ("金币总排行 6", "rank", 6),
        ("查看钱包排名 7", "rank", 7),
    ],
)
def test_public_and_legacy_shortcuts_keep_trailing_arguments(message, component, num):
    result = _command.parse(message)

    assert result.matched
    assert component in result.components
    if num is not None:
        assert result.all_matched_args["num"] == num
    if message.startswith("金币总排行"):
        assert "global" in result.components


@pytest.mark.parametrize("raw_delta", ["+100", "-50"])
def test_superuser_adjust_command_accepts_signed_integers(raw_delta):
    result = _gold_adjust_matcher.command().parse(
        UniMessage.text(f"金币增减 {raw_delta} ").at("42")
    )

    assert result.matched
    assert result.all_matched_args == {
        "delta": raw_delta,
        "at_user": At("user", "42"),
    }


def test_superuser_adjust_command_preserves_unsigned_value_for_handler_validation():
    result = _gold_adjust_matcher.command().parse(
        UniMessage.text("金币增减 100 ").at("42")
    )

    assert result.matched
    assert result.all_matched_args["delta"] == "100"
    with pytest.raises(ValueError, match="必须是带正负号的整数"):
        _parse_signed_delta(result.all_matched_args["delta"])


@pytest.fixture
async def wallet_db(tmp_path):
    db_path = tmp_path / "wallet.sqlite3"
    await Tortoise.init(
        db_url=f"sqlite://{db_path}",
        modules={
            "models": [
                "zhenxun.models.user_console",
                "zhenxun.models.user_gold_log",
                "zhenxun.models.group_member_info",
                "zhenxun.models.friend_user",
            ]
        },
        timezone="Asia/Shanghai",
    )
    await Tortoise.generate_schemas()
    UserConsole._uid_counter = None
    yield
    UserConsole._uid_counter = None
    await Tortoise.close_connections()


@pytest.mark.asyncio
async def test_transfer_gold_updates_balances_and_logs(wallet_db):
    sender = await UserConsole.get_user("sender", "qq")
    receiver = await UserConsole.get_user("receiver", "qq")
    sender.gold = 150
    receiver.gold = 20
    await sender.save(update_fields=["gold"])
    await receiver.save(update_fields=["gold"])

    await UserConsole.transfer_gold("sender", "receiver", 30, "qq")

    sender = await UserConsole.get(user_id="sender")
    receiver = await UserConsole.get(user_id="receiver")
    logs = await UserGoldLog.all().order_by("id")
    assert sender.gold == 120
    assert receiver.gold == 50
    assert [(log.user_id, log.gold, log.handle, log.source) for log in logs] == [
        ("sender", 30, GoldHandle.TRANSFER_OUT, "wallet:receiver"),
        ("receiver", 30, GoldHandle.TRANSFER_IN, "wallet:sender"),
    ]


@pytest.mark.asyncio
async def test_transfer_gold_rejects_invalid_requests(wallet_db):
    sender = await UserConsole.get_user("sender", "qq")
    receiver = await UserConsole.get_user("receiver", "qq")
    sender.gold = 10
    receiver.gold = 20
    await sender.save(update_fields=["gold"])
    await receiver.save(update_fields=["gold"])

    with pytest.raises(ValueError, match="不能给自己转账"):
        await UserConsole.transfer_gold("sender", "sender", 1, "qq")
    for invalid in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="转账金额必须为正整数"):
            await UserConsole.transfer_gold("sender", "receiver", invalid, "qq")
    with pytest.raises(InsufficientGold):
        await UserConsole.transfer_gold("sender", "receiver", 99, "qq")

    sender = await UserConsole.get(user_id="sender")
    receiver = await UserConsole.get(user_id="receiver")
    assert sender.gold == 10
    assert receiver.gold == 20
    assert await UserGoldLog.all().count() == 0


@pytest.mark.asyncio
async def test_get_balance_creates_user_with_default_gold(wallet_db):
    assert await wallet_data_source.get_balance("new_user", "qq") == 100


@pytest.mark.asyncio
async def test_wallet_rank_marks_non_member_as_unranked(monkeypatch, wallet_db):
    session = SimpleNamespace(user=SimpleNamespace(id="sender"))
    rendered: dict[str, object] = {}

    class _FakeTable:
        def __init__(self, title: str):
            rendered["title"] = title
            self.rows = []
            self.divider_before_rows = set()

        def set_headers(self, headers):
            rendered["headers"] = headers
            return self

        def add_rows(self, rows):
            self.rows.extend(rows)
            return self

        def add_divider(self):
            self.divider_before_rows.add(len(self.rows))
            return self

        def add_row(self, row):
            self.rows.append(row)
            rendered["rows"] = self.rows
            rendered["divider_before_rows"] = self.divider_before_rows
            return self

    async def fake_render(_table):
        return b"rank-image"

    async def fake_get_avatar_path(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        wallet_data_source.PlatformUtils, "get_platform", lambda _s: "qq"
    )
    monkeypatch.setattr(
        wallet_data_source.avatar_service, "get_avatar_path", fake_get_avatar_path
    )
    monkeypatch.setattr(wallet_data_source.ui, "table", _FakeTable)
    monkeypatch.setattr(wallet_data_source.ui, "render", fake_render)

    result = await wallet_data_source.wallet_rank(session, "group_1", 10)

    assert result == b"rank-image"
    assert rendered["title"] == "金币群组排行"
    assert rendered["divider_before_rows"] == {0}
    assert len(rendered["rows"]) == 1
    assert rendered["rows"][0][0].content == "未上榜"
    assert rendered["rows"][0][2].content == "sender"
    assert rendered["rows"][0][3].content == "100"


@pytest.mark.asyncio
async def test_wallet_rank_renders_group_members(monkeypatch, wallet_db):
    sender = await UserConsole.get_user("sender", "qq")
    receiver = await UserConsole.get_user("receiver", "qq")
    tied = await UserConsole.get_user("tied", "discord")
    lower = await UserConsole.get_user("lower", "qq")
    sender.gold = 100
    receiver.gold = 250
    tied.gold = 100
    lower.gold = 50
    await UserConsole.bulk_update([sender, receiver, tied, lower], ["gold"])
    await GroupInfoUser.create(user_id="sender", group_id="group_1", user_name="我")
    await GroupInfoUser.create(user_id="receiver", group_id="group_1", user_name="你")
    await GroupInfoUser.create(user_id="tied", group_id="group_1", user_name="同分")
    await GroupInfoUser.create(user_id="lower", group_id="group_1", user_name="低分")
    await FriendUser.create(user_id="receiver", user_name="好友")
    session = SimpleNamespace(user=SimpleNamespace(id="sender"))
    rendered: dict[str, object] = {}

    class _FakeTable:
        def __init__(self, title: str):
            rendered["title"] = title
            self.rows = []
            self.divider_before_rows = set()

        def set_headers(self, headers):
            rendered["headers"] = headers
            return self

        def add_rows(self, rows):
            self.rows.extend(rows)
            return self

        def add_divider(self):
            self.divider_before_rows.add(len(self.rows))
            return self

        def add_row(self, row):
            self.rows.append(row)
            rendered["rows"] = self.rows
            rendered["divider_before_rows"] = self.divider_before_rows
            return self

    async def fake_render(table):
        rendered["table"] = table
        return b"rank-image"

    async def fake_get_avatar_path(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        wallet_data_source.PlatformUtils, "get_platform", lambda _s: "qq"
    )
    monkeypatch.setattr(
        wallet_data_source.avatar_service,
        "get_avatar_path",
        fake_get_avatar_path,
    )
    monkeypatch.setattr(wallet_data_source.ui, "table", _FakeTable)
    monkeypatch.setattr(wallet_data_source.ui, "render", fake_render)

    result = await wallet_data_source.wallet_rank(session, "group_1", 10)

    assert result == b"rank-image"
    assert rendered["title"] == "金币群组排行"
    assert rendered["headers"] == ["排名", "-", "名称", "金币", "平台"]
    assert rendered["divider_before_rows"] == {4}
    assert len(rendered["rows"]) == 5
    assert [row[2].content for row in rendered["rows"][:4]] == [
        "好友",
        "我",
        "同分",
        "低分",
    ]
    assert [row[0].content for row in rendered["rows"]] == [
        "1",
        "2",
        "2",
        "4",
        "2",
    ]
    assert rendered["rows"][-1][2].content == "我"
    assert rendered["rows"][-1][3].content == "100"


@pytest.mark.asyncio
async def test_wallet_rank_fetches_unique_avatars_with_bounded_concurrency(
    monkeypatch, wallet_db, tmp_path
):
    users = []
    for index in range(10):
        user = await UserConsole.get_user(f"user_{index}", "qq")
        user.gold = 1000 - index
        users.append(user)
    await UserConsole.bulk_update(users, ["gold"])
    session = SimpleNamespace(user=SimpleNamespace(id="user_0"))
    rendered: dict[str, object] = {}
    calls = []
    active = 0
    peak_active = 0
    avatar_path = tmp_path / "avatar.png"

    class _FakeTable:
        def __init__(self, title: str):
            rendered["title"] = title
            self.rows = []

        def set_headers(self, headers):
            rendered["headers"] = headers
            return self

        def add_rows(self, rows):
            self.rows.extend(rows)
            return self

        def add_divider(self):
            return self

        def add_row(self, row):
            self.rows.append(row)
            rendered["rows"] = self.rows
            return self

    async def fake_render(_table):
        return b"rank-image"

    async def fake_get_avatar_path(platform, user_id):
        nonlocal active, peak_active
        calls.append((platform, user_id))
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return avatar_path if user_id == "user_0" else None

    monkeypatch.setattr(
        wallet_data_source.PlatformUtils, "get_platform", lambda _s: "qq"
    )
    monkeypatch.setattr(
        wallet_data_source.avatar_service,
        "get_avatar_path",
        fake_get_avatar_path,
    )
    monkeypatch.setattr(wallet_data_source.ui, "table", _FakeTable)
    monkeypatch.setattr(wallet_data_source.ui, "render", fake_render)

    result = await wallet_data_source.wallet_rank(session, None, 10)

    assert result == b"rank-image"
    assert len(calls) == 10
    assert len(set(calls)) == 10
    assert peak_active == 8
    assert len(rendered["rows"]) == 11
    assert rendered["rows"][0][1].src == avatar_path.as_uri()
    assert rendered["rows"][-1][1].src == avatar_path.as_uri()
    assert rendered["rows"][1][1].content == ""


# ---------------------------------------------------------------------------
# 真实响应器流程：Alconna 解析 + 依赖注入 + handler 一起跑
#
# 上面的用例都直接调用解析函数或 handler，绕过了响应器这一层，因此覆盖不到：
#   * `钱包 @某人` 曾因为主命令缺少 At 参数，规则阶段直接返回 False，还被 Alconna
#     当成“参数 [at] 匹配失败”的提示发出去，用户看不到任何余额；
#   * matcher 依赖注入拿到类对象时，`matcher.stop_propagation()` 会抛 TypeError。
# ---------------------------------------------------------------------------

GROUP_ID = 178732453
SELF_ID = "3072536533"
SENDER_ID = "1163272259"
TARGET_ID = "42"
SENDER_GOLD = 500
TARGET_GOLD = 100

# 记录响应器发出的消息，供断言检查
_sent: list[str] = []
# alconna 以消息 id 为键缓存解析结果（LRU 16），每个用例必须用不同的 id
_message_seq = 0


class _FakeAdapter:
    def get_name(self) -> str:
        return "OneBot V11"


class _FakeFetcher:
    async def fetch(self, bot: Bot, event: GroupMessageEvent) -> Session:
        return Session(
            self_id=str(bot.self_id),
            adapter="OneBot V11",
            scope="QQClient",
            scene=Scene(id=str(GROUP_ID), type=SceneType.GROUP, name="摸鱼同好会"),
            user=User(id=SENDER_ID, name="tester"),
        )


async def _fake_call_api(api: str, **data):
    if api in {"send_group_msg", "send_msg"}:
        _sent.append(str(data.get("message")))
        return {"message_id": 1}
    if api == "get_group_member_info":
        return {
            "user_id": int(data.get("user_id", 0)),
            "nickname": "tester",
            "role": "member",
        }
    if api == "get_stranger_info":
        return {"user_id": int(data.get("user_id", 0)), "nickname": "tester"}
    if api == "get_login_info":
        return {"user_id": int(SELF_ID), "nickname": "bot"}
    return {}


def _make_event(raw_message: str) -> GroupMessageEvent:
    global _message_seq
    _message_seq += 1
    return GroupMessageEvent(
        time=1792284103,
        self_id=SELF_ID,
        post_type="message",
        sub_type="normal",
        user_id=int(SENDER_ID),
        message_type="group",
        message_id=_message_seq,
        group_id=GROUP_ID,
        message=Message(raw_message),
        raw_message=raw_message,
        font=14,
        sender=Sender(
            user_id=int(SENDER_ID), nickname="tester", card="", role="member"
        ),
    )


async def _run_matcher(
    matcher_cls, raw_message: str, bot: Bot
) -> tuple[bool, list[str], list[str]]:
    """按真实流程跑一次响应器，返回 (规则结果, 规则阶段的提示, handler 的回复)。"""
    event = _make_event(raw_message)
    state: dict = {}
    _sent.clear()
    rule = await matcher_cls.check_rule(bot, event, state, None, {})
    tips = list(_sent)
    _sent.clear()
    if rule:
        await matcher_cls().run(bot, event, state)
    replies = list(_sent)
    _sent.clear()
    return rule, tips, replies


@pytest.fixture
async def wallet_gold(wallet_db):
    sender = await UserConsole.get_user(SENDER_ID, "qq")
    target = await UserConsole.get_user(TARGET_ID, "qq")
    sender.gold = SENDER_GOLD
    target.gold = TARGET_GOLD
    await sender.save(update_fields=["gold"])
    await target.save(update_fields=["gold"])


@pytest.fixture
def wallet_bot(monkeypatch):
    monkeypatch.setitem(
        uninfo_params.INFO_FETCHER_MAPPING, "OneBot V11", _FakeFetcher()
    )
    bot = Bot(adapter=_FakeAdapter(), self_id=SELF_ID)
    bot.call_api = _fake_call_api  # type: ignore[method-assign]
    yield bot
    _sent.clear()


@pytest.mark.asyncio
async def test_wallet_matcher_without_at_replies_own_balance(wallet_gold, wallet_bot):
    rule, tips, replies = await _run_matcher(_matcher, "钱包", wallet_bot)

    assert rule is True
    assert tips == []
    assert len(replies) == 1
    assert f"你的钱包余额: {SENDER_GOLD}" in replies[0]


@pytest.mark.asyncio
async def test_wallet_matcher_with_at_replies_target_balance(wallet_gold, wallet_bot):
    rule, tips, replies = await _run_matcher(
        _matcher, f"钱包 [CQ:at,qq={TARGET_ID}]", wallet_bot
    )

    assert rule is True, "带艾特的钱包指令必须仍然命中规则"
    assert tips == [], "不应回“参数 [at] 匹配失败”"
    assert len(replies) == 1
    assert "用户" in replies[0]
    assert f"的钱包余额: {TARGET_GOLD}" in replies[0]


@pytest.mark.asyncio
async def test_wallet_matcher_balance_subcommand_with_at_replies_target_balance(
    wallet_gold, wallet_bot
):
    rule, tips, replies = await _run_matcher(
        _matcher, f"钱包 balance [CQ:at,qq={TARGET_ID}]", wallet_bot
    )

    assert rule is True
    assert tips == []
    assert len(replies) == 1
    assert f"的钱包余额: {TARGET_GOLD}" in replies[0]


@pytest.mark.asyncio
async def test_transfer_matcher_with_at_moves_gold(wallet_gold, wallet_bot):
    rule, tips, replies = await _run_matcher(
        _transfer_matcher, f"转账100 [CQ:at,qq={TARGET_ID}]", wallet_bot
    )

    assert rule is True, "带艾特的转账指令必须仍然命中规则"
    assert tips == []
    assert len(replies) == 1
    assert "转账成功" in replies[0]
    assert (await UserConsole.get(user_id=SENDER_ID)).gold == SENDER_GOLD - 100
    assert (await UserConsole.get(user_id=TARGET_ID)).gold == TARGET_GOLD + 100
