"""Skill 脚本行为回归：付费搜图前先完成必填参数校验。"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "scripts" / "grok_search.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "grok_search_script_under_test", SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_missing_query_exits_before_reverse_search(monkeypatch, tmp_path):
    """--saucenao + 图片但缺少 --query 时，必须先本地报错（rc=2），不触发搜图。"""
    mod = _load_script()
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    calls = []

    def fake_reverse_search(args, config, images):
        calls.append(list(images))
        return {}

    monkeypatch.setattr(mod, "_run_reverse_image_search_sync", fake_reverse_search)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grok_search.py",
            "--saucenao",
            "--image-files",
            str(img),
            "--base-url",
            "https://x.example",
            "--api-key",
            "k",
        ],
    )

    rc = mod.main()
    assert rc == 2
    assert calls == []  # 缺 --query 时不得发起任何搜图
