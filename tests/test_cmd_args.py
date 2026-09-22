"""parse_cmd_args 的参数解析回归测试。"""

from tool.image_search import parse_cmd_args


def test_plain_query_untouched():
    parsed = parse_cmd_args("最近的 AI 新闻")
    assert parsed["ok"]
    assert parsed["query"] == "最近的 AI 新闻"
    assert not parsed["use_serpapi"]
    assert not parsed["use_saucenao"]
    assert parsed["search_depth"] == "basic"


def test_all_expands_to_both_and_deep():
    parsed = parse_cmd_args("--all 找出处")
    assert parsed["ok"]
    assert parsed["use_serpapi"] and parsed["use_saucenao"]
    assert parsed["search_depth"] == "deep"
    assert parsed["query"] == "找出处"


def test_all_overrides_earlier_depth():
    parsed = parse_cmd_args("--depth advanced --all 查证")
    assert parsed["ok"]
    assert parsed["search_depth"] == "deep"


def test_all_overrides_later_depth():
    parsed = parse_cmd_args("--all --depth advanced 查证")
    assert parsed["ok"]
    assert parsed["search_depth"] == "deep"


def test_individual_flags():
    parsed = parse_cmd_args("--serpapi --depth deep 图片来源")
    assert parsed["ok"]
    assert parsed["use_serpapi"]
    assert not parsed["use_saucenao"]
    assert parsed["search_depth"] == "deep"


def test_depth_alias_and_inline_value():
    assert parse_cmd_args("--depth=advanced x")["search_depth"] == "advanced"
    assert parse_cmd_args("--search-depth deep y")["search_depth"] == "deep"


def test_terminator_keeps_literal_flags():
    parsed = parse_cmd_args("-- --all 不是参数")
    assert parsed["ok"]
    assert not parsed["use_serpapi"]
    assert parsed["query"] == "--all 不是参数"


def test_unknown_flag_rejected():
    parsed = parse_cmd_args("--nope x")
    assert not parsed["ok"]
    assert "未知参数" in parsed["error"]


def test_depth_missing_value_rejected():
    parsed = parse_cmd_args("--depth")
    assert not parsed["ok"]


def test_depth_invalid_value_rejected():
    parsed = parse_cmd_args("--depth max x")
    assert not parsed["ok"]


def test_flags_only_query_empty():
    parsed = parse_cmd_args("--serpapi --saucenao")
    assert parsed["ok"]
    assert parsed["query"] == ""


def test_bool_flag_with_value_rejected():
    parsed = parse_cmd_args("--all=1 x")
    assert not parsed["ok"]
