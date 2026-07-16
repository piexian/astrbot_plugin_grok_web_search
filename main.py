"""
AstrBot 插件：Grok 联网搜索

通过 Grok API 进行实时联网搜索，支持：
- /grok 指令
- LLM Tool (grok_web_search)
- Skill 脚本动态安装
"""

import asyncio
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Forward, Image, Node, Nodes, Plain, Reply
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.io import download_image_by_url, file_to_base64
from astrbot.core.utils.quoted_message.chain_parser import (
    _extract_image_refs_from_component_chain,
    _extract_text_from_component_chain,
)

from .api.grok_chat import grok_fetch, grok_search
from .api.grok_responses import grok_responses_search

try:
    from astrbot.core.provider.register import llm_tools as _llm_tools_registry
except ImportError:
    _llm_tools_registry = None
try:
    from astrbot.core.utils.quoted_message_parser import (
        extract_quoted_message_images as _extract_quoted_message_images,
    )
    from astrbot.core.utils.quoted_message_parser import (
        extract_quoted_message_text as _extract_quoted_message_text,
    )
except ImportError:
    _extract_quoted_message_images = None
    _extract_quoted_message_text = None
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None
from .tool.card_render import (
    init_fonts,
    render_search_card,
)
from .tool.card_render import (
    set_logger as set_card_logger,
)
from .tool.tool import (
    DEFAULT_JSON_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    build_headers,
    build_search_time_constraints,
    normalize_api_key,
    normalize_base_url,
    normalize_search_options,
    parse_json_config,
    resolve_mode_model,
    resolve_reasoning_params,
    resolve_search_mode,
    resolve_system_prompt,
    safe_number,
)

PLUGIN_NAME = "astrbot_plugin_grok_web_search"
FORWARD_SENDER_NAME = "Grok搜索助手"

CONFIG_PATHS = {
    "model": ("provider_settings", "model"),
    "use_responses_api": ("provider_settings", "use_responses_api"),
    "quick_model": ("provider_settings", "quick_model"),
    "detailed_model": ("provider_settings", "detailed_model"),
    "deep_model": ("provider_settings", "deep_model"),
    "base_url": ("connection_settings", "base_url"),
    "api_key": ("connection_settings", "api_key"),
    "timeout_seconds": ("connection_settings", "timeout_seconds"),
    "proxy": ("connection_settings", "proxy"),
    "max_retries": ("request_settings", "max_retries"),
    "retry_delay": ("request_settings", "retry_delay"),
    "retryable_status_codes": ("request_settings", "retryable_status_codes"),
    "custom_system_prompt": ("request_settings", "custom_system_prompt"),
    "enable_stream": ("request_settings", "enable_stream"),
    "extra_body": ("advanced_settings", "extra_body"),
    "extra_headers": ("advanced_settings", "extra_headers"),
    "show_sources": ("output_settings", "show_sources"),
    "render_as_image": ("output_settings", "render_as_image"),
    "send_as_forward": ("output_settings", "send_as_forward"),
    "card_theme": ("output_settings", "card_theme"),
    "max_sources": ("output_settings", "max_sources"),
    "enable_fetch": ("tool_settings", "enable_fetch"),
    "enable_skill": ("tool_settings", "enable_skill"),
}

CONFIG_DEFAULTS = {
    "model": DEFAULT_MODEL,
    "use_responses_api": False,
    "quick_model": "",
    "detailed_model": "",
    "deep_model": "",
    "base_url": "",
    "api_key": "",
    "timeout_seconds": 60,
    "proxy": "",
    "max_retries": 3,
    "retry_delay": 1.0,
    "retryable_status_codes": [429, 500, 502, 503, 504],
    "custom_system_prompt": "",
    "enable_stream": False,
    "extra_body": "",
    "extra_headers": "",
    "show_sources": False,
    "render_as_image": False,
    "send_as_forward": False,
    "card_theme": "auto",
    "max_sources": 5,
    "enable_fetch": False,
    "enable_skill": False,
}


def _fmt_tokens(n: int) -> str:
    """将 token 数量格式化为简短形式，如 1m2k、3.5k、800。"""
    if n >= 1_000_000:
        m, remain = divmod(n, 1_000_000)
        k = remain // 1_000
        return f"{m}m{k}k" if k else f"{m}m"
    if n >= 1_000:
        k, remain = divmod(n, 1_000)
        h = remain // 100
        return f"{k}.{h}k" if h else f"{k}k"
    return str(n)


class GrokSearchPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._card_fonts_ready = False
        self._font_init_task: asyncio.Task | None = None
        self._migrate_legacy_config()

    def _cfg(self, key: str, default=None):
        path = CONFIG_PATHS.get(key)
        if path:
            section = self.config.get(path[0], {})
            if isinstance(section, dict) and path[1] in section:
                return section[path[1]]
        return self.config.get(key, default)

    def _migrate_legacy_config(self) -> None:
        """Move old flat config values into the grouped schema once."""
        changed = False
        for key, path in CONFIG_PATHS.items():
            if key not in self.config:
                continue

            default = CONFIG_DEFAULTS.get(key)
            legacy_value = self.config.get(key)
            if legacy_value == default:
                continue

            section = self.config.get(path[0])
            if not isinstance(section, dict):
                section = {}
                self.config[path[0]] = section

            current_value = section.get(path[1], default)
            if current_value != default:
                continue

            section[path[1]] = legacy_value
            self.config[key] = list(default) if isinstance(default, list) else default
            changed = True

        save_config = getattr(self.config, "save_config", None)
        if changed and callable(save_config):
            try:
                save_config()
                logger.info(f"[{PLUGIN_NAME}] 已迁移旧版平铺配置到新版分组配置")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 保存迁移后的配置失败: {e}")

    async def _extract_content_from_event(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, list[str]]:
        """Extract text and images from the user's message.

        Prefer AstrBot core's public quoted_message_parser for Reply/forward
        fallback parsing. Older cores fall back to chain_parser helpers.

        Returns:
            A tuple of (text, images):
            - text: extracted text from the message chain (or None)
            - images: list of base64-encoded image strings (without prefix)
        """
        try:
            chain = event.get_messages()
        except AttributeError:
            # LLM Tool 路径传入的是 ContextWrapper（仅有 .messages 属性），
            # 不是原始 AstrMessageEvent（有 .get_messages() 方法），无需提取消息链内容
            return None, []
        text: str | None = None
        image_refs: list[str] = []

        use_legacy_parser = True
        if (
            _extract_quoted_message_text is not None
            and _extract_quoted_message_images is not None
        ):
            try:
                text = await _extract_quoted_message_text(event)
                image_refs = await _extract_quoted_message_images(event)
                use_legacy_parser = not text and not image_refs
            except Exception as e:
                logger.warning(
                    f"[{PLUGIN_NAME}] quoted_message_parser failed, falling back to chain_parser: {e}"
                )

        if use_legacy_parser:
            text = _extract_text_from_component_chain(chain)
            image_refs = _extract_image_refs_from_component_chain(chain)

        images: list[str] = []
        seen: set[str] = set()

        # 提取消息链顶层的 Image 组件并转为 base64
        for comp in chain:
            if isinstance(comp, Image):
                await self._append_image_base64(comp, images, seen)

        # 将嵌套组件中的图片引用（URL/路径）转为 base64
        for ref in image_refs:
            await self._append_image_base64(ref, images, seen)

        return text, images

    async def _append_image_base64(
        self,
        image: Image | str,
        images: list[str],
        seen: set[str],
    ) -> None:
        try:
            if isinstance(image, Image):
                b64 = await image.convert_to_base64()
            else:
                image_ref = image.strip()
                if image_ref.startswith("base64://"):
                    b64 = image_ref.removeprefix("base64://")
                elif image_ref.startswith("data:image/"):
                    b64 = image_ref.split(",", 1)[1] if "," in image_ref else ""
                elif image_ref.startswith(("http://", "https://")):
                    b64 = await Image.fromURL(image_ref).convert_to_base64()
                else:
                    b64 = await Image(file=image_ref).convert_to_base64()

            b64 = b64.removeprefix("base64://")
            if b64 and b64 not in seen:
                seen.add(b64)
                images.append(b64)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] Failed to convert image to base64: {e}")

    def _unregister_disabled_tools(self):
        """根据配置在初始化时直接卸载不需要的 LLM Tool，避免 AI 看到无用工具"""
        if _llm_tools_registry is None:
            return

        if self._cfg("enable_skill", False):
            # Skill 接管，移除所有 LLM Tool
            _llm_tools_registry.remove_func("grok_web_search")
            _llm_tools_registry.remove_func("grok_web_fetch")
            logger.info(
                f"[{PLUGIN_NAME}] Skill 已启用，已卸载 grok_web_search 和 grok_web_fetch 工具"
            )
            return

        if not self._cfg("enable_fetch", False):
            _llm_tools_registry.remove_func("grok_web_fetch")
            logger.info(f"[{PLUGIN_NAME}] 网页抓取未启用，已卸载 grok_web_fetch 工具")

    def _init_fonts(self):
        """Initialize card rendering fonts (runs in background)."""
        logger.info(f"[{PLUGIN_NAME}] 正在后台初始化卡片渲染字体 ...")
        try:
            from .tool import font_loader

            font_loader.set_proxy(self._cfg("proxy", "") or None)
            if get_astrbot_data_path:
                font_dir = str(
                    Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "font"
                )
            else:
                font_dir = os.path.join(os.path.dirname(__file__), "font")
            self._card_fonts_ready = init_fonts(font_dir)
            if self._card_fonts_ready:
                logger.info(f"[{PLUGIN_NAME}] 卡片渲染字体已就绪: {font_dir}")
            else:
                logger.warning(f"[{PLUGIN_NAME}] 卡片渲染字体初始化失败")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 字体初始化异常: {e}")

    async def initialize(self):
        """插件初始化：验证配置并处理 Skill 安装"""
        # 在后台初始化字体，仅在开启图片渲染模式下
        if self._cfg("render_as_image", False):
            set_card_logger(logger)
            self._font_init_task = asyncio.create_task(
                asyncio.to_thread(self._init_fonts)
            )

        # 根据配置卸载不需要的 LLM Tool
        self._unregister_disabled_tools()

        # 校验 base_url/api_key
        await self._validate_config()

        # 首次安装：将插件目录的 skill 移动到持久化目录
        self._migrate_skill_to_persistent()

        if self._cfg("enable_skill", False):
            self._install_skill()
        else:
            self._uninstall_skill()

    async def _validate_config(self):
        """验证必要配置，并通过 v1/models 接口检查连通性"""
        base_url = normalize_base_url(self._cfg("base_url", ""))
        api_key = normalize_api_key(self._cfg("api_key", ""))
        if not base_url:
            logger.warning(
                f"[{PLUGIN_NAME}] 缺少 base_url 配置，请在插件设置中填写 Grok API 端点"
            )
            return
        if not api_key:
            logger.warning(
                f"[{PLUGIN_NAME}] 缺少 api_key 配置，请在插件设置中填写 API 密钥"
            )
            return

        # 通过 v1/models 接口验证连通性和密钥有效性
        models_url = f"{base_url}/v1/models"
        extra_headers = self._parse_json_config("extra_headers")
        headers = build_headers(api_key, extra_headers or None)

        # 获取代理配置
        proxy = self._cfg("proxy", "").strip() or None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    models_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                    proxy=proxy,
                ) as resp:
                    if resp.status == 401:
                        logger.warning(
                            f"[{PLUGIN_NAME}] API 密钥无效（401），请检查 api_key 配置"
                        )
                    elif resp.status == 403:
                        logger.warning(
                            f"[{PLUGIN_NAME}] API 密钥权限不足（403），请检查 api_key 权限"
                        )
                    elif resp.status == 404:
                        logger.warning(
                            f"[{PLUGIN_NAME}] v1/models 端点不存在（404），请检查 base_url 配置是否正确"
                        )
                    elif resp.status != 200:
                        logger.warning(
                            f"[{PLUGIN_NAME}] API 连通性检查返回 HTTP {resp.status}，请确认配置"
                        )
                    else:
                        logger.info(f"[{PLUGIN_NAME}] API 连通性检查通过")
        except aiohttp.ClientError as e:
            logger.warning(
                f"[{PLUGIN_NAME}] API 连通性检查失败（网络错误）: {e}，请检查 base_url 配置"
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] API 连通性检查超时，请检查 base_url 是否可达"
            )

    def _get_skill_manager(self):
        """获取 SkillManager 实例（延迟导入）"""
        if hasattr(self, "_skill_mgr"):
            return self._skill_mgr
        try:
            from astrbot.core.skills import SkillManager

            self._skill_mgr = SkillManager()
        except ImportError:
            self._skill_mgr = None
        return self._skill_mgr

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
        """同步插件内置 Skill 到持久化目录，保留用户本地配置。"""
        source_dir = Path(__file__).parent / "skill"
        persistent_dir = self._get_skill_persistent_path()

        if not source_dir.exists():
            return
        if persistent_dir.is_symlink():
            logger.error(
                f"[{PLUGIN_NAME}] Skill 持久化目录是 symlink，拒绝同步: {persistent_dir}"
            )
            return

        try:
            persistent_dir.mkdir(parents=True, exist_ok=True)
            for source_path in source_dir.rglob("*"):
                rel_path = source_path.relative_to(source_dir)
                target_path = persistent_dir / rel_path
                if source_path.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                if (
                    rel_path.name in {"config.json", "config.local.json"}
                    and target_path.exists()
                ):
                    continue
                shutil.copy2(source_path, target_path)

            logger.info(f"[{PLUGIN_NAME}] Skill 已同步到持久化目录: {persistent_dir}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Skill 同步到持久化目录失败: {e}")

    def _install_skill(self):
        """通过 SkillManager 安装 Skill（打包为 zip 后调用官方接口）"""
        source_dir = self._get_skill_persistent_path()

        if not source_dir.exists():
            logger.error(f"[{PLUGIN_NAME}] Skill 持久化目录不存在: {source_dir}")
            return

        if source_dir.is_symlink():
            logger.error(
                f"[{PLUGIN_NAME}] Skill 源目录是 symlink，拒绝安装: {source_dir}"
            )
            return

        skill_mgr = self._get_skill_manager()
        if not skill_mgr:
            logger.error(f"[{PLUGIN_NAME}] SkillManager 不可用，无法安装 Skill")
            return

        tmp_zip = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_zip = Path(tmp.name)

            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in source_dir.rglob("*"):
                    if file.is_file():
                        arcname = f"grok-search/{file.relative_to(source_dir)}"
                        zf.write(file, arcname)

            skill_mgr.install_skill_from_zip(str(tmp_zip), overwrite=True)
            logger.info(f"[{PLUGIN_NAME}] Skill 已通过 SkillManager 安装并激活")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Skill 安装失败: {e}")
        finally:
            if tmp_zip:
                tmp_zip.unlink(missing_ok=True)

    def _uninstall_skill(self):
        """通过 SkillManager 卸载 Skill"""
        skill_mgr = self._get_skill_manager()
        if not skill_mgr:
            logger.error(f"[{PLUGIN_NAME}] SkillManager 不可用，无法卸载 Skill")
            return

        try:
            skill_mgr.delete_skill("grok-search")
            logger.info(f"[{PLUGIN_NAME}] Skill 已通过 SkillManager 卸载")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Skill 卸载失败: {e}")

    def _parse_json_config(self, key: str) -> dict:
        """解析 JSON 格式的配置项"""
        value = self._cfg(key, "")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            result, error = parse_json_config(value)
            if error:
                logger.warning(f"[{PLUGIN_NAME}] {key} {error}")
            return result
        return {}

    async def _do_search(
        self,
        query: str,
        system_prompt: str | None = None,
        use_retry: bool = False,
        images: list[str] | None = None,
        search_depth: str = "basic",
        max_results: int = 7,
        topic: str = "general",
        days: int = 0,
        time_range: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        """Execute a search.

        Args:
            query: Search query content
            system_prompt: Custom system prompt, uses default when None
            use_retry: Whether to enable retry (command invocation only)
            images: Optional list of base64-encoded images for multimodal queries
            search_depth: Search depth ("basic"|"advanced"|"deep")
            max_results: Desired result count (5-20)
            topic: Search topic ("general"|"news")
            days: Days to look back (0 = not set)
            time_range: Time range ("day"|"week"|"month"|"year")
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        """
        # 规范化搜索选项
        opts = normalize_search_options(
            search_depth=search_depth,
            max_results=max_results,
            topic=topic,
            days=days,
            time_range=time_range,
            start_date=start_date,
            end_date=end_date,
        )

        # 使用全局 timeout 配置
        timeout = safe_number(
            self._cfg("timeout_seconds", 60),
            60.0,
            cast=float,
            min_val=0.001,
        )

        # 根据 search_depth 解析模式和对应的模型
        mode = resolve_search_mode(str(opts["search_depth"]))
        mode_model = resolve_mode_model(
            self._cfg(f"{mode}_model", ""),
            self._cfg("model", DEFAULT_MODEL),
        )

        # 推理参数
        reasoning_effort, reasoning_budget_tokens = resolve_reasoning_params(
            str(opts["search_depth"])
        )

        # 构建时间约束提示词
        time_constraints = build_search_time_constraints(
            topic=str(opts["topic"]),
            days=int(opts["days"]),
            time_range=str(opts["time_range"]),
            start_date=str(opts["start_date"]),
            end_date=str(opts["end_date"]),
        )

        # 重试配置（仅指令调用时使用）
        max_retries = 0
        retry_delay = 1.0
        retryable_status_codes = None
        if use_retry:
            max_retries = self._cfg("max_retries", 3)
            retry_delay = self._cfg("retry_delay", 1.0)

            # 解析可重试状态码（直接从 list 类型配置获取）
            retryable_codes = self._cfg("retryable_status_codes", [])
            if retryable_codes and isinstance(retryable_codes, list):
                retryable_status_codes = set(retryable_codes)

        # 自定义系统提示词（传入优先，其次配置，最后默认 JSON 提示词）
        if system_prompt is None:
            system_prompt = resolve_system_prompt(
                self._cfg("custom_system_prompt", ""),
                DEFAULT_JSON_SYSTEM_PROMPT,
            )

        return await self._do_search_via_http(
            query=query,
            system_prompt=system_prompt,
            images=images,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            reasoning_budget_tokens=reasoning_budget_tokens,
            max_retries=max_retries,
            retry_delay=retry_delay,
            retryable_status_codes=retryable_status_codes,
            mode_model=mode_model,
            search_depth=str(opts["search_depth"]),
            max_results=int(opts["max_results"]),
            time_constraints=time_constraints,
        )

    async def _do_search_via_http(
        self,
        *,
        query: str,
        system_prompt: str,
        images: list[str] | None,
        timeout: float,
        reasoning_effort: str | None,
        reasoning_budget_tokens: int | None,
        max_retries: int,
        retry_delay: float,
        retryable_status_codes: set[int] | None,
        mode_model: str,
        search_depth: str = "basic",
        max_results: int = 7,
        time_constraints: str = "",
    ) -> dict:
        """通过外部 Grok HTTP API 执行搜索。"""
        try:
            proxy = self._cfg("proxy", "").strip() or None

            # 将时间约束和搜索引导注入到查询前缀
            enriched_query = query
            if time_constraints:
                enriched_query = f"{time_constraints}\n{enriched_query}"

            depth_guide = {
                "basic": "Provide a quick, concise overview.",
                "advanced": "Conduct thorough research with multiple sources.",
                "deep": "Perform an exhaustive, in-depth analysis with maximum sources.",
            }.get(search_depth, "")
            if depth_guide:
                enriched_query = (
                    f"[Search Guide]\n"
                    f"- Depth: {search_depth} ({depth_guide})\n"
                    f"- Desired results: {max_results}\n"
                    f"\n{enriched_query}"
                )

            common_kwargs = {
                "query": enriched_query,
                "base_url": self._cfg("base_url", ""),
                "api_key": self._cfg("api_key", ""),
                "model": mode_model,
                "timeout": timeout,
                "extra_body": self._parse_json_config("extra_body"),
                "extra_headers": self._parse_json_config("extra_headers"),
                "system_prompt": system_prompt,
                "max_retries": max_retries,
                "retry_delay": retry_delay,
                "retryable_status_codes": retryable_status_codes,
                "images": images,
                "proxy": proxy,
            }

            if self._cfg("use_responses_api", False):
                result = await grok_responses_search(**common_kwargs)
            else:
                result = await grok_search(
                    reasoning_effort=reasoning_effort,
                    reasoning_budget_tokens=reasoning_budget_tokens,
                    stream=self._cfg("enable_stream", False),
                    **common_kwargs,
                )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] API 调用异常: {e}")
            return {"ok": False, "error": f"API 调用异常: {e}"}

        if not result.get("ok"):
            logger.warning(
                f"[{PLUGIN_NAME}] API 调用失败: {result.get('error', '未知错误')}"
            )
        return result

    def _render_sources(
        self,
        sources: list,
        *,
        header: str,
        with_snippet: bool,
    ) -> list[str]:
        """渲染来源列表，遵循 show_sources / max_sources 配置。"""
        if not self._cfg("show_sources", False) or not sources:
            return []
        max_sources = self._cfg("max_sources", 5)
        if max_sources > 0:
            sources = sources[:max_sources]
        lines = [f"\n{header}:"]
        for i, src in enumerate(sources, 1):
            url = src.get("url", "")
            title = src.get("title", "")
            if title:
                if with_snippet:
                    lines.append(f"  {i}. {title}")
                    lines.append(f"     {url}")
                else:
                    lines.append(f"  {i}. {title}\n     {url}")
            else:
                lines.append(f"  {i}. {url}")
            if with_snippet:
                snippet = src.get("snippet", "")
                if snippet:
                    lines.append(f"     {snippet}")
        return lines

    def _format_result(self, result: dict) -> str:
        """格式化搜索结果为用户友好的消息"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            return f"搜索失败: {error}"

        content = result.get("content", "")
        sources = result.get("sources", [])
        elapsed = result.get("elapsed_ms", 0) / 1000

        lines = [content]
        lines.extend(self._render_sources(sources, header="来源", with_snippet=False))

        # 显示耗时、重试次数和 token 用量
        retry_info = ""
        retries = result.get("retries", 0)
        if retries > 0:
            retry_info = f"，重试 {retries} 次"

        token_info = ""
        usage = result.get("usage") or {}
        total_tokens = usage.get("total_tokens", 0)
        if total_tokens:
            token_info = f"，tokens: {_fmt_tokens(total_tokens)}"

        lines.append(f"\n(耗时: {elapsed:.1f}s{retry_info}{token_info})")

        return "\n".join(lines)

    def _format_result_for_llm(self, result: dict) -> str:
        """格式化搜索结果供 LLM 使用（纯文本，无 Markdown）"""
        if not result.get("ok"):
            error = result.get("error", "未知错误")
            raw = result.get("raw", "")
            return f"搜索失败: {error}\n{raw}"

        content = result.get("content", "")
        sources = result.get("sources", [])

        lines = [f"搜索结果:\n{content}"]
        lines.extend(
            self._render_sources(sources, header="参考来源", with_snippet=True)
        )

        # 提示主 LLM 使用纯文本格式回复用户
        lines.append("\n[提示: 请使用纯文本格式回复用户，不要使用 Markdown 格式]")

        return "\n".join(lines)

    def _supports_forward_output(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp" and bool(event.get_self_id())

    def _help_text(self) -> str:
        """返回帮助文本"""
        mode = "自定义"
        provider_id = self._cfg("base_url", "") or "未配置"
        model = self._cfg("model", DEFAULT_MODEL) or "默认"
        has_custom_prompt = bool((self._cfg("custom_system_prompt", "") or "").strip())
        if has_custom_prompt:
            prompt_info = "自定义"
        else:
            prompt_info = "内置中文（/grok 指令）/ 内置英文 JSON（LLM Tool）"

        return (
            "Grok 联网搜索\n"
            "\n"
            "用法:\n"
            "  /grok help           显示此帮助\n"
            "  /grok <搜索内容>     执行联网搜索\n"
            "\n"
            "示例:\n"
            "  /grok Python 3.12 有什么新特性\n"
            "  /grok 最新的 AI 新闻\n"
            "  /grok React 19 发布了吗\n"
            "\n"
            "调用方式:\n"
            "  - /grok 指令：直接搜索并返回结果\n"
            "  - LLM Tool：模型自动调用 grok_web_search\n"
            "\n"
            f"当前配置:\n"
            f"  供应商来源: {mode}\n"
            f"  供应商: {provider_id}\n"
            f"  模型: {model}\n"
            f"  系统提示词: {prompt_info}"
        )

    @staticmethod
    def _message_has_quoted(event: AstrMessageEvent) -> bool:
        """Return True if the message chain contains a quoted/forwarded component."""
        try:
            messages = event.get_messages()
        except AttributeError:
            # LLM Tool 路径传入 ContextWrapper，无 get_messages() 方法
            return False
        return any(
            isinstance(comp, (Reply, Forward, Node, Nodes))
            for comp in messages
        )

    @filter.command("grok")
    async def grok_cmd(self, event: AstrMessageEvent, query: GreedyStr):
        """执行 Grok 搜索

        用法: /grok <搜索内容>
        """
        # 提取消息中的文本和图片（包括引用消息/转发消息）
        extra_text, images = await self._extract_content_from_event(event)
        if images:
            logger.info(
                f"[{PLUGIN_NAME}] /grok command: extracted {len(images)} image(s) from message"
            )

        # 只有消息链中确实包含引用/转发组件时，才使用 extra_text
        # 避免普通消息的原文（含唤醒词+指令名）被重复拼接
        if not self._message_has_quoted(event):
            extra_text = None

        # 仅在明确输入 help 时显示帮助
        if query.strip().lower() == "help":
            yield event.plain_result(self._help_text())
            return

        # 无查询文本但有图片或引用内容时，继续搜索
        has_content = bool(images) or bool(extra_text)
        if not query.strip() and not has_content:
            yield event.plain_result(self._help_text())
            return

        # 将引用/转发消息中提取的文本拼接到查询前面作为上下文
        if extra_text:
            if query.strip():
                query = f"[Referenced message content]\n{extra_text}\n\n[User query]\n{query}"
            else:
                query = extra_text

        # 仅有图片无文本时，使用默认提示词
        if not query.strip() and images:
            query = "请搜索这张图片的内容"

        # 优先使用自定义提示词，未设置则使用内置提示词（英文指令 + JSON 格式 + 中文回复）
        cmd_system_prompt = resolve_system_prompt(
            self._cfg("custom_system_prompt", ""),
            (
                "You are a web research assistant. Use live web search/browsing when answering. "
                "Return ONLY a single JSON object with keys: "
                "content (string), sources (array of objects with url/title/snippet when possible). "
                "Keep content concise and evidence-backed. "
                "IMPORTANT: Respond in Chinese. Do NOT use Markdown formatting in the content field - use plain text only. "
                "Keep proper nouns and names in their original language."
            ),
        )

        result = await self._do_search(
            query,
            system_prompt=cmd_system_prompt,
            use_retry=True,
            images=images or None,
        )
        event.should_call_llm(True)

        if self._cfg("send_as_forward", False):
            forward_sent = await self._send_as_forward(event, result)
            if forward_sent:
                return

        # 判断是否以图片卡片形式发送
        use_image = self._cfg("render_as_image", False) and self._card_fonts_ready
        image_sent = False

        if use_image and result.get("ok"):
            image_sent = await self._send_as_image_card(event, result)

        # 文本模式或图片发送失败时降级
        if not image_sent:
            try:
                await event.send(MessageChain().message(self._format_result(result)))
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 发送搜索结果失败: {e}")

    async def _send_as_forward(self, event: AstrMessageEvent, result: dict) -> bool:
        """使用 OneBot 合并转发发送 /grok 结果。非 OneBot 平台自动降级。"""
        if not self._supports_forward_output(event):
            return False

        sender_uin = event.get_self_id()
        nodes: list[Node] = []

        use_image = (
            self._cfg("render_as_image", False)
            and self._card_fonts_ready
            and result.get("ok")
        )
        tmp_path: str | None = None
        try:
            if use_image:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name
                render_search_card(
                    content=result.get("content", ""),
                    model=self._cfg("model", ""),
                    elapsed_ms=result.get("elapsed_ms", 0),
                    total_tokens=(result.get("usage") or {}).get("total_tokens", 0),
                    output_path=tmp_path,
                    theme=self._cfg("card_theme", "auto"),
                )
                nodes.append(
                    Node(
                        uin=sender_uin,
                        name=FORWARD_SENDER_NAME,
                        content=[Image.fromFileSystem(tmp_path)],
                    )
                )
            else:
                nodes.append(
                    Node(
                        uin=sender_uin,
                        name=FORWARD_SENDER_NAME,
                        content=[Plain(self._format_result(result))],
                    )
                )

            if use_image:
                src_lines = self._render_sources(
                    result.get("sources", []),
                    header="来源",
                    with_snippet=False,
                )
                if src_lines:
                    nodes.append(
                        Node(
                            uin=sender_uin,
                            name=FORWARD_SENDER_NAME,
                            content=[Plain("\n".join(src_lines).lstrip("\n"))],
                        )
                    )

            await event.send(MessageChain([Nodes(nodes)]))
            return True
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 合并转发发送失败，降级为普通发送: {e}")
            return False
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    async def _send_as_image_card(self, event: AstrMessageEvent, result: dict) -> bool:
        """将搜索结果渲染为图片卡片并发送，附带文本来源链接。

        返回 True 表示图片已发送（来源链接以文本形式分开发送）；
        返回 False 表示渲染或发送失败，调用方应降级为文本模式。
        """
        content = result.get("content", "")
        sources = result.get("sources", [])
        elapsed = result.get("elapsed_ms", 0)
        usage = result.get("usage") or {}
        total_tokens = usage.get("total_tokens", 0)
        model = self._cfg("model", "")
        theme = self._cfg("card_theme", "auto")

        tmp_path: str | None = None
        image_sent = False
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            render_search_card(
                content=content,
                model=model,
                elapsed_ms=elapsed,
                total_tokens=total_tokens,
                output_path=tmp_path,
                theme=theme,
            )
            await event.send(MessageChain().file_image(tmp_path))
            image_sent = True
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 图片卡片发送失败，降级为文本: {e}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        # 来源链接单独以文本发送（可点击/复制）
        if image_sent:
            src_lines = self._render_sources(sources, header="来源", with_snippet=False)
            if src_lines:
                try:
                    # _render_sources 返回的首行带前导换行，去掉以避免空行
                    text = "\n".join(src_lines).lstrip("\n")
                    await event.send(MessageChain().message(text))
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 来源链接发送失败: {e}")

        return image_sent

    @filter.llm_tool(name="grok_web_search")
    async def grok_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        image_urls: str = "",
        search_depth: str = "basic",
        max_results: int = 7,
        topic: str = "general",
        days: int = 0,
        time_range: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """Real-time web search tool. Search the internet and X (Twitter) for the latest, most accurate information.

        When to use:
        - User asks about real-time info, latest news, weather, stock prices, or time-sensitive content
        - You need to verify factual accuracy or are uncertain about some information
        - User explicitly asks you to search or look something up
        - Questions involving content beyond your training data cutoff
        - Need the latest status of a specific URL, product, or person
        - Need to find discussions, posts, or social media sentiment on X (Twitter)

        Returns: Search result summary text with optional source links. Error message on failure.

        Args:
            query(string): Search query — clear, specific, self-contained natural language question or keywords
            image_urls(string): Optional comma-separated image URLs for image-based search
            search_depth(string): "basic" (quick overview), "advanced" (thorough research), or "deep" (exhaustive analysis). Default "basic"
            max_results(int): Desired number of results, 5-20. Default 7
            topic(string): "general" or "news". Default "general"
            days(int): Days to look back from today. Only meaningful with topic="news". 0 = unset
            time_range(string): Time range — "day", "week", "month", or "year"
            start_date(string): Start date in YYYY-MM-DD format
            end_date(string): End date in YYYY-MM-DD format
        """
        # 收集图片：从 LLM 传入的 image_urls 参数 + 用户消息中提取
        images: list[str] = []

        # 1. 解析 LLM 传入的 image_urls
        if image_urls and isinstance(image_urls, str):
            for url in image_urls.split(","):
                url = url.strip()
                if not url:
                    continue
                if url.startswith("base64://"):
                    images.append(url.removeprefix("base64://"))
                elif url.startswith("http"):
                    # 下载并转为 base64
                    try:
                        file_path = await download_image_by_url(url)
                        b64 = file_to_base64(file_path)
                        b64 = b64.removeprefix("base64://")
                        if b64:
                            images.append(b64)
                    except Exception as e:
                        logger.warning(
                            f"[{PLUGIN_NAME}] Failed to download image from URL {url}: {e}"
                        )

        # 2. 从用户消息事件中自动提取内容
        extra_text, event_images = await self._extract_content_from_event(event)
        images.extend(event_images)

        # 只有消息链中确实包含引用/转发组件时，才使用 extra_text
        # 避免普通消息的原文（含唤醒词+指令名）被重复拼接
        if not self._message_has_quoted(event):
            extra_text = None

        # 将引用/转发消息中提取的文本拼接到查询前面作为上下文
        if extra_text:
            query = (
                f"[Referenced message content]\n{extra_text}\n\n[User query]\n{query}"
            )

        if images:
            logger.info(
                f"[{PLUGIN_NAME}] grok_web_search tool: processing with {len(images)} image(s)"
            )

        result = await self._do_search(
            query,
            use_retry=False,
            images=images or None,
            search_depth=search_depth,
            max_results=max_results,
            topic=topic,
            days=days,
            time_range=time_range,
            start_date=start_date,
            end_date=end_date,
        )
        return self._format_result_for_llm(result)

    @filter.llm_tool(name="grok_web_fetch")
    async def grok_fetch_tool(self, event: AstrMessageEvent, url: str):
        """Web content fetching tool. Fetches the full content of a given URL and converts it to structured Markdown format via Grok's web capability.

        When to use:
        - Need to read the full content of a webpage (article, documentation, post, etc.)
        - Need to extract specific data from a webpage (tables, code examples, lists, etc.)
        - User provides a URL and asks to view or summarize its content

        Args:
            url(string): The webpage URL to fetch, must be a complete HTTP/HTTPS address
        """
        if not url or not url.startswith("http"):
            return "错误：请提供完整的 HTTP/HTTPS URL"

        base_url = self._cfg("base_url", "")
        api_key = self._cfg("api_key", "")
        model = self._cfg("model", DEFAULT_MODEL)
        timeout = self._cfg("timeout_seconds", 60)
        proxy = self._cfg("proxy", "") or None

        extra_body_str = self._cfg("extra_body", "")
        extra_headers_str = self._cfg("extra_headers", "")
        extra_body, _ = parse_json_config(extra_body_str)
        extra_headers, _ = parse_json_config(extra_headers_str)

        result = await grok_fetch(
            url=url,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=float(timeout) if timeout else 60.0,
            extra_body=extra_body or None,
            extra_headers=extra_headers or None,
            proxy=proxy,
        )

        if result.get("ok"):
            content = result.get("content", "")
            elapsed = result.get("elapsed_ms", 0)
            if content:
                return f"{content}\n\n---\n耗时: {elapsed}ms"
            return "抓取成功但页面内容为空"
        else:
            error = result.get("error", "未知错误")
            return f"网页抓取失败: {error}"

    async def terminate(self):
        """插件销毁：等待后台字体任务完成。"""
        if self._font_init_task and self._font_init_task.done():
            try:
                await self._font_init_task
            except Exception:
                pass
        self._font_init_task = None
