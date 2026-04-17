# Stop Hook — Remote Prompt Injection via Telegram

## Problem

When Claude finishes a task and goes idle, the user must return to the terminal to type the next instruction. With `cc-remote-approval`, the user receives an idle notification on Telegram but cannot reply with a new task — they can only observe.

## Solution

Add a **Stop hook** that intercepts Claude before it goes idle, sends an interactive message to Telegram with "Continue" and "Dismiss" buttons, and polls for a reply. If the user sends a new instruction via Telegram, the hook blocks the stop and injects the instruction as `additionalContext`, causing Claude to continue working.

## Flow

```
Claude finishes task → Stop hook fires
  → Send TG message:
      💤 Agent idle · {session_tag}
      {context}
      [✏️ Continue]  [❌ Dismiss]

  → Poll TG for response (up to POLL_TIMEOUT_SECONDS):

    A) User clicks "✏️ Continue"
       → Send ForceReply prompt: "Reply with your next instruction"
       → Wait for text reply
       → User sends "fix the login bug"
       → Edit message: "✅ New task sent: fix the login bug"
       → Return {decision: "block", additionalContext: "..."}
       → Claude continues with new instruction

    B) User clicks "❌ Dismiss"
       → Edit message: "💤 Dismissed"
       → Return empty (allow idle)
       → Write signal file to dedup with Notification hook

    C) Timeout (POLL_TIMEOUT_SECONDS)
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
$TMPDIR/cc-remote-approval/stop_handled
```
Contains a timestamp. `notification.py` checks this file — if it exists and was created within the last 30 seconds, skip the idle_prompt notification.

### Config

New field in `config.json`:
```json
{
  "stop_hook_enabled": true
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

## additionalContext Format

```xml
<cc-remote-approval>
The user sent a new instruction via the remote messaging channel (Telegram).
Please execute this instruction:

{user_message}
</cc-remote-approval>
```

## Message States

1. **Initial**: idle message + 2 buttons + context
2. **Continue clicked**: "⏳ Waiting for instruction..." (buttons removed) + ForceReply prompt
3. **Instruction received**: "✅ New task sent: {instruction}" (prompt deleted)
4. **Dismissed**: "💤 Dismissed"
5. **Timeout**: "💤 Timed out"

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
- additionalContext format validation
