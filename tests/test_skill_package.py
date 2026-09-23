"""Skill 自包含打包、持久化同步与插件安装时序回归。"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import ROOT, load

skill_package = load("tool.skill_package")


def test_manifest_files_all_exist():
    for rel in skill_package.MANAGED_FILES:
        assert (ROOT / rel).is_file(), f"受管清单文件缺失: {rel}"


def test_manifest_covers_shared_core_and_excludes_noise():
    rels = set(skill_package.MANAGED_FILES)
    assert "skill/SKILL.md" in rels
    assert "tool/config.py" in rels and "tool/search_service.py" in rels
    assert "api/grok_chat.py" in rels
    # 测试、缓存、渲染模块（PIL 依赖）不进 Skill 包
    assert not any("test" in r for r in rels)
    assert "tool/card_render.py" not in rels
    assert "tool/font_loader.py" not in rels


def test_sync_to_persistent_copies_and_preserves_config(tmp_path):
    (tmp_path / "config.json").write_text('{"stale": true}', encoding="utf-8")
    synced = skill_package.sync_to_persistent(tmp_path)
    assert synced is None
    assert (tmp_path / "SKILL.md").is_file()
    assert (tmp_path / "scripts" / "grok_search.py").is_file()
    assert (tmp_path / "tool" / "config.py").is_file()
    assert (tmp_path / "api" / "saucenao.py").is_file()
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == '{"stale": true}'


def test_sync_to_persistent_prunes_only_snapshot_managed_files(tmp_path, monkeypatch):
    """清理只针对上一份快照记录过的受管文件；用户文件不动。"""
    # 第一次同步：写入快照
    skill_package.sync_to_persistent(tmp_path)
    assert (tmp_path / "tool" / "tool.py").is_file()

    # 模拟清单缩减：移除 tool/config.py 后再同步，旧文件应被清理
    reduced = tuple(
        rel for rel in skill_package.MANAGED_FILES if rel != "tool/config.py"
    )
    monkeypatch.setattr(skill_package, "MANAGED_FILES", reduced)
    skill_package.sync_to_persistent(tmp_path)
    assert not (tmp_path / "tool" / "config.py").exists()
    assert (tmp_path / "tool" / "tool.py").is_file()


def test_sync_never_deletes_user_config_or_unmanaged_files(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.local.json").write_text("{}", encoding="utf-8")
    user_note = tmp_path / "scripts" / "my-notes.txt"
    user_note.parent.mkdir(parents=True)
    user_note.write_text("keep me", encoding="utf-8")
    skill_package.sync_to_persistent(tmp_path)
    # 未在快照中的用户文件原样保留
    assert user_note.read_text(encoding="utf-8") == "keep me"
    assert (tmp_path / "config.json").is_file()


def test_sync_fails_when_manifest_file_missing(tmp_path, monkeypatch):
    broken = skill_package.MANAGED_FILES + ("tool/__missing__.py",)
    monkeypatch.setattr(skill_package, "MANAGED_FILES", broken)
    with pytest.raises(FileNotFoundError, match="清单文件缺失"):
        skill_package.sync_to_persistent(tmp_path)


def test_build_zip_missing_file_raises(tmp_path, monkeypatch):
    skill_package.sync_to_persistent(tmp_path)
    (tmp_path / "tool" / "tool.py").unlink()
    with pytest.raises(FileNotFoundError, match="包文件缺失"):
        skill_package.build_zip(tmp_path / "s.zip", tmp_path)


def test_sync_rejects_symlink_target(tmp_path):
    link = tmp_path / "tool"
    link.mkdir()
    (link / "tool.py").symlink_to("/etc/hostname")
    with pytest.raises(ValueError, match="symlink"):
        skill_package.sync_to_persistent(tmp_path)


def test_installed_package_imports_without_astrbot(tmp_path):
    """真实安装产物可独立导入：共享核心不得隐式依赖 astrbot。"""
    import subprocess

    skill_package.sync_to_persistent(tmp_path)
    code = (
        f"import sys; sys.path.insert(0, r'{tmp_path}'); "
        "import tool.config, tool.tool, tool.search_service, tool.image_search; "
        "import api.grok_chat, api.grok_responses; "
        "assert 'astrbot' not in sys.modules; print('standalone-ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "standalone-ok" in proc.stdout


def test_build_zip_contains_managed_files_under_skill_root(tmp_path):
    skill_package.sync_to_persistent(tmp_path)
    zip_path = tmp_path / "skill.zip"
    skill_package.build_zip(zip_path, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "grok-search/SKILL.md" in names
    assert "grok-search/scripts/grok_search.py" in names
    assert "grok-search/tool/tool.py" in names
    assert "grok-search/api/grok_chat.py" in names
    assert not any(name.endswith("/") for name in names)


def test_installed_skill_runs_search_and_fetch_outside_repo(tmp_path):
    """安装产物在非仓库 cwd 下真实执行搜索与抓取（Mock transport，不发网络）。"""
    import subprocess
    import textwrap

    skill_package.sync_to_persistent(tmp_path)
    script = tmp_path / "scripts" / "grok_search.py"
    assert script.is_file()

    driver = textwrap.dedent(
        """
        import importlib.util, json, os, sys
        from pathlib import Path

        skill_root = Path(sys.argv[1])
        result_path = Path(sys.argv[2])
        sys.path.insert(0, str(skill_root))

        # 模拟 aiohttp transport：只记录请求，不发网络
        captured = []

        class Response:
            status = 200
            headers = {'Content-Type': 'application/json'}
            def __init__(self, payload):
                self._payload = payload
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def text(self):
                return self._payload

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            def post(self, url, **kwargs):
                captured.append((url, kwargs.get('headers', {}).get('Authorization')))
                payload = {'choices': [{'message': {'content': 'InstalledAnswer'}}],
                           'usage': {'total_tokens': 3}}
                return Response(json.dumps(payload))

        import aiohttp
        aiohttp.ClientSession = Session

        spec = importlib.util.spec_from_file_location(
            'installed_skill', skill_root / 'scripts' / 'grok_search.py'
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mod._load_astrbot_plugin_config = lambda: ({}, '')
        mod._default_skill_config_paths = lambda: [str(skill_root / 'none.json')]
        mod._default_user_config_path = lambda: str(skill_root / 'none-user.json')

        os.environ['GROK_BASE_URL'] = 'https://installed.invalid'
        os.environ['GROK_API_KEY'] = 'installed-fixture'

        for argv in (
            ['--query', 'Q', '--output', 'llm'],
            ['--fetch-url', 'https://example.org', '--output', 'llm'],
        ):
            sys.argv = ['grok_search.py', *argv]
            rc = mod.main()
            assert rc == 0, rc
        result_path.write_text(json.dumps({'captured': captured}))
        """
    )

    result_file = tmp_path / "captured.json"
    proc = subprocess.run(
        [sys.executable, "-c", driver, str(tmp_path), str(result_file)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path.parent),  # 非仓库 cwd
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(result_file.read_text(encoding="utf-8"))["captured"]
    assert len(seen) == 2, seen  # 搜索 + 抓取各一次
    assert all(url.startswith("https://installed.invalid/v1/") for url, _ in seen)
    assert all(auth == "Bearer installed-fixture" for _, auth in seen)


def test_installed_skill_consumes_restored_private_config(tmp_path):
    """安装后恢复的私有 config.json 被安装态脚本实际读取（非仅文件存在）。"""
    import subprocess
    import textwrap

    persistent = _prepare_persistent(
        tmp_path / "persistent",
        {"base_url": "https://private.invalid", "api_key": "private-fixture"},
    )
    host = _FakeHostManager(tmp_path / "skills")
    skill_package.install_skill_package(host, persistent)
    installed = Path(host.skills_root) / "grok-search"
    assert json.loads((installed / "config.json").read_text(encoding="utf-8")) == {
        "base_url": "https://private.invalid",
        "api_key": "private-fixture",
    }

    driver = textwrap.dedent(
        """
        import importlib.util, json, sys
        from pathlib import Path

        skill_root = Path(sys.argv[1])
        result_path = Path(sys.argv[2])
        sys.path.insert(0, str(skill_root))

        captured = []

        class Response:
            status = 200
            headers = {'Content-Type': 'application/json'}
            def __init__(self, payload):
                self._payload = payload
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def text(self):
                return self._payload

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            def post(self, url, **kwargs):
                captured.append(
                    (url, kwargs.get('headers', {}).get('Authorization'))
                )
                payload = {'choices': [{'message': {'content': 'InstalledAnswer'}}],
                           'usage': {'total_tokens': 3}}
                return Response(json.dumps(payload))

        import aiohttp
        aiohttp.ClientSession = Session

        spec = importlib.util.spec_from_file_location(
            'installed_skill', skill_root / 'scripts' / 'grok_search.py'
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sys.argv = ['grok_search.py', '--query', 'Q', '--output', 'llm']
        rc = mod.main()
        result_path.write_text(json.dumps({'rc': rc, 'captured': captured}))
        """
    )
    result_file = tmp_path / "captured.json"
    proc = subprocess.run(
        [sys.executable, "-c", driver, str(installed), str(result_file)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path.parent),  # 非仓库 cwd
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "",
            "HOME": str(tmp_path),  # 避免读取开发机用户配置
        },
    )
    assert proc.returncode == 0, proc.stderr
    outcome = json.loads(result_file.read_text(encoding="utf-8"))
    assert outcome["rc"] == 0
    assert outcome["captured"], "安装态脚本必须实际发起请求"
    url, auth = outcome["captured"][0]
    assert url.startswith("https://private.invalid/v1/")
    assert auth == "Bearer private-fixture"


def test_installed_skill_layout_imports_without_repo(tmp_path):
    """安装态结构：skill 根下能直接找到 tool/api 代码包。"""
    skill_package.sync_to_persistent(tmp_path)
    for rel in (
        "tool/tool.py",
        "tool/config.py",
        "api/grok_chat.py",
        "scripts/grok_search.py",
    ):
        assert (tmp_path / rel).is_file(), rel


# initialize 安装门控与 terminate 非阻塞等待的行为回归见 tests/test_render_lifecycle.py
# （以 AST 摘取方法 + 受控命名空间真实执行验证，而非源码字符串断言）。


def test_main_card_render_is_offloaded_to_thread():
    raw = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(" in raw
    assert "await self._render_card_async(" in raw
    # 渲染同步调用点已移除（import 语句除外）
    body_calls = [
        line.strip()
        for line in raw.splitlines()
        if line.strip().startswith("render_search_card(")
    ]
    assert body_calls == [], f"仍存在同步渲染调用: {body_calls}"


class _FakeHostManager:
    """忠实模拟宿主 SkillManager：overwrite 时先删除旧目录再解包。"""

    def __init__(self, root, fail=False):
        self.skills_root = Path(root)
        self.fail = fail

    def install_skill_from_zip(self, zip_path, overwrite=True):
        dest = self.skills_root / "grok-search"
        if dest.exists() and overwrite:
            shutil.rmtree(dest)
        if self.fail:
            raise RuntimeError("host install boom")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(self.skills_root)


def _prepare_persistent(tmp_path, config=None):
    skill_package.sync_to_persistent(tmp_path)
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def test_sync_rejects_parent_directory_symlink(tmp_path):
    """父目录是 symlink 时必须拒绝，不得写出受管根之外。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    (persistent / "tool").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        skill_package.sync_to_persistent(persistent)
    assert not any(outside.iterdir()), "不得通过 symlink 写出受管目录"


def test_snapshot_symlink_is_ignored(tmp_path):
    skill_package.sync_to_persistent(tmp_path)
    snapshot = tmp_path / ".managed-files.txt"
    snapshot.unlink()
    snapshot.symlink_to("/etc/hostname")
    # symlink 快照不可读 → 视为空快照；同步不得跟随 symlink 写出
    skill_package.sync_to_persistent(tmp_path)
    assert (tmp_path / "tool" / "tool.py").is_file()


def test_build_zip_rejects_symlink_member(tmp_path):
    skill_package.sync_to_persistent(tmp_path)
    managed_file = tmp_path / "tool" / "tool.py"
    managed_file.unlink()
    managed_file.symlink_to("/etc/hostname")
    with pytest.raises(FileNotFoundError, match="不可打包"):
        skill_package.build_zip(tmp_path / "s.zip", tmp_path)


def test_prune_never_touches_root_level_files(tmp_path, monkeypatch):
    """清理只作用于受管代码目录；根级文件即使列入旧快照也不删。"""
    skill_package.sync_to_persistent(tmp_path)
    snapshot = tmp_path / ".managed-files.txt"
    snapshot.write_text("SKILL.md\ntool/tool.py\n", encoding="utf-8")
    # 清单缩减：SKILL.md 不再受管，但旧快照中的根级条目不得被清理
    reduced = tuple(
        rel for rel in skill_package.MANAGED_FILES if rel != "skill/SKILL.md"
    )
    monkeypatch.setattr(skill_package, "MANAGED_FILES", reduced)
    skill_package.sync_to_persistent(tmp_path)
    assert (tmp_path / "SKILL.md").is_file()
    assert (tmp_path / "tool" / "tool.py").is_file()


def test_install_fresh_restores_persistent_config(tmp_path):
    persistent = _prepare_persistent(tmp_path / "persistent", {"from": "persistent"})
    host = _FakeHostManager(tmp_path / "skills")
    skill_package.install_skill_package(host, persistent)
    installed = Path(host.skills_root) / "grok-search"
    assert (installed / "SKILL.md").is_file()
    assert json.loads((installed / "config.json").read_text()) == {"from": "persistent"}


def test_install_never_packs_user_config_into_zip(tmp_path):
    persistent = _prepare_persistent(tmp_path / "persistent", {"secret-ish": True})
    zip_path = tmp_path / "skill.zip"
    skill_package.build_zip(zip_path, persistent)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(name.endswith("config.json") for name in names)
    assert not any(name.endswith("config.local.json") for name in names)


def test_upgrade_keeps_installed_user_edits_over_persistent_copy(tmp_path):
    """升级安装：已安装目录的用户配置修改优先于持久化旧副本。"""
    persistent = _prepare_persistent(tmp_path / "persistent", {"v": 1})
    host = _FakeHostManager(tmp_path / "skills")
    # 预置旧安装（含用户已修改的配置）
    installed = Path(host.skills_root) / "grok-search"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("old skill", encoding="utf-8")
    (installed / "config.json").write_text('{"v": 2, "user": true}', encoding="utf-8")

    skill_package.install_skill_package(host, persistent)

    assert json.loads((installed / "config.json").read_text()) == {"v": 2, "user": True}
    assert (installed / "SKILL.md").read_text(encoding="utf-8") != "old skill"
    # 持久化副本不被反向覆盖
    assert json.loads((persistent / "config.json").read_text()) == {"v": 1}


def test_install_failure_rolls_back_to_previous_install(tmp_path):
    persistent = _prepare_persistent(tmp_path / "persistent", {"v": 1})
    host = _FakeHostManager(tmp_path / "skills", fail=True)
    installed = Path(host.skills_root) / "grok-search"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("old skill", encoding="utf-8")
    (installed / "config.json").write_text('{"user": true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="host install boom"):
        skill_package.install_skill_package(host, persistent)

    # 原可用安装（含用户配置）完整恢复
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "old skill"
    assert json.loads((installed / "config.json").read_text()) == {"user": True}
