# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.5.1] - 2026-08-01

### Fixed
- **卡片渲染正文成“文字墙”**：`/grok` 内置提示词原先一律禁止 Markdown，与图片卡片渲染器（按标题分面板）的格式需求冲突，导致卡片正文渲染成一整块纯文本。现按渲染目标自动选择内置提示词：卡片模式请求结构化 Markdown（`##` 标题 + 列表 + 粗体），文本模式保持纯文本
- **中转端点装饰行泄漏**：新增 `strip_stream_decorations()` 保守清理中转/转发端点可能泄漏进正文的 `GROK DATA STREAM` / `SYS.STATUS` / `MODEL ::` / `耗时 · tokens` 等终端装饰行（仅动首尾，中间正文不改）
- **`grok_web_fetch` 工具返回污染上下文**：移除工具返回中追加的 `---\n耗时: Xms` 后缀，该元信息仅对用户有意义，不应进入主模型上下文

### Changed
- **移除硬编码纯文本返回提示**：删除 `grok_web_search` 工具结果中强制主模型“使用纯文本回复”的追加提示，避免与主模型自身格式策略冲突
- **提示词常量集中**：新增 `CMD_CARD_SYSTEM_PROMPT` / `CMD_TEXT_SYSTEM_PROMPT` 常量，`/grok` 指令不再内联硬编码提示词
- **来源链接统一去重**：在共享出口 `parse_sources_from_message()` 对 `sources` 按 url 保序去重（并跳过空白 url），指令 / 工具 / 卡片 / Skill 四条下游一次到位，消除原先仅 `extract_urls` 回退分支去重、主列表不去重的不一致

## [1.5.0] - 2026-05-21

### Added
- **三种搜索模式**：快速(quick)、详细(detailed)、深度(deep)，`search_depth` 参数控制，配置中可为每种模式指定独立模型（`quick_model`/`detailed_model`/`deep_model`）
- **扩展搜索参数**：`grok_web_search` LLM Tool 新增 `search_depth`、`max_results`、`topic`、`days`、`time_range`、`start_date`、`end_date` 参数
- **时间约束自动填充**：`build_search_time_constraints()` 根据参数自动计算时间窗口，`topic=news` 默认最近 7 天
- **SSE 响应格式开关**：`enable_stream` 配置项，启用后请求 Server-Sent Events 响应并聚合解析最终结果
- **共享规范化函数**：`normalize_search_options`、`resolve_mode_model`、`resolve_reasoning_params`、`resolve_search_mode`

### Changed
- **推理参数内部化**：`enable_thinking`/`thinking_budget` 配置移除，改为根据 `search_depth` 自动设置 `reasoning_effort`
- **工具描述更新**：`grok_web_search`/`grok_web_fetch` 文档字符串更新为英文，精简冗余描述

### Removed
- **内置供应商支持**：移除 `use_builtin_provider`、`provider` 配置及 `_do_search_via_builtin_provider` 方法
- **HTTP 会话复用**：移除 `reuse_session` 配置及插件级 `_session` 成员

## [1.4.1] - 2026-05-05

### Added
- **合并转发**：`send_as_forward` 配置项，OneBot v11/aiocqhttp 平台下 `/grok` 结果以合并转发发送，其他平台自动降级
- **配置迁移**：`_migrate_legacy_config()` 自动将旧版平铺配置迁移到新版分组结构

### Changed
- **配置 Schema 重构**：平铺式配置重组为分组结构（`provider_settings` / `connection_settings` / `request_settings` / `output_settings` / `tool_settings` / `advanced_settings`），旧版平铺键保留 `invisible` 以兼容
- **配置读取统一**：`_cfg()` 方法按 `CONFIG_PATHS` 映射分层读取配置，带默认值回退
- **图片 Base64 转换重构**：`_append_image_base64()` 统一处理 Image 组件、base64://、data:image/、http(s)://、本地路径五种来源
- **引用消息解析增强**：优先使用 astrbot 新版 `quoted_message_parser`，失败时回退 `chain_parser`

## [1.4.0] - 2026-05-01

### Added
- **代理支持（字体下载）**：`font_loader` 新增 `set_proxy()` / `_urlopen()` 包装，字体初始化时自动从插件 `proxy` 配置注入代理，在受限网络环境下也能正常下载 Sarasa Gothic 字体
- **`tool/__init__.py` 公开导出**：re-export `tool.tool` 中的 22 个常用符号，`skill/scripts/grok_search.py` 可直接 `from tool import ...` 而无需 ImportError

### Changed
- **代码重构（18 项优化）**：将大量重复逻辑提取为独立辅助函数（`build_user_content`、`safe_number`、`resolve_system_prompt`、`is_safe_url` 等），减少代码重复；`_do_search` 拆分为内置供应商与 HTTP 两个分支
- **`font_loader` 独立模块**：从 `card_render.py` 提取为 `tool/font_loader.py`，自动发现最新版本、多线程分段下载、自动解压；所有 `urlopen` 调用均通过 context manager 管理，避免连接泄漏
- **CI 工作流**：切换为代码质量检查工作流（Lint/Syntax/Metadata），修复 isort/UP037 问题
- **依赖声明**：将运行时依赖从 `metadata.yaml` 迁移到 `requirements.txt`，去除 aiohttp/Pillow 重复声明
- **`terminate()` 行为修正**：不再对 `asyncio.to_thread` 包装的字体初始化任务调用 `cancel()`（无法终止底层线程），改为 detach 让其自行结束

### Fixed
- **冗余 `isinstance` 检查**：`grok_chat.py` 和 `grok_responses.py` 中对 `IMAGE_UNSUPPORTED_ERROR` 的 `isinstance(x, dict) and x is ...` 改为单纯 `is` 同一性比较
- **`subprocess` 安全告警**：`_extract_7z` 改用 `shutil.which()` 解析 7z/7za 绝对路径，消除 PATH 注入风险

## [1.3.1] - 2026-04-12

### Fix
-修复了会误把指令和唤醒词当成消息发给api的问题

## [1.3.0] - 2026-03-17

### Added
- **搜索结果图片卡片渲染**：新增 `card_render.py`，基于 Pillow 纯本地渲染，将搜索结果渲染为面板式深色/浅色卡片图片
  - 支持 Markdown 子集：标题、列表、代码块、引用、**粗体**、`行内代码`
  - 每个标题自动分割为独立面板，圆角矩形 + 科技青竖条装饰
  - 来源链接单独以文本消息发送（可点击/复制）
- **日/夜自动主题**：`card_theme` 配置项支持 `auto`（7:00-18:00 浅色，其余深色）、`dark`、`light`
- **字体自动下载**：首次使用时从清华镜像自动下载 Sarasa Term Slab SC 字体（7z），解压后保留所需 ttf
- **自定义字体**：在字体目录放入自定义 .ttf 文件即可替代默认字体
- **`render_as_image` 配置项**：图片卡片渲染开关（默认关闭）
- **`card_theme` 配置项**：卡片主题选择（auto/dark/light）
- **行内代码渲染**：`` `code` `` 以带背景色的药丸样式渲染

### Changed
- 项目文件重构：API 客户端移入 `api/` 子目录，工具模块移入 `tool/` 子目录
- 字体存储路径为 `data/plugin_data/{plugin_name}/font/`，不再随插件源码

## [1.2.0] - 2026-03-17

### Added
- **Responses API 支持**：新增 `grok_responses.py` 模块，支持 xAI `/v1/responses` 接口（PR #5 by [@Stonesan233](https://github.com/Stonesan233)）
- **`use_responses_api` 配置项**：切换 Chat Completions / Responses API 模式
- **x_search 工具**：同时启用 `web_search` 和 `x_search`，支持 X/Twitter 平台搜索
- **`proxy` 配置项**：支持 HTTP 代理（应用于连通性检查和搜索请求）
- 官方错误码友好提示（400-429）
- **`grok_web_fetch` LLM Tool**：网页内容抓取工具，将 URL 转为结构化 Markdown，利用 Grok 联网能力实现
- **`enable_fetch` 配置项**：网页抓取工具开关（默认关闭），关闭时初始化阶段直接卸载工具
- **时间注入**：搜索时自动注入当前日期、星期、时间、时区上下文，提升时效性查询准确度
- **Retry-After 解析**：429 错误时优先使用服务端 `Retry-After` 头指定的等待时间（支持秒数和 HTTP 日期格式）
- Skill 脚本适配 Responses API，通过读取插件配置 `use_responses_api` 自动切换 API 模式
- Skill 脚本新增 `--fetch-url` 抓取模式，利用 Grok 联网能力将网页转为结构化 Markdown

### Changed
- **架构重构**：提取共享代码到 `tool.py`（常量、工具函数、重试逻辑、响应解析等），`grok_client.py` 重命名为 `grok_chat.py`
  - `grok_chat.py`：Chat Completions API（747→287 行）
  - `grok_responses.py`：Responses API（440→263 行）
  - `tool.py`：共享工具模块（338 行）
- LLM Tool 描述优化，明确多模态和 X 平台搜索能力
- **搜索提示词增强**：广度优先→深度优先搜索策略，优先权威来源，支持中英双语搜索
- `extra_body` / `extra_headers` 配置改为 JSON 编辑器模式，默认为空
- `retry_delay` hint 更新以反映 Retry-After 优先策略
- **工具按需加载**：`enable_skill` / `enable_fetch` 在初始化时直接从全局注册表卸载不需要的 LLM Tool，而非每次请求时移除

### Fixed
- 图片格式自动检测与转换：通过 PIL（优先）或魔数字节识别图片格式（JPEG/PNG/GIF/WebP），不支持的格式自动转换为 PNG/JPEG；无法识别时直接报错并给出友好提示
- 添加 `detail: "high"` 参数以获得更好的图片理解效果

## [1.1.0] - 2026-03-13

### Added
- **图片搜索**：`/grok` 指令、`grok_web_search` LLM Tool、Skill 脚本均支持图片输入
- `/grok` 指令：自动提取用户消息中的图片，支持直接发送图片、回复带图片的消息、QQ 转发消息（嵌套）
- `/grok` 指令：自动提取回复消息和转发消息中的文本内容作为查询上下文
- `grok_web_search` LLM Tool：新增 `image_urls` 参数，支持传入图片 URL 或 base64 链接
- `grok_web_search` LLM Tool：自动提取用户消息中的图片和文本上下文
- Skill 脚本：新增 `--image-files` 参数，支持传入本地图片文件路径
- `grok_chat.py`（原 `grok_client.py`）：`grok_search()` 支持 `images` 参数，构建 OpenAI 接口的`image_url` 消息

### Changed
- CI 工作流改为自动修复模式：`ruff format` + `ruff check --fix`，格式变更自动提交


<details>
<summary>历史版本</summary>

## [1.0.9] - 2026-03-11

### Fixed
- 修复 `/grok` 指令关键词含空格时只取第一个词的问题（如 `/grok 1 2 3` 只搜索 `1`）
- 使用 AstrBot 框架的 `GreedyStr` 类型捕获命令后的完整文本

## [1.0.8] - 2026-03-08

### Changed
- Skill 安装改用 `SkillManager.install_skill_from_zip()` 官方接口，正式注册到 `skills.json` 配置
- Skill 卸载改用 `SkillManager.delete_skill()` 官方接口，同步清理目录和配置
- Skill 首次迁移从移动改为复制，插件源目录始终保留原始副本
- 移除手动路径管理回退逻辑，统一依赖 SkillManager API

## [1.0.7] - 2026-03-04

### Added
- 新增 JSON 响应降级处理：当内置供应商返回非 JSON 格式时，自动提取纯文本和 URL 作为来源，不再直接报错
- 新增 `_try_parse_json_response()` 方法：支持解析多种格式（纯 JSON、Markdown 代码块、混合文本中的嵌套 JSON）
- 新增 `_extract_sources_from_text()` 方法：从非 JSON 文本中提取 URL 作为来源

### Changed
- `/grok` 指令提示词改为英文指令 + JSON 格式 + 中文回复要求（专有名词保留原文）
- LLM Tool 和 Skill 提示词保持英文 + JSON 格式（无语言要求）
- JSON 解析改用 `json.JSONDecoder().raw_decode` 支持嵌套结构，避免正则截断问题

### Fixed
- 修复混合文本中嵌套 JSON 解析失败的问题
- 修复内置供应商返回非 JSON 时用户看到"获取到非 JSON 文本"错误的问题

### Security
- URL 协议白名单校验：仅允许 `http`/`https`，拒绝 `javascript:`、`data:`、`file:` 等协议
- URL 长度限制：最大 2048 字符
- URL 控制字符过滤：拒绝包含 ASCII 控制字符的 URL
- 错误响应检测：识别 rate limit、unauthorized 等错误模式，避免将错误文案误判为成功

## [1.0.6] - 2026-02-21

### Added
- 新增 `astrbot_version` 元数据字段：声明最低 AstrBot 版本要求 (>=4.9.2)
- 新增 `support_platforms` 元数据字段：声明支持的平台（空数组表示全平台支持）

### Changed
- 适配 AstrBot PR #5235 插件元数据规范，支持版本兼容性检查

## [1.0.5] - 2026-02-12

### Added
- 新增 `use_builtin_provider` 配置项：支持使用 AstrBot 自带供应商
- 新增 `provider` 配置项：选择已配置的 LLM 供应商（仅当启用自带供应商时生效）
- 新增 `max_retries` 配置项：最大重试次数（默认: 3，支持滑块调节 0-10）
- 新增 `retry_delay` 配置项：重试间隔时间（默认: 1 秒，支持滑块调节 0.1-5 秒）
- 新增 `retryable_status_codes` 配置项：可重试的 HTTP 状态码列表（默认: 429, 500, 502, 503, 504）
- 新增 `custom_system_prompt` 配置项：自定义系统提示词（支持多行编辑器）
- `/grok` 指令使用独立的中文系统提示词，要求使用中文回复
- `/grok help` 显示当前配置状态（供应商来源、模型、提示词类型）
- 支持延迟初始化：启用自带供应商时，在 AstrBot 加载完成后初始化

### Changed
- 当启用自带供应商时，自动使用供应商默认模型和参数（不覆盖 model/reasoning 等字段）
- 重试功能仅对 `/grok` 指令启用，LLM Tool 不再自动重试（由 AI 自行决定是否重新调用）
- `retryable_status_codes` 仅对自定义 HTTP 客户端生效，内置供应商使用异常重试机制
- 内置供应商重试延迟改为线性退避策略（`retry_delay * attempts`），与外部客户端行为一致
- 配置项描述和提示信息拆分为 `description` + `hint`，提升可读性
- 简化 `max_retries` / `retry_delay` 配置解析逻辑，由 UI 滑块约束输入范围

### Fixed
- 修复 `/grok` 指令发送失败后 LLM 兜底重复调用 `grok_web_search` 的问题
- 修复自定义供应商模式下 `/grok help` 仍显示内置供应商名称的问题


## [1.0.4] - 2026-02-03

### Added
- 兼容 SSE 流式响应：自动检测并解析 `text/event-stream` 格式的响应，合并所有 chunk 内容后返回
- 新增 `enable_thinking` 配置项：是否开启思考模式（默认开启）
- 新增 `thinking_budget` 配置项：思考 token 预算（默认 32000）

### Changed
- 默认模型从 `grok-4-expert` 改为 `grok-4-fast`
- 开启思考模式时自动添加 `reasoning_effort: "high"` 和 `reasoning_budget_tokens` 参数

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
