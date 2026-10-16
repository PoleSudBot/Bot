from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pytest


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text


class _Image:
    def __init__(self, *, url: str | None = None, path: str | None = None) -> None:
        self.url = url
        self.path = path

    def __str__(self) -> str:
        return "[图片]"


class _Reply:
    def __init__(self, message_id: str) -> None:
        self.id = message_id

    def __str__(self) -> str:
        return "[引用消息]"


class _At:
    def __init__(self, flag: str, target: str) -> None:
        self.flag = flag
        self.target = target

    def __str__(self) -> str:
        return f"@{self.target}"


class _FakeUniMessage(list):
    def __init__(self, items=None) -> None:
        super().__init__(items or [])

    def dump(self, _media_save_dir=None):
        data = []
        for item in self:
            if isinstance(item, _Text):
                data.append({"type": "text", "text": item.text})
            elif isinstance(item, _Image):
                segment = {"type": "image"}
                if item.url:
                    segment["url"] = item.url
                if item.path:
                    segment["path"] = item.path
                data.append(segment)
            elif isinstance(item, _Reply):
                data.append({"type": "reply", "id": item.id})
            elif isinstance(item, _At):
                data.append({"type": "at", "flag": item.flag, "target": item.target})
        return data

    def load(self, data):
        items = []
        for segment in data:
            if segment["type"] == "text":
                items.append(_Text(segment["text"]))
            elif segment["type"] == "image":
                items.append(_Image(url=segment.get("url"), path=segment.get("path")))
            elif segment["type"] == "reply":
                items.append(_Reply(segment["id"]))
            elif segment["type"] == "at":
                items.append(_At(segment["flag"], segment["target"]))
        return _FakeUniMessage(items)

    def __str__(self) -> str:
        return "".join(str(item) for item in self)


class _FakeAsyncHttpx:
    downloads: ClassVar[list[tuple[str, Path]]] = []

    @classmethod
    async def download_file(cls, url: str, path: str | Path):
        image_path = Path(path)
        image_path.write_bytes(b"image")
        cls.downloads.append((url, image_path))
        return True


class _FakePlatformUtils:
    @staticmethod
    def get_platform(_session) -> str:
        return "qq"


class _FakeLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _module(name: str, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_helpers():
    module_path = (
        Path(__file__).resolve().parents[1] / "zhenxun" / "utils" / "welcome_message.py"
    )
    spec = importlib.util.spec_from_file_location(
        "welcome_message_helpers_under_test", module_path
    )
    if not spec or not spec.loader:
        raise RuntimeError("无法加载 welcome_message 测试模块")
    module = importlib.util.module_from_spec(spec)
    stub = _module(
        "nonebot_plugin_alconna",
        At=_At,
        Text=_Text,
        UniMessage=_FakeUniMessage,
    )
    previous = sys.modules.get("nonebot_plugin_alconna")
    try:
        sys.modules["nonebot_plugin_alconna"] = stub
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("nonebot_plugin_alconna", None)
        else:
            sys.modules["nonebot_plugin_alconna"] = previous
    module._load_message_types = lambda: (_At, _FakeUniMessage)
    return module


def _load_manager(tmp_path: Path, helpers):
    module_path = (
        Path(__file__).resolve().parents[1]
        / "zhenxun"
        / "builtin_plugins"
        / "admin"
        / "welcome_message"
        / "data_source.py"
    )
    spec = importlib.util.spec_from_file_location(
        "welcome_message_manager_under_test", module_path
    )
    if not spec or not spec.loader:
        raise RuntimeError("无法加载 welcome_message data_source 测试模块")
    module = importlib.util.module_from_spec(spec)
    driver = SimpleNamespace()
    lifecycle = SimpleNamespace(on_startup=lambda **_kwargs: lambda func: func)
    stubs = {
        "nonebot": _module("nonebot", get_driver=lambda: driver),
        "nonebot_plugin_alconna": _module(
            "nonebot_plugin_alconna",
            UniMessage=_FakeUniMessage,
            UniMsg=_FakeUniMessage,
        ),
        "nonebot_plugin_uninfo": _module(
            "nonebot_plugin_uninfo", Uninfo=type("Uninfo", (), {})
        ),
        "zhenxun.configs.path_config": _module(
            "zhenxun.configs.path_config", DATA_PATH=tmp_path
        ),
        "zhenxun.services.log": _module("zhenxun.services.log", logger=_FakeLogger()),
        "zhenxun.utils._build_image": _module(
            "zhenxun.utils._build_image", BuildImage=type("BuildImage", (), {})
        ),
        "zhenxun.utils._image_template": _module(
            "zhenxun.utils._image_template",
            ImageTemplate=type("ImageTemplate", (), {}),
        ),
        "zhenxun.utils.http_utils": _module(
            "zhenxun.utils.http_utils", AsyncHttpx=_FakeAsyncHttpx
        ),
        "zhenxun.utils.manager.priority_manager": _module(
            "zhenxun.utils.manager.priority_manager", PriorityLifecycle=lifecycle
        ),
        "zhenxun.utils.platform": _module(
            "zhenxun.utils.platform", PlatformUtils=_FakePlatformUtils
        ),
        "zhenxun.utils.welcome_message": helpers,
    }
    previous_modules = {name: sys.modules.get(name) for name in stubs}
    try:
        sys.modules.update(stubs)
        spec.loader.exec_module(module)
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    _FakeAsyncHttpx.downloads.clear()
    return module


def test_strip_command_and_load_old_reply_data():
    helpers = _load_helpers()
    message = _FakeUniMessage([_Reply("1000"), _Text("设置欢迎消息 -at 欢迎新人")])

    helpers.strip_welcome_command(message, "设置欢迎消息")
    loaded = helpers.load_welcome_message(
        message.dump(True), user_id="2000", at_user=True
    )

    assert message.dump(True)[1]["text"] == "-at 欢迎新人"
    assert loaded.dump(True) == [
        {"type": "at", "flag": "user", "target": "2000"},
        {"type": "text", "text": "-at 欢迎新人"},
    ]


def test_select_random_old_polluted_message_is_sanitized(monkeypatch):
    helpers = _load_helpers()
    data = {
        "normal": {
            "at": False,
            "status": True,
            "message": [{"type": "text", "text": "正常欢迎"}],
        },
        "polluted": {
            "at": True,
            "status": True,
            "message": [
                {"type": "reply", "id": "1000"},
                {"type": "text", "text": "，欢迎新人"},
            ],
        },
    }
    monkeypatch.setattr(helpers.random, "choice", lambda _items: "polluted")

    message = helpers.select_welcome_message(data, "2000")

    assert message.dump(True) == [
        {"type": "at", "flag": "user", "target": "2000"},
        {"type": "text", "text": "，欢迎新人"},
    ]


@pytest.mark.asyncio
async def test_manager_save_filters_reply_and_preserves_image_and_at(tmp_path: Path):
    helpers = _load_helpers()
    manager_module = _load_manager(tmp_path, helpers)
    path = tmp_path / "welcome_message" / "qq" / "100"
    path.mkdir(parents=True)
    message = _FakeUniMessage(
        [
            _Reply("1000"),
            _Text("-at 欢迎"),
            _Image(url="https://example.com/welcome.png"),
        ]
    )

    saved = await manager_module.Manager.save(path, message)

    assert str(saved) == "欢迎[图片]"
    json_data = manager_module.json.load((path / "text.json").open(encoding="utf8"))
    welcome_data = next(iter(json_data.values()))
    assert welcome_data["at"] is True
    assert [segment["type"] for segment in welcome_data["message"]] == [
        "text",
        "image",
    ]
    assert welcome_data["message"][0]["text"] == "欢迎"
    assert Path(welcome_data["message"][1]["path"]).exists()


@pytest.mark.asyncio
async def test_manager_view_and_delete_sanitize_old_reply_data(tmp_path: Path):
    helpers = _load_helpers()
    manager_module = _load_manager(tmp_path, helpers)
    session = SimpleNamespace(group=SimpleNamespace(id="100", parent=None))
    path = manager_module.Manager.get_path(session)
    assert path is not None
    message_file = path / "text.json"
    old_data = {
        "old": {
            "at": True,
            "status": True,
            "message": [
                {"type": "reply", "id": "1000"},
                {"type": "text", "text": "欢迎新人"},
            ],
        }
    }
    with message_file.open("w", encoding="utf8") as file:
        manager_module.json.dump(old_data, file, ensure_ascii=False, indent=4)

    viewed = await manager_module.Manager.get_group_message(session, 0)
    deleted = await manager_module.Manager.delete_group_message(session, 0)

    assert viewed.dump(True) == [{"type": "text", "text": "欢迎新人"}]
    assert deleted == "删除群组欢迎消息成功！消息内容: 欢迎新人"
    with message_file.open(encoding="utf8") as file:
        assert manager_module.json.load(file) == {}
