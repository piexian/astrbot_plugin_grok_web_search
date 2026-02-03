# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.4] - 2026-02-03

### Added
- 兼容 SSE 流式响应：自动检测并解析 `text/event-stream` 格式的响应，合并所有 chunk 内容后返回
- 新增 `enable_thinking` 配置项：是否开启思考模式（默认开启）
- 新增 `thinking_budget` 配置项：思考 token 预算（默认 32000）

### Changed
- 默认模型从 `grok-4-expert` 改为 `grok-4-fast`
- 开启思考模式时自动添加 `reasoning_effort: "high"` 和 `reasoning_budget_tokens` 参数

<details>
<summary>历史版本</summary>

## [1.0.3] - 2026-02-02

### Added
- 新增 `reuse_session` 配置项：复用 HTTP 会话，高频调用场景可开启以减少连接开销（默认关闭）

### Changed
- `parse_json_config()` 不再直接输出到 stderr，改为返回错误信息由调用方通过 logger 记录
- `grok_search()` 支持传入外部 `aiohttp.ClientSession` 以复用连接
- 所有错误信息改为中文友好提示，包含具体原因和解决建议
- 异常处理细化：捕获具体异常类型，记录详细解析失败原因

### Fixed
- 修复 JSON 配置解析失败时日志绕过 AstrBot logger 的问题

### Security
- `extra_body` 保护关键字段（`model`、`messages`、`stream`）不被覆盖
- `extra_headers` 保护关键请求头（`Authorization`、`Content-Type`）不被覆盖

## [1.0.2] - 2026-02-02

### Changed
- 启用 Skill 时自动禁用 LLM Tool，避免 AI 重复调用

### Added
- 新增 `show_sources` 配置项：控制是否显示来源 URL（默认关闭）
- 新增 `max_sources` 配置项：控制最大返回来源数量

### Changed
- LLM Tool 返回结果改为纯文本格式（无 Markdown）
- Grok 提示词添加禁止返回 Markdown 格式的要求

## [1.0.0] - 2026-02-02

### Added
- `/grok` 指令：直接执行联网搜索
- `grok_web_search` LLM Tool：供 LLM 自动调用
- Skill 脚本支持：可安装到 skills 目录供 LLM 脚本调用
- 配置项支持：
  - `base_url`: Grok API 端点
  - `api_key`: API 密钥
  - `model`: 模型名称
  - `timeout_seconds`: 超时时间
  - `extra_body`: 额外请求体参数
  - `extra_headers`: 额外请求头
  - `enable_skill`: Skill 安装开关
- GitHub Issue 模板（Bug 报告、功能请求）
- GitHub Actions CI 配置（ruff lint + format check）

### Security
- JSON 响应解析异常处理
- API 错误和空响应检测
- Skill 安装 symlink 安全检查
- 占位符 URL/API Key 过滤

</details>
