"""AstrBot 插件日志硬性规则：生产代码只用 astrbot.api.logger。

- 禁止 import logging / from logging import ...
- 禁止 getLogger 默认记录器
- 禁止 set_logger 注入机制残留
"""

import ast

from conftest import ROOT

# 参与运行的生产模块（不含 tests / .ccg / 缓存）
PRODUCTION_FILES = sorted(
    [
        *ROOT.glob("*.py"),
        *ROOT.glob("api/*.py"),
        *ROOT.glob("tool/*.py"),
        *ROOT.glob("skill/scripts/*.py"),
    ]
)


def test_production_files_exist():
    assert len(PRODUCTION_FILES) >= 15, "生产文件扫描清单异常"


def test_no_logging_imports_anywhere():
    for path in PRODUCTION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "logging", f"{path}: 禁止 import logging"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "logging", f"{path}: 禁止 from logging import"


def test_no_getlogger_or_set_logger():
    forbidden = {"getLogger", "set_logger"}
    for path in PRODUCTION_FILES:
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{path}: 禁止 {name} 残留"


def test_font_loader_logs_via_astrbot_api():
    source = (ROOT / "tool" / "font_loader.py").read_text(encoding="utf-8")
    assert "from astrbot.api import logger" in source
    assert source.count("logger.") >= 5, "font_loader 必须实际使用宿主 logger"


def test_shared_core_does_not_import_astrbot():
    """共享核心/Skill 包内模块保持宿主无关。"""
    shared = [
        ROOT / "tool" / "config.py",
        ROOT / "tool" / "search_service.py",
        ROOT / "tool" / "skill_package.py",
        ROOT / "tool" / "tool.py",
        ROOT / "tool" / "image_search.py",
    ]
    for path in shared:
        source = path.read_text(encoding="utf-8")
        assert "astrbot" not in source, f"{path}: 共享核心不得依赖 astrbot"
