"""共享配置模块：分组/平铺兼容、JSON 文本解析与旧配置迁移。"""

from conftest import load

config_mod = load("tool.config")
tool = load("tool.tool")


def test_config_paths_match_schema_groups():
    assert config_mod.CONFIG_PATHS["base_url"] == ("connection_settings", "base_url")
    assert config_mod.CONFIG_PATHS["extra_body"] == ("advanced_settings", "extra_body")
    assert config_mod.CONFIG_DEFAULTS["timeout_seconds"] == 60
    assert config_mod.CONFIG_DEFAULTS["model"] == tool.DEFAULT_MODEL


def test_config_value_grouped_flat_and_default():
    config = {
        "connection_settings": {"base_url": "https://grouped.example"},
        "flat_key": "flat",
    }
    assert config_mod.config_value(config, "base_url") == "https://grouped.example"
    assert config_mod.config_value(
        {"base_url": "https://flat.example"}, "base_url"
    ) == ("https://flat.example")
    assert config_mod.config_value({}, "timeout_seconds") is None
    assert config_mod.config_value({}, "timeout_seconds", 30) == 30


def test_parse_json_setting_accepts_dict_and_text():
    assert config_mod.parse_json_setting({"a": 1}) == ({"a": 1}, None)
    assert config_mod.parse_json_setting('{"a": 1}') == ({"a": 1}, None)
    assert config_mod.parse_json_setting("") == ({}, None)
    assert config_mod.parse_json_setting("   ") == ({}, None)
    assert config_mod.parse_json_setting(None) == ({}, None)
    assert config_mod.parse_json_setting(123) == ({}, None)


def test_parse_json_setting_returns_error_for_invalid_json():
    result, error = config_mod.parse_json_setting("not json")
    assert result == {}
    assert error and "JSON" in error


def test_migrate_legacy_config_moves_and_resets():
    config = {"timeout_seconds": 90}
    changed = config_mod.migrate_legacy_config(config)
    assert changed is True
    assert config["connection_settings"]["timeout_seconds"] == 90
    assert config["timeout_seconds"] == 60  # 归位为默认值，避免重复迁移

    # 已迁移过的配置不再变更
    assert config_mod.migrate_legacy_config(config) is False


def test_migrate_legacy_config_keeps_non_default_grouped_value():
    config = {
        "timeout_seconds": 90,
        "connection_settings": {"timeout_seconds": 120},
    }
    assert config_mod.migrate_legacy_config(config) is False
    assert config["connection_settings"]["timeout_seconds"] == 120


def test_migrate_legacy_config_calls_save_on_change():
    config = {"timeout_seconds": 90}
    calls = []
    config_mod.migrate_legacy_config(config, save=lambda: calls.append(1))
    assert calls == [1]
    config_mod.migrate_legacy_config(config, save=lambda: calls.append(1))
    assert calls == [1]
