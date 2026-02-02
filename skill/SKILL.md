---
name: grok-search
description: Real-time web research/search via Grok API (outputs JSON with content and sources).
disable_tools: true
---

## Important

**使用此 Skill 时必须禁用工具调用**，否则 AI 会直接调用 `grok_web_search` 工具而非执行脚本。

## When to use

Use this skill **aggressively** - default to searching before answering anything that might be:
- Outdated or time-sensitive (API versions, release info, changelogs)
- Error messages or troubleshooting
- Documentation lookups
- Real-time status queries
- Any information you're not 100% confident about

If you feel even slightly unsure, search first, then answer with evidence.

## Configuration

**AstrBot 插件自动配置（推荐）**：如果在 AstrBot 中已配置 `astrbot_plugin_grok_web_search` 插件，脚本会自动读取插件配置，无需额外设置。

**手动配置（备选）**：

环境变量：
```bash
export GROK_BASE_URL="https://your-grok-endpoint.example"
export GROK_API_KEY="your-api-key"
export GROK_MODEL="grok-4-expert"  # optional
```

或使用配置文件：
- AstrBot 插件配置（自动读取，优先级最高）
- `./config.json` (skill directory)
- `./config.local.json` (skill directory, gitignored)
- `~/.codex/config/grok-search.json` (user global)

## Run

```bash
python scripts/grok_search.py --query "your search query"
```

### Options

| Option | Description |
|--------|-------------|
| `--query` | Search query (required) |
| `--config` | Path to config file |
| `--base-url` | Override base URL |
| `--api-key` | Override API key |
| `--model` | Override model name |
| `--timeout-seconds` | Request timeout in seconds |
| `--extra-body-json` | Extra JSON to merge into request body |
| `--extra-headers-json` | Extra JSON to merge into request headers |

## Output

JSON to stdout (敏感信息如 base_url、api_key 不会输出):

```json
{
  "ok": true,
  "query": "your query",
  "config_path": "[AstrBot Plugin Config]",
  "model": "grok-4-expert",
  "content": "synthesized answer...",
  "sources": [
    {"url": "https://...", "title": "...", "snippet": "..."}
  ],
  "raw": "",
  "usage": {"prompt_tokens": 123, "completion_tokens": 456},
  "elapsed_ms": 3456
}
```

On failure:

```json
{
  "ok": false,
  "error": "HTTP 401",
  "detail": "Unauthorized",
  "config_path": "[AstrBot Plugin Config]",
  "config_status": "OK",
  "model": "grok-4-expert",
  "elapsed_ms": 234
}
```

## Notes

- Endpoint: `POST {base_url}/v1/chat/completions`
- If your provider requires custom flags to enable search, pass them via `--extra-body-json`
- The script uses only Python standard library (no external dependencies)
