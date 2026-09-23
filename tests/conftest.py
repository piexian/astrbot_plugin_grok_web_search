"""测试引导：以合成包名加载插件模块，使 api/* 的跨包相对导入可用。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "grok_plugin_under_test"


def _stub_astrbot_if_missing():
    """CI 无宿主依赖时提供窄范围 astrbot.api.logger stub（仅测试用）。

    真实 astrbot 存在时，把 ASTRBOT_ROOT 指到临时目录，
    避免其 import 副作用在仓库内创建 data/ 默认文件。
    """
    import os
    import tempfile

    os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="astrbot-test-"))
    try:
        import astrbot.api  # noqa: F401

        return
    except ImportError:
        pass

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")

    class _StubLogger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    api_mod.logger = _StubLogger()
    astrbot_mod.api = api_mod
    sys.modules.setdefault("astrbot", astrbot_mod)
    sys.modules.setdefault("astrbot.api", api_mod)


_stub_astrbot_if_missing()


def _setup_package():
    if PKG in sys.modules:
        return
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PKG] = pkg
    for sub in ("tool", "api"):
        name = f"{PKG}.{sub}"
        mod = types.ModuleType(name)
        mod.__path__ = [str(ROOT / sub)]
        sys.modules[name] = mod
        setattr(pkg, sub, mod)


_setup_package()


def load(rel: str):
    """加载插件子模块（如 load('tool.image_search')、load('api.saucenao')）。"""
    return importlib.import_module(f"{PKG}.{rel}")
