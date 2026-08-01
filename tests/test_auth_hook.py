from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace

import nonebot
from nonebot.exception import IgnoredException
import pytest

nonebot.init()

auth_hook = importlib.import_module("zhenxun.builtin_plugins.hooks.auth_hook")
auth_checker = importlib.import_module("zhenxun.builtin_plugins.hooks.auth_checker")
auth_cost_mod = importlib.import_module(
    "zhenxun.builtin_plugins.hooks.auth.auth_cost"
)
auth_group_mod = importlib.import_module(
    "zhenxun.builtin_plugins.hooks.auth.auth_group"
)
auth_utils_mod = importlib.import_module("zhenxun.builtin_plugins.hooks.auth.utils")
auth_exception_mod = importlib.import_module(
    "zhenxun.builtin_plugins.hooks.auth.exception"
)
init_plugin = importlib.import_module("zhenxun.builtin_plugins.init")
EventSession = importlib.import_module("nonebot_plugin_session").EventSession
PluginType = importlib.import_module("zhenxun.utils.enum").PluginType
InsufficientGold = importlib.import_module(
    "zhenxun.utils.exception"
).InsufficientGold


class _FakeEvent:
    def __init__(
        self,
        *,
        event_type: str = "message",
        user_id: int | None = 123,
        group_id: int | None = 456,
        channel_id: int | None = None,
        self_id: int = 3072536533,
        event_time: int = 1_776_182_486,
        plaintext: str = "测试命令",
    ) -> None:
        self.user_id = user_id
        self.group_id = group_id
        self.channel_id = channel_id
        self.self_id = self_id
        self.time = event_time
        self._event_type = event_type
        self._plaintext = plaintext

    def get_type(self) -> str:
        return self._event_type

    def get_plaintext(self) -> str:
        return self._plaintext


class _FakeAdapter:
    def __init__(self):
        self.connections = {"3072536533": object()}
        self.onebot_config = SimpleNamespace(onebot_api_roots={})

    def get_name(self) -> str:
        return "OneBot V11"


def _make_bot():
    return SimpleNamespace(
        self_id="3072536533",
        adapter=_FakeAdapter(),
        config=SimpleNamespace(superusers={"1163272259"}),
    )


def _make_session():
    return SimpleNamespace(
        id1="123",
        id2="456",
        id3=None,
        platform="qq",
        bot_type="OneBot V11",
    )


def _make_matcher():
    return SimpleNamespace(
        plugin=SimpleNamespace(name="demo", module_name="demo.module"),
        plugin_name="demo",
        state={},
        type="message",
    )


@pytest.fixture(scope="session", autouse=True)
async def nonebug_init(_nonebot_init: None, after_nonebot_init: None):
    return None


def test_auth_hooks_depend_on_event_session():
    preprocessor_sig = inspect.signature(auth_hook._auth_preprocessor)
    postprocessor_sig = inspect.signature(auth_hook._unblock_after_matcher)

    assert preprocessor_sig.parameters["session"].annotation is EventSession
    assert postprocessor_sig.parameters["session"].annotation is EventSession


@pytest.mark.asyncio
async def test_drop_message_before_uninfo_lookup_when_transport_unavailable(
    monkeypatch,
):
    noted: list[str] = []
    bot = _make_bot()
    event = _FakeEvent()

    monkeypatch.setattr(auth_hook, "is_cache_ready", lambda: True)
    monkeypatch.setattr(auth_hook, "_get_event_bot", lambda _event: bot)
    monkeypatch.setattr(auth_hook.onebot_transport, "should_drop", lambda _bot: True)
    monkeypatch.setattr(
        auth_hook.onebot_transport,
        "note_unavailable",
        lambda _bot, action, extra=None: noted.append(f"{action}|{extra}") or True,
    )

    with pytest.raises(IgnoredException):
        await auth_hook._drop_message_before_cache_ready(event)

    assert noted


@pytest.mark.asyncio
async def test_auth_preprocessor_builds_local_light_session(monkeypatch):
    captured: dict[str, object] = {}
    bot = _make_bot()
    matcher = _make_matcher()
    event = _FakeEvent()
    session = _make_session()
    state: dict[str, object] = {}

    async def _fake_route_context(_text, _event_cache):
        return {"demo"}

    async def _fake_route_precheck(*_args, **_kwargs):
        return False

    async def _fake_auth(_matcher, _event, _bot, light_session, *_args, **_kwargs):
        captured["session"] = light_session

    monkeypatch.setattr(auth_hook, "is_cache_ready", lambda: True)
    monkeypatch.setattr(auth_hook, "_get_route_context", _fake_route_context)
    monkeypatch.setattr(auth_hook, "route_precheck", _fake_route_precheck)
    monkeypatch.setattr(auth_hook, "auth", _fake_auth)

    await auth_hook._auth_preprocessor(
        matcher=matcher,
        event=event,
        bot=bot,
        session=session,
        state=state,
        message=None,
    )

    light_session = state["_zx_light_session"]
    entity = state["_zx_entity"]

    assert light_session.user.id == "123"
    assert entity.user_id == "123"
    assert entity.group_id == "456"
    assert captured["session"].user.id == "123"


@pytest.mark.asyncio
async def test_unblock_after_matcher_uses_local_light_session(monkeypatch):
    calls: list[tuple[str, str, str | None, str | None]] = []

    monkeypatch.setattr(
        auth_hook.LimitManager,
        "unblock",
        lambda module, user_id, group_id, channel_id: calls.append(
            (module, user_id, group_id, channel_id)
        ),
    )

    await auth_hook._unblock_after_matcher(
        matcher=_make_matcher(),
        bot=_make_bot(),
        session=_make_session(),
        event=_FakeEvent(),
    )

    assert calls == [("demo", "123", "456", None)]


@pytest.mark.asyncio
async def test_init_hook_skips_group_sync_until_transport_ready(monkeypatch):
    bot = _make_bot()
    noted: list[str] = []

    async def _fake_wait_until_ready(*_args, **_kwargs):
        return False

    async def _unexpected_group_list(*_args, **_kwargs):
        raise AssertionError("transport 未就绪时不应拉取群列表")

    monkeypatch.setattr(init_plugin.PlatformUtils, "get_platform", lambda _bot: "qq")
    monkeypatch.setattr(
        init_plugin.onebot_transport, "wait_until_ready", _fake_wait_until_ready
    )
    monkeypatch.setattr(
        init_plugin.onebot_transport,
        "note_unavailable",
        lambda _bot, action, extra=None: noted.append(f"{action}|{extra}") or True,
    )
    monkeypatch.setattr(
        init_plugin.PlatformUtils, "get_group_list", _unexpected_group_list
    )

    await init_plugin._sync_group_auth_on_bot_connect(bot)

    assert noted


@pytest.mark.asyncio
async def test_init_hook_syncs_groups_after_transport_recovers(monkeypatch):
    bot = _make_bot()
    group = SimpleNamespace(group_id="456", group_name="测试群", group_flag=0)
    bulk_create_calls: list[list[object]] = []

    class _FakeQuery:
        async def values_list(self, *_args, **_kwargs):
            return []

    class _FakeFilter:
        async def update(self, **_kwargs):
            return 0

    async def _fake_wait_until_ready(*_args, **_kwargs):
        return True

    async def _fake_get_group_list(*_args, **_kwargs):
        return [group], "qq"

    async def _fake_bulk_create(groups, *_args, **_kwargs):
        bulk_create_calls.append(groups)

    monkeypatch.setattr(init_plugin.PlatformUtils, "get_platform", lambda _bot: "qq")
    monkeypatch.setattr(
        init_plugin.onebot_transport, "wait_until_ready", _fake_wait_until_ready
    )
    monkeypatch.setattr(
        init_plugin.PlatformUtils, "get_group_list", _fake_get_group_list
    )
    monkeypatch.setattr(init_plugin.GroupConsole, "all", lambda: _FakeQuery())
    monkeypatch.setattr(init_plugin.GroupConsole, "bulk_create", _fake_bulk_create)
    monkeypatch.setattr(
        init_plugin.GroupConsole, "filter", lambda **_kwargs: _FakeFilter()
    )

    await init_plugin._sync_group_auth_on_bot_connect(bot)

    assert group.group_flag == 1
    assert bulk_create_calls == [[group]]


@pytest.mark.asyncio
async def test_auth_cost_appends_sign_in_hint_to_existing_message(monkeypatch):
    sent_messages: list[str] = []

    async def _fake_send_message(_session, message, *_args, **_kwargs):
        sent_messages.append(message)

    monkeypatch.setattr(auth_cost_mod, "send_message", _fake_send_message)

    plugin = SimpleNamespace(cost_gold=100, name="测试功能", module="demo")
    user = SimpleNamespace(gold=1)

    with pytest.raises(auth_exception_mod.SkipPluginException):
        await auth_cost_mod.auth_cost(user, plugin, _make_session())

    assert sent_messages == ["金币不足..该功能需要100金币（也许..可以试试签到？）"]


@pytest.mark.asyncio
async def test_get_plugin_cost_timeout_stops_paid_feature(monkeypatch):
    sent_messages: list[str] = []

    async def _fake_with_timeout(coro, **_kwargs):
        coro.close()
        raise TimeoutError

    async def _fake_send_message(_session, message, *_args, **_kwargs):
        sent_messages.append(message)

    monkeypatch.setattr(auth_checker, "with_timeout", _fake_with_timeout)
    monkeypatch.setattr(auth_checker, "send_message", _fake_send_message)
    plugin = SimpleNamespace(module="paid_demo", plugin_type=PluginType.NORMAL)

    with pytest.raises(IgnoredException, match="gold cost timeout"):
        await auth_checker.get_plugin_cost(
            _make_bot(), SimpleNamespace(gold=100), plugin, _make_session()
        )

    assert sent_messages == ["金币费用查询超时，本次功能未执行，请稍后重试。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [InsufficientGold(), TimeoutError()])
async def test_reduce_gold_failure_stops_paid_feature(monkeypatch, failure):
    sent_messages: list[str] = []

    async def _fake_reduce_gold(*_args, **_kwargs):
        raise failure

    async def _fake_send_message(_session, message, *_args, **_kwargs):
        sent_messages.append(message)

    monkeypatch.setattr(auth_checker.UserConsole, "reduce_gold", _fake_reduce_gold)
    monkeypatch.setattr(auth_checker, "send_message", _fake_send_message)
    monkeypatch.setattr(
        auth_checker.PlatformUtils, "get_platform", lambda _session: "qq"
    )

    with pytest.raises(IgnoredException):
        await auth_checker.reduce_gold("123", "paid_demo", 20, _make_session())

    assert len(sent_messages) == 1
    assert "本次功能未执行" in sent_messages[0]


@pytest.mark.asyncio
async def test_auth_group_sends_group_level_tip(monkeypatch):
    sent_messages: list[tuple[str, str | None, bool]] = []

    async def _fake_send_message(
        _session, message, check_tag=None, background=False
    ):
        sent_messages.append((message, check_tag, background))

    monkeypatch.setattr(auth_group_mod, "send_message", _fake_send_message)
    monkeypatch.setattr(
        auth_group_mod.freq,
        "is_send_group_level_message",
        lambda *_args, **_kwargs: True,
    )

    plugin = SimpleNamespace(level=7, name="测试功能", module="demo")
    group = SimpleNamespace(level=5, status=True)

    with pytest.raises(auth_exception_mod.SkipPluginException):
        await auth_group_mod.auth_group(
            plugin,
            group,
            "测试命令",
            "456",
            _make_session(),
            False,
        )

    assert sent_messages == [
        (
            "当前群权限等级不足喔，该功能需要群权限等级: 7，当前群的群权限等级: 5",
            "456",
            True,
        )
    ]


def test_group_level_tip_has_independent_switch(monkeypatch):
    checked: list[str] = []
    limiter = SimpleNamespace(check=lambda sid: checked.append(sid) or True)
    freq = auth_utils_mod.FreqUtils.__new__(auth_utils_mod.FreqUtils)
    freq._flmt_s = limiter
    plugin = SimpleNamespace(plugin_type=PluginType.NORMAL, ignore_prompt=False)

    monkeypatch.setattr(
        auth_utils_mod,
        "base_config",
        {
            "IS_SEND_TIP_MESSAGE": False,
            "IS_SEND_GROUP_LEVEL_TIP_MESSAGE": True,
        },
    )

    assert not auth_utils_mod.FreqUtils.is_send_limit_message(
        freq, plugin, "456", False
    )
    assert auth_utils_mod.FreqUtils.is_send_group_level_message(
        freq, plugin, "456", False
    )
    assert checked == ["456"]
