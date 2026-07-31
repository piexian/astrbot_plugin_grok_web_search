# Grok 联网搜索 (astrbot_plugin_grok_web_search)

通过 Grok API 进行实时联网搜索，返回综合答案和来源链接。支持多模态图片搜索、网页内容抓取。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | |
| AstrBot | >= v4.9.2 | 基础功能（指令 + LLM Tool） |
| AstrBot | >= v4.13.2 | 使用 Skill 功能 |

**平台支持**: 全平台（无限制）

## 功能

- `/grok` 指令 - 直接执行搜索，支持附带图片进行多模态搜索
- LLM Tool (`grok_web_search`) - 供 LLM 自动调用的实时搜索工具，支持搜索网页和 X (Twitter) 平台
- LLM Tool (`grok_web_fetch`) - 网页内容抓取工具，将 URL 转为结构化 Markdown，利用 Grok 联网能力实现
- Skill 脚本 - 可安装到 skills 目录供 LLM 脚本调用，支持 `--image-files` 传入图片
- 搜索结果图片卡片 - 基于 Pillow 纯本地渲染，面板式布局，支持日/夜自动主题

## 安装

### 俩种方式

1. 在 AstrBot 插件市场搜索 `Grok联网搜索` 点击安装
2. 在插件界面右下角点击加号选择从链接安装输入 ` https://github.com/piexian/astrbot_plugin_grok_web_search  `

## 配置

### 供应商设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `model` | string | 否 | 通用模型名称（默认: grok-4.1-fast），作为各搜索模式的回退模型 |
| `use_responses_api` | bool | 否 | 使用 xAI Responses API（仅官方 API 支持，非官方端点兼容性不佳） |
| `quick_model` | string | 否 | 快速搜索模式模型，留空回退到 `model` |
| `detailed_model` | string | 否 | 详细搜索模式模型，留空回退到 `model` |
| `deep_model` | string | 否 | 深度搜索模式模型，留空回退到 `model` |

### 连接设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `base_url` | string | 是 | Grok API 端点 URL |
| `api_key` | string | 是 | API 密钥 |
| `timeout_seconds` | int | 否 | 超时时间（默认: 60 秒） |
| `proxy` | string | 否 | HTTP 代理地址（例如: http://127.0.0.1:7890） |

### 请求设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `enable_stream` | bool | 否 | 启用 SSE 响应格式并聚合解析最终结果（默认: false） |
| `max_retries` | int | 否 | 最大重试次数（默认: 3） |
| `retry_delay` | float | 否 | 重试间隔时间（默认: 1 秒），429 时优先使用 Retry-After 头 |
| `retryable_status_codes` | list | 否 | 可重试的 HTTP 状态码（默认: [429, 500, 502, 503, 504]） |
| `custom_system_prompt` | text | 否 | 自定义系统提示词；留空时按渲染目标自动选择内置提示词（卡片模式请求结构化 Markdown，文本模式用纯文本） |

### 输出设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `show_sources` | bool | 否 | 是否显示来源 URL（默认: false） |
| `render_as_image` | bool | 否 | 是否将搜索结果渲染为图片卡片（默认: false） |
| `markdown_plain_fallback` | bool | 否 | 卡片渲染/发送失败降级为文本时，是否将结构化 Markdown 转为纯文本（避免裸标记）；仅作用于卡片降级路径，若误伤可关闭（默认: true） |
| `send_as_forward` | bool | 否 | 将 `/grok` 结果以合并转发发送，仅 OneBot v11/aiocqhttp 支持，其他平台自动降级（默认: false） |
| `card_theme` | string | 否 | 卡片主题：auto（按时间自动）/ dark / light（默认: auto） |
| `max_sources` | int | 否 | 最大返回来源数量，0 表示不限制（默认: 5） |

### 工具设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `enable_fetch` | bool | 否 | 启用网页抓取工具（默认: false），关闭时工具不会注册 |
| `enable_skill` | bool | 否 | 安装 Skill 到 skills 目录（启用后所有 LLM Tool 不会注册） |

> 工具开关在插件初始化时生效，修改配置后插件会自动重载卸载工具。

### 图片卡片渲染

启用 `render_as_image` 后，`/grok` 指令的搜索结果将渲染为精美的图片卡片发送：

- **面板式布局**：每个标题自动分割为独立面板，圆角矩形 + 科技青竖条装饰
- **结构化正文**：卡片模式下内置提示词会要求 Grok 返回带标题/列表的 Markdown，正文按 `##` 标题自动分面板，避免渲染成一大块文字墙
- **装饰行清理**：自动剥离中转端点可能泄漏进正文的 `GROK DATA STREAM` / `SYS.STATUS` / `MODEL ::` 等终端装饰行
- **日/夜自动主题**：`card_theme` 为 `auto` 时根据系统时间自动切换（7:00-18:00 浅色）
- **Markdown 支持**：标题、列表、代码块、引用、**粗体**、`行内代码`
- **来源链接**：以单独文本消息发送（可点击/复制）

启用 `send_as_forward` 后，OneBot v11/aiocqhttp 平台会优先将 `/grok` 结果作为合并转发发送。

#### 效果展示

| 深色主题 | 浅色主题 |
|:---:|:---:|
| ![深色主题](https://github.com/piexian/astrbot_plugin_grok_web_search/blob/master/image/dark.png) | ![浅色主题](https://github.com/piexian/astrbot_plugin_grok_web_search/blob/master/image/light.png) |

**字体说明**：首次启用时自动从清华镜像下载 Sarasa Term Slab SC 字体。也可在 `data/plugin_data/astrbot_plugin_grok_web_search/font/` 目录放入自定义 `.ttf` 字体文件。

### 扩展参数

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `extra_body` | JSON | 否 | 额外请求体参数 |
| `extra_headers` | JSON | 否 | 额外请求头 |

## 使用

### 指令

```
/grok Python 3.12 有什么新特性
/grok 最新的 AI 新闻
/grok help              # 显示帮助和当前配置状态
```

发送图片时附带 `/grok` 指令，可进行多模态图片搜索：

```
[图片] /grok 这张图片里有什么？
```

> `/grok help` 会显示当前供应商来源、模型、系统提示词类型等配置信息。

### 重试机制

- `/grok` 指令启用自动重试，429 时优先使用服务端 `Retry-After` 头指定的等待时间，其他错误使用线性退避
- LLM Tool 不自动重试，失败立即返回，由 AI 自行决定是否重新调用
- 重试仅对自定义 HTTP 客户端通过 `retryable_status_codes` 匹配状态码
- 使用 AstrBot 自带供应商时，采用异常重试机制（不受 `retryable_status_codes` 限制）

### LLM Tool

当 LLM 需要搜索实时信息时，会自动调用 `grok_web_search` 工具。如果用户消息中包含图片，工具会自动提取图片进行多模态搜索。LLM 也可以通过 `image_urls` 参数主动传入图片链接。

每次搜索请求会自动注入当前时间上下文（日期、星期、时区），帮助 Grok 更好地处理时效性查询。

### Web Fetch

`grok_web_fetch` 工具可抓取指定 URL 的网页内容并转为结构化 Markdown。利用 Grok 的联网能力实现。

```
# LLM 可自动调用，例如用户说：
"帮我看看 https://example.com 这个页面的内容"
```

### Skill

开启 `enable_skill` 后，会安装 Skill 到 `data/skills/grok-search/`，LLM 可读取 SKILL.md 后执行脚本。

Skill 脚本支持通过 `--image-files` 参数传入本地图片进行多模态搜索：

```bash
python scripts/grok_search.py --query "这张图片是什么？" --image-files "/path/to/image.jpg"
```

## 输出示例

```
Python 3.12 的主要新特性包括:

1. 更好的错误消息 - 改进了语法错误提示
2. 类型参数语法 - 支持泛型类型参数
3. 性能提升 - 解释器启动更快

来源:
  1. Python 3.12 Release Notes
     https://docs.python.org/3/whatsnew/3.12.html
  2. ...

(耗时: 2345ms)
```

## 项目结构

```
astrbot_plugin_grok_web_search/
├── main.py              # 插件主入口
├── api/                 # API 客户端
│   ├── grok_chat.py     # Chat Completions API 客户端
│   └── grok_responses.py# Responses API 客户端（xAI 官方）
├── tool/                # 工具模块
│   ├── tool.py          # 共享工具（常量、工具函数、重试逻辑）
│   └── card_render.py   # 搜索结果图片卡片渲染器
├── image/               # 示例图片
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项 Schema
├── README.md
└── skill/               # Skill 脚本（运行时同步到 plugin_data，保留本地配置）
    ├── SKILL.md         # Skill 说明文档
    └── scripts/
        └── grok_search.py  # 独立搜索脚本（仅标准库）
```

## 致谢

- [grok-skill](https://github.com/Frankieli123/grok-skill) — 原始 Skill 脚本项目，感谢 [@a3180623](https://linux.do/u/a3180623/summary) 的开源贡献。
- [GrokSearch](https://github.com/GuDaStudio/GrokSearch) — 网页内容抓取功能参考了该项目的实现，感谢 [GuDa Studio](https://github.com/GuDaStudio) 的开源贡献。
- [@Stonesan233](https://github.com/Stonesan233) — PR [#5](https://github.com/piexian/astrbot_plugin_grok_web_search/pull/5) 贡献了 Responses API 支持、x_search 工具和代理配置。

## 更新日志

查看 [CHANGELOG.md](https://github.com/piexian/astrbot_plugin_grok_web_search/blob/master/CHANGELOG.md) 了解版本更新历史。

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [Issues](https://github.com/piexian/astrbot_plugin_grok_web_search/issues)

## 🔗 相关链接
- [AstrBot](https://docs.astrbot.app/)
- [grok2api](https://github.com/chenyme/grok2api)

## 许可

AGPL-3.0 License

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>
