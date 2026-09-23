"""main.py 相对导入的静态接线检查。

main.py 依赖 astrbot 运行时无法直接导入，这里解析其 AST，
校验每个 `from .xxx import` 的符号确实存在于目标模块，
防止「模块级 ImportError 导致整个插件无法加载」的回归。
"""

import ast

from conftest import ROOT, load

MODULES = {
    "tool.tool": load("tool.tool"),
    "tool.image_search": load("tool.image_search"),
    "tool.card_render": load("tool.card_render"),
    "api.grok_chat": load("api.grok_chat"),
    "api.grok_responses": load("api.grok_responses"),
    "api.saucenao": load("api.saucenao"),
    "api.serpapi_lens": load("api.serpapi_lens"),
}


def test_main_relative_imports_resolve():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module in MODULES
        ):
            mod = MODULES[node.module]
            for alias in node.names:
                assert hasattr(mod, alias.name), (
                    f"main.py: from .{node.module} import {alias.name} — "
                    "符号不存在于目标模块，插件加载将失败"
                )
                checked += 1
    assert checked >= 15  # 确保检查确实覆盖了 main.py 的相对导入
