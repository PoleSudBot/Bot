from pathlib import Path


CHECK_PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "zhenxun"
    / "builtin_plugins"
    / "check"
    / "__init__.py"
)


def test_check_plugin_only_keeps_message_self_check():
    source = CHECK_PLUGIN_PATH.read_text(encoding="utf-8")

    assert 'Alconna("自检")' in source
    assert "rule=to_me()" in source
    assert "permission=SUPERUSER" in source
    assert "PokeNotifyEvent" not in source
    assert "_self_check_poke_matcher" not in source
    assert "戳一戳BOT" not in source
    assert "RegisterConfig" not in source
    assert "Config.get_config" not in source
