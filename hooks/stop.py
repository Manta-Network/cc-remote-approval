#!/usr/bin/env python3
"""
Stop hook — intercept Claude before idle, offer remote prompt injection.

When Claude finishes a task and is about to stop:
1. Send Telegram message with "Continue" / "Dismiss" buttons
2. Poll for user response (up to stop_wait_seconds)
3. If user sends a new instruction → block stop, inject as additionalContext
4. If user dismisses or timeout → allow stop (Claude goes idle)

This replaces the Notification(idle_prompt) for the remote user — a signal
file prevents the Notification hook from sending a duplicate idle message.
"""
import json
import os
import sys
import time

from utils.common import (load_config, html_escape, make_logger, mask_secrets,
                          format_context_lines, format_context_block,
                          STOP_SIGNAL_DIR)
from utils.channel import create_channel

_log = make_logger("stop")

# Signal file lifespan — Notification hook ignores idle_prompt if signal
# was written within this many seconds.
SIGNAL_TTL_SECONDS = 30


def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    _log(f"event: {json.dumps(event, ensure_ascii=False)[:500]}")

    cfg = load_config()
    if not cfg.get("stop_hook_enabled", True):
        _log("Stop hook disabled (stop_hook_enabled=false)")
        sys.exit(0)

    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        sys.exit(0)

    wait_seconds = cfg["stop_wait_seconds"]
    if not wait_seconds or wait_seconds <= 0:
        _log("stop_wait_seconds <= 0, skipping")
        sys.exit(0)

    # Build idle message
    transcript_path = event.get("transcript_path", "")
    context_lines = format_context_lines(
        transcript_path,
        max_turns=1,
        max_chars=cfg["context_max_chars"],
    )

    text = f"💤 <b>Agent idle</b>"
    session_tag = _session_tag(event)
    if session_tag:
        text += f" · <code>{html_escape(session_tag)}</code>"
    text += f"\n\n⏳ Waiting {wait_seconds}s for new instruction..."
    text += format_context_block(context_lines)

    buttons = [
        [
            {"text": "✏️ Continue", "callback_data": "stop:continue"},
            {"text": "❌ Dismiss", "callback_data": "stop:dismiss"},
        ]
    ]

    try:
        msg_id = ch.send_message(text, buttons=buttons)
        _log(f"Sent stop message msg_id={msg_id}")
    except Exception as e:
        _log(f"Send failed: {e}")
        sys.exit(0)

    # Track prompt message IDs for cleanup
    prompt_ids = []

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        update = ch.poll(msg_id if not prompt_ids else [msg_id] + prompt_ids)
        if update is None:
            time.sleep(1)
            continue

        if update["type"] == "callback":
            data = update["data"]

            if data == "stop:continue":
                _log("User clicked Continue")
                ch.edit_message(msg_id, text=_status_text("⏳ Waiting for instruction...", session_tag), buttons=[])
                prompt_msg_id = ch.send_reply_prompt(
                    "💬 Reply with your next instruction:",
                    reply_to=msg_id,
                )
                if prompt_msg_id:
                    prompt_ids.append(prompt_msg_id)
                continue

            elif data == "stop:dismiss":
                _log("User clicked Dismiss")
                ch.edit_message(msg_id, text=_status_text("💤 Dismissed", session_tag), buttons=[])
                _cleanup_prompts(ch, prompt_ids)
                _write_signal()
                sys.exit(0)

        elif update["type"] == "text":
            instruction = update["text"].strip()
            if not instruction:
                continue
            _log(f"Received instruction: {instruction[:200]}")
            ch.edit_message(
                msg_id,
                text=_status_text(f"✅ New task sent: <code>{html_escape(mask_secrets(instruction[:200]))}</code>", session_tag),
                buttons=[],
            )
            _cleanup_prompts(ch, prompt_ids)
            # Block stop, inject instruction
            json.dump({
                "decision": "block",
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": (
                        "<cc-remote-approval>\n"
                        "The user sent a new instruction via the remote messaging channel (Telegram).\n"
                        "Please execute this instruction:\n\n"
                        f"{instruction}\n"
                        "</cc-remote-approval>"
                    ),
                }
            }, sys.stdout)
            sys.stdout.flush()
            sys.exit(0)

    # Timeout
    _log(f"Timeout after {wait_seconds}s")
    ch.edit_message(msg_id, text=_status_text("💤 Timed out", session_tag), buttons=[])
    _cleanup_prompts(ch, prompt_ids)
    _write_signal()
    sys.exit(0)


def _session_tag(event):
    """Short label identifying the session (project name)."""
    cwd = event.get("cwd") or ""
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or None
    return None


def _status_text(status, session_tag=None):
    """Build a short resolved-state message."""
    text = f"<b>{status}</b>"
    if session_tag:
        text += f" · <code>{html_escape(session_tag)}</code>"
    return text


def _cleanup_prompts(ch, prompt_ids):
    """Delete ForceReply prompt messages."""
    for pid in prompt_ids:
        ch.delete_message(pid)


def _write_signal():
    """Write signal file so Notification hook skips duplicate idle message."""
    os.makedirs(STOP_SIGNAL_DIR, exist_ok=True)
    signal_path = os.path.join(STOP_SIGNAL_DIR, "handled")
    with open(signal_path, "w") as f:
        f.write(str(time.time()))
    _log(f"Wrote signal file: {signal_path}")


def check_stop_signal():
    """Check if Stop hook recently handled the idle event.
    Called by notification.py to avoid duplicate idle messages.
    Returns True if signal is fresh (within TTL)."""
    signal_path = os.path.join(STOP_SIGNAL_DIR, "handled")
    try:
        with open(signal_path) as f:
            ts = float(f.read().strip())
        if time.time() - ts < SIGNAL_TTL_SECONDS:
            return True
    except (FileNotFoundError, ValueError):
        pass
    return False


if __name__ == "__main__":
    main()
