# CC Remote Approval

> User docs: [README.md](README.md)

## Overview

`cc-remote-approval` is a Claude Code plugin that forwards permission requests, questions, and forms
to a messaging channel (currently Telegram) via hooks. When the user is away from their computer,
they can respond remotely.

**Core principles**:
- Hooks run **in parallel** with native dialogs (non-blocking, non-replacing)
- First responder wins — the other side auto-syncs
- Channel-agnostic architecture — adding Slack/Discord only requires a new channel implementation
- Zero external dependencies (Python stdlib only)

## Project Structure

```
cc-remote-approval/
├── .claude-plugin/
│   └── plugin.json              # Plugin manifest (name, version, hooks path)
├── hooks/                       # Hook entry scripts (channel-agnostic)
│   ├── hooks.json               # Hook event registration (uses ${CLAUDE_PLUGIN_ROOT})
│   ├── permission_request.py    # PermissionRequest — core approval hook
│   ├── elicitation.py           # Elicitation — MCP form hook (hybrid mode)
│   ├── elicitation_result.py    # ElicitationResult — local form completion signal
│   ├── notification.py          # Notification — idle alert
│   ├── stop.py                  # Stop — remote prompt injection when idle
│   └── session_start.py         # SessionStart — AskUserQuestion preference hint injection
├── utils/                       # Shared utilities (channel-agnostic)
│   ├── common.py                # Config, masking, logging, IPC helpers
│   └── channel.py               # Channel base class + create_channel() factory
├── channels/                    # Channel implementations
│   └── telegram/
│       ├── client.py            # TelegramChannel + tg_request API
│       └── poll.py              # getUpdates coordination (flock + pending queue)
├── skills/
│   ├── setup/
│   │   └── SKILL.md         # /cc-remote-approval:setup interactive configuration
│   └── status/
│       └── SKILL.md         # /cc-remote-approval:status health check
├── test/                        # 208 automated tests
│   ├── scenarios.py             # FakeChannel + shared test scenarios (channel-agnostic)
│   ├── test_common.py           # utils/common.py tests
│   ├── test_hooks.py            # Hook component tests (via FakeChannel)
│   └── telegram/
│       ├── conftest.py          # FakeTelegram(FakeChannel) — one definition
│       ├── test_poll.py         # Telegram polling tests
│       └── test_integration.py  # Telegram E2E (inherits shared scenarios)
├── README.md
└── CLAUDE.md
```

## Architecture

```
hooks/                          utils/                      channels/
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ permission_req   │    │ common.py        │    │ telegram/            │
│ elicitation      │───▶│  load_config()   │    │   client.py          │
│ notification     │    │  mask_secrets()  │    │     TelegramChannel  │
│                  │    │  make_logger()   │    │     tg_request()     │
│                  │    ├──────────────────┤    │   poll.py            │
│                  │───▶│ channel.py       │───▶│     poll_once()      │
│                  │    │  Channel (base)  │    ├──────────────────────┤
│                  │    │  create_channel()│    │ slack/ (future)      │
│                  │    └──────────────────┘    │   client.py          │
└──────────────────┘                           │     SlackChannel     │
                                               └──────────────────────┘

Hooks import from utils/ only. Never from channels/ directly.
create_channel(cfg) reads cfg["channel_type"] and returns the right implementation.
```

## Key Files

| File | Description |
|---|---|
| `hooks/permission_request.py` | Sleep N seconds → detect local response → if none, send via channel → poll for callback → return decision |
| `hooks/elicitation.py` | Fork: child sends form immediately (with timeout countdown hint), parent blocks 60s. Channel responds → return data; timeout → show local form + activate terminal. Boolean defaults applied at submit, not pre-filled. Resolved messages include form title + submitted values. |
| `hooks/elicitation_result.py` | User fills form locally → write signal file → child updates channel |
| `hooks/stop.py` | Intercept before idle → send TG with Continue/Dismiss buttons → poll for new instruction → block stop with `reason` (injects as continuation directive) or allow idle. Signal file dedup with Notification hook. |
| `hooks/notification.py` | Fire-and-forget: send notification when agent is idle. Skips if Stop hook recently handled. |
| `hooks/session_start.py` | Fires on new session — injects a system-context hint steering Claude to prefer the `AskUserQuestion` tool over free-text option lists (structured tool = reliable button UI on the channel, no heuristic parsing needed). Only fires when a channel is configured. |
| `utils/common.py` | Config loading, secret masking, HTML escaping, logging, local response detection, session_tag helper, transcript context extraction (strips Claude Code's `<system-reminder>` / `<command-name>` / `<local-command-caveat>` wrappers so only real user intent appears in channel previews) |
| `utils/channel.py` | Channel interface + factory. Hooks call `ch.send_message()`, `ch.poll()`, `ch.edit_message()` |
| `channels/telegram/client.py` | TelegramChannel: Bot API via urllib, token in-process |
| `channels/telegram/poll.py` | Coordinated getUpdates: flock + pending.json queue (5-min TTL) for concurrent hooks |

## Configuration

All hooks read from `~/.cc-remote-approval/config.json`:

```json
{
  "channel_type": "telegram",
  "bot_token": "Telegram bot token",
  "chat_id": "Your chat ID",
  "escalation_seconds": 20,
  "elicitation_timeout": 60,
  "stop_hook_enabled": false,
  "stop_wait_seconds": 180,
  "context_turns": 3,
  "context_max_chars": 200,
  "session_hint_enabled": true
}
```

`channel_type` determines which channel implementation to use. Currently only `"telegram"` is supported.

## Testing

```bash
pytest test/ -v    # 208 tests, ~0.1s
```

### Test Architecture

```
test/scenarios.py              # FakeChannel mock + shared scenario base classes
                               # (ApprovalScenarios, AskUserQuestionScenarios, LocalResponseScenarios)
                               #
test/test_common.py            # utils/common.py — config, masking, logging
test/test_hooks.py             # Hook functions — via FakeChannel (channel-agnostic)
                               #
test/telegram/conftest.py      # FakeTelegram(FakeChannel) — single definition
test/telegram/test_poll.py     # Telegram polling — concurrent routing, pending queue
test/telegram/test_integration.py  # Inherits shared scenarios + Telegram-specific tests
```

**Adding a new channel's tests:**
```python
# test/slack/conftest.py
from scenarios import FakeChannel
class FakeSlack(FakeChannel):
    def queue_callback(self, msg_id, data): ...
    def queue_text(self, msg_id, text): ...

# test/slack/test_integration.py
from scenarios import ApprovalScenarios
class TestSlackApproval(ApprovalScenarios): pass  # inherits all scenarios
```

Zero test duplication — scenarios written once, channel fixtures written once.

## Scenario Coverage

### A. Hookable — Can Be Handled via Channel

| # | Scenario | Hook Type | Status | Notes |
|---|---|---|---|---|
| 1 | **Bash execution** | PermissionRequest | ✅ | Allow / Always / Deny buttons with context |
| 2 | **File write (Write)** | PermissionRequest | ✅ | Same handler |
| 3 | **File edit (Edit)** | PermissionRequest | ✅ | Shows file path |
| 4 | **WebFetch** | PermissionRequest | ✅ | Shows URL |
| 5-10 | **Notebook/PowerShell/Skill/Sandbox/ComputerUse/EnterPlan** | PermissionRequest | ✅ | Same hook |
| 11 | **Exit Plan mode** | PermissionRequest | ✅ | No Always button |
| 12 | **AskUserQuestion** | PermissionRequest | ✅ | Single/multi-select/text |
| 13 | **MCP form (Elicitation)** | Elicitation | ✅ | Hybrid: channel direct or timeout → local form. Shows timeout countdown hint; resolved messages include form title |
| 14 | **Prompt change** | PromptRequest | ❌ | Not implemented |
| 15 | **PreToolUse intercept** | PreToolUse | ❌ | Not implemented |
| 16 | **Idle waiting** | Notification | ✅ | idle_prompt (skipped when Stop hook handles) |
| 17 | **Permission dialog (`permission_prompt`)** | Notification | 🚫 | Intentionally suppressed — PermissionRequest hook already sends a richer actionable message for the same event |
| 18 | **System-prompt nudge toward `AskUserQuestion`** | SessionStart | ✅ | Injected automatically when channel is configured — steers the model to use the structured tool (with buttons) instead of free-text numbered lists when presenting choices |
| 19 | **Remote task continuation** | Stop | ✅ | Intercept before idle → Continue/Dismiss buttons → user sends new instruction via TG → block stop with `reason` as continuation directive |

### B. Non-Hookable (Claude Code doesn't expose hooks)

#20-30: MCP Server approval, API Key, Worktree, OAuth, Session resume, etc. — require Claude Code to add hook support.

### Summary

| Scope | Coverage |
|---|---|
| Hookable scenarios (#1-19) | **16/19 (84%)** — #17 intentionally suppressed as duplicate |
| Automated tests | **208 tests in ~0.1s** |
| All UI scenarios (#1-30) | **16/30 (53%)** |

## Coding Standards

- Python 3.11+, stdlib only (no third-party packages)
- Hook scripts communicate via stdin/stdout JSON with Claude Code
- Channel API calls via `urllib.request` (token stays in-process, not in `ps`)
- IPC via files (signal files, flock, JSON), not sockets
- Logs: `$TMPDIR/cc-remote-approval/logs/{name}.log`, PID-tagged, auto-rotate at 1MB
- Sensitive data masked before sending to any channel (tokens, passwords, API keys)
- Button callback_data uses short index IDs (`opt:0`, `f:1:e:2`), never real values — avoids platform byte limits and data corruption
- Channel method error semantics: `send_message` **propagates** transport errors (the hook logs `SEND FAILED: <real cause>`); fire-and-forget methods (`edit_message`, `delete_message`, `send_notification`, `edit_buttons`, `send_reply_prompt`) swallow internally so transient failures don't break resolve paths

## Runtime Files & Directories

All path constants defined in `utils/common.py`.

```
~/.cc-remote-approval/                      # Persistent (user-managed)
└── config.json                              # Channel type, credentials, settings
                                             # Lifecycle: permanent, user creates via /cc-remote-approval:setup

$TMPDIR/cc-remote-approval/                  # Temporary (OS-managed)
│
├── tg/                                      # Telegram polling state
│   ├── poll.lock                            # flock for concurrent hook coordination
│   │                                        # Lifecycle: permanent, recreated as needed
│   ├── offset                               # getUpdates offset (integer)
│   │                                        # Lifecycle: permanent, ~10 bytes
│   ├── offset.corrupt-{ts}                  # Archived on parse failure
│   │                                        # Lifecycle: kept for postmortem; safe to delete
│   ├── pending.json                         # Updates not yet claimed by any hook
│   │                                        # Lifecycle: auto-pruned (entries >5 min TTL dropped)
│   └── pending.json.corrupt-{ts}            # Archived on JSON error OR type mismatch
│                                            # (must be list[dict]; {} / [1,2] / null all count as corrupt)
│                                            # Lifecycle: kept for postmortem; safe to delete
│
├── elicit/                                  # Elicitation signal files (per-request)
│   ├── {request_id}.active                  # Registry: maps request_id → server_name
│   │                                        # Lifecycle: <1 min, deleted by child on exit
│   ├── {request_id}.response                # Child → parent: form data
│   │                                        # Lifecycle: <1 min, deleted by child on exit
│   ├── {request_id}.timeout                 # Parent → child: 60s expired, show local form
│   │                                        # Lifecycle: <1 min, deleted by child on exit
│   └── {request_id}.done                    # elicitation_result → child: user filled locally
│                                            # Lifecycle: <1 min, deleted by child on exit
│
├── stop/                                    # Stop hook signal files (session-scoped)
│   └── handled_{session_id}                 # Timestamp — Notification hook skips if fresh (<30s)
│                                            # Session-scoped so concurrent sessions don't dedup each other
│                                            # Lifecycle: overwritten each time Stop hook resolves for that session
│
└── logs/                                    # Debug logs (PID-tagged for multi-session)
    ├── permission_request.log               # Lifecycle: auto-rotate at 1MB (keep last 512KB)
    ├── elicitation.log                      # Lifecycle: auto-rotate at 1MB
    ├── stop.log                             # Lifecycle: auto-rotate at 1MB
    └── notification.log                     # Lifecycle: auto-rotate at 1MB
```

`{request_id}` = `{server_name}_{8-char-uuid}`, e.g. `my-server_a1b2c3d4`

On macOS, `$TMPDIR` = `/var/folders/.../T/` (user-private, cleaned on reboot).
On Linux, `$TMPDIR` = `/tmp` (cleaned on reboot on most distros).

## Known Issues & Design Boundaries

Things that aren't obvious from reading the code — invariants, external constraints, and accepted trade-offs.

1. **Hook timeouts** — interactive hooks (PermissionRequest, Elicitation, Stop) are set to 3 days in `hooks.json`; fire-and-forget hooks (Notification, ElicitationResult) are seconds. Poll loops internally use `POLL_TIMEOUT_SECONDS` (3 days) as the matching safety net.
2. **Elicitation hook is serial** — unlike PermissionRequest, the Elicitation hook must exit before the native form shows, so it can't truly run in parallel. This is why `elicitation.py` forks: the parent blocks briefly to capture channel responses, the child keeps polling in the background.
3. **Session-level auto-allow** — after clicking "Always", similar operations stop triggering hooks. Expected Claude Code behavior, not a bug we can override.
4. **Telegram getUpdates is a single global cursor** — concurrent hooks must go through the coordinated `poll_once()` (flock + `pending.json` queue), otherwise one hook's `getUpdates` consumes another hook's updates.
5. **Same-server concurrent elicitation** — `ElicitationResult` has no request-level ID, so filling one form locally signals "handled locally" to **all** active requests from that MCP server. Accepted trade-off since concurrent elicitations from the same server are rare.
6. **Text replies require quote-reply anchoring** — `poll.py` routes incoming text by `reply_to_message.message_id`. Bare text is dropped so concurrent hooks don't steal each other's replies. `send_reply_prompt` uses Telegram's `ForceReply` to make quoting automatic (works in notification quick-reply, Apple Watch, etc.).
7. **No end-of-turn question detection** — Claude Code doesn't expose a "this turn was a question" signal, and heuristic regex detection proved too fragile. We steer the model toward the `AskUserQuestion` tool via `SessionStart additionalContext` instead. The Stop hook's `{decision: block, reason}` mechanism is for **user-driven** remote continuation (user taps Continue) — different use case from auto-question-detection.

## Adding a New Channel

1. Create `channels/<name>/client.py` implementing `Channel` from `utils/channel.py`
2. Update `create_channel()` in `utils/channel.py` to handle new `channel_type`
3. Add channel-specific config fields to `DEFAULTS` in `utils/common.py`
4. Create `test/<name>/conftest.py` with `Fake<Name>(FakeChannel)`
5. Create `test/<name>/test_integration.py` inheriting shared scenarios
6. Hook code stays unchanged
