#!/usr/bin/env python3
"""
SessionStart hook — inject a system-context hint that nudges Claude to
prefer the AskUserQuestion tool over free-text numbered lists when
presenting choices.

Why this exists:
  Our PermissionRequest hook already forwards AskUserQuestion to the
  channel as a clean button UI. Free-text "1. … 2. … 3. … which?"
  questions at turn-end rely on the Stop hook's heuristic classifier,
  which is approximate and language-sensitive. Steering Claude toward
  the structured tool eliminates that whole class of parsing entirely
  — we get the best channel UX for free.

  This is the cleanest way a plugin can influence model behavior
  without touching user-owned files like CLAUDE.md.

Gating:
  Only injects when a channel is actually configured. Users who
  installed the plugin but haven't set up a bot_token/chat_id don't
  need the hint (their questions aren't going anywhere anyway).
"""
import json
import sys

from utils.common import load_config, make_logger
from utils.channel import create_channel

_log = make_logger("session_start")


ASKUSERQUESTION_HINT = """\
<cc-remote-approval>
This session has cc-remote-approval enabled — user interactions are \
forwarded to a messaging channel (e.g. Telegram) so the user can \
respond remotely without returning to the terminal.

**Strongly prefer the AskUserQuestion tool over free-text numbered \
lists whenever you need the user to pick from a discrete set of \
options.** The tool surfaces on the remote channel as emoji-keycap \
buttons that route the user's choice back reliably. A free-text \
"Here are 3 options — which do you prefer?" at the end of a turn \
gives the user nothing actionable on the remote channel — they'd \
have to return to the terminal to type a number, which defeats the \
entire point of the plugin.

Use AskUserQuestion whenever:
  - There are 2-10 well-defined choices
  - You want a single answer or explicit multi-select
  - The decision blocks your next step
  - You're offering "Option A vs Option B" style tradeoffs

Do NOT fall back to a free-text numbered list just because the \
options are "similar to" something; almost any option-picking \
question is better as an AskUserQuestion call.

Free-text questions are still appropriate for:
  - Open-ended input (code snippets, paths, descriptions, free text)
  - Yes/no clarifications where nuance matters in the follow-up
  - Status updates and confirmations (you're not really asking)
</cc-remote-approval>
"""


def main():
    try:
        sys.stdin.read()  # drain stdin; we don't need the event contents
    except Exception:
        sys.exit(0)

    cfg = load_config()
    if not cfg["session_hint_enabled"]:
        _log("Hint injection disabled (session_hint_enabled=false)")
        sys.exit(0)

    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable, skipping hint injection: {ch_err}")
        sys.exit(0)

    _log("Injecting AskUserQuestion preference hint")
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ASKUSERQUESTION_HINT,
        }
    }, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
