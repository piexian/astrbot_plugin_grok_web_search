"""测试引导：以合成包名加载插件模块，使 api/* 的跨包相对导入可用。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "grok_plugin_under_test"


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
