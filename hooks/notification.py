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
                          format_context_lines, format_context_block)
from utils.channel import create_channel

_log = make_logger("notification")

MESSAGES = {
    "idle_prompt": "💤 <b>Agent idle</b>\n\nAgent finished the current task and is waiting for your next instruction.",
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

    transcript_path = event.get("transcript_path", "")
    context_lines = format_context_lines(
        transcript_path,
        max_turns=cfg["context_turns"],
        max_chars=cfg["context_max_chars"],
    )

    text = MESSAGES[notification_type]
    # Session tag — when multiple CC sessions share one TG chat this tells
    # the user which project the notification is from.
    session_tag = _session_tag(event)
    if session_tag:
        text = text.replace("</b>", f"</b> · <code>{html_escape(session_tag)}</code>", 1)
    text += format_context_block(context_lines)

    ch.send_notification(text)


def _session_tag(event):
    """Short label identifying the originating session for the TG reader.
    Picks cwd basename (e.g. 'cc-remote-approval') — short, human, and
    unique enough when the user runs one session per repo."""
    cwd = event.get("cwd") or ""
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or None
    return None


if __name__ == "__main__":
    main()
