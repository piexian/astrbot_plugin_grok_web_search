"""渲染卸载/主题隔离与字体作业生命周期的实际行为回归。

main.py 无法在测试进程直接 import（astrbot 运行时），与 test_prompt_contracts
一致地以 AST 摘取方法、注入受控命名空间后执行，验证真实行为而非源码字符串。
"""

import ast
import asyncio
import contextlib
import copy
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import ROOT, load

card_render = load("tool.card_render")
font_loader = load("tool.font_loader")

MAIN_TREE = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
METHODS = {
    node.name: node
    for node in ast.walk(MAIN_TREE)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def _exec_method(name: str, namespace: dict):
    """摘取 main.py 的单个方法，注入 namespace 后编译为可实例化类。"""
    method = copy.deepcopy(METHODS[name])
    method.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            ast.ClassDef(
                name="Plugin",
                bases=[],
                keywords=[],
                body=[method],
                decorator_list=[],
            ),
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), f"main:{name}", "exec"), namespace)
    return namespace["Plugin"]


def _stub_fonts(monkeypatch):
    """用 PIL 内置默认字体替换卡片渲染字体，避免依赖系统/下载字体。"""
    from PIL import ImageFont

    def fake_get_font(bold: bool = False, size: int = 18):
        return ImageFont.load_default(size=size)

    monkeypatch.setattr(card_render, "_get_font", fake_get_font)
    monkeypatch.setattr(card_render, "_fonts_ready", True)


# ─── 主题隔离与渲染确定性 ────────────────────────────────────

CONTENT = "## T1\n- a **b** `c`\n\n> quote\n```py\nprint(1)\n```"


def test_render_theme_isolation_and_determinism(monkeypatch):
    _stub_fonts(monkeypatch)
    dark = card_render.render_search_card(CONTENT, theme="dark")
    light = card_render.render_search_card(CONTENT, theme="light")
    dark_again = card_render.render_search_card(CONTENT, theme="dark")
    assert dark != light, "暗/亮主题必须产出不同图片"
    assert dark == dark_again, "同主题同内容必须确定性输出（无跨调用主题串扰）"


def test_render_sources_panel_theme_isolation(monkeypatch):
    _stub_fonts(monkeypatch)
    sources = [{"url": "https://example.org", "title": "T", "snippet": "s"}]
    dark = card_render.render_search_card(CONTENT, sources=sources, theme="dark")
    light = card_render.render_search_card(CONTENT, sources=sources, theme="light")
    assert dark != light


# ─── 渲染卸载到线程、并发串行化与取消安全 ────────────────────


def _render_namespace(fake_render):
    return {
        "__name__": "render_ns",
        "asyncio": asyncio,
        "contextlib": contextlib,
        "render_search_card": fake_render,
        "_CARD_RENDER_SEMAPHORE": asyncio.Semaphore(1),
    }


def _make_render_plugin(fake_render):
    plugin_cls = _exec_method("_render_card_async", _render_namespace(fake_render))
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._cfg = lambda key, default=None: {
        "card_theme": "light",
        "model": "m",
    }.get(key, default)
    return plugin


def test_render_card_async_offloads_and_serializes():
    """渲染在事件循环之外执行，且同一时刻最多一个渲染（内存峰值控制）。"""
    main_thread = threading.get_ident()
    render_threads = []
    active = {"count": 0, "max": 0}
    lock = threading.Lock()
    themes = []

    def fake_render(*args, **kwargs):
        with lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            render_threads.append(threading.get_ident())
            themes.append(kwargs.get("theme"))
        time.sleep(0.05)
        with lock:
            active["count"] -= 1
        return kwargs.get("output_path")

    plugin = _make_render_plugin(fake_render)

    async def scenario():
        await asyncio.gather(
            plugin._render_card_async({"content": "a", "usage": {}}, "/tmp/a.png"),
            plugin._render_card_async({"content": "b", "usage": {}}, "/tmp/b.png"),
        )

    asyncio.run(scenario())
    assert render_threads, "渲染必须实际发生"
    assert all(t != main_thread for t in render_threads), "渲染不得阻塞事件循环线程"
    assert active["max"] == 1, "并发渲染必须被信号量串行化"
    assert set(themes) == {"light"}


def test_render_cancel_semantics():
    """取消渲染协程：等线程写完再传播，信号量不被提前释放。"""
    events = []
    started = threading.Event()

    def tracking_render(*args, **kwargs):
        started.set()
        time.sleep(0.15)
        events.append("render-done")

    plugin = _make_render_plugin(tracking_render)

    async def scenario():
        task = asyncio.create_task(
            plugin._render_card_async({"content": "x", "usage": {}}, "/tmp/x.png")
        )
        await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # 取消传播后：渲染线程已完成（无"删文件后线程又写回"窗口）
        assert events == ["render-done"]
        # 信号量已释放：下一次渲染可立即进行
        await asyncio.wait_for(
            plugin._render_card_async({"content": "y", "usage": {}}, "/tmp/y.png"),
            timeout=1,
        )
        events.append("second-done")

    asyncio.run(scenario())
    assert events.count("render-done") == 2 and events[-1] == "second-done", (
        f"取消后应完成首次渲染且第二次可立即执行: {events}"
    )


def test_render_repeated_cancel_keeps_semaphore_until_writer_done():
    """重复取消：信号量在渲染线程真正写完前不得释放（不并发写回）。"""
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def slow_render(*args, **kwargs):
        if not first_started.is_set():
            first_started.set()
            release.wait(5)
        else:
            second_started.set()

    plugin = _make_render_plugin(slow_render)

    async def scenario():
        first = asyncio.create_task(
            plugin._render_card_async({"content": "x", "usage": {}}, "/tmp/x.png")
        )
        await asyncio.to_thread(first_started.wait, 5)
        first.cancel()
        await asyncio.sleep(0.02)
        first.cancel()  # 第二次取消同样不得提前释放信号量
        second = asyncio.create_task(
            plugin._render_card_async({"content": "y", "usage": {}}, "/tmp/y.png")
        )
        await asyncio.sleep(0.05)
        second_started_early = second_started.is_set()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, timeout=5)
        return second_started_early

    second_started_early = asyncio.run(scenario())
    assert second_started_early is False, "渲染线程未结束时第二次渲染不得开始"
    assert second_started.is_set(), "首次渲染结束后第二次渲染必须执行"


def test_render_cancel_preserves_cancelled_semantics_on_render_error():
    """取消交付后渲染线程才失败：仍以 CancelledError 结束，不泄露 RuntimeError。"""
    started = threading.Event()
    release = threading.Event()

    def failing_render(*args, **kwargs):
        started.set()
        release.wait(5)
        raise RuntimeError("render boom")

    plugin = _make_render_plugin(failing_render)

    async def scenario():
        task = asyncio.create_task(
            plugin._render_card_async({"content": "x", "usage": {}}, "/tmp/x.png")
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        release.set()
        return await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_render_repeated_cancel_preserves_cancelled_semantics_on_render_error():
    """取消期间渲染失败 + 重复取消：最终仍是 CancelledError，信号量不泄漏。"""
    started = threading.Event()
    release = threading.Event()
    second_render_started = threading.Event()
    calls = []

    def failing_render(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            release.wait(5)
            raise RuntimeError("render boom")
        second_render_started.set()

    plugin = _make_render_plugin(failing_render)

    async def scenario():
        task = asyncio.create_task(
            plugin._render_card_async({"content": "x", "usage": {}}, "/tmp/x.png")
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()  # 第二次取消不得替换 CancelledError 语义
        release.set()
        outcome = None
        try:
            await task
        except asyncio.CancelledError:
            outcome = "cancelled"
        # 信号量必须已释放：下一次渲染立即执行（线程会正常返回）
        await asyncio.wait_for(
            plugin._render_card_async({"content": "y", "usage": {}}, "/tmp/y.png"),
            timeout=5,
        )
        return outcome

    assert asyncio.run(scenario()) == "cancelled"
    assert second_render_started.is_set()


# ─── initialize：Skill 同步/安装是工具切换门槛 ────────────────


def _init_namespace():
    return {"__name__": "init_ns", "asyncio": asyncio, "threading": threading}


def _make_init_plugin(migrate_ok, install_ok, enable_skill=True):
    plugin_cls = _exec_method("initialize", _init_namespace())
    plugin = plugin_cls.__new__(plugin_cls)
    plugin.config = {}
    plugin._font_thread = None
    plugin._font_job = None
    plugin._cfg = lambda key, default=None: {
        "render_as_image": False,
        "enable_skill": enable_skill,
        "enable_fetch": False,
        "base_url": "",
        "api_key": "",
    }.get(key, default)
    calls = []

    async def _validate():
        calls.append("validate")

    plugin._validate_config = _validate
    plugin._migrate_skill_to_persistent = lambda: calls.append("migrate") or migrate_ok
    plugin._install_skill = lambda: calls.append("install") or install_ok
    plugin._unregister_skill_tools = lambda: calls.append("skill-tools-removed")
    plugin._unregister_fetch_tool_if_disabled = lambda: calls.append("fetch-maybe")
    plugin._uninstall_skill = lambda: calls.append("uninstall-skill")
    return plugin, calls
    return plugin, calls


def test_initialize_switches_tools_only_when_sync_and_install_succeed():
    plugin, calls = _make_init_plugin(migrate_ok=True, install_ok=True)
    asyncio.run(plugin.initialize())
    assert "skill-tools-removed" in calls
    assert "fetch-maybe" not in calls


def test_initialize_sync_failure_keeps_llm_tools_and_skips_install():
    plugin, calls = _make_init_plugin(migrate_ok=False, install_ok=True)
    asyncio.run(plugin.initialize())
    assert "migrate" in calls
    assert "install" not in calls, "同步失败不得打包旧内容安装"
    assert "skill-tools-removed" not in calls
    assert "fetch-maybe" in calls


def test_initialize_install_failure_keeps_llm_tools():
    plugin, calls = _make_init_plugin(migrate_ok=True, install_ok=False)
    asyncio.run(plugin.initialize())
    assert "skill-tools-removed" not in calls
    assert "fetch-maybe" in calls


def test_initialize_without_skill_uninstalls_and_gates_fetch():
    plugin, calls = _make_init_plugin(
        migrate_ok=True, install_ok=True, enable_skill=False
    )
    asyncio.run(plugin.initialize())
    assert "uninstall-skill" in calls
    assert "skill-tools-removed" not in calls
    assert "fetch-maybe" in calls


# ─── terminate：取消作业 + 异步有界等待 ───────────────────────


def _make_terminate_plugin(job, thread):
    namespace = {
        "__name__": "term_ns",
        "__package__": "grok_plugin_under_test",
        "asyncio": asyncio,
        "threading": threading,
        "PLUGIN_NAME": "grok_plugin_under_test",
        "logger": SimpleNamespace(
            warning=lambda *a, **k: None, info=lambda *a, **k: None
        ),
        "_FONT_STOP_JOIN_SECONDS": 10.0,
    }
    plugin_cls = _exec_method("terminate", namespace)
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._font_job = job
    plugin._font_thread = thread
    return plugin


def test_terminate_cancels_job_and_keeps_loop_responsive():
    """terminate 等待期间事件循环持续运行（不因 thread.join 冻结）。"""
    job = font_loader.DownloadJob()
    started = threading.Event()

    def slow_join(timeout=None):
        started.set()
        time.sleep(0.15)  # 模拟线程退出延迟

    thread = SimpleNamespace(is_alive=lambda: True, join=slow_join)
    plugin = _make_terminate_plugin(job, thread)

    async def scenario():
        ticks = {"n": 0}

        async def heartbeat():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)
        await plugin.terminate()
        ticks_during = ticks["n"]
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
        return ticks_during

    ticks_during = asyncio.run(scenario())
    assert started.is_set()
    assert job.cancelled is True, "terminate 必须取消本实例作业"
    assert plugin._font_thread is None and plugin._font_job is None
    assert ticks_during >= 5, "等待线程退出期间事件循环必须保持响应"


def test_terminate_skips_finished_thread():
    plugin = _make_terminate_plugin(None, SimpleNamespace(is_alive=lambda: False))
    asyncio.run(plugin.terminate())
    assert plugin._font_thread is None


# ─── 字体作业：取消隔离与临时目录所有权 ──────────────────────


def test_slow_download_cancelled_by_job_does_not_publish(monkeypatch, tmp_path):
    """慢下载被取消后：作业线程不发布任何字体，作业目录被回收。"""
    started = threading.Event()

    def slow_fetch(url, dest, job_dir, **kwargs):
        started.set()
        deadline = time.monotonic() + 30
        while not kwargs["job"].cancelled:
            kwargs["job"].check()
            if time.monotonic() > deadline:
                raise TimeoutError
            time.sleep(0.01)
        kwargs["job"].check()  # 抛出取消

    monkeypatch.setattr(font_loader, "_fetch_to_file", slow_fetch)
    monkeypatch.setattr(font_loader, "build_download_urls", lambda v: ("slow",))

    job = font_loader.DownloadJob()
    font_dir = tmp_path / "font"

    def run():
        try:
            font_loader.download_and_install(
                str(font_dir),
                job=job,
                version="1.0.41",
                record={"size": None, "sha256": None},
            )
        except RuntimeError:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    time.sleep(0.02)
    job.cancel()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not list(font_dir.rglob("*.ttf")), "取消后不得发布字体"
    assert not list(font_dir.glob(".fontjob-*")), "作业目录必须被回收"


def test_fontjob_temp_dirs_are_per_job(monkeypatch, tmp_path):
    """旧作业失败清理不影响新作业；每作业使用独立临时目录。"""
    created = []
    real_makedirs = font_loader.os.makedirs

    def tracking_makedirs(name, *args, **kwargs):
        if ".fontjob-" in str(name):
            created.append(name)
        return real_makedirs(name, *args, **kwargs)

    monkeypatch.setattr(font_loader.os, "makedirs", tracking_makedirs)

    def fail_fetch(url, dest, job_dir, **kwargs):
        raise OSError("down")

    monkeypatch.setattr(font_loader, "_fetch_to_file", fail_fetch)
    monkeypatch.setattr(font_loader, "build_download_urls", lambda v: ("s-1",))

    job_a = font_loader.DownloadJob()
    job_b = font_loader.DownloadJob()
    with pytest.raises(RuntimeError):
        font_loader.download_and_install(
            str(tmp_path / "font_a"),
            job=job_a,
            version="1.0.41",
            record=_record_for(1),
        )
    font_loader.init_fonts(str(tmp_path / "font_b"), job=job_b)

    assert job_a.id != job_b.id
    # 两个作业各自创建并回收自己的目录，互不删除对方产物
    assert all(f".fontjob-{job_a.id}" in d for d in created[:1])
    assert all(f".fontjob-{job_b.id}" in d for d in created[1:])
    assert not list((tmp_path / "font_a").glob(".fontjob-*"))
    assert not list((tmp_path / "font_b").glob(".fontjob-*"))


def _record_for(size: int) -> dict:
    # 仅验证取消/清理路径；完整记录才能进入下载流程
    return {"size": size, "sha256": "a" * 64}
