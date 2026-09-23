"""字体下载多源校验、缓存修复、staging 发布、作业取消与资产记录回归。"""

import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import ROOT, load

font_loader = load("tool.font_loader")
# 保存真实加载性校验实现（autouse/单测中的打桩不影响本引用）
_REAL_FONTS_LOADABLE = font_loader._fonts_loadable
FIXTURES = ROOT / "tests" / "fixtures"
FONTS_OK = FIXTURES / "fonts_ok.7z"
FONTS_PARTIAL = FIXTURES / "fonts_partial.7z"
FONTS_DUP = FIXTURES / "fonts_dup.7z"


def _extractor_available() -> bool:
    return bool(shutil.which("7z") or shutil.which("7za")) or _py7zr_available()


def _py7zr_available() -> bool:
    try:
        import py7zr  # noqa: F401

        return True
    except ImportError:
        return False


_needs_extractor = pytest.mark.skipif(
    not _extractor_available(),
    reason="系统无 7z/7za 且未安装 py7zr，无法运行解压链路",
)


def _fixture_bytes(path: Path) -> bytes:
    return path.read_bytes()


OK_BYTES = _fixture_bytes(FONTS_OK)
PARTIAL_BYTES = _fixture_bytes(FONTS_PARTIAL)
DUP_BYTES = _fixture_bytes(FONTS_DUP)
OK_SHA = hashlib.sha256(OK_BYTES).hexdigest()
PARTIAL_SHA = hashlib.sha256(PARTIAL_BYTES).hexdigest()
DUP_SHA = hashlib.sha256(DUP_BYTES).hexdigest()


def _record(data: bytes) -> dict:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    monkeypatch.setattr(font_loader, "_proxy", None)
    monkeypatch.setattr(font_loader, "_default_headers", None)


def _stub_loadable(monkeypatch):
    """fixture 字体非真实 TTF；安装路径测试打桩加载性校验。"""
    monkeypatch.setattr(font_loader, "_fonts_loadable", lambda paths: True)


def test_fonts_loadable_validation():
    real = _find_real_font()
    if real is None:
        pytest.skip("系统无已知真实字体可校验")
    assert _REAL_FONTS_LOADABLE([real]) is True
    assert _REAL_FONTS_LOADABLE(["/nonexistent/font.ttf"]) is False


def _find_real_font() -> str | None:
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        if Path(cand).is_file():
            return cand
    return None


def _install_with_payload(monkeypatch, tmp_path, payloads, record, job=None):
    """按源顺序模拟下载内容；返回 (尝试的源列表, font_dir, job)。"""
    _stub_loadable(monkeypatch)
    attempts = []

    def fake_fetch(url, dest_archive, job_dir, **kwargs):
        attempts.append(url)
        payload = payloads[min(len(attempts) - 1, len(payloads) - 1)]
        with open(dest_archive, "wb") as f:
            f.write(payload)

    monkeypatch.setattr(font_loader, "_fetch_to_file", fake_fetch)
    monkeypatch.setattr(
        font_loader,
        "build_download_urls",
        lambda v: tuple(f"source-{i}" for i in range(len(payloads))),
    )
    font_dir = tmp_path / "font"
    job = job or font_loader.DownloadJob()
    font_loader.download_and_install(
        str(font_dir), job=job, version="1.0.41", record=record
    )
    return attempts, font_dir, job


def test_download_urls_order_and_layout():
    urls = font_loader.build_download_urls("1.0.41")
    assert urls[0].startswith("https://mirror.nju.edu.cn/github-release/")
    assert "Sarasa%20Gothic%2C%20Version%201.0.41" in urls[0]
    assert any("/LatestRelease/" in u and "nju" in u for u in urls)
    assert any("tuna" in u for u in urls)
    assert any(u.startswith("https://astrdark.cyou/gh/") for u in urls)
    assert urls[-1].startswith("https://github.com/")
    assert len([u for u in urls if "nju" in u]) == 2
    assert len([u for u in urls if "tuna" in u]) == 2


def test_fallback_version_has_verified_record():
    assert font_loader.FALLBACK_VERSION == "1.0.41"
    record = font_loader.VERIFIED_ASSETS["1.0.41"]
    assert record["size"] == 66119192
    assert record["sha256"].startswith("d240c69b")


def test_asset_record_from_release_metadata():
    real_sha = "a" * 64
    data = {
        "tag_name": "v1.0.42",
        "assets": [
            {
                "name": "SarasaTermSlabSC-TTF-1.0.42.7z",
                "size": 1234,
                "digest": f"sha256:{real_sha}",
            }
        ],
    }
    version, record = font_loader._asset_record_from_release(data)
    assert version == "1.0.42"
    assert record == {
        "filename": "SarasaTermSlabSC-TTF-1.0.42.7z",
        "size": 1234,
        "sha256": real_sha,
    }


@pytest.mark.parametrize(
    "record",
    [
        {"size": 0, "sha256": "a" * 64},
        {"size": -5, "sha256": "a" * 64},
        {"size": True, "sha256": "a" * 64},
        {"size": "100", "sha256": "a" * 64},
        {"size": 100, "sha256": "abc123"},
        {"size": 100, "sha256": "g" * 64},
        {"size": 100, "sha256": None},
        {"size": None, "sha256": "a" * 64},
        None,
    ],
)
def test_incomplete_records_are_rejected(record):
    assert font_loader._is_complete_record(record) is False


def test_complete_record_accepted():
    assert font_loader._is_complete_record({"size": 10, "sha256": "A" * 64}) is True


def test_asset_record_without_digest_falls_back_to_builtin():
    """记录不完整不放宽：回退到内置已核验记录/版本。"""
    version, record = font_loader._asset_record_from_release({"tag_name": "v2.0.0"})
    assert version == "2.0.0"
    assert font_loader._is_complete_record(record) is False

    resolved_ver, resolved = font_loader.resolve_asset_record("2.0.0", record)
    assert resolved_ver == font_loader.FALLBACK_VERSION
    assert resolved == font_loader.VERIFIED_ASSETS[font_loader.FALLBACK_VERSION]

    ver2, rec2 = font_loader.resolve_asset_record(
        font_loader.FALLBACK_VERSION, {"size": None, "sha256": None}
    )
    assert ver2 == font_loader.FALLBACK_VERSION
    assert rec2["sha256"] == font_loader.VERIFIED_ASSETS["1.0.41"]["sha256"]

    complete = {"size": 1, "sha256": "a" * 64}
    ver3, rec3 = font_loader.resolve_asset_record("3.0.0", complete)
    assert (ver3, rec3) == ("3.0.0", complete)


def test_resolve_raises_when_no_trusted_record_exists(monkeypatch):
    monkeypatch.setattr(font_loader, "VERIFIED_ASSETS", {})
    with pytest.raises(RuntimeError, match="没有可信资产记录"):
        font_loader.resolve_asset_record("9.9.9", None)


def test_discover_failure_uses_builtin_record(monkeypatch):
    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(font_loader, "_urlopen", boom)
    version, record = font_loader.discover_latest_asset()
    assert version == font_loader.FALLBACK_VERSION
    assert font_loader._is_complete_record(record)


def test_validate_archive_rejects_bad_magic(tmp_path):
    bad = tmp_path / "bad.7z"
    bad.write_bytes(b"<!DOCTYPE html>" + b"\x00" * 64)
    with pytest.raises(ValueError):
        font_loader.validate_archive(str(bad), None)


def test_validate_archive_checks_size_and_sha(tmp_path):
    data = b"7z\xbc\xaf\x27\x1c" + b"x" * 100
    archive = tmp_path / "a.7z"
    archive.write_bytes(data)

    with pytest.raises(ValueError):
        font_loader.validate_archive(str(archive), {"size": 999, "sha256": None})
    with pytest.raises(ValueError):
        font_loader.validate_archive(str(archive), {"size": None, "sha256": "0" * 64})
    font_loader.validate_archive(str(archive), _record(data))


def test_parse_content_range():
    assert font_loader._parse_content_range("bytes 0-9/100") == (0, 9, 100)
    assert font_loader._parse_content_range("bytes 10-19/40") == (10, 19, 40)
    assert font_loader._parse_content_range("bytes */100") is None
    assert font_loader._parse_content_range("0-9/100") is None
    assert font_loader._parse_content_range("") is None


class _FakeResponse:
    def __init__(self, chunks, status=206, content_range=""):
        self._chunks = list(chunks)
        self.status = status
        self.headers = {"Content-Range": content_range}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _patch_urlopen_responses(monkeypatch, responses):
    queue = list(responses)

    def fake_urlopen(req, timeout):
        return queue.pop(0)

    monkeypatch.setattr(font_loader, "_urlopen", fake_urlopen)


def test_threaded_download_rejects_wrong_content_range(monkeypatch, tmp_path):
    total = 40  # 4 段 × 10 字节
    probe = _FakeResponse([b""], status=206, content_range=f"bytes 0-0/{total}")
    bad = _FakeResponse(
        [b"x" * 10], status=206, content_range="bytes 0-4/40"
    )  # 起止不符
    _patch_urlopen_responses(monkeypatch, [probe, bad, bad, bad, bad])
    job = font_loader.DownloadJob()
    with pytest.raises(ValueError, match="区间不符"):
        font_loader._download_threaded(
            "https://mirror.invalid/x",
            str(tmp_path / "out.7z"),
            str(tmp_path),
            total,
            deadline=time.monotonic() + 60,
            job=job,
        )


def test_threaded_download_rejects_total_mismatch(monkeypatch, tmp_path):
    probe = _FakeResponse([b""], status=206, content_range="bytes 0-0/40")

    # 每段起止正确，但总长与探测值不符
    def fake_urlopen(req, timeout):
        range_header = dict(req.header_items()).get("Range", "")
        first, last = range_header.replace("bytes=", "").split("-")
        if first == "0" and last == "0":
            return probe
        return _FakeResponse(
            [b"x" * 10],
            status=206,
            content_range=f"bytes {first}-{last}/39",  # 总长错误
        )

    monkeypatch.setattr(font_loader, "_urlopen", fake_urlopen)
    job = font_loader.DownloadJob()
    with pytest.raises(ValueError, match="总长不符"):
        font_loader._download_threaded(
            "https://mirror.invalid/x",
            str(tmp_path / "out.7z"),
            str(tmp_path),
            40,
            deadline=time.monotonic() + 60,
            job=job,
        )


def test_threaded_download_rejects_overlong_chunk(monkeypatch, tmp_path):
    probe = _FakeResponse([b""], status=206, content_range="bytes 0-0/40")

    def fake_urlopen(req, timeout):
        range_header = dict(req.header_items()).get("Range", "")
        first, last = range_header.replace("bytes=", "").split("-")
        if first == "0" and last == "0":
            return probe
        if first == "0":
            return _FakeResponse(
                [b"x" * 10, b"overflow"],  # 超出区间，读循环内立即拒绝
                status=206,
                content_range=f"bytes {first}-{last}/40",
            )
        return _FakeResponse(
            [b"x" * 10], status=206, content_range=f"bytes {first}-{last}/40"
        )

    monkeypatch.setattr(font_loader, "_urlopen", fake_urlopen)
    job = font_loader.DownloadJob()
    with pytest.raises(ValueError, match="超出请求区间"):
        font_loader._download_threaded(
            "https://mirror.invalid/x",
            str(tmp_path / "out.7z"),
            str(tmp_path),
            40,
            deadline=time.monotonic() + 60,
            job=job,
        )
    # 分段临时文件有界：不会写入超过区间长度的数据
    assert not list(tmp_path.glob("chunk-*.part"))


def test_single_download_rejects_overlong_response(monkeypatch, tmp_path):
    resp = _FakeResponse([b"x" * 8, b"x" * 8], status=200)
    _patch_urlopen_responses(monkeypatch, [resp])
    job = font_loader.DownloadJob()
    dest = tmp_path / "out.7z"
    with pytest.raises(ValueError, match="超过期望长度"):
        font_loader._download_single(
            "https://mirror.invalid/x",
            str(dest),
            total_size=0,
            deadline=time.monotonic() + 60,
            job=job,
            expected_size=10,
        )
    # 有界写入：超长响应不会产生无限增长文件
    assert dest.with_suffix(".7z.part").exists() or dest.exists()
    for leftover in list(tmp_path.glob("*")):
        assert leftover.stat().st_size <= 10


@_needs_extractor
def test_corrupt_cache_is_redownloaded_and_repaired(monkeypatch, tmp_path):
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    cache = font_dir / "_font_download.7z"
    cache.write_bytes(b"corrupted junk")

    attempts, font_dir, _job = _install_with_payload(
        monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES)
    )
    assert attempts == ["source-0"]  # 坏缓存清理后进入正常下载链
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").exists()
    assert (font_dir / "_font_download.7z").read_bytes() == OK_BYTES  # 缓存已修复


@_needs_extractor
def test_valid_cache_skips_download(monkeypatch, tmp_path):
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    (font_dir / "_font_download.7z").write_bytes(OK_BYTES)

    def fail_fetch(*args, **kwargs):
        raise AssertionError("有效缓存不应触发下载")

    monkeypatch.setattr(font_loader, "_fetch_to_file", fail_fetch)
    _stub_loadable(monkeypatch)
    font_loader.download_and_install(
        str(font_dir), version="1.0.41", record=_record(OK_BYTES)
    )
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").exists()


@_needs_extractor
def test_cleanup_failure_does_not_block_install(monkeypatch, tmp_path):
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    (font_dir / "_font_download.7z").write_bytes(b"corrupted junk")

    real_remove = font_loader.os.remove

    def failing_remove(path):
        if "_font_download" in str(path):
            raise OSError("permission denied")
        real_remove(path)

    monkeypatch.setattr(font_loader.os, "remove", failing_remove)
    attempts, font_dir, _job = _install_with_payload(
        monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES)
    )
    assert attempts == ["source-0"]
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").exists()


@_needs_extractor
def test_stale_cache_validation_never_deletes_replaced_cache(monkeypatch, tmp_path):
    """旧作业校验失败时，若缓存已被新作业替换，不得删除新缓存。"""
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    cache = font_dir / "_font_download.7z"
    cache.write_bytes(b"stale corrupted junk")

    real_validate = font_loader.validate_archive

    def swapping_validate(path, record, job=None, deadline=None):
        if str(path) == str(cache):
            # 模拟校验期间新作业完成下载并替换了缓存
            cache.write_bytes(OK_BYTES)
            raise ValueError("stale validation")
        return real_validate(path, record, job, deadline)

    monkeypatch.setattr(font_loader, "validate_archive", swapping_validate)
    monkeypatch.setattr(font_loader, "_fonts_loadable", lambda paths: True)
    monkeypatch.setattr(
        font_loader,
        "_fetch_to_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
    )
    monkeypatch.setattr(font_loader, "build_download_urls", lambda v: ("source-0",))
    with pytest.raises(RuntimeError, match="所有镜像均下载失败"):
        font_loader.download_and_install(
            str(font_dir), version="1.0.41", record=_record(OK_BYTES)
        )
    assert cache.read_bytes() == OK_BYTES, "替换后的有效缓存不得被旧作业清理"


@_needs_extractor
def test_bad_package_falls_through_to_next_source(monkeypatch, tmp_path):
    html = b"<html>not a 7z</html>"
    attempts, font_dir, _job = _install_with_payload(
        monkeypatch, tmp_path, [html, OK_BYTES], _record(OK_BYTES)
    )
    assert len(attempts) == 2  # 坏包在单源校验后立即换源
    assert (font_dir / "SarasaTermSlabSC-Regular.ttf").exists()
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").exists()
    # 本次临时产物已清理（作业目录 + 渲染残留）
    assert not list(font_dir.glob("_font_download*.part"))
    assert not list(font_dir.glob(".fontjob-*"))


@_needs_extractor
def test_sha_mismatch_falls_through(monkeypatch, tmp_path):
    wrong_record = {"size": len(OK_BYTES), "sha256": "f" * 64}
    with pytest.raises(RuntimeError, match="所有镜像均下载失败"):
        _install_with_payload(monkeypatch, tmp_path, [OK_BYTES, OK_BYTES], wrong_record)
    font_dir = tmp_path / "font"
    assert not (font_dir / "SarasaTermSlabSC-Regular.ttf").exists()  # 未启用


@_needs_extractor
def test_incomplete_archive_is_rejected_without_touching_live_dir(
    monkeypatch, tmp_path
):
    record = _record(PARTIAL_BYTES)

    def fake_fetch(url, dest, job_dir, **kwargs):
        with open(dest, "wb") as f:
            f.write(PARTIAL_BYTES)

    monkeypatch.setattr(font_loader, "_fetch_to_file", fake_fetch)
    monkeypatch.setattr(font_loader, "build_download_urls", lambda v: ("s-1",))
    font_dir = tmp_path / "font"
    with pytest.raises(RuntimeError, match="缺少所需字体"):
        font_loader.download_and_install(str(font_dir), version="1.0.41", record=record)
    # 半套字体不得落入正式目录（否则下次本地探测会误认成功）
    assert not list(font_dir.rglob("*.ttf"))


@_needs_extractor
def test_duplicate_nested_font_names_install_once(monkeypatch, tmp_path):
    attempts, font_dir, _job = _install_with_payload(
        monkeypatch, tmp_path, [DUP_BYTES], _record(DUP_BYTES)
    )
    assert len(attempts) == 1
    assert (font_dir / "SarasaTermSlabSC-Regular.ttf").exists()
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").exists()


@_needs_extractor
def test_publish_failure_restores_previous_pair(monkeypatch, tmp_path):
    """第二个文件发布失败：回滚整个发布，保留旧字体对，不留半成品。"""
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    old_regular = font_dir / "SarasaTermSlabSC-Regular.ttf"
    old_bold = font_dir / "SarasaTermSlabSC-Bold.ttf"
    old_regular.write_bytes(b"old-regular")
    old_bold.write_bytes(b"old-bold")

    real_replace = font_loader.os.replace

    def replacing(first, second):
        # 常规缓存晋升照常；发布阶段在写 Bold 时失败
        if str(second).endswith("Bold.ttf") and ".fontnew" in str(first):
            raise OSError("disk full")
        return real_replace(first, second)

    monkeypatch.setattr(font_loader.os, "replace", replacing)
    with pytest.raises(OSError, match="disk full"):
        _install_with_payload(monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES))
    assert old_regular.read_bytes() == b"old-regular"
    assert old_bold.read_bytes() == b"old-bold"
    assert not list(font_dir.glob("*.fontnew*"))
    assert not list(font_dir.glob("*.fontbak*"))


@_needs_extractor
@pytest.mark.parametrize("fail_on", ["Bold", "Regular"])
def test_partial_backup_write_keeps_live_fonts_intact(monkeypatch, tmp_path, fail_on):
    """备份写一半即失败（如 ENOSPC）：不得用半成品备份覆盖完好的原字体。"""
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    old_regular = font_dir / "SarasaTermSlabSC-Regular.ttf"
    old_bold = font_dir / "SarasaTermSlabSC-Bold.ttf"
    old_regular.write_bytes(b"old-regular")
    old_bold.write_bytes(b"old-bold")

    real_copy2 = font_loader.shutil.copy2

    def failing_copy2(src, dst, *args, **kwargs):
        if ".fontbak" in str(dst) and fail_on in str(src):
            with open(dst, "wb") as f:  # 模拟只写入 1 字节后磁盘满
                f.write(b"x")
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(font_loader.shutil, "copy2", failing_copy2)
    with pytest.raises(OSError, match="No space left"):
        _install_with_payload(monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES))
    assert old_regular.read_bytes() == b"old-regular"
    assert old_bold.read_bytes() == b"old-bold"
    assert not list(font_dir.glob("*.fontbak*"))
    assert not list(font_dir.glob("*.fontnew*"))
    assert not list(font_dir.glob("*.part"))


@_needs_extractor
def test_cancelled_job_aborts_before_any_download(monkeypatch, tmp_path):
    job = font_loader.DownloadJob()
    job.cancel()
    with pytest.raises(RuntimeError, match="取消"):
        _install_with_payload(
            monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES), job=job
        )
    font_dir = tmp_path / "font"
    assert not list(font_dir.rglob("*.ttf"))
    assert not list(font_dir.glob(".fontjob-*"))


@_needs_extractor
def test_cancelled_midpublish_restores_old_pair(monkeypatch, tmp_path):
    """发布中途取消：已替换的文件回滚为旧字体对。"""
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    (font_dir / "SarasaTermSlabSC-Regular.ttf").write_bytes(b"old-regular")
    (font_dir / "SarasaTermSlabSC-Bold.ttf").write_bytes(b"old-bold")

    job = font_loader.DownloadJob()
    real_replace = font_loader.os.replace

    def replacing(first, second):
        first_s, second_s = str(first), str(second)
        if second_s.endswith("Bold.ttf") and ".fontnew" in first_s:
            job.cancel()  # 发布第一阶段（Bold）后收到取消
        return real_replace(first, second)

    monkeypatch.setattr(font_loader.os, "replace", replacing)
    with pytest.raises(RuntimeError, match="取消|所有镜像"):
        _install_with_payload(
            monkeypatch, tmp_path, [OK_BYTES], _record(OK_BYTES), job=job
        )
    assert (font_dir / "SarasaTermSlabSC-Regular.ttf").read_bytes() == b"old-regular"
    assert (font_dir / "SarasaTermSlabSC-Bold.ttf").read_bytes() == b"old-bold"
    assert not list(font_dir.glob("*.fontnew*"))
    assert not list(font_dir.glob("*.fontbak*"))


def test_sweep_reclaims_only_stale_publish_temps(tmp_path):
    """发布临时/备份文件按作业命名；仅回收停滞超龄的残留，不影响活跃作业。"""
    font_dir = tmp_path / "font"
    font_dir.mkdir()
    stale = font_dir / "SarasaTermSlabSC-Regular.ttf.fontbak.oldjob"
    fresh = font_dir / "SarasaTermSlabSC-Bold.ttf.fontnew.newjob"
    live = font_dir / "SarasaTermSlabSC-Regular.ttf"
    for f in (stale, fresh, live):
        f.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))

    font_loader._sweep_stale_job_dirs(str(font_dir))

    assert not stale.exists(), "超龄的发布临时文件应被回收"
    assert fresh.exists(), "活跃作业的临时文件不得被回收"
    assert live.read_bytes() == b"x"


@_needs_extractor
def test_all_sources_failed_keeps_dir_clean(monkeypatch, tmp_path):
    """全部源失败：不安装、不遗留临时产物，目录保持干净。"""

    def fail_fetch(url, dest, font_dir, **kwargs):
        raise OSError("mirror down")

    monkeypatch.setattr(font_loader, "_fetch_to_file", fail_fetch)
    monkeypatch.setattr(font_loader, "build_download_urls", lambda v: ("s-1", "s-2"))
    assert font_loader.init_fonts(str(tmp_path)) is None
    assert not list(tmp_path.rglob("*.ttf"))
    assert not list(tmp_path.rglob(".fontjob-*"))


@_needs_extractor
def test_user_custom_font_survives_failed_download(monkeypatch, tmp_path):
    """用户已放置的自定义字体在下载失败时原样保留并直接可用。"""
    custom = tmp_path / "MyFont-Regular.ttf"
    custom.write_bytes(b"user-font")

    def fail_fetch(url, dest, font_dir, **kwargs):
        raise AssertionError("已有可用字体时不应触发下载")

    monkeypatch.setattr(font_loader, "_fetch_to_file", fail_fetch)
    regular, bold = font_loader.init_fonts(str(tmp_path))
    assert regular == bold == str(custom)
    assert custom.read_bytes() == b"user-font"


@_needs_extractor
def test_existing_fonts_short_circuit(monkeypatch, tmp_path):
    regular = tmp_path / "SarasaTermSlabSC-Regular.ttf"
    regular.write_bytes(b"r")
    bold = tmp_path / "SarasaTermSlabSC-Bold.ttf"
    bold.write_bytes(b"b")

    def fail(*a, **k):
        raise AssertionError("must not download")

    monkeypatch.setattr(font_loader, "download_and_install", fail)
    assert font_loader.init_fonts(str(tmp_path)) == (str(regular), str(bold))


def test_job_tokens_are_independent():
    old_job = font_loader.DownloadJob()
    new_job = font_loader.DownloadJob()
    old_job.cancel()
    assert old_job.cancelled is True
    assert new_job.cancelled is False
    new_job.check()  # 旧作业取消不影响新作业（热重载隔离）
    with pytest.raises(RuntimeError, match="取消"):
        old_job.check()
    assert old_job.id != new_job.id


def test_default_headers_injection(monkeypatch):
    font_loader.set_default_headers({"User-Agent": "astrbot/4.28.1"})
    assert font_loader._merged_headers()["User-Agent"] == "astrbot/4.28.1"
    font_loader.set_default_headers(None)
    assert font_loader._merged_headers()["User-Agent"].startswith("astrbot_plugin")


def test_no_subprocess_regression():
    """安全边界：7z 解压仍走 which() 绝对路径与参数列表。"""
    import inspect

    source = inspect.getsource(font_loader._extract_7z)
    assert "shutil.which" in source
    assert "shell=True" not in source
