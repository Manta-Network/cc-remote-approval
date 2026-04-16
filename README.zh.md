# CC Remote Approval

[English](README.md) | **中文**

远程审批 Claude Code 权限请求 — 不在电脑前也能让 agent 继续运行。

`cc-remote-approval` 是一个 Claude Code 插件。当 Claude 需要你审批（权限、表单、问题）时，如果你没在电脑前操作，几秒后通知发到你的消息渠道，你远程点按钮就能继续。

---

## 工作原理

```
Claude Code 需要权限 → 本地弹出原生对话框（照常）
                     → 同时 hook 开始计时
                        ↓ 20 秒没操作（可配置）
                     → 消息渠道收到通知 + 按钮
                        ↓
                     你远程点 Allow / Deny / Always
                        ↓
                     Claude Code 继续
```

**两边赛跑**：本地对话框和远程渠道同时可用，谁先响应谁生效，另一边自动同步状态。

---

## 支持的场景

| 场景 | Hook | 说明 |
|---|---|---|
| Bash / Edit / Write 审批 | PermissionRequest | Allow / Always / Deny 按钮 |
| AskUserQuestion | PermissionRequest | 选项按钮 + 文本输入 |
| MCP 表单 (Elicitation) | Elicitation | 远程表单，60s 超时回退本地 |
| Agent 空闲 | Notification | 💤 idle 通知 |

---

## 安装

在 Claude Code 中运行：

```
/plugin marketplace add Manta-Network/cc-remote-approval
/plugin install cc-remote-approval@manta
/reload-plugins
```

本地开发时指向你的 clone 目录：

```
/plugin marketplace add /path/to/cc-remote-approval
/plugin install cc-remote-approval@manta
/reload-plugins
```

## 配置

在 Claude Code 中运行 `/cc-remote-approval:setup` 进行交互式配置，或手动创建 `~/.cc-remote-approval/config.json`：

```json
{
  "channel_type": "telegram",
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "你的 chat ID",
  "escalation_seconds": 20,
  "elicitation_timeout": 60
}
```

### 获取 Telegram Bot Token

1. Telegram 搜索 @BotFather → `/newbot` → 复制 token
2. 给你的 bot 发一条消息
3. 获取 chat_id：
```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | \
  python3 -c "import json,sys; u=json.load(sys.stdin)['result']; \
  print(u[-1]['message']['chat']['id']) if u else print('先给 bot 发条消息')"
```

---

## 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `channel_type` | `"telegram"` | 消息渠道（目前支持 `telegram`，更多即将到来） |
| `bot_token` | 必填 | Telegram bot token |
| `chat_id` | 必填 | 你的 Telegram chat ID |
| `escalation_seconds` | 20 | 本地无操作多久后发到消息渠道 |
| `elicitation_timeout` | 60 | MCP 表单超时后回退到本地 form |
| `context_turns` | 3 | 消息里显示几轮对话上下文 |
| `context_max_chars` | 200 | 每轮上下文最大字符数 |

所有时间参数均可配置。也可以通过环境变量覆盖任意配置项，前缀为 `CC_REMOTE_APPROVAL_`（例如 `CC_REMOTE_APPROVAL_ESCALATION_SECONDS=60`）。

---

## 设计原则

1. **本地优先** — 所有逻辑在你电脑上跑，只有渠道 API 是外部调用
2. **不替换 UI** — Claude Code 原生对话框照常显示，hook 并行运行
3. **零依赖** — 只用 Python 标准库，无需 pip install
4. **渠道无关** — Hook 逻辑和渠道实现分离，加 Slack 只需加一个文件
5. **并发安全** — 多 agent 同时需要审批时，共享轮询队列避免消息丢失

项目结构、已知限制和开发者文档见 [CLAUDE.md](CLAUDE.md)。

---

## License

MIT
