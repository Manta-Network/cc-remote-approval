# Stop Hook — Remote Prompt Injection via Telegram

## Problem

When Claude finishes a task and goes idle, the user must return to the terminal to type the next instruction. With `cc-remote-approval`, the user receives an idle notification on Telegram but cannot reply with a new task — they can only observe.

## Solution

Add a **Stop hook** that intercepts Claude before it goes idle, sends an interactive message to Telegram with "Continue" and "Dismiss" buttons, and polls for a reply. If the user sends a new instruction via Telegram, the hook blocks the stop and passes the instruction via the `reason` field (Stop hook schema doesn't support `hookSpecificOutput.additionalContext`), causing Claude to continue working.

## Flow

```
Claude finishes task → Stop hook fires
  → Send TG message:
      💤 Agent idle · {session_tag}
      {context}
      [✏️ Continue]  [❌ Dismiss]

  → Poll TG for response (up to stop_wait_seconds, default 180s from config):

    A) User clicks "✏️ Continue"
       → Send ForceReply prompt: "Reply with your next instruction"
       → Wait for text reply
       → User sends "fix the login bug"
       → Edit message: "✅ New task sent: fix the login bug"
       → Return {decision: "block", reason: "..."}
       → Claude continues with new instruction

    B) User clicks "❌ Dismiss"
       → Edit message: "💤 Dismissed"
       → Return empty (allow idle)
       → Write signal file to dedup with Notification hook

    C) Timeout (stop_wait_seconds, default 180s from config)
       → Edit message: "💤 Timed out"
       → Return empty (allow idle)
       → Write signal file to dedup with Notification hook
```

## Architecture

### New file: `hooks/stop.py`

Follows the same patterns as `permission_request.py`:
- Reads config via `load_config()`
- Creates channel via `create_channel(cfg)`
- Sends message with inline buttons
- Polls for callback/text using `ch.poll()`
- Returns JSON to stdout for Claude Code

### Signal file for Notification dedup

When Stop hook handles the idle event (dismiss or timeout), it writes:
```
$TMPDIR/cc-remote-approval/stop/handled_{session_id}
```
Contains a timestamp. `notification.py` checks this file — if it exists and was created within the last 30 seconds, skip the idle_prompt notification.

The signal file is **session-scoped** (keyed on `session_id`) so concurrent Claude Code sessions don't dedup each other's notifications — each session has its own dedup window.

### Config

New field in `config.json`:
```json
{
  "stop_hook_enabled": false
}
```

### hooks.json addition

```json
"Stop": [{
  "matcher": "",
  "hooks": [{
    "type": "command",
    "command": "PYTHONPATH=${CLAUDE_PLUGIN_ROOT} python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py",
    "timeout": 259200
  }]
}]
```

## Button Design

| Button | callback_data | Action |
|---|---|---|
| ✏️ Continue | `stop:continue` | Send ForceReply, wait for text, block stop |
| ❌ Dismiss | `stop:dismiss` | Allow idle, write signal file |

## `reason` Format

The Stop hook schema only supports `decision` + `reason` at the top level (not `hookSpecificOutput.additionalContext`, which is for PreToolUse/UserPromptSubmit/PostToolUse). The `reason` string is shown to Claude as the continuation directive:

```
The user sent a new instruction via the remote messaging channel (Telegram). Please execute this instruction: {user_message}
```

## Message States

Resolved messages keep the `💤 Agent idle` title and append the status + `{session_tag}` so users can tell concurrent sessions apart at a glance. Format: `💤 Agent idle · {status} · {session_tag}`.

1. **Initial**: `💤 Agent idle · {session_tag}` + context + 2 buttons
2. **Continue clicked**: `💤 Agent idle · ⏳ Waiting for instruction... · {session_tag}` (buttons removed) + ForceReply prompt
3. **New task sent**: `💤 Agent idle · ✅ New task sent: {instruction} · {session_tag}` (prompt deleted)
4. **Dismissed**: `💤 Agent idle · ❌ Dismissed · {session_tag}`
5. **Timeout**: `💤 Agent idle · ⏰ Timed out · {session_tag}`
6. **Handled locally**: `💤 Agent idle · 🖥️ Handled locally · {session_tag}` (user typed in the terminal before the remote side resolved)

### Terminal race detection

During polling the hook performs a best-effort check for transcript growth (signal that the user typed something locally). If detected, the message transitions to **Handled locally** and the hook exits without blocking. Note: this check may not fire reliably during active hook execution (Claude Code doesn't flush transcript writes on a fixed schedule), so it is defensive insurance rather than a guaranteed path. The primary local-handled signal is the same `check_local_response` used by other hooks; transcript polling is kept here to leave room for future Claude Code improvements.

## Edge Cases

- **Multiple sessions**: Each Stop hook instance has its own message. Polling uses quote-reply routing (same as existing hooks), so concurrent sessions don't cross.
- **User replies after timeout**: Message already edited to "Timed out", buttons gone. Reply is dropped (same behavior as existing permission_request timeout).
- **Channel unavailable**: Exit immediately, allow idle normally.
- **Stop hook fires repeatedly**: Each invocation is independent — sends a new TG message, polls, returns. Previous messages are already resolved.

## Testing

- Button callback routing (continue/dismiss)
- ForceReply → text reply → block decision
- Timeout → allow idle
- Signal file write/read for Notification dedup
- Signal file TTL (stale files ignored)
- Channel unavailable → exit silently
- Block decision uses `reason` field (not `hookSpecificOutput` — schema rejects it for Stop)
