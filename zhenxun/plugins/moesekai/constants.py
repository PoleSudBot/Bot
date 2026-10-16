from __future__ import annotations

from pathlib import Path

from zhenxun.configs.path_config import DATA_PATH

MODULE_NAME = "moesekai"
BINDING_NOTICE_PREFIX = "[南极] "

SERVERS = ("cn", "jp", "tw")
SERVER_SET = set(SERVERS)
ALL_SERVERS_KEYWORD = "all"
SERVER_LABELS = {
    "cn": "国服",
    "jp": "日服",
    "tw": "台服",
}
FEATURE_LIVE_REMINDER = "live_reminder"
FEATURE_NEW_CARD_REMINDER = "new_card_reminder"
SCOPE_GLOBAL = "global"

ALIAS_TARGET_CHARACTER = "character"
ALIAS_TARGET_MUSIC = "music"

PLUGIN_DATA_DIR = DATA_PATH / MODULE_NAME
PACKAGE_DIR = Path(__file__).resolve().parent
SEEDS_DIR = PACKAGE_DIR / "seeds"
MASTER_DATA_DIR = PLUGIN_DATA_DIR / "master"
SCREENSHOT_CACHE_DIR = PLUGIN_DATA_DIR / "screenshots"
CHARACTER_CACHE_DIR = SCREENSHOT_CACHE_DIR / "characters"
STORY_CACHE_DIR = SCREENSHOT_CACHE_DIR / "stories"
STATE_DIR = PLUGIN_DATA_DIR / "state"
ASSET_CACHE_DIR = PLUGIN_DATA_DIR / "asset_cache"
ASSET_MIRROR_DIR = PLUGIN_DATA_DIR / "assets"
PROFILE_STATIC_ASSET_DIR = ASSET_MIRROR_DIR / "profile_static"
ASSET_CACHE_INDEX_PATH = STATE_DIR / "asset_cache_index.json"
REMINDER_SNAPSHOT_DIR = PLUGIN_DATA_DIR / "reminder_snapshots"

for path in (
    PLUGIN_DATA_DIR,
    MASTER_DATA_DIR,
    SCREENSHOT_CACHE_DIR,
    CHARACTER_CACHE_DIR,
    STORY_CACHE_DIR,
    STATE_DIR,
    ASSET_CACHE_DIR,
    ASSET_MIRROR_DIR,
    PROFILE_STATIC_ASSET_DIR,
    REMINDER_SNAPSHOT_DIR,
):
    path.mkdir(parents=True, exist_ok=True)


def server_label(server: str | None) -> str:
    if not server:
        return "未设置"
    return SERVER_LABELS.get(server, server.upper())


def format_binding_notice(message: str) -> str:
    if message.startswith(BINDING_NOTICE_PREFIX.rstrip()):
        return message
    return f"{BINDING_NOTICE_PREFIX}{message}"


def make_scope_key(group_id: str | None = None, *, platform: str | None = None) -> str:
    if not group_id:
        return SCOPE_GLOBAL
    prefix = platform or "group"
    return f"{prefix}:{group_id}"


def normalize_alias(value: str) -> str:
    return "".join(value.lower().strip().split())


def plugin_data_path(*parts: str) -> Path:
    path = PLUGIN_DATA_DIR
    for part in parts:
        path = path / part
    return path
