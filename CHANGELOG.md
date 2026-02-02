# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.0.1] - 2026-02-02

### Added
- 新增 `show_sources` 配置项：控制是否显示来源 URL（默认关闭）
- 新增 `max_sources` 配置项：控制最大返回来源数量

### Changed
- LLM Tool 返回结果改为纯文本格式（无 Markdown）
- Grok 提示词添加禁止返回 Markdown 格式的要求

<details>
<summary>历史版本</summary>

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
