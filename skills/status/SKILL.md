---
name: status
description: Check cc-remote-approval channel health — bot token validity, chat_id reachability, getUpdates offset, any corrupt state files, and recent hook activity.
argument-hint:
user-invocable: true
allowed-tools: [Bash, Read]
---

# cc-remote-approval Status Check

Run a health check on the remote-approval channel and report what's working vs what's broken. Use this when the user:

- Just installed the plugin and wants to confirm setup
- Notices TG messages aren't arriving
- Rotated a bot token / changed chat_id
- Is debugging why a hook "didn't fire"

## Steps

Run each bash command, interpret the output, and summarize at the end.

### 1. Config file present and readable

```bash
test -r ~/.cc-remote-approval/config.json && echo "OK config file exists" || echo "MISSING config file — run /cc-remote-approval:setup"
```

If missing, stop and direct the user to `/cc-remote-approval:setup`.

### 2. Config fields complete

```bash
python3 -c "
import json, os
p = os.path.expanduser('~/.cc-remote-approval/config.json')
c = json.load(open(p))
missing = [k for k in ['channel_type', 'bot_token', 'chat_id'] if not c.get(k)]
print('OK all required fields set' if not missing else f'MISSING: {missing}')
print(f'channel_type={c.get(\"channel_type\")!r}')
print(f'escalation_seconds={c.get(\"escalation_seconds\", 20)}')
print(f'session_hint_enabled={c.get(\"session_hint_enabled\", True)}')
print(f'bot_token={c.get(\"bot_token\", \"\")[:10]}... (masked)')
print(f'chat_id={c.get(\"chat_id\")}')
"
```

### 3. Bot token is live — `getMe`

```bash
python3 -c "
import json, os, urllib.request
c = json.load(open(os.path.expanduser('~/.cc-remote-approval/config.json')))
token = c.get('bot_token', '')
if not token:
    print('SKIP — no token'); raise SystemExit
try:
    r = urllib.request.urlopen(f'https://api.telegram.org/bot{token}/getMe', timeout=10)
    d = json.loads(r.read())
    if d.get('ok'):
        me = d['result']
        print(f'OK bot @{me.get(\"username\")} (id={me.get(\"id\")})')
    else:
        print(f'FAIL bot token rejected: {d}')
except Exception as e:
    print(f'FAIL getMe error: {e}')
"
```

If `FAIL bot token rejected` → token was revoked or typo'd. Tell user to check @BotFather.
If network error → no internet / firewall.

### 4. Chat is reachable — send a real health-check message

**Note: this sends a real "🩺 health check" message to the user's TG chat.** Tell them before running. If they want to skip the live test (e.g. running status frequently), steps 1-3 are enough to confirm config + token validity.

```bash
python3 -c "
import json, os, urllib.request
c = json.load(open(os.path.expanduser('~/.cc-remote-approval/config.json')))
token, chat = c.get('bot_token'), c.get('chat_id')
if not token or not chat:
    print('SKIP — token or chat_id missing'); raise SystemExit
try:
    body = json.dumps({'chat_id': chat, 'text': '🩺 cc-remote-approval health check'}).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=body, headers={'Content-Type': 'application/json'})
    r = urllib.request.urlopen(req, timeout=10)
    d = json.loads(r.read())
    print('OK sendMessage succeeded' if d.get('ok') else f'FAIL {d}')
except Exception as e:
    print(f'FAIL sendMessage error: {e}')
"
```

If `FAIL chat not found` → chat_id is wrong. Help user find the right one.

### 5. Runtime state directory

```bash
TG=$(python3 -c 'import tempfile,os; print(os.path.join(tempfile.gettempdir(), "cc-remote-approval/tg"))')
echo "state dir: $TG"
ls -la "$TG" 2>/dev/null | head -20
echo ""
echo "corrupt files (postmortem evidence from past failures):"
ls "$TG"/*.corrupt-* 2>/dev/null || echo "  none"
```

### 6. Recent hook activity

```bash
LOGS=$(python3 -c 'import tempfile,os; print(os.path.join(tempfile.gettempdir(), "cc-remote-approval/logs"))')
for h in permission_request notification elicitation stop session_start; do
  f="$LOGS/$h.log"
  if [ -f "$f" ]; then
    echo "=== $h (last 3 lines) ==="
    tail -3 "$f"
  fi
done
```

## Reporting format

Wrap up with a short human-friendly summary:

```
cc-remote-approval status:
  ✅ config file readable
  ✅ bot @xxxxx responds to getMe
  ✅ chat_id 123456 reachable
  ℹ️  stop_hook_enabled=false (default — enable for remote task continuation)
  ✅ session_hint_enabled=true (AskUserQuestion steering active)
  ⚠️  1 corrupt state file from 2026-04-14 — safe to delete
  ℹ️  last permission_request hook fired 2h ago

Good to go. / Issues: <list>
```

Use green check / red cross / warning / info emoji appropriate to each finding. If anything failed, suggest concrete next steps.
