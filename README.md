# Grok 联网搜索 (astrbot_plugin_grok_web_search)

通过 Grok API 进行实时联网搜索，返回综合答案和来源链接。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | |
| AstrBot | >= v4.9.2 | 基础功能（指令 + LLM Tool） |
| AstrBot | >= v4.13.2 | 使用 Skill 功能 |

## 功能

- `/grok` 指令 - 直接执行搜索
- LLM Tool (`grok_web_search`) - 供 LLM 自动调用的函数工具
- Skill 脚本 - 可安装到 skills 目录供 LLM 脚本调用

## 安装

1. 在 AstrBot 插件市场搜索 `Grok联网搜索` 或手动克隆到 `data/plugins/` 目录
2. 在管理面板配置必要参数

## 配置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `base_url` | string | 是 | Grok API 端点 URL |
| `api_key` | string | 是 | API 密钥 |
| `model` | string | 否 | 模型名称（默认: grok-4-fast） |
| `enable_thinking` | bool | 否 | 是否开启思考模式（默认: true） |
| `thinking_budget` | int | 否 | 思考 token 预算（默认: 32000） |
| `timeout_seconds` | int | 否 | 超时时间（默认: 60秒） |
| `show_sources` | bool | 否 | 是否显示来源 URL（默认: false） |
| `max_sources` | int | 否 | 最大返回来源数量，0 表示不限制（默认: 5） |
| `extra_body` | JSON | 否 | 额外请求体参数 |
| `extra_headers` | JSON | 否 | 额外请求头 |
| `enable_skill` | bool | 否 | 是否安装 Skill 到 skills 目录（启用后将禁用 LLM Tool） |
| `reuse_session` | bool | 否 | 是否复用 HTTP 会话（高频调用场景可开启，默认: false） |

## 使用

### 指令

```
/grok Python 3.12 有什么新特性
/grok 最新的 AI 新闻
/grok help
```

### LLM Tool

当 LLM 需要搜索实时信息时，会自动调用 `grok_web_search` 工具。

### Skill

开启 `enable_skill` 后，会安装 Skill 到 `data/skills/grok-search/`，LLM 可读取 SKILL.md 后执行脚本。

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
├── grok_client.py       # Grok API 异步客户端
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项 Schema
├── README.md
└── skill/               # Skill 脚本（首次运行后迁移到 plugin_data）
    ├── SKILL.md         # Skill 说明文档
    └── scripts/
        └── grok_search.py  # 独立搜索脚本（仅标准库）
```

## 致谢

本插件基于 [grok-skill](https://github.com/Frankieli123/grok-skill) 项目改造，感谢原作者 [@a3180623](https://linux.do/u/a3180623/summary) 的贡献。

## 更新日志

查看 [CHANGELOG.md](CHANGELOG.md) 了解版本更新历史。

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [Issues](https://github.com/piexian/astrbot_plugin_grok_web_search/issues)

## 🔗 相关链接
- [AstrBot](https://docs.astrbot.app/)
- [grok2api](https://github.com/chenyme/grok2api) 

## 许可

AGPL-3.0 License
