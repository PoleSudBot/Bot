from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment
import pytest


def _load_logic_module():
    # 直接加载纯逻辑文件，避免测试导入插件入口时触发 NoneBot 注册流程。
    module_path = (
        Path(__file__).resolve().parents[1]
        / "zhenxun"
        / "plugins"
        / "name_history"
        / "_logic.py"
    )
    spec = importlib.util.spec_from_file_location(
        "name_history_logic_test", module_path
    )
    if not spec or not spec.loader:
        raise RuntimeError("无法加载历史昵称逻辑模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


logic = _load_logic_module()


def _load_plugin_tree() -> ast.Module:
    source = (
        Path(__file__).resolve().parents[1]
        / "zhenxun"
        / "plugins"
        / "name_history"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    return ast.parse(source)


def _literal_string_set(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Set):
        return set()
    return {item.value for item in node.elts if isinstance(item, ast.Constant)}


class FakeRepository:
    def __init__(self):
        self.latest: dict[str, str] = {}
        self.created: list[dict[str, Any]] = []
        self.next_id = 1

    async def get_latest_display_name(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
    ) -> str | None:
        # 测试只关心同一用户的名称类型变化，其他定位参数由调用断言覆盖。
        assert platform == "qq"
        assert group_id == "1000"
        assert user_id == "2000"
        return self.latest.get(name_type)

    async def create_history(
        self,
        *,
        platform: str,
        group_id: str,
        user_id: str,
        name_type: str,
        display_name: str,
    ) -> int:
        # 写入后同步 latest，模拟数据库下一次查询看到刚创建的记录。
        self.latest[name_type] = display_name
        record_id = self.next_id
        self.next_id += 1
        self.created.append(
            {
                "id": record_id,
                "platform": platform,
                "group_id": group_id,
                "user_id": user_id,
                "name_type": name_type,
                "display_name": display_name,
            }
        )
        return record_id


@dataclass(slots=True)
class Record:
    name_type: str
    display_name: str
    record_time: datetime
    avatar_hash: str | None = None


@dataclass(slots=True)
class AvatarRecord:
    avatar_hash: str
    avatar_uri: str
    record_time: datetime


def test_choose_history_display_name_handles_group_card_empty_rules():
    assert logic.choose_history_display_name(logic.GROUP_CARD, None, None) is None
    assert logic.choose_history_display_name(logic.GROUP_CARD, "小明", None) == "小明"
    assert logic.choose_history_display_name(logic.GROUP_CARD, "小明", "小明") is None
    assert (
        logic.choose_history_display_name(logic.GROUP_CARD, None, "小明")
        == logic.GROUP_CARD_EMPTY_LABEL
    )
    assert (
        logic.choose_history_display_name(
            logic.GROUP_CARD, None, logic.GROUP_CARD_EMPTY_LABEL
        )
        is None
    )


def test_normalize_display_name_flattens_line_breaks():
    assert logic.normalize_display_name(" 小明\n今天\t想睡觉 ") == "小明 今天 想睡觉"


def test_extract_member_snapshot_uses_onebot_card_and_nickname():
    snapshot = logic.extract_member_snapshot(
        {"card": " 群名片\nA ", "nickname": " QQ名\tA "}
    )

    assert snapshot == logic.SenderNameSnapshot(group_card="群名片 A", qq_name="QQ名 A")


def test_build_group_info_defaults_does_not_override_custom_nickname():
    defaults = logic.build_group_info_defaults(
        logic.SenderNameSnapshot(group_card="群名片", qq_name="QQ名"),
        "qq",
    )

    assert defaults == {"platform": "qq", "user_name": "QQ名"}
    assert "nickname" not in defaults


@pytest.mark.parametrize(
    ("created_count", "cache_hit", "member_exists", "expected"),
    [
        (1, True, True, True),
        (0, False, False, True),
        (0, True, False, True),
        (0, True, True, False),
    ],
)
def test_should_sync_group_info_recovers_deleted_member_rows(
    created_count: int,
    cache_hit: bool,
    member_exists: bool,
    expected: bool,
):
    assert (
        logic.should_sync_group_info(
            created_count=created_count,
            cache_hit=cache_hit,
            member_exists=member_exists,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_record_sender_snapshot_records_two_name_types_independently():
    repo = FakeRepository()
    snapshot = logic.SenderNameSnapshot(group_card="群名片A", qq_name="QQ名A")

    created_ids = await logic.record_sender_snapshot(
        repo,
        platform="qq",
        group_id="1000",
        user_id="2000",
        snapshot=snapshot,
    )

    assert created_ids == [1, 2]
    assert [(item["name_type"], item["display_name"]) for item in repo.created] == [
        (logic.GROUP_CARD, "群名片A"),
        (logic.QQ_NAME, "QQ名A"),
    ]


@pytest.mark.asyncio
async def test_record_sender_snapshot_skips_unchanged_names_and_keeps_a_b_a_chain():
    repo = FakeRepository()
    first = logic.SenderNameSnapshot(group_card="A", qq_name="QQ")
    second = logic.SenderNameSnapshot(group_card="B", qq_name="QQ")
    third = logic.SenderNameSnapshot(group_card="A", qq_name="QQ")

    assert await logic.record_sender_snapshot(
        repo, platform="qq", group_id="1000", user_id="2000", snapshot=first
    ) == [1, 2]
    assert (
        await logic.record_sender_snapshot(
            repo, platform="qq", group_id="1000", user_id="2000", snapshot=first
        )
        == []
    )
    assert await logic.record_sender_snapshot(
        repo, platform="qq", group_id="1000", user_id="2000", snapshot=second
    ) == [3]
    assert await logic.record_sender_snapshot(
        repo, platform="qq", group_id="1000", user_id="2000", snapshot=third
    ) == [4]

    assert [(item["name_type"], item["display_name"]) for item in repo.created] == [
        (logic.GROUP_CARD, "A"),
        (logic.QQ_NAME, "QQ"),
        (logic.GROUP_CARD, "B"),
        (logic.GROUP_CARD, "A"),
    ]


@pytest.mark.asyncio
async def test_record_sender_snapshot_records_group_card_becoming_empty():
    repo = FakeRepository()

    assert (
        await logic.record_sender_snapshot(
            repo,
            platform="qq",
            group_id="1000",
            user_id="2000",
            snapshot=logic.SenderNameSnapshot(group_card=None, qq_name=None),
        )
        == []
    )
    assert await logic.record_sender_snapshot(
        repo,
        platform="qq",
        group_id="1000",
        user_id="2000",
        snapshot=logic.SenderNameSnapshot(group_card="有名片", qq_name=None),
    ) == [1]
    assert await logic.record_sender_snapshot(
        repo,
        platform="qq",
        group_id="1000",
        user_id="2000",
        snapshot=logic.SenderNameSnapshot(group_card=None, qq_name=None),
    ) == [2]

    assert repo.created[-1]["display_name"] == logic.GROUP_CARD_EMPTY_LABEL


def test_format_history_records_splits_types_and_marks_latest_per_type_as_current():
    now = datetime(2026, 5, 14, 21, 3)
    records = [
        Record(logic.QQ_NAME, "QQ旧名", now - timedelta(days=2)),
        Record(logic.GROUP_CARD, "群名片旧名", now - timedelta(days=1)),
        Record(logic.QQ_NAME, "QQ当前名", now),
        Record(logic.GROUP_CARD, "群名片当前名", now - timedelta(hours=1)),
    ]

    result = logic.format_history_records(records, target_user_id="2000")

    assert "2000 的历史昵称" in result
    assert "只记录我看到过的变化" not in result
    assert "\n群名片\n" in result
    assert "\nQQ名称\n" in result
    assert result.index("\n群名片\n") < result.index("\nQQ名称\n")
    assert result.index("群名片当前名") < result.index("QQ当前名")
    assert "05-14 20:03  「群名片当前名」（当前）" in result
    assert "05-14 21:03  「QQ当前名」（当前）" in result
    assert "出现次数" not in result


def test_format_history_records_can_filter_group_card_only():
    now = datetime(2026, 5, 14, 21, 3)
    records = [
        Record(logic.QQ_NAME, "QQ名", now),
        Record(logic.GROUP_CARD, "群名片", now - timedelta(minutes=1)),
    ]

    result = logic.format_history_records(
        records,
        target_user_id="2000",
        name_type=logic.GROUP_CARD,
    )

    assert "2000 的历史群名片" in result
    assert "\n群名片\n" in result
    assert "05-14 21:02  「群名片」（当前）" in result
    assert "QQ名" not in result


def test_build_history_display_data_keeps_empty_sections_user_friendly():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.QQ_NAME, "QQ名", now)]

    data = logic.build_history_display_data(records, target_user_id="2000")

    assert data.title == "2000 的历史昵称"
    assert data.profile_name == "QQ名"
    assert [section.label for section in data.sections] == ["群名片", "QQ名称"]
    assert data.sections[0].items == []
    assert data.sections[1].items[0].display_name == "QQ名"
    assert data.sections[1].items[0].is_current
    assert data.empty_text == "暂无记录。之后我看到名称变化时会慢慢记下来。"


def test_build_history_display_data_does_not_mark_departed_member_current():
    now = datetime(2026, 5, 14, 21, 3)
    records = [
        Record(logic.GROUP_CARD, "离群前群名片", now),
        Record(logic.QQ_NAME, "离群前QQ名", now),
    ]

    data = logic.build_history_display_data(
        records,
        target_user_id="2000",
        is_current_member=False,
    )
    text = logic.format_history_records(
        records,
        target_user_id="2000",
        is_current_member=False,
    )

    assert all(
        not item.is_current for section in data.sections for item in section.items
    )
    assert "（当前）" not in text


def test_build_history_display_data_profile_name_prefers_current_group_card():
    now = datetime(2026, 5, 14, 21, 3)
    records = [
        Record(logic.QQ_NAME, "QQ当前名", now),
        Record(logic.GROUP_CARD, "群名片当前名", now - timedelta(minutes=1)),
    ]

    data = logic.build_history_display_data(records, target_user_id="2000")

    assert data.profile_name == "群名片当前名"


def test_build_history_display_data_resolves_avatar_uris():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.GROUP_CARD, "群名片", now, avatar_hash="hash-a")]

    data = logic.build_history_display_data(
        records,
        target_user_id="2000",
        avatar_uri_map={"hash-a": "file:///avatar-a.webp"},
        avatar_history=["file:///avatar-a.webp"],
        avatar_records=[
            AvatarRecord("hash-a", "file:///avatar-a.webp", now),
        ],
    )

    assert data.avatar_history == ["file:///avatar-a.webp"]
    assert data.sections[0].items[0].avatar_uri == "file:///avatar-a.webp"


def test_build_history_display_data_does_not_use_current_avatar_for_old_records():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.GROUP_CARD, "群名片", now)]

    data = logic.build_history_display_data(records, target_user_id="2000")

    assert data.sections[0].items[0].avatar_uri == ""


def test_build_history_display_data_uses_nearest_avatar_for_unbound_old_record():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.GROUP_CARD, "旧群名片", now)]

    data = logic.build_history_display_data(
        records,
        target_user_id="2000",
        avatar_uri_map={
            "hash-a": "file:///avatar-a.webp",
            "hash-b": "file:///avatar-b.webp",
        },
        avatar_records=[
            AvatarRecord("hash-a", "file:///avatar-a.webp", now - timedelta(days=2)),
            AvatarRecord("hash-b", "file:///avatar-b.webp", now + timedelta(hours=2)),
        ],
    )

    assert data.sections[0].items[0].avatar_uri == "file:///avatar-b.webp"


def test_build_history_display_data_prefers_exact_avatar_hash_over_nearest_avatar():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.GROUP_CARD, "群名片", now, avatar_hash="hash-a")]

    data = logic.build_history_display_data(
        records,
        target_user_id="2000",
        avatar_uri_map={
            "hash-a": "file:///avatar-a.webp",
            "hash-b": "file:///avatar-b.webp",
        },
        avatar_records=[
            AvatarRecord("hash-a", "file:///avatar-a.webp", now - timedelta(days=3)),
            AvatarRecord("hash-b", "file:///avatar-b.webp", now + timedelta(minutes=1)),
        ],
    )

    assert data.sections[0].items[0].avatar_uri == "file:///avatar-a.webp"


def test_build_history_display_data_prefers_earlier_avatar_when_distance_ties():
    now = datetime(2026, 5, 14, 21, 3)
    records = [Record(logic.GROUP_CARD, "旧群名片", now)]

    data = logic.build_history_display_data(
        records,
        target_user_id="2000",
        avatar_uri_map={
            "hash-a": "file:///avatar-a.webp",
            "hash-b": "file:///avatar-b.webp",
        },
        avatar_records=[
            AvatarRecord("hash-b", "file:///avatar-b.webp", now + timedelta(hours=1)),
            AvatarRecord("hash-a", "file:///avatar-a.webp", now - timedelta(hours=1)),
        ],
    )

    assert data.sections[0].items[0].avatar_uri == "file:///avatar-a.webp"


def test_format_history_records_empty_single_filter_keeps_compact_text():
    result = logic.format_history_records(
        [],
        target_user_id="2000",
        name_type=logic.QQ_NAME,
    )

    assert result == "2000 的历史QQ名称\n暂无记录。之后我看到名称变化时会慢慢记下来。"


def test_plugin_metadata_declares_all_public_aliases():
    tree = _load_plugin_tree()
    alias_sets: dict[str, set[str]] = {}
    plugin_extra_aliases = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.endswith("_ALIASES"):
                alias_sets[target.id] = _literal_string_set(node.value)
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "PluginExtraData":
            continue
        plugin_extra_aliases = any(
            keyword.arg == "aliases" for keyword in node.keywords
        )

    expected_aliases = {
        "曾用名",
        "过去昵称",
        "nickname",
        "群名片历史",
        "历史qq名",
        "QQ名历史",
        "qq名历史",
    }
    declared_aliases = set().union(*alias_sets.values())

    assert expected_aliases <= declared_aliases
    assert plugin_extra_aliases


def test_plugin_logger_calls_only_use_supported_context_keywords():
    tree = _load_plugin_tree()
    supported_keywords = {"session", "group_id", "adapter", "target", "platform", "e"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "logger"
        ):
            continue
        for keyword in node.keywords:
            assert keyword.arg in supported_keywords


def test_plugin_entry_declares_htmlrender_and_avatar_dependencies():
    tree = _load_plugin_tree()
    required_plugins: set[str] = set()
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            required_plugins.add(node.args[0].value)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)

    assert "nonebot_plugin_htmlrender" in required_plugins
    assert "template_to_pic" in imported_names
    assert "avatar_service" in imported_names


def test_plugin_entry_registers_avatar_refresh_callback_and_startup_import():
    tree = _load_plugin_tree()
    registered_refresh_callback = False
    schedules_startup_import = False
    refreshes_avatar_on_query = False

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_refresh_callback"
        ):
            registered_refresh_callback = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_existing_avatar_cache"
        ):
            schedules_startup_import = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "refresh_avatar_history_on_query"
        ):
            refreshes_avatar_on_query = True

    assert registered_refresh_callback
    assert schedules_startup_import
    assert refreshes_avatar_on_query


def test_plugin_entry_handles_member_lifecycle_and_uncached_member_queries():
    tree = _load_plugin_tree()
    imported_names: set[str] = set()
    registers_notice_matcher = False
    requests_uncached_member = False
    validates_cached_member_row = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "on_notice"
        ):
            registers_notice_matcher = True
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "get_group_member_info":
            requests_uncached_member = any(
                keyword.arg == "no_cache"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
        if node.func.attr == "exists":
            validates_cached_member_row = True

    assert {"GroupIncreaseNoticeEvent", "GroupDecreaseNoticeEvent"} <= imported_names
    assert registers_notice_matcher
    assert requests_uncached_member
    assert validates_cached_member_row


def test_avatar_history_model_and_group_name_migration_are_declared():
    root = Path(__file__).resolve().parents[1]
    group_name_source = (
        root / "zhenxun" / "models" / "group_name_history.py"
    ).read_text(encoding="utf-8")
    avatar_model_source = (
        root / "zhenxun" / "models" / "group_name_avatar_history.py"
    ).read_text(encoding="utf-8")

    assert "avatar_hash" in group_name_source
    assert "ADD COLUMN avatar_hash" in group_name_source
    assert "class GroupNameAvatarHistory" in avatar_model_source
    assert "avatar_path" not in avatar_model_source


def test_avatar_history_template_uses_wrapping_avatar_lists():
    root = Path(__file__).resolve().parents[1]
    template_source = (
        root / "zhenxun" / "plugins" / "name_history" / "templates" / "history.html"
    ).read_text(encoding="utf-8")

    assert "avatar-history" in template_source
    assert "flex-wrap: wrap" in template_source
    assert "row-avatar" in template_source


def test_avatar_service_declares_refresh_callback_hook():
    root = Path(__file__).resolve().parents[1]
    source = (root / "zhenxun" / "services" / "avatar_service.py").read_text(
        encoding="utf-8"
    )

    assert "register_refresh_callback" in source
    assert "_notify_avatar_refreshed" in source
    assert "asyncio.create_task" in source


def test_avatar_persistence_uses_hashing_threading_and_limited_import():
    root = Path(__file__).resolve().parents[1]
    source = (root / "zhenxun" / "plugins" / "name_history" / "_avatar.py").read_text(
        encoding="utf-8"
    )

    assert "sha256" in source
    assert "asyncio.to_thread" in source
    assert "IMPORT_CONCURRENCY = 2" in source
    assert "IMPORT_SLEEP_SECONDS = 0.05" in source
    assert "GroupNameHistory.filter(id__in=ids)" in source
    assert "refresh_avatar_history_on_query" in source
    assert "force_refresh=True" in source
    assert 'source="history_query"' in source


def test_resolve_query_target_supports_self_at_and_qq_number():
    assert logic.resolve_query_target(Message(""), "100").user_id == "100"
    assert (
        logic.resolve_query_target(
            Message([MessageSegment.at("200"), MessageSegment.text(" 其他")]),
            "100",
        ).user_id
        == "200"
    )
    assert logic.resolve_query_target(Message("300"), "100").user_id == "300"

    invalid = logic.resolve_query_target(Message("张三"), "100")
    assert invalid.user_id == "100"
    assert invalid.error
