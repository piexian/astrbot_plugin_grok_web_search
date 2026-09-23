"""Skill 自包含运行包的受管文件清单与打包/同步。

Skill 安装目录（data/skills/grok-search/）必须自带 tool/、api/ 代码包，
脚本导入不依赖仓库 cwd 或 sys.path 之外的插件源码；本清单是唯一事实来源。

同步策略：清单文件缺失即抛错（保留原可用安装，不产生残缺包）；清理只针对
上一份快照记录过的受管文件，绝不触碰用户私有文件（config.json 等）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

# 相对插件根目录的受管文件（固定清单，不含测试/缓存/文档/字体等无关产物）
MANAGED_FILES: tuple[str, ...] = (
    "skill/SKILL.md",
    "skill/scripts/grok_search.py",
    "tool/__init__.py",
    "tool/tool.py",
    "tool/config.py",
    "tool/search_service.py",
    "tool/image_search.py",
    "api/__init__.py",
    "api/grok_chat.py",
    "api/grok_responses.py",
    "api/saucenao.py",
    "api/serpapi_lens.py",
)

# Skill 包内的受管目录；快照文件名（记录上次同步的受管文件，用于定向清理）
_MANAGED_DIRS = ("scripts", "tool", "api")
_SKILL_DIR = "skill"
_SNAPSHOT_NAME = ".managed-files.txt"
# 用户私有文件，任何情况下不由本模块删除
_USER_CONFIG_NAMES = {"config.json", "config.local.json"}


def _iter_managed() -> list[tuple[Path, str]]:
    """返回 (源文件, 包内相对路径) 列表；清单缺失直接抛错，避免残缺安装包。"""
    root = Path(__file__).resolve().parent.parent
    out: list[tuple[Path, str]] = []
    for rel in MANAGED_FILES:
        source = root / rel
        if not source.is_file():
            raise FileNotFoundError(
                f"Skill 受管清单文件缺失: {rel}，拒绝生成不完整安装包"
            )
        # 包内路径去掉 skill/ 前缀：SKILL.md、scripts/... 位于包根
        arc_rel = (
            rel[len(_SKILL_DIR) + 1 :] if rel.startswith(_SKILL_DIR + "/") else rel
        )
        out.append((source, arc_rel))
    return out


def _snapshot_path(persistent_dir: Path) -> Path:
    return persistent_dir / _SNAPSHOT_NAME


def _read_snapshot(persistent_dir: Path) -> set[str]:
    """读取受管文件快照；快照缺失、是 symlink 或不可读时按空快照处理。"""
    snapshot = _snapshot_path(persistent_dir)
    if not snapshot.is_file() or snapshot.is_symlink():
        return set()
    try:
        text = snapshot.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _write_snapshot(persistent_dir: Path, managed_rel: set[str]) -> None:
    """写入受管文件快照；快照位置若是 symlink 先移除链接，避免跟随写出根目录。"""
    snapshot = _snapshot_path(persistent_dir)
    if snapshot.is_symlink():
        snapshot.unlink(missing_ok=True)
    snapshot.write_text("\n".join(sorted(managed_rel)) + "\n", encoding="utf-8")


def _ensure_managed_path(root: Path, rel: str) -> Path:
    """校验受管相对路径：目录链上不得有 symlink，且不得越出根目录。"""
    root = Path(root)
    parts = Path(rel).parts
    if not parts:
        raise ValueError("受管路径为空")
    target = root
    for part in parts[:-1]:
        target = target / part
        if target.is_symlink():
            raise ValueError(f"受管路径目录链存在 symlink，拒绝写入: {target}")
    target = target / parts[-1]
    if target.is_symlink():
        raise ValueError(f"受管目标路径是 symlink，拒绝写入: {target}")
    try:
        inside = target.resolve().is_relative_to(root.resolve())
    except OSError as e:
        raise ValueError(f"受管路径解析失败: {target}") from e
    if not inside:
        raise ValueError(f"受管路径越出根目录: {target}")
    return target


def _is_prunable(rel: str) -> bool:
    """清理仅限受管代码目录下的文件；根级文件（SKILL.md/配置等）不动。"""
    return bool(rel) and Path(rel).parts[0] in _MANAGED_DIRS


def sync_to_persistent(persistent_dir: Path) -> None:
    """把受管文件同步到持久化目录，并按快照定向清理过期受管文件。

    - 用户私有配置（config.json / config.local.json）不覆盖、不删除；
    - 只删除上一份快照记录过、且本次清单已移除的受管目录内文件；
    - 目录链 symlink 或越界路径一律拒绝，避免跟随链接写出受管根。
    """
    persistent_dir = Path(persistent_dir)
    if persistent_dir.is_symlink():
        raise ValueError(f"Skill 持久化目录是 symlink，拒绝同步: {persistent_dir}")

    persistent_dir.mkdir(parents=True, exist_ok=True)
    managed_rel: set[str] = set()
    for source, arc_rel in _iter_managed():
        target = _ensure_managed_path(persistent_dir, arc_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        managed_rel.add(arc_rel)

    # 定向清理：仅上一份快照中、受管目录内的旧受管文件；用户文件永远不动
    for stale_rel in sorted(_read_snapshot(persistent_dir) - managed_rel):
        if stale_rel in _USER_CONFIG_NAMES or not _is_prunable(stale_rel):
            continue
        try:
            stale = _ensure_managed_path(persistent_dir, stale_rel)
        except ValueError:
            continue
        if stale.is_file():
            stale.unlink(missing_ok=True)

    _write_snapshot(persistent_dir, managed_rel)


def build_zip(
    zip_path: Path, persistent_dir: Path, *, skill_name: str = "grok-search"
) -> None:
    """把持久化目录中的受管文件打包为 SkillManager 可安装的 zip。"""
    persistent_dir = Path(persistent_dir)
    managed = {rel for _, rel in _iter_managed()}
    missing = []
    members: list[tuple[Path, str]] = []
    for rel in sorted(managed):
        try:
            file = _ensure_managed_path(persistent_dir, rel)
        except ValueError:
            missing.append(rel)
            continue
        if not file.is_file():
            missing.append(rel)
        else:
            members.append((file, rel))
    if missing:
        raise FileNotFoundError(
            f"Skill 包文件缺失或不可打包: {', '.join(missing)}，拒绝生成不完整安装包"
        )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file, rel in members:
            zf.write(file, f"{skill_name}/{rel}")


def _installed_skill_dir(skill_mgr, skill_name: str) -> Path | None:
    """通过宿主 SkillManager 的 skills_root 推导安装目录；拿不到则返回 None。"""
    root = getattr(skill_mgr, "skills_root", None)
    if not root:
        return None
    return Path(root) / skill_name


def _restore_user_configs(
    installed_dir: Path | None,
    backup_dir: Path | None,
    persistent_dir: Path,
) -> None:
    """安装后恢复用户私有配置；已安装目录的用户修改优先于持久化副本。"""
    if installed_dir is None:
        return
    for name in sorted(_USER_CONFIG_NAMES):
        dst = installed_dir / name
        if dst.exists():
            continue
        for cand in (backup_dir, persistent_dir):
            if cand is None:
                continue
            src = Path(cand) / name
            if src.is_file() and not src.is_symlink():
                shutil.copy2(src, dst)
                break


def install_skill_package(
    skill_mgr, persistent_dir: Path, *, skill_name: str = "grok-search"
) -> None:
    """打包并安装 Skill；保护用户配置，失败整体回滚到原可用安装。

    流程：备份已安装目录 → 调用宿主 install_skill_from_zip(overwrite=True)
    → 恢复用户私有配置（安装态用户修改优先，其次持久化副本）→ 清理备份；
    任一步失败：移除半安装目录，恢复备份，原样抛出异常。
    """
    persistent_dir = Path(persistent_dir)
    zip_path: Path | None = None
    backup_parent: Path | None = None
    installed_dir = _installed_skill_dir(skill_mgr, skill_name)
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        zip_path = Path(tmp_name)
        build_zip(zip_path, persistent_dir, skill_name=skill_name)

        if installed_dir is not None and installed_dir.exists():
            if installed_dir.is_symlink():
                raise ValueError(f"Skill 安装目录是 symlink，拒绝覆盖: {installed_dir}")
            backup_parent = Path(tempfile.mkdtemp(prefix="grok-skill-bak-"))
            shutil.move(str(installed_dir), str(backup_parent / skill_name))

        try:
            skill_mgr.install_skill_from_zip(str(zip_path), overwrite=True)
            backup = backup_parent / skill_name if backup_parent else None
            _restore_user_configs(installed_dir, backup, persistent_dir)
        except BaseException:
            # 回滚：移除半安装目录，恢复备份（含用户配置）
            if installed_dir is not None and installed_dir.exists():
                shutil.rmtree(installed_dir, ignore_errors=True)
            backup = backup_parent / skill_name if backup_parent else None
            if backup is not None and backup.exists() and installed_dir is not None:
                shutil.move(str(backup), str(installed_dir))
            raise

        if backup_parent is not None:
            shutil.rmtree(backup_parent, ignore_errors=True)
    finally:
        if zip_path is not None:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
