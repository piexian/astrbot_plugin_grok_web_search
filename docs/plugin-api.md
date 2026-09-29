# 插件服务接口（SDK v1）

本插件向同进程的其他 AstrBot 插件公开一个版本化服务门面（`public_api.GrokSearchService`），
复用与指令 / LLM Tool / Skill 完全相同的业务编排（`tool/search_service.py`、
`tool/fetch_service.py`、`tool/image_search.py`），不叠加额外的网络、重试或费用策略。

## 发现与版本协商

按 metadata 名称做原生发现，禁止按目录路径导入插件内部实现：

```python
def get_grok_service(context):
    meta = context.get_registered_star("astrbot_plugin_grok_web_search")
    if meta is None:
        raise RuntimeError("注册表中未发现 Grok 插件，请检查安装与加载状态")
    if not meta.activated:
        raise RuntimeError("Grok 插件已禁用")
    if meta.star_cls is None:
        raise RuntimeError("Grok 插件尚无可调用实例")
    getter = getattr(meta.star_cls, "get_service", None)
    if not callable(getter):
        raise RuntimeError("Grok 插件版本不支持 SDK，请升级")
    return getter(api_version=1)

service = get_grok_service(context)
```

- `get_service(api_version=1)` 为同步方法；同次插件加载返回同一服务对象。
- 只接受真正的 `int` 1（`bool` 不算）；其他值抛 `PluginServiceError`，`code="unsupported_version"`。
- 未配置、初始化中也可以取得服务并查询状态；不能从服务 `ready` 推断插件是否安装。

## 状态与能力

`get_status()` / `capabilities()` 为同步、本地、无网络副作用的快照：不联网、
不初始化客户端、不读写文件、不返回配置、密钥或内部对象。

```python
status = service.get_status()
# {
#   "api_version": 1,
#   "instance_id": "本次加载的唯一标识",
#   "state": "ready",            # initializing / ready / unavailable / closing / closed
#   "ready": True,               # 当且仅当 state == "ready"
#   "reason": None,              # not_configured / service_closed / ...
#   "search_ready": True,        # base_url + api_key 已配置
#   "fetch_ready": True,         # search_ready 且 enable_fetch 开启
#   "image_search_ready": True,  # SerpAPI 或 SauceNAO Key 任一已配置
#   "config_errors": [],         # 如 ["invalid_extra_body"]，扩展参数 JSON 解析失败
# }

caps = service.capabilities()
# {"api_version": 1, "features": ["web.search", "web.fetch", "image.search"]}
```

- 根 `ready` 由任一可用能力满足；例如仅配置了搜图 Key 时根服务即 `ready`，
  但 `search()` 会因缺少 `base_url/api_key` 被拒绝。
- `features` 表示实现支持的能力，不代表账号权限或当前可执行。
- Skill 安装 / LLM Tool 展示切换只影响模型入口，不影响 SDK 能力可用性。
- `wait_ready(timeout)` 成功返回与 `get_status()` 同形的快照；超时抛 `TimeoutError`；
  `closing/closed` 抛 `PluginServiceError(code="service_closed")`。请显式设置有界超时。
- 不在消费者的 `initialize()` 中等待依赖；在事件处理或后台业务中使用有界等待。
- `search_ready/fetch_ready/image_search_ready` 描述各能力的配置条件，调用前仍须检查根 `ready`；`config_errors` 非空时按报告修正配置。

## 业务方法

```python
await service.search(
    query,
    images=None,            # 可选 base64 图片列表（多模态搜索）
    search_depth="basic",   # basic / advanced / deep
    max_results=7,          # 目标来源数，规范化为 5-20
    topic="general",        # general / news
    days=0, time_range="", start_date="", end_date="",
    system_prompt=None,     # None 使用配置/内置默认提示词
)
```

返回现有搜索字典：`ok / content / sources / raw / model / usage / elapsed_ms / retries`
（失败时 `ok=False` + `error`）。固定 `use_retry=False`，SDK 调用不自动重试；
Chat / Responses 协议选择沿用 `use_responses_api` 配置。

```python
await service.fetch(url)  # -> API 原有字典：ok / content / model / usage / elapsed_ms
```

遵守 `enable_fetch` 配置：未启用时抛 `PluginServiceError(code="feature_disabled")`。
URL 不合法或扩展参数 JSON 无效时返回结构化失败字典（`error_kind="invalid_url" /
"invalid_config"`），不抛出、不静默返回空成功。LLM Tool `grok_web_fetch` 展示的
字符串与 SDK 字典来自同一共享入口（`tool/fetch_service.py`）。
仅接受完整 HTTP/HTTPS URL；无效端口在本地拒绝。超时配置与搜索一致：无法转换为数值或低于 0.001 秒时回退到 60 秒。

```python
await service.reverse_image_search(
    images,                 # base64 图片列表
    use_serpapi=False,      # Google Lens（SerpAPI，收费）
    use_saucenao=False,     # SauceNAO（收费）
)
```

返回既有聚合字典：`requested / serpapi / saucenao / notes / evidence_text`。
两个开关默认 `False`，不会替调用方启用任何收费后端；未配置 Key 的后端按现有
编排本地跳过并在 `notes` 说明。

## 错误与生命周期

- 基础接口错误抛 `PluginServiceError`（`RuntimeError` 子类），稳定 `code`：
  `unsupported_version` / `not_ready`（未配置或初始化中） / `service_closed` /
  `feature_disabled` / `invalid_request`。原有业务失败保留结构化返回，不吞成空成功。
- 插件卸载/重载开始后服务进入 `closing/closed`：拒绝新业务调用；旧 `service`
  与旧 `instance_id` 永久失效，新实例通过 `get_service` 重新获取。
- 已受理的在途请求沿用底层超时与清理规则，不因 SDK 状态查询被取消。
- `ready` 只表示本地配置条件；不保证上游网络、余额或目标服务可用。
