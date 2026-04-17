# CC Remote Approval

**English** | [中文](README.zh.md)

Approve Claude Code permission requests remotely — keep your agent running when you're away from the computer.

`cc-remote-approval` is a Claude Code plugin. When Claude needs your approval (permissions, forms, questions), if you don't respond locally within a few seconds, the request is forwarded to your messaging channel so you can respond remotely.

---

## How It Works

```
Claude Code needs permission → Native dialog shows locally (as usual)
                             → Hook starts counting down
                                ↓ 20 seconds with no response (configurable)
                             → Notification with buttons on your channel
                                ↓
                             You tap Allow / Deny / Always
                                ↓
                             Claude Code continues
```

**Race mode**: Both the local dialog and your remote channel are active simultaneously. Whichever you respond to first wins, and the other side auto-syncs.

---

## Supported Scenarios

| Scenario | Hook | Description |
|---|---|---|
| Bash / Edit / Write approval | PermissionRequest | Allow / Always / Deny buttons |
| AskUserQuestion | PermissionRequest | Option buttons + text input |
| MCP forms (Elicitation) | Elicitation | Remote form (string/enum/boolean/integer/number fields), 60s timeout falls back to local |
| Agent idle | Notification | 💤 idle notification |

---

## Installation

In Claude Code, run:

```
/plugin marketplace add Manta-Network/cc-remote-approval
/plugin install cc-remote-approval@manta
/reload-plugins
```

Then **start a new session** (or `/clear`) so the SessionStart hook can inject the AskUserQuestion preference hint into the conversation context.

For local development, point the marketplace at your clone instead:

```
/plugin marketplace add /path/to/cc-remote-approval
/plugin install cc-remote-approval@manta
/reload-plugins
```

## Setup

In Claude Code, run `/cc-remote-approval:setup` to configure interactively, or manually create `~/.cc-remote-approval/config.json`. After setup, run `/cc-remote-approval:status` any time to verify the channel is working (bot token valid, chat reachable, recent hook activity).

Manual config:

```json
{
  "channel_type": "telegram",
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "your-chat-id",
  "escalation_seconds": 20,
  "elicitation_timeout": 60,
  "stop_hook_enabled": true,
  "stop_wait_seconds": 180,
  "session_hint_enabled": true
}
```

### Getting Your Telegram Bot Token

1. Open Telegram, search for @BotFather → `/newbot` → copy the token
2. Send any message to your new bot
3. Get your chat_id:
```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | \
  python3 -c "import json,sys; u=json.load(sys.stdin)['result']; \
  print(u[-1]['message']['chat']['id']) if u else print('Send a message to your bot first')"
```

---

## Configuration

| Field | Default | Description |
|---|---|---|
| `channel_type` | `"telegram"` | Messaging channel (`telegram`, more coming) |
| `bot_token` | required | Telegram bot token from @BotFather |
| `chat_id` | required | Your Telegram chat ID |
| `escalation_seconds` | 20 | Seconds before escalating to channel |
| `elicitation_timeout` | 60 | Seconds before falling back to local form for MCP elicitations |
| `context_turns` | 3 | Conversation turns shown in message context |
| `context_max_chars` | 200 | Max chars per context turn |
| `stop_hook_enabled` | `true` | Enable Stop hook for remote task continuation. Set `false` to disable. |
| `stop_wait_seconds` | 180 | Seconds to wait for remote instruction before allowing idle (local input in Claude Code releases immediately) |
| `session_hint_enabled` | `true` | Inject SessionStart hint that steers Claude to prefer `AskUserQuestion` tool for option-picking (renders as buttons on channel). Set `false` to let Claude use its natural behavior. |

All values are configurable. You can also override any config field via environment variable with the `CC_REMOTE_APPROVAL_` prefix (e.g., `CC_REMOTE_APPROVAL_SESSION_HINT_ENABLED=false`).

---

## Design Principles

1. **Local-first** — Everything runs on your machine; only channel API calls go external
2. **Non-invasive** — Claude Code's native dialogs still show; hooks run in parallel
3. **Zero dependencies** — Python stdlib only, no pip install needed
4. **Channel-agnostic** — Hook logic separated from channel implementation; adding Slack = add one file
5. **Concurrency-safe** — Shared polling queue prevents message loss with multiple agents

For project structure, known limitations, and developer guide, see [CLAUDE.md](CLAUDE.md).

---

## License

MIT
