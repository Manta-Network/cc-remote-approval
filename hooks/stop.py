#!/usr/bin/env python3
"""
Stop hook — intercept Claude before idle, offer remote task continuation.

When Claude finishes a task and is about to stop:
1. Send Telegram message with "Continue" / "Dismiss" buttons
2. Poll for user response (up to stop_wait_seconds, default 180s)
3. If user sends a new instruction → block stop, inject via reason field
4. If user dismisses, presses ESC locally, or times out → allow stop

The terminal race (transcript growth detection) releases immediately when
the user types in Claude Code. If the user presses ESC inside Claude Code,
the hook is abandoned by Claude Code (no signal sent) and keeps polling
in the background until the stop_wait_seconds timeout — the TG message
then transitions to "Timed out".

A signal file per session prevents the Notification hook from sending a
duplicate idle message for the same stop.
"""
import json
import os
import sys
import time

from utils.common import (load_config, html_escape, make_logger, mask_secrets,
                          format_context_lines, format_context_block,
                          check_local_response, STOP_SIGNAL_DIR,
                          build_full_context_chunks,
                          session_tag as common_session_tag)
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
    if not cfg.get("stop_hook_enabled", False):
        _log("Stop hook disabled (stop_hook_enabled=false)")
        sys.exit(0)

    wait_seconds = cfg.get("stop_wait_seconds", 180)
    if wait_seconds <= 0:
        _log("stop_wait_seconds <= 0, skipping")
        sys.exit(0)

    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        sys.exit(0)

    session_id = event.get("session_id", "")

    # Build idle message
    transcript_path = event.get("transcript_path", "")
    context_lines = format_context_lines(
        transcript_path,
        max_turns=cfg["context_turns"],
        max_chars=cfg["context_max_chars"],
    )

    session_tag = common_session_tag(event)
    text = "💤 <b>Agent idle</b>"
    if session_tag:
        text += f" · <code>{html_escape(session_tag)}</code>"
    text += f"\n\n⏳ Tap Continue within {wait_seconds}s, or Claude Code will idle."
    text += format_context_block(context_lines)

    def _build_buttons(show_more=True):
        rows = [[
            {"text": "✏️ Continue", "callback_data": "stop:continue"},
            {"text": "❌ Dismiss", "callback_data": "stop:dismiss"},
        ]]
        if show_more:
            rows.append([{"text": "📖 More context", "callback_data": "stop:more"}])
        return rows

    buttons = _build_buttons()
    more_available = bool(transcript_path)

    try:
        msg_id = ch.send_message(text, buttons=buttons if more_available else _build_buttons(show_more=False))
        _log(f"Sent stop message msg_id={msg_id}")
    except Exception as e:
        _log(f"SEND FAILED: {e}")
        sys.exit(0)

    prompt_ids = []

    poll_start_size = 0
    if transcript_path:
        try:
            poll_start_size = os.path.getsize(transcript_path)
        except OSError:
            pass

    def resolve(status):
        ch.edit_message(msg_id, text=_status_text(status, session_tag, context_lines), buttons=[])
        _cleanup_prompts(ch, prompt_ids)

    def local_response():
        return transcript_path and check_local_response(transcript_path, poll_start_size)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if local_response():
            _log("User responded locally, releasing stop")
            resolve("🖥️ Handled locally")
            sys.exit(0)

        update = ch.poll([msg_id, *prompt_ids])
        if update is None:
            time.sleep(1)
            continue

        # Close TOCTOU: local action may have happened in parallel with poll.
        if local_response():
            _log("User responded locally (parallel with callback), releasing stop")
            resolve("🖥️ Handled locally")
            sys.exit(0)

        if update["type"] == "callback":
            data = update["data"]

            if data == "stop:more":
                _log("User clicked More")
                chunks = build_full_context_chunks(
                    transcript_path, max_turns=cfg["context_turns"])
                for chunk in chunks:
                    ch.send_reply(msg_id, chunk)
                more_available = False
                ch.edit_buttons(msg_id, _build_buttons(show_more=False))
                continue

            if data == "stop:continue":
                _log("User clicked Continue")
                # Send the ForceReply prompt FIRST so we only commit to the
                # transition if it succeeds. On failure, leave the original
                # Continue/Dismiss buttons intact so the user can retry or
                # dismiss instead of being stranded.
                prompt_msg_id = ch.send_reply_prompt(
                    msg_id,
                    "💬 Reply with your next instruction:",
                )
                if not prompt_msg_id:
                    _log("send_reply_prompt failed; keeping original buttons")
                    continue
                prompt_ids.append(prompt_msg_id)
                ch.edit_message(msg_id, text=_status_text("⏳ Waiting for instruction...", session_tag), buttons=[])
                continue

            elif data == "stop:dismiss":
                _log("User clicked Dismiss")
                resolve("❌ Dismissed")
                _write_signal(session_id)
                sys.exit(0)

        elif update["type"] == "text":
            instruction = update["text"].strip()
            if not instruction:
                continue
            masked = mask_secrets(instruction[:200])
            _log(f"Received instruction: {masked}")
            resolve(f"✅ New task sent: <code>{html_escape(masked)}</code>")
            # Block stop, inject instruction via `reason` field.
            json.dump({
                "decision": "block",
                "reason": (
                    "The user sent a new instruction via the remote messaging channel (Telegram). "
                    f"Please execute this instruction: {instruction}"
                ),
            }, sys.stdout)
            sys.stdout.flush()
            sys.exit(0)

    _log("Timeout")
    resolve("⏰ Timed out")
    _write_signal(session_id)
    sys.exit(0)


def _status_text(status, session_tag=None, context_lines=None):
    """Build a resolved-state message — keeps the "Agent idle" title and
    appends the status inline so users can tell at a glance which event
    this is."""
    text = f"💤 <b>Agent idle</b> · {status}"
    if session_tag:
        text += f" · <code>{html_escape(session_tag)}</code>"
    if context_lines:
        text += format_context_block(context_lines)
    return text


def _cleanup_prompts(ch, prompt_ids):
    """Delete ForceReply prompt messages."""
    for pid in prompt_ids:
        ch.delete_message(pid)


def _write_signal(session_id):
    """Write session-scoped signal file so Notification hook skips duplicate idle message.
    No-op when session_id is empty — otherwise a global "handled" file would
    interfere with unrelated sessions' Notification hooks."""
    if not session_id:
        _log("No session_id; skipping signal write to avoid cross-session interference")
        return
    os.makedirs(STOP_SIGNAL_DIR, exist_ok=True)
    signal_path = os.path.join(STOP_SIGNAL_DIR, f"handled_{session_id}")
    with open(signal_path, "w") as f:
        f.write(str(time.time()))
    _log(f"Wrote signal file: {signal_path}")


def check_stop_signal(session_id=""):
    """Check if Stop hook recently handled the idle event for this session.
    Called by notification.py to avoid duplicate idle messages.
    Returns True if signal is fresh (within TTL)."""
    if not session_id:
        return False
    signal_path = os.path.join(STOP_SIGNAL_DIR, f"handled_{session_id}")
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
