#!/usr/bin/env python3
"""
Notification hook — send Telegram when agent is idle.

Only forwards:
  - idle_prompt: agent finished, waiting for next instruction

Intentionally does NOT forward:
  - permission_prompt: redundant with PermissionRequest hook, which
    fires in parallel with an actionable Allow/Deny/Always button UI
    and carries tool_input detail. A plain-text "Claude needs
    permission" notification would just duplicate the actionable one
    with less information and confuse which message to respond to.

This hook is fire-and-forget (one-way). Cannot return decisions.
"""
import json
import os
import sys

from utils.common import (load_config, html_escape, make_logger,
                          format_context_lines, format_context_block,
                          session_tag as common_session_tag)
from hooks.stop import check_stop_signal
from utils.channel import create_channel

_log = make_logger("notification")

MESSAGES = {
    "idle_prompt": (
        "💤 <b>Agent idle</b>\n\n"
        "Agent finished the current task and is waiting for your next instruction in Claude Code."
    ),
}


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
    except Exception:
        sys.exit(0)

    _log(f"event: {json.dumps(event, ensure_ascii=False)[:500]}")

    cfg = load_config()
    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        sys.exit(0)

    notification_type = event.get("notification_type", "")

    if notification_type not in MESSAGES:
        _log(f"Ignoring notification_type={notification_type!r} (no message template)")
        sys.exit(0)

    # Dedup: Stop hook already sent an interactive idle message — skip the
    # plain notification so the user doesn't see two "idle" messages.
    # Scoped by session_id so concurrent sessions don't interfere.
    if notification_type == "idle_prompt":
        session_id = event.get("session_id", "")
        if check_stop_signal(session_id):
            _log("Stop hook recently handled idle, skipping duplicate notification")
            sys.exit(0)

    transcript_path = event.get("transcript_path", "")
    context_lines = format_context_lines(
        transcript_path,
        max_turns=cfg["context_turns"],
        max_chars=cfg["context_max_chars"],
    )

    text = MESSAGES[notification_type]
    tag = common_session_tag(event)
    if tag:
        text = text.replace("</b>", f"</b> · <code>{html_escape(tag)}</code>", 1)
    text += format_context_block(context_lines)

    ch.send_notification(text)


if __name__ == "__main__":
    main()
