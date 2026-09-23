"""Sarasa Gothic 字体下载/校验/解压/发布。

多源下载顺序：NJU → 清华 TUNA → astrdark GitHub 加速 → GitHub 直连；
每个源在同一轮内完成 7z 魔数、长度与 SHA256 校验，坏包立即清理并换源。
资产记录优先取官方 Release 元数据；记录不完整一律回退内置已核验记录，
不做"仅魔数"放宽。解压在作业独立 staging 中完成，Regular/Bold 集合与
可加载性验证通过后才发布到正式目录；发布中断/失败自动回滚，保留原字体。

取消采用 DownloadJob 令牌：每个作业持有独立取消标记与临时目录
（``.fontjob-<id>/``），terminate 只取消本实例作业，热重载互不干扰。
日志直接使用宿主 ``astrbot.api.logger``；本模块仅供插件进程使用，
不进入 Skill 安装包。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from astrbot.api import logger

# ─── 常量 ────────────────────────────────────────────────────

REPO = "be5invis/Sarasa-Gothic"
ARCHIVE_TEMPLATE = "SarasaTermSlabSC-TTF-{version}.7z"
DEFAULT_FONT_REGULAR = "SarasaTermSlabSC-Regular.ttf"
DEFAULT_FONT_BOLD = "SarasaTermSlabSC-Bold.ttf"
GITHUB_API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

# 内置已核验资产记录（2026-09-23 经真实网络核验：整包 SHA256 与官方发行摘要一致）。
# 镜像只展示最新版本，旧版本文件在镜像上已 404，回退版本必须带完整记录。
FALLBACK_VERSION = "1.0.41"
VERIFIED_ASSETS: dict[str, dict[str, object]] = {
    FALLBACK_VERSION: {
        "filename": "SarasaTermSlabSC-TTF-1.0.41.7z",
        "size": 66119192,
        "sha256": "d240c69b2424dc7165f9af57a6e9ecac653afae5ff4d4c034d8d203efc13c92c",
    },
}

# 下载源：同一版本的固定目录与 LatestRelease 目录均可（NJU/TUNA 已核验 Range 支持）
MIRROR_BASES: tuple[str, ...] = (
    f"https://mirror.nju.edu.cn/github-release/{REPO}",
    f"https://mirrors.tuna.tsinghua.edu.cn/github-release/{REPO}",
)
# 用户提供的 GitHub 文件加速（仅覆盖 release 资产下载，不代理 API）
_ACCELERATOR_TEMPLATE = (
    "https://astrdark.cyou/gh/{repo}/releases/download/v{version}/{archive}"
)
_GITHUB_TEMPLATE = "https://github.com/{repo}/releases/download/v{version}/{archive}"

_NUM_THREADS = 4
_SEVEN_Z_MAGIC = b"7z\xbc\xaf\x27\x1c"
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_PROBE_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 180.0
# 整次下载（含所有源）的总预算；超时抛错并保留旧字体
_TOTAL_BUDGET_SECONDS = 1200.0
# 疑似停滞作业目录的回收年龄
_STALE_JOB_DIR_SECONDS = 24 * 3600

# 可选的 HTTP/HTTPS 代理；为 None 时使用 urllib 默认行为（含 *_PROXY 环境变量）。
_proxy: str | None = None
# 宿主通用请求头（如 User-Agent）；None 时使用内置默认头。仅用于公开字体下载。
_default_headers: dict[str, str] | None = None


class DownloadJob:
    """一次字体下载作业：独立取消令牌 + 独立临时目录所有权。"""

    def __init__(self, job_id: str | None = None):
        self.id = job_id or uuid.uuid4().hex[:12]
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """请求取消本作业；网络读写、解压、发布各阶段都会检查并尽快退出。"""
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check(self) -> None:
        if self._cancel_event.is_set():
            raise RuntimeError("字体下载已取消")


def begin_job() -> DownloadJob:
    """创建新的下载作业令牌。"""
    return DownloadJob()


def set_proxy(proxy: str | None) -> None:
    """配置字体下载使用的 HTTP/HTTPS 代理（如 'http://127.0.0.1:7890'）。"""
    global _proxy
    _proxy = proxy or None


def set_default_headers(headers: dict[str, str] | None) -> None:
    """注入宿主通用请求头（如 astrbot UA）；仅用于公开字体下载，缺失不阻断。"""
    global _default_headers
    _default_headers = dict(headers) if headers else None


def _merged_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": "astrbot_plugin_grok_web_search font-loader"}
    if _default_headers:
        headers.update(_default_headers)
    if extra:
        headers.update(extra)
    return headers


def _urlopen(req_or_url, timeout: float):
    """带代理感知的 urlopen 包装。"""
    if _proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": _proxy, "https": _proxy})
        )
        return opener.open(req_or_url, timeout=timeout)
    return urllib.request.urlopen(req_or_url, timeout=timeout)


# ─── 资产记录与版本发现 ──────────────────────────────────────


def build_download_urls(version: str) -> tuple[str, ...]:
    """根据版本号生成候选下载 URL（NJU → TUNA → astrdark → GitHub）。"""
    archive = ARCHIVE_TEMPLATE.format(version=version)
    archive_q = urllib.parse.quote(archive)
    versioned_dir = urllib.parse.quote(f"Sarasa Gothic, Version {version}")
    urls = [f"{base}/{versioned_dir}/{archive_q}" for base in MIRROR_BASES]
    urls += [f"{base}/LatestRelease/{archive_q}" for base in MIRROR_BASES]
    urls.append(
        _ACCELERATOR_TEMPLATE.format(repo=REPO, version=version, archive=archive_q)
    )
    urls.append(_GITHUB_TEMPLATE.format(repo=REPO, version=version, archive=archive_q))
    return tuple(urls)


def _asset_record_from_release(data: dict) -> tuple[str, dict[str, object]] | None:
    """从 GitHub Release 元数据解析 (version, 资产记录)。"""
    tag = str(data.get("tag_name", "")).strip()
    m = _VERSION_RE.match(tag)
    if not m:
        return None
    version = m.group(1)
    filename = ARCHIVE_TEMPLATE.format(version=version)
    for asset in data.get("assets", []) or []:
        if not isinstance(asset, dict) or asset.get("name") != filename:
            continue
        digest = str(asset.get("digest") or "")
        sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        record: dict[str, object] = {
            "filename": filename,
            "size": int(asset.get("size") or 0) or None,
            "sha256": sha256 or None,
        }
        return version, record
    return version, {"filename": filename, "size": None, "sha256": None}


def _is_complete_record(record: dict[str, object] | None) -> bool:
    """记录完整 = 正整数 size + 64 位十六进制 sha256（排除 bool）。"""
    if not isinstance(record, dict):
        return False
    size = record.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return False
    sha = record.get("sha256")
    return isinstance(sha, str) and bool(_SHA256_RE.match(sha))


def resolve_asset_record(
    version: str, record: dict[str, object] | None
) -> tuple[str, dict[str, object]]:
    """资产记录不完整时回退内置已核验记录/版本，绝不放宽为"仅魔数"校验。"""
    if _is_complete_record(record):
        return version, record
    builtin = VERIFIED_ASSETS.get(version)
    if _is_complete_record(builtin):
        logger.warning(
            f"[font-loader] 版本 {version} 的资产记录不完整，使用内置已核验记录"
        )
        return version, dict(builtin)
    fallback = VERIFIED_ASSETS.get(FALLBACK_VERSION)
    if version != FALLBACK_VERSION and _is_complete_record(fallback):
        logger.warning(
            f"[font-loader] 版本 {version} 无可信资产记录，回退内置版本 {FALLBACK_VERSION}"
        )
        return FALLBACK_VERSION, dict(fallback)
    raise RuntimeError(f"版本 {version} 没有可信资产记录，拒绝下载")


def discover_latest_asset(timeout: float = 8.0) -> tuple[str, dict[str, object]]:
    """探测最新版本与资产记录；失败回退到内置已核验记录。"""
    try:
        req = urllib.request.Request(
            GITHUB_API_LATEST,
            headers=_merged_headers({"Accept": "application/vnd.github+json"}),
        )
        with _urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        parsed = _asset_record_from_release(data if isinstance(data, dict) else {})
        if parsed is not None:
            ver, record = parsed
            logger.info(f"[font-loader] 检测到 Sarasa Gothic 最新版本: {ver}")
            return resolve_asset_record(ver, record)
        logger.warning("[font-loader] 无法解析 release tag，使用内置已核验记录")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.info(
            f"[font-loader] 探测最新版本失败 ({e})，使用内置记录 {FALLBACK_VERSION}"
        )
    fallback = dict(VERIFIED_ASSETS[FALLBACK_VERSION])
    return FALLBACK_VERSION, fallback


# ─── 本地字体探测 ────────────────────────────────────────────


def find_fonts_in_dir(font_dir: str) -> tuple[str, str] | None:
    """在目录中查找可用字体对 (regular, bold)。"""
    if not os.path.isdir(font_dir):
        return None
    ttf_files = [f for f in os.listdir(font_dir) if f.lower().endswith(".ttf")]
    if not ttf_files:
        return None

    regular = bold = None
    for f in sorted(ttf_files):
        fl = f.lower()
        if "bold" in fl:
            bold = bold or os.path.join(font_dir, f)
        elif any(k in fl for k in ("regular", "normal", "medium")):
            regular = regular or os.path.join(font_dir, f)

    if not regular and not bold and ttf_files:
        regular = bold = os.path.join(font_dir, ttf_files[0])
    elif regular and not bold:
        bold = regular
    elif bold and not regular:
        regular = bold

    return (regular, bold) if regular and bold else None


# ─── 解压 ─────────────────────────────────────────────────────


def _extract_7z(archive_path: str, output_dir: str) -> None:
    """优先用系统 7z / 7za 解压，回退 py7zr。"""
    for cmd in ("7z", "7za"):
        # 解析为绝对路径，避免 PATH 注入；找不到则跳过
        exe = shutil.which(cmd)
        if not exe:
            continue
        try:
            result = subprocess.run(  # noqa: S603  # exe 来自 which()，参数为常量
                [exe, "x", archive_path, f"-o{output_dir}", "-y"],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info("[font-loader] 使用 7z 解压成功")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    logger.info("[font-loader] 系统未安装 7z，尝试使用 py7zr 解压 ...")
    try:
        import py7zr
    except ImportError as e:
        raise RuntimeError(
            "解压字体压缩包需要系统 7z 工具或 py7zr Python 包，"
            "请安装之后重试：pip install py7zr"
        ) from e

    with py7zr.SevenZipFile(archive_path, "r") as z:
        z.extractall(path=output_dir)


# ─── 校验 ─────────────────────────────────────────────────────


def _file_sha256(
    path: str, deadline: float | None = None, job: DownloadJob | None = None
) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if job is not None:
                job.check()
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("字体下载总预算超时")
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(
    archive_path: str,
    record: dict[str, object] | None,
    job: DownloadJob | None = None,
    deadline: float | None = None,
) -> None:
    """校验已下载压缩包：7z 魔数、长度与 SHA256（记录中存在才校验对应项）。"""
    with open(archive_path, "rb") as f:
        header = f.read(6)
    if header != _SEVEN_Z_MAGIC:
        raise ValueError("下载的文件不是有效的 7z 压缩包")

    if record:
        expected_size = record.get("size")
        if expected_size:
            actual = os.path.getsize(archive_path)
            if actual != int(expected_size):
                raise ValueError(
                    f"文件大小不符: 期望 {expected_size} 字节，实际 {actual} 字节"
                )
        expected_sha = record.get("sha256")
        if expected_sha:
            actual_sha = _file_sha256(archive_path, deadline, job)
            if actual_sha.lower() != str(expected_sha).lower():
                raise ValueError(f"SHA256 不符: 期望 {expected_sha}，实际 {actual_sha}")


# ─── 下载 ─────────────────────────────────────────────────────


def _probe_range(url: str) -> tuple[bool, int]:
    """探测 Range 支持与文件总大小；206 + Content-Range 才认为支持分段。"""
    try:
        req = urllib.request.Request(
            url, headers=_merged_headers({"Range": "bytes=0-0"})
        )
        with _urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            if resp.status == 206:
                content_range = resp.headers.get("Content-Range", "")
                if "/" in content_range:
                    return True, int(content_range.split("/")[-1])
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    return False, 0


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    """解析 "bytes start-end/total"；格式不符返回 None。"""
    m = _CONTENT_RANGE_RE.match(value.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _fetch_to_file(
    url: str,
    dest_archive: str,
    job_dir: str,
    *,
    job: DownloadJob,
    expected_size: int | None = None,
    deadline: float,
) -> None:
    """从 url 下载字体压缩包到 dest_archive（同源分段或单线程），带取消/预算检查。"""
    job.check()
    logger.info(f"[font-loader] 正在下载字体: {url}")

    supports_range, total_size = _probe_range(url)
    if expected_size and total_size and total_size != expected_size:
        raise ValueError(f"Range 探测大小不符: 期望 {expected_size}，实际 {total_size}")
    if time.monotonic() > deadline:
        raise TimeoutError("字体下载总预算超时")

    if supports_range and total_size > 1024 * 1024:
        _download_threaded(url, dest_archive, job_dir, total_size, deadline, job=job)
    else:
        if not supports_range:
            logger.info("[font-loader] 服务器不支持分段下载，使用单线程模式")
        _download_single(
            url,
            dest_archive,
            total_size,
            deadline,
            job=job,
            expected_size=expected_size,
        )

    logger.info("[font-loader] 字体下载完成")


def _download_threaded(
    url: str,
    dest_archive: str,
    job_dir: str,
    total_size: int,
    deadline: float,
    *,
    job: DownloadJob,
) -> None:
    """多线程分段下载；分段校验完整 Content-Range/长度，超长立即拒绝。"""
    logger.info(
        f"[font-loader] 文件大小: {total_size / 1024 / 1024:.1f}MB, "
        f"使用 {_NUM_THREADS} 线程下载"
    )
    chunk_sz = total_size // _NUM_THREADS
    chunk_ranges = []
    for i in range(_NUM_THREADS):
        start = i * chunk_sz
        end = (total_size - 1) if i == _NUM_THREADS - 1 else (start + chunk_sz - 1)
        chunk_ranges.append((i, start, end))

    downloaded_lock = threading.Lock()
    downloaded_bytes = [0]
    last_logged = [-1]

    def _download_chunk(idx: int, byte_start: int, byte_end: int) -> str:
        part_file = os.path.join(job_dir, f"chunk-{idx}.part")
        req = urllib.request.Request(
            url,
            headers=_merged_headers({"Range": f"bytes={byte_start}-{byte_end}"}),
        )
        expected_len = byte_end - byte_start + 1
        with _urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as response:
            # Range 响应必须为 206，且 Content-Range 起止/总长完整匹配请求区间
            if response.status != 206:
                raise ValueError(f"分段下载未返回 206 (HTTP {response.status})")
            parsed = _parse_content_range(response.headers.get("Content-Range", ""))
            if parsed is None:
                raise ValueError(
                    "Content-Range 缺失或格式不符: "
                    f"{response.headers.get('Content-Range')!r}"
                )
            cr_start, cr_end, cr_total = parsed
            if (cr_start, cr_end) != (byte_start, byte_end):
                raise ValueError(
                    f"Content-Range 区间不符: 期望 {byte_start}-{byte_end}，"
                    f"实际 {cr_start}-{cr_end}"
                )
            if cr_total != total_size:
                raise ValueError(
                    f"Content-Range 总长不符: 期望 {total_size}，实际 {cr_total}"
                )
            received = 0
            with open(part_file, "wb") as f:
                while True:
                    job.check()
                    if time.monotonic() > deadline:
                        raise TimeoutError("字体下载总预算超时")
                    buf = response.read(64 * 1024)
                    if not buf:
                        break
                    received += len(buf)
                    if received > expected_len:
                        raise ValueError(
                            f"分段响应超出请求区间（>{expected_len} 字节），拒绝写入"
                        )
                    f.write(buf)
                    with downloaded_lock:
                        downloaded_bytes[0] += len(buf)
                        pct = int(downloaded_bytes[0] / total_size * 100)
                        if pct // 10 > last_logged[0] // 10:
                            dl_mb = downloaded_bytes[0] / 1024 / 1024
                            tot_mb = total_size / 1024 / 1024
                            logger.info(
                                f"[font-loader] 字体下载进度: {pct}% "
                                f"({dl_mb:.1f}/{tot_mb:.1f}MB)"
                            )
                            last_logged[0] = pct
            if received != expected_len:
                raise ValueError(
                    f"分段长度不符: 期望 {expected_len} 字节，实际 {received} 字节"
                )
        return part_file

    part_files: list[str | None] = [None] * _NUM_THREADS
    try:
        with ThreadPoolExecutor(max_workers=_NUM_THREADS) as pool:
            futures = {
                pool.submit(_download_chunk, idx, s, e): idx
                for idx, s, e in chunk_ranges
            }
            for future in as_completed(futures):
                idx = futures[future]
                part_files[idx] = future.result()

        with open(dest_archive, "wb") as out:
            for pf in part_files:
                if pf is None:
                    raise RuntimeError("分段下载缺失")
                with open(pf, "rb") as inp:
                    shutil.copyfileobj(inp, out)
        logger.info("[font-loader] 正在校验下载的字体包 ...")
    finally:
        # 异常/取消中断时也回收全部分段临时文件（含未消费的 future 产物）
        for pf in part_files:
            if pf and os.path.exists(pf):
                try:
                    os.remove(pf)
                except OSError:
                    pass
        for leftover in glob.glob(os.path.join(job_dir, "chunk-*.part")):
            _remove_quietly(leftover)


def _download_single(
    url: str,
    dest_archive: str,
    total_size: int,
    deadline: float,
    *,
    job: DownloadJob,
    expected_size: int | None = None,
) -> None:
    """单线程流式下载；超出期望长度立即拒绝，不等待 EOF。"""
    total_mb = total_size / 1024 / 1024 if total_size > 0 else 0
    if total_mb:
        logger.info(f"[font-loader] 文件大小: {total_mb:.1f}MB")

    part_path = dest_archive + ".part"
    downloaded = 0
    last_logged_pct = -1

    req = urllib.request.Request(url, headers=_merged_headers())
    with _urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp, open(part_path, "wb") as f:
        while True:
            job.check()
            if time.monotonic() > deadline:
                raise TimeoutError("字体下载总预算超时")
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if expected_size and downloaded > expected_size:
                raise ValueError(f"响应超过期望长度（>{expected_size} 字节），拒绝写入")
            f.write(chunk)
            if total_size > 0:
                pct = int(min(downloaded / total_size, 1.0) * 100)
                if pct // 10 > last_logged_pct // 10:
                    f.flush()
                    os.fsync(f.fileno())
                    logger.info(
                        f"[font-loader] 字体下载进度: {pct}% "
                        f"({downloaded / 1024 / 1024:.1f}/{total_mb:.1f}MB)"
                    )
                    last_logged_pct = pct

    if expected_size and downloaded != expected_size:
        raise ValueError(
            f"下载长度不符: 期望 {expected_size} 字节，实际 {downloaded} 字节"
        )
    os.rename(part_path, dest_archive)


def _stat_identity(path: str) -> tuple[int, int, int] | None:
    """文件身份 (inode, size, mtime_ns)；用于清理前确认仍是同一份文件。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _sweep_stale_job_dirs(
    font_dir: str, max_age: float = _STALE_JOB_DIR_SECONDS
) -> None:
    """回收长期停滞的历史作业目录与发布临时文件（仅限本插件命名）。"""
    now = time.time()
    try:
        entries = os.listdir(font_dir)
    except OSError:
        return
    for name in entries:
        path = os.path.join(font_dir, name)
        if name.startswith(".fontjob-"):
            try:
                if now - os.path.getmtime(path) > max_age:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
        elif ".fontbak." in name or ".fontnew." in name:
            # 作业 id 隔离后的发布临时文件；仅回收长期停滞的残留
            try:
                if now - os.path.getmtime(path) > max_age:
                    _remove_quietly(path)
            except OSError:
                pass


# ─── staging 校验与发布 ──────────────────────────────────────


def _fonts_loadable(paths: list[str]) -> bool:
    """尽力校验字体文件可被 FreeType 加载；PIL 不可用时跳过（视为通过）。"""
    try:
        from PIL import ImageFont
    except ImportError:
        return True
    for path in paths:
        try:
            ImageFont.truetype(path, size=18)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[font-loader] 字体文件无法加载: {path} ({e})")
            return False
    return True


def _collect_fonts(staging: str, expected: set[str]) -> dict[str, str]:
    """在 staging 中收集所需字体（嵌套目录支持，重名取首个）。"""
    found: dict[str, str] = {}
    for root, _dirs, files in os.walk(staging):
        for fname in files:
            if fname in expected and fname not in found:
                found[fname] = os.path.join(root, fname)
    return found


def _publish_fonts(job: DownloadJob, found: dict[str, str], font_dir: str) -> None:
    """把 staging 中验证过的字体发布到正式目录；中断/失败回滚，保留原字体。

    备份/临时文件按作业 id 命名；备份先写 .part 再原子改名，
    写坏的半成品备份不会被用于回滚。
    """
    job.check()
    suffix = f".{job.id}"
    plan = []  # (src, dst, backup, new)
    for fname in sorted(found):
        dst = os.path.join(font_dir, fname)
        plan.append(
            (found[fname], dst, dst + ".fontbak" + suffix, dst + ".fontnew" + suffix)
        )

    backed_up: list[tuple[str, str]] = []  # (dst, backup)，仅完整备份
    replaced: list[str] = []
    try:
        # 1) 备份将被替换的旧字体；先写 .part，成功后原子改名
        for _src, dst, backup, _new in plan:
            if not os.path.exists(dst):
                continue
            part = backup + ".part"
            shutil.copy2(dst, part)
            os.replace(part, backup)
            backed_up.append((dst, backup))
        # 2) 复制新字体到同目录临时名
        for src, dst, _backup, new in plan:
            shutil.copy2(src, new)
        # 3) 逐个原子替换；每步检查取消，中断即回滚
        for _src, dst, _backup, new in plan:
            job.check()
            os.replace(new, dst)
            replaced.append(dst)
    except BaseException:
        for _src, _dst, _backup, new in plan:
            _remove_quietly(new)
        for _src, _dst, backup, _new in plan:
            _remove_quietly(backup + ".part")
        backed_up_dsts = {dst for dst, _backup in backed_up}
        for dst, backup in backed_up:
            if dst in replaced:
                try:
                    os.replace(backup, dst)
                except OSError:
                    pass  # 保留备份文件，避免连原字体也丢失
            else:
                _remove_quietly(backup)
        for dst in replaced:
            if dst not in backed_up_dsts:
                _remove_quietly(dst)  # 原不存在的新增项撤回
        raise
    else:
        for _src, _dst, backup, _new in plan:
            _remove_quietly(backup)
            _remove_quietly(backup + ".part")


# ─── 主流程 ───────────────────────────────────────────────────


def download_and_install(
    font_dir: str,
    job: DownloadJob | None = None,
    version: str | None = None,
    record: dict[str, object] | None = None,
) -> None:
    """下载（多源轮替 + 单源内校验）→ staging 解压验证 → 原子发布。"""
    job = job or DownloadJob()
    os.makedirs(font_dir, exist_ok=True)
    job_dir = os.path.join(font_dir, f".fontjob-{job.id}")
    os.makedirs(job_dir, exist_ok=True)
    _sweep_stale_job_dirs(font_dir)
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    cache_path = os.path.join(font_dir, "_font_download.7z")
    archive_path = os.path.join(job_dir, "font.7z")

    try:
        job.check()
        if version is None or record is None:
            discovered_ver, discovered_record = discover_latest_asset()
            if version is None:
                version = discovered_ver
            if record is None:
                record = discovered_record
        # 记录不完整时回退内置已核验记录（可能连带回退版本）；不可信则拒绝
        version, record = resolve_asset_record(version, record)

        # ── 缓存层：损坏缓存清理后照常进入下载链；有效缓存才跳过下载 ──
        cache_valid = False
        if os.path.exists(cache_path):
            logger.info("[font-loader] 检测到已下载的字体包，正在校验 ...")
            cache_identity = _stat_identity(cache_path)
            try:
                validate_archive(cache_path, record, job, deadline)
                cache_valid = True
            except (ValueError, OSError) as e:
                logger.warning(f"[font-loader] 字体缓存校验失败（{e}），重新下载")
                # 只清理本次校验的那份缓存；期间被新作业替换则不动，避免删新产物
                if _stat_identity(cache_path) == cache_identity:
                    _remove_quietly(cache_path)

        if not cache_valid:
            last_err: Exception | None = None
            downloaded_ok = False
            for url in build_download_urls(version):
                try:
                    job.check()
                    expected_size = (
                        int(record["size"]) if record and record.get("size") else None
                    )
                    _fetch_to_file(
                        url,
                        archive_path,
                        job_dir,
                        job=job,
                        expected_size=expected_size,
                        deadline=deadline,
                    )
                    # 单源尝试内完成全部校验，坏包清理后立即换源
                    validate_archive(archive_path, record, job, deadline)
                    downloaded_ok = True
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.warning(f"[font-loader] 从 {url} 下载/校验失败: {e}")
                    _remove_quietly(archive_path)
                    _remove_quietly(archive_path + ".part")
                    if job.cancelled:
                        break
            if not downloaded_ok:
                raise RuntimeError(
                    f"所有镜像均下载失败，最后错误: {last_err}"
                ) from last_err
            # 下载成功：晋升为共享缓存，供异常中断后的下次启动复用
            try:
                os.replace(archive_path, cache_path)
            except OSError:
                pass
        else:
            job.check()

        # ── staging 解压验证（正式目录不受失败影响）──
        job.check()
        staging = os.path.join(job_dir, "staging")
        os.makedirs(staging, exist_ok=True)
        source_archive = cache_path if os.path.exists(cache_path) else archive_path
        _extract_7z(source_archive, staging)
        expected = {DEFAULT_FONT_REGULAR, DEFAULT_FONT_BOLD}
        found = _collect_fonts(staging, expected)
        if set(found) != expected:
            missing = sorted(expected - set(found))
            raise RuntimeError(
                f"压缩包缺少所需字体文件（缺失: {', '.join(missing)}），保留原字体"
            )
        if not _fonts_loadable(sorted(found.values())):
            raise RuntimeError("字体文件无法加载，保留原字体")

        _publish_fonts(job, found, font_dir)
        logger.info(f"[font-loader] 字体安装完成 ({len(found)} 个文件)")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def init_fonts(font_dir: str, job: DownloadJob | None = None) -> tuple[str, str] | None:
    """探测/下载字体，返回 (regular_path, bold_path) 或 None。"""
    found = find_fonts_in_dir(font_dir)
    if found:
        return found
    try:
        download_and_install(font_dir, job=job)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[font-loader] 字体下载失败: {e}")
        return None
    return find_fonts_in_dir(font_dir)
