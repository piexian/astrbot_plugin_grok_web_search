"""
AstrBot 插件：Grok 联网搜索

通过 Grok API 进行实时联网搜索，支持：
- /grok 指令
- LLM Tool (grok_web_search)
- Skill 脚本动态安装
"""

import shutil
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .grok_client import (
    grok_search,
    normalize_api_key,
    normalize_base_url,
    parse_json_config,
)

PLUGIN_NAME = "astrbot_plugin_grok_web_search"


class GrokSearchPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

    async def initialize(self):
        """插件初始化：验证配置并处理 Skill 安装"""
        self._validate_config()

        # 首次安装：将插件目录的 skill 移动到持久化目录
        self._migrate_skill_to_persistent()

        if self.config.get("enable_skill", False):
            self._install_skill()
        else:
            self._uninstall_skill()

    def _validate_config(self):
        """验证必要配置"""
        base_url = normalize_base_url(self.config.get("base_url", ""))
        api_key = normalize_api_key(self.config.get("api_key", ""))
        if not base_url:
            logger.warning(f"[{PLUGIN_NAME}] 缺少 base_url 配置")
        if not api_key:
            logger.warning(f"[{PLUGIN_NAME}] 缺少 api_key 配置")

    def _get_skills_path(self) -> Path:
        """获取 skills 目录路径"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_skills_path

            return Path(get_astrbot_skills_path())
        except ImportError:
            # 回退到相对路径
            return Path(__file__).parent.parent.parent / "skills"

    def _get_plugin_data_path(self) -> Path:
        """获取插件持久化数据目录"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            plugin_data_root = Path(get_astrbot_plugin_data_path())
        except ImportError:
            # 回退到相对路径
            plugin_data_root = Path(__file__).parent.parent.parent / "plugin_data"

        # 插件专属目录
        plugin_data_dir = plugin_data_root / PLUGIN_NAME
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        return plugin_data_dir

    def _get_skill_persistent_path(self) -> Path:
        """获取 Skill 持久化存储路径"""
        return self._get_plugin_data_path() / "skill"

    def _migrate_skill_to_persistent(self):
        """首次安装：将插件目录的 skill 移动到持久化目录"""
        source_dir = Path(__file__).parent / "skill"
        persistent_dir = self._get_skill_persistent_path()

        # 如果插件目录有 skill 且持久化目录没有，则移动
        if source_dir.exists() and not persistent_dir.exists():
            try:
                shutil.move(str(source_dir), str(persistent_dir))
                logger.info(
                    f"[{PLUGIN_NAME}] Skill 已迁移到持久化目录: {persistent_dir}"
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] Skill 迁移失败: {e}")
                # 迁移失败则复制
                try:
                    shutil.copytree(source_dir, persistent_dir)
                    logger.info(
                        f"[{PLUGIN_NAME}] Skill 已复制到持久化目录: {persistent_dir}"
                    )
                except Exception as e2:
                    logger.error(f"[{PLUGIN_NAME}] Skill 复制也失败: {e2}")

    def _install_skill(self):
        """从持久化目录安装 Skill 到 skills 目录"""
        skills_path = self._get_skills_path()
        target_dir = skills_path / "grok-search"
        source_dir = self._get_skill_persistent_path()

        if not source_dir.exists():
            logger.warning(f"[{PLUGIN_NAME}] Skill 持久化目录不存在: {source_dir}")
            return

        # 安全检查：确保源目录不是 symlink
        if source_dir.is_symlink():
            logger.error(
                f"[{PLUGIN_NAME}] Skill 源目录是 symlink，拒绝安装: {source_dir}"
            )
            return

        try:
            skills_path.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            # 使用 symlinks=True 避免跟随 symlink 复制敏感文件
            shutil.copytree(source_dir, target_dir, symlinks=True)
            logger.info(f"[{PLUGIN_NAME}] Skill 已安装到 {target_dir}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Skill 安装失败: {e}")

    def _uninstall_skill(self):
        """从 skills 目录卸载 Skill，移动回持久化目录"""
        skills_path = self._get_skills_path()
        source_dir = skills_path / "grok-search"

        if not source_dir.exists():
            return

        persistent_dir = self._get_skill_persistent_path()

        try:
            # 更新持久化目录（可能有新版本）
            if persistent_dir.exists():
                shutil.rmtree(persistent_dir)
            shutil.move(str(source_dir), str(persistent_dir))
            logger.info(f"[{PLUGIN_NAME}] Skill 已移动回持久化目录: {persistent_dir}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Skill 移动失败: {e}")
            # 移动失败则直接删除
            try:
                shutil.rmtree(source_dir)
            except Exception:
                pass

    def _parse_json_config(self, key: str) -> dict:
        """解析 JSON 格式的配置项"""
        value = self.config.get(key, "")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return parse_json_config(value)
        return {}

    async def _do_search(self, query: str) -> dict:
        """执行搜索"""
        # 安全解析 timeout 配置
        try:
            timeout_val = self.config.get("timeout_seconds", 60)
            timeout = float(timeout_val) if timeout_val is not None else 60.0
            if timeout <= 0:
                timeout = 60.0
        except (ValueError, TypeError):
            timeout = 60.0

        return await grok_search(
            query=query,
            base_url=self.config.get("base_url", ""),
            api_key=self.config.get("api_key", ""),
            model=self.config.get("model", "grok-4-expert"),
            timeout=timeout,
            extra_body=self._parse_json_config("extra_body"),
            extra_headers=self._parse_json_config("extra_headers"),
        )

    def _format_result(self, result: dict) -> str:
        """格式化搜索结果为用户友好的消息"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            return f"搜索失败: {error}"

        content = result.get("content", "")
        sources = result.get("sources", [])
        elapsed = result.get("elapsed_ms", 0)

        show_sources = self.config.get("show_sources", False)
        max_sources = self.config.get("max_sources", 5)

        lines = [content]

        if show_sources and sources:
            if max_sources > 0:
                sources = sources[:max_sources]
            lines.append("\n来源:")
            for i, src in enumerate(sources, 1):
                url = src.get("url", "")
                title = src.get("title", "")
                if title:
                    lines.append(f"  {i}. {title}\n     {url}")
                else:
                    lines.append(f"  {i}. {url}")

        lines.append(f"\n(耗时: {elapsed}ms)")

        return "\n".join(lines)

    def _format_result_for_llm(self, result: dict) -> str:
        """格式化搜索结果供 LLM 使用（纯文本，无 Markdown）"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            raw = result.get("raw", "")
            return f"搜索失败: {error}\n{raw}"

        content = result.get("content", "")
        sources = result.get("sources", [])

        show_sources = self.config.get("show_sources", False)
        max_sources = self.config.get("max_sources", 5)

        lines = [f"搜索结果:\n{content}"]

        if show_sources and sources:
            if max_sources > 0:
                sources = sources[:max_sources]
            lines.append("\n参考来源:")
            for i, src in enumerate(sources, 1):
                url = src.get("url", "")
                title = src.get("title", "")
                snippet = src.get("snippet", "")
                if title:
                    lines.append(f"  {i}. {title}")
                    lines.append(f"     {url}")
                else:
                    lines.append(f"  {i}. {url}")
                if snippet:
                    lines.append(f"     {snippet}")

        return "\n".join(lines)

    def _help_text(self) -> str:
        """返回帮助文本"""
        return """Grok 联网搜索

用法: /grok <搜索内容>

示例:
  /grok Python 3.12 有什么新特性
  /grok 最新的 AI 新闻
  /grok React 19 发布了吗

功能:
  - 实时联网搜索
  - 返回综合答案和来源链接
  - 支持 LLM 自动调用"""

    @filter.command("grok")
    async def grok_cmd(self, event: AstrMessageEvent, query: str = ""):
        """执行 Grok 搜索

        用法: /grok <搜索内容>
        """
        if not query or query.strip().lower() == "help":
            yield event.plain_result(self._help_text())
            return

        result = await self._do_search(query)
        yield event.plain_result(self._format_result(result))

    @filter.llm_tool(name="grok_web_search")
    async def grok_tool(self, event: AstrMessageEvent, query: str) -> str:
        """通过 Grok 进行实时联网搜索，获取最新信息和来源

        当需要搜索实时信息、最新新闻、API 版本、错误解决方案或验证过时/不确定信息时使用。

        Args:
            query(string): 搜索查询内容，应该是清晰具体的问题或关键词
        """
        # 启用 Skill 时禁用 Tool，避免重复
        if self.config.get("enable_skill", False):
            return "此工具已禁用，请使用 Skill 脚本进行搜索"

        result = await self._do_search(query)
        return self._format_result_for_llm(result)

    async def terminate(self):
        """插件销毁"""
        pass
