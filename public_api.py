"""SDK v1 公开服务门面：供其他插件以稳定接口复用搜索/抓取/反向搜图能力。

通过原生发现使用：meta.star_cls.get_service(api_version=1)。门面只暴露
具名方法与白名单快照，不透传插件实例、原始配置、密钥或内部对象；
业务路径复用 main.py 与 tool/ 的共享编排，不叠加网络、重试或费用策略。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .tool.config import parse_json_setting
from .tool.tool import normalize_api_key, normalize_base_url

# SDK v1 固定能力标识：声明实现支持的能力，不代表账号权限或当前可执行。
FEATURES = ("web.search", "web.fetch", "image.search")

_READY_POLL_SECONDS = 0.1


class PluginServiceError(RuntimeError):
    """SDK v1 基础接口错误；code 为稳定原因码（unsupported_version 等）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class GrokSearchService:
    """同进程、版本化的 Grok 搜索公开服务（SDK v1）。

    状态最低字段为 api_version/instance_id/state/ready/reason，并附加
    search_ready/fetch_ready/image_search_ready 与 config_errors。
    插件重载后旧实例永久失效；新服务 instance_id 不同。
    """

    api_version = 1
    features = list(FEATURES)

    def __init__(self, plugin):
        self._plugin = plugin
        self.instance_id = uuid.uuid4().hex
        self._state = "initializing"

    # ── 生命周期（由插件 Main 驱动） ──────────────────────────────

    def mark_initialized(self) -> None:
        """initialize 完成后调用；此后状态按本地配置推导。"""
        if self._state == "initializing":
            self._state = "ready"

    def begin_shutdown(self) -> None:
        """terminate 开始时调用：拒绝新业务调用，旧引用永久失效。"""
        if self._state in ("initializing", "ready"):
            self._state = "closing"

    def close(self) -> None:
        self._state = "closed"

    def for_api_version(self, api_version: int = 1) -> GrokSearchService:
        """按 SDK 约定协商版本；只接受真正的 int 1（不接受 bool）。"""
        if type(api_version) is not int or api_version != 1:
            raise PluginServiceError("unsupported_version", "仅支持插件搜索服务接口 v1")
        return self

    # ── 基础接口：状态 / 能力 / 等待 ──────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """本地快照：只解析配置，不联网、不初始化客户端、不返回配置或密钥。"""
        state, reason = self._state, None
        if state in ("closing", "closed"):
            reason = "service_closed"
        search_ready = self._search_ready()
        flags = {
            "search_ready": search_ready,
            "fetch_ready": search_ready
            and bool(self._plugin._cfg("enable_fetch", False)),
            "image_search_ready": self._image_search_ready(),
        }
        # 根就绪可由任一可用能力满足；均未配置则明确不可用
        if state == "ready" and not any(flags.values()):
            state, reason = "unavailable", "not_configured"
        status: dict[str, Any] = {
            "api_version": self.api_version,
            "instance_id": self.instance_id,
            "state": state,
            "ready": state == "ready",
            "reason": reason,
        }
        status.update(flags)
        status["config_errors"] = self._config_errors()
        return status

    def capabilities(self) -> dict[str, Any]:
        return {"api_version": self.api_version, "features": list(self.features)}

    async def wait_ready(self, timeout: float | None = None) -> dict[str, Any]:
        """等待根服务就绪；成功返回与 get_status 同形的快照。

        超时抛 TimeoutError；closing/closed 抛 code=service_closed 的
        PluginServiceError。调用方应显式设置有界超时。
        """

        status = self.get_status()
        if status["ready"]:
            return status
        if status["state"] in ("closing", "closed"):
            raise PluginServiceError("service_closed", "服务实例已关闭，请重新获取")

        async def _wait() -> dict[str, Any]:
            while True:
                status = self.get_status()
                if status["ready"]:
                    return status
                if status["state"] in ("closing", "closed"):
                    raise PluginServiceError(
                        "service_closed", "服务实例已关闭，请重新获取"
                    )
                await asyncio.sleep(_READY_POLL_SECONDS)

        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("wait_ready 超时") from exc

    # ── 本地配置判定（无副作用；Skill/Tool 展示切换不影响可用性） ──

    def _search_ready(self) -> bool:
        base_url = self._plugin._cfg("base_url", "")
        api_key = self._plugin._cfg("api_key", "")
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            return False
        return bool(normalize_base_url(base_url)) and bool(normalize_api_key(api_key))

    def _image_search_ready(self) -> bool:
        cfg = self._plugin._cfg
        return bool(
            str(cfg("serpapi_api_key", "") or "").strip()
            or str(cfg("saucenao_api_key", "") or "").strip()
        )

    def _config_errors(self) -> list[str]:
        """扩展参数 JSON 解析失败的稳定原因码；不回显配置内容。"""
        errors: list[str] = []
        for key, code in (
            ("extra_body", "invalid_extra_body"),
            ("extra_headers", "invalid_extra_headers"),
        ):
            _, error = parse_json_setting(self._plugin._cfg(key, ""))
            if error:
                errors.append(code)
        return errors

    # ── 业务入口 ─────────────────────────────────────────────────

    def _admit_open(self) -> dict[str, Any]:
        """业务调用前置检查：已关闭/初始化中直接拒绝。"""
        status = self.get_status()
        if status["state"] in ("closing", "closed"):
            raise PluginServiceError("service_closed", "服务实例已关闭，请重新获取")
        if status["state"] == "initializing":
            raise PluginServiceError("not_ready", "服务初始化尚未完成，请先 wait_ready")
        return status

    async def search(
        self,
        query: str,
        *,
        images: list[str] | None = None,
        search_depth: str = "basic",
        max_results: int = 7,
        topic: str = "general",
        days: int = 0,
        time_range: str = "",
        start_date: str = "",
        end_date: str = "",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """执行一次联网搜索；固定不重试，返回现有 ok/content/sources/... 字典。

        Chat/Responses 协议选择与既有配置保持一致；images 为可选的
        base64 编码图片列表。
        """
        status = self._admit_open()
        if not status["search_ready"]:
            raise PluginServiceError(
                "not_ready", "缺少 base_url/api_key 配置，搜索不可用"
            )
        if not isinstance(query, str) or not query.strip():
            raise PluginServiceError("invalid_request", "query 必须是非空字符串")
        if images is not None and (
            not isinstance(images, (list, tuple))
            or not all(isinstance(item, str) for item in images)
        ):
            raise PluginServiceError(
                "invalid_request", "images 必须是 base64 字符串列表"
            )
        return await self._plugin._do_search(
            query,
            system_prompt=system_prompt,
            use_retry=False,
            images=list(images) if images else None,
            search_depth=search_depth,
            max_results=max_results,
            topic=topic,
            days=days,
            time_range=time_range,
            start_date=start_date,
            end_date=end_date,
        )

    async def fetch(self, url: str) -> dict[str, Any]:
        """抓取网页；返回 API 原有结构化字典，遵守 enable_fetch 开关。"""
        status = self._admit_open()
        if not bool(self._plugin._cfg("enable_fetch", False)):
            raise PluginServiceError(
                "feature_disabled", "网页抓取未启用（enable_fetch）"
            )
        if not status["search_ready"]:
            raise PluginServiceError(
                "not_ready", "缺少 base_url/api_key 配置，抓取不可用"
            )
        if not isinstance(url, str) or not url.strip():
            raise PluginServiceError("invalid_request", "url 必须是非空字符串")
        return await self._plugin._do_fetch(url)

    async def reverse_image_search(
        self,
        images: list[str],
        *,
        use_serpapi: bool = False,
        use_saucenao: bool = False,
    ) -> dict[str, Any]:
        """反向搜图；两个开关默认 False，不替调用方启用收费后端。

        返回含后端结果与 evidence_text 的既有聚合字典。
        """
        status = self._admit_open()
        if not status["image_search_ready"]:
            raise PluginServiceError(
                "not_ready", "缺少 SerpAPI/SauceNAO Key 配置，反向搜图不可用"
            )
        if not isinstance(use_serpapi, bool) or not isinstance(use_saucenao, bool):
            raise PluginServiceError(
                "invalid_request", "use_serpapi/use_saucenao 必须是布尔值"
            )
        if not isinstance(images, list) or not all(
            isinstance(item, str) for item in images
        ):
            raise PluginServiceError(
                "invalid_request", "images 必须是 base64 字符串列表"
            )
        return await self._plugin._run_reverse_image_search(
            list(images), use_serpapi, use_saucenao
        )
