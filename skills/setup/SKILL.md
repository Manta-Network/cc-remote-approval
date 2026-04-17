---
name: setup
description: Configure cc-remote-approval — set up messaging channel for remote approval
argument-hint: 
user-invocable: true
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

# CC Remote Approval Setup

Help the user configure their messaging channel for remote approval notifications.

## Prerequisite

The plugin must already be installed. If the user hasn't installed it, walk them through:

```
/plugin marketplace add Manta-Network/cc-remote-approval
/plugin install cc-remote-approval@manta
/reload-plugins
```

Then come back here to finish configuration.

## Steps

1. Ask which channel they want to use. Currently only Telegram is supported.

2. For Telegram, ask if they already have a bot token. If not, guide them:
   - Open Telegram, search for @BotFather
   - Send `/newbot`, follow the prompts
   - Copy the bot token (format: `123456:ABC-DEF...`)

3. Ask for their Telegram chat ID. If they don't know it:
   - Send any message to their new bot
   - Run: `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "import json,sys; u=json.load(sys.stdin)['result']; print(u[-1]['message']['chat']['id']) if u else print('No messages yet — send a message to your bot first')"`

4. Ask for escalation delay (default: 20 seconds) — how long to wait locally before sending to the channel

5. Write the config file:

```bash
mkdir -p ~/.cc-remote-approval
cat > ~/.cc-remote-approval/config.json << 'CONF'
{
  "channel_type": "telegram",
  "bot_token": "<TOKEN>",
  "chat_id": "<CHAT_ID>",
  "escalation_seconds": 20,
  "elicitation_timeout": 60,
  "stop_hook_enabled": true,
  "stop_wait_seconds": 180,
  "context_turns": 3,
  "context_max_chars": 200,
  "session_hint_enabled": true
}
CONF
chmod 600 ~/.cc-remote-approval/config.json
```

6. Test the connection (this will send a real message to TG — tell the user to check Telegram):

```bash
curl -s -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "<CHAT_ID>", "text": "✅ cc-remote-approval connected!"}' | python3 -c "import json,sys; r=json.load(sys.stdin); print('Success!' if r.get('ok') else f'Error: {r}')"
```

7. Confirm setup is complete and explain:
   - Permission requests will appear on the channel after the escalation delay
   - User can Allow, Always Allow, or Deny remotely
   - If they respond locally first, the remote message auto-resolves
   - MCP form requests also go to the channel with a 60s timeout before showing locally
   - All timing values are configurable, and can be overridden via environment variables with the `CC_REMOTE_APPROVAL_` prefix
   - Run `/cc-remote-approval:status` any time to health-check the channel (bot token valid, chat reachable, recent hook activity).

## Config Fields

| Field | Default | Description |
|---|---|---|
| `channel_type` | `"telegram"` | Messaging channel to use |
| `bot_token` | required | Telegram bot token from @BotFather |
| `chat_id` | required | Your Telegram chat ID |
| `escalation_seconds` | 20 | Seconds before escalating to channel |
| `elicitation_timeout` | 60 | Seconds before showing local form for MCP elicitations |
| `context_turns` | 3 | Number of conversation turns to show in context |
| `context_max_chars` | 200 | Max chars per context turn |
| `stop_hook_enabled` | `true` | Enable Stop hook for remote task continuation. Set `false` to disable. |
| `stop_wait_seconds` | 180 | Seconds to wait for remote instruction before allowing idle (local input in Claude Code releases immediately) |
| `session_hint_enabled` | `true` | Inject SessionStart hint steering Claude to prefer AskUserQuestion tool for option-picking. Set `false` to disable. |
