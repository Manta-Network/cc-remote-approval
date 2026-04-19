#!/usr/bin/env python3
"""
Elicitation hook — hybrid mode.

Phase 1 (0-60s): Parent blocks, child sends Telegram form immediately.
  - Telegram responds → parent returns hookSpecificOutput → native form never shows.
Phase 2 (60s+): Timeout → parent exits (no output) → native form shows in terminal.
  - Telegram message updated: "Please fill the form in terminal"
  - Terminal window activated via osascript
  - Child waits for ElicitationResult signal → updates Telegram as "Handled locally"
"""
import json
import os
import subprocess
import sys
import time

from utils.common import (load_config, html_escape, make_logger,
                     sanitize_name, smart_truncate, ELICIT_SIGNAL_DIR,
                     send_full_context)
from utils.channel import create_channel
_log = make_logger("elicitation")

# Elicitation response states (written to response_file, read by parent)
ELICIT_ACCEPT = "accept"    # User submitted form on remote channel
ELICIT_DECLINE = "decline"  # User cancelled on remote channel
ELICIT_FAIL = "fail"        # Channel unavailable or send error → show local form


# ---------------------------------------------------------------- main

def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    cfg = load_config()
    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        # Exit immediately — let native form show
        sys.exit(0)

    import uuid
    server_name = sanitize_name(event.get("mcp_server_name", "unknown"))
    request_id = f"{server_name}_{uuid.uuid4().hex[:8]}"
    message = event.get("message", "")
    schema = event.get("requested_schema", event.get("schema", {}))

    fields = _parse_fields(schema)
    if not fields:
        sys.exit(0)

    # Signal files — ALL scoped by request_id to avoid collision
    os.makedirs(ELICIT_SIGNAL_DIR, exist_ok=True)
    response_file = os.path.join(ELICIT_SIGNAL_DIR, f"{request_id}.response")
    timeout_file = os.path.join(ELICIT_SIGNAL_DIR, f"{request_id}.timeout")
    done_file = os.path.join(ELICIT_SIGNAL_DIR, f"{request_id}.done")

    # Register active request so ElicitationResult can find it by server_name
    active_file = os.path.join(ELICIT_SIGNAL_DIR, f"{request_id}.active")
    with open(active_file, "w") as f:
        json.dump({"server_name": server_name, "request_id": request_id}, f)

    for f in [response_file, timeout_file, done_file]:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    _log(f"START server={server_name}, request_id={request_id}, timeout={cfg['elicitation_timeout']}s")

    pid = os.fork()

    if pid > 0:
        # ====== PARENT: block, wait for Telegram response or timeout ======
        _log(f"PARENT blocking, child={pid}")

        deadline = time.monotonic() + cfg["elicitation_timeout"]
        while time.monotonic() < deadline:
            time.sleep(0.5)

            if os.path.exists(response_file):
                try:
                    with open(response_file) as f:
                        data = json.load(f)
                    # Clean up after reading (child intentionally leaves this for us)
                    try:
                        os.remove(response_file)
                    except OSError:
                        pass
                    action = data.get("action", "")
                    if action == ELICIT_FAIL:
                        _log("Channel failed, falling back to local form")
                        sys.exit(0)
                    _log(f"Channel response: {data}")
                    json.dump({
                        "hookSpecificOutput": {
                            "hookEventName": "Elicitation",
                            "action": action or ELICIT_ACCEPT,
                            "content": data.get("content", {}),
                        }
                    }, sys.stdout)
                    sys.stdout.flush()
                    sys.exit(0)
                except Exception as e:
                    _log(f"Error reading response: {e}")

        # Timeout — signal child, activate terminal, exit to show form
        _log("TIMEOUT, falling back to local form")
        with open(timeout_file, "w") as f:
            json.dump({"timeout": True}, f)

        _activate_terminal()
        # Exit with no output → Claude Code shows native form
        sys.exit(0)

    else:
        # ====== CHILD: daemon, send Telegram, poll ======
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)  # CRITICAL: free stdout pipe to Claude Code
        os.dup2(devnull, 2)
        os.close(devnull)

        _log(f"CHILD started, sending Telegram form")
        try:
            _child_run(cfg, server_name, message, fields,
                        response_file, timeout_file, done_file,
                        transcript_path=event.get("transcript_path", ""))
        finally:
            # Clean up signal files (NOT response_file — parent needs to read it)
            for f in [active_file, timeout_file, done_file]:
                try:
                    os.remove(f)
                except OSError:
                    pass
        os._exit(0)


# ---------------------------------------------------------------- child logic

def _child_run(cfg, server_name, message, fields,
               response_file, timeout_file, done_file,
               transcript_path=""):
    """Child process: send form via channel, poll for response."""
    ch, ch_err = create_channel(cfg)
    if not ch:
        _log("No channel in child, signaling parent to fall back")
        _write_response(response_file, ELICIT_FAIL, {})
        return

    # Send form immediately
    timeout = cfg.get("elicitation_timeout")
    text, buttons = _build_form_message(message, fields, timeout=timeout,
                                        show_more=bool(transcript_path))
    try:
        msg_id = ch.send_message(text, buttons=buttons)
        _log(f"Sent form msg_id={msg_id}")
    except Exception as e:
        _log(f"Send failed: {e}, signaling parent to fall back")
        _write_response(response_file, ELICIT_FAIL, {})
        return

    # Pre-fill defaults so the user doesn't have to manually set fields
    # that already have a sensible value in the schema.
    # Exclude booleans — their toggle buttons should stay visible until
    # the user explicitly clicks; defaults are applied at submit time.
    form_data = {f["name"]: f["default"] for f in fields
                 if f["default"] is not None and f["type"] != "boolean"}

    more_shown = bool(transcript_path)

    while True:
        if os.path.exists(timeout_file):
            _log("Parent timed out, updating message")
            _edit_terminal_fallback(ch, msg_id, message, fields, form_data)
            _wait_for_local_done(ch, msg_id, done_file, message)
            return

        if os.path.exists(done_file):
            _log("User filled locally during Phase 1")
            ch.edit_message(msg_id, f"🖥 <b>Handled locally</b>\n\n<i>{html_escape(message)}</i>", buttons=[])
            return

        update = ch.poll(msg_id)
        if update is None:
            time.sleep(1)
            continue

        if update["type"] == "callback":
            data = update["data"]

            if data == "more" and more_shown:
                # Flip first so rapid-duplicate clicks drop; restore on
                # partial/total failure so the user can still retry.
                more_shown = False
                _log("User clicked More")
                sent, total = send_full_context(ch, msg_id, transcript_path,
                                                cfg.get("context_turns", 3))
                if total > 0 and sent == total:
                    # Rebuild the form with current filled-state so the user
                    # sees an accurate reflection of what will be submitted.
                    _update_form(ch, msg_id, message, fields, form_data,
                                 timeout=timeout, show_more=False)
                else:
                    _log(f"Full context incomplete ({sent}/{total}); keeping button")
                    more_shown = True
                continue

            if data.startswith("f:"):
                # Format: f:{field_idx}:{type}:{value_idx_or_flag}
                parts = data.split(":")
                if len(parts) >= 4:
                    fi, ftype, val = int(parts[1]), parts[2], parts[3]
                    if fi < len(fields):
                        field = fields[fi]
                        if ftype == "e" and field.get("enum"):
                            ei = int(val)
                            if ei < len(field["enum"]):
                                form_data[field["name"]] = field["enum"][ei]
                        elif ftype == "b":
                            form_data[field["name"]] = val == "1"
                        _update_form(ch, msg_id, message, fields, form_data, timeout=timeout, show_more=more_shown)

            elif data == "submit":
                # Block submit if any required field is still empty.
                missing = [f["title"] for f in fields
                           if f.get("required") and f["name"] not in form_data]
                if missing:
                    _log(f"Submit blocked — missing required: {missing}")
                    _update_form(ch, msg_id, message, fields, form_data, timeout=timeout, show_more=more_shown)
                    continue
                # Apply boolean defaults for fields the user didn't touch
                for f in fields:
                    if f["type"] == "boolean" and f["name"] not in form_data and f["default"] is not None:
                        form_data[f["name"]] = f["default"]
                _log(f"Submit: {form_data}")
                summary = f"✅ <b>Form submitted</b>\n\n<i>{html_escape(message)}</i>"
                for name, val in form_data.items():
                    summary += f"\n  • {html_escape(name)}: <code>{html_escape(str(val))}</code>"
                ch.edit_message(msg_id, summary, buttons=[])
                _write_response(response_file, ELICIT_ACCEPT, form_data)
                return

            elif data == "cancel":
                _log("Cancel")
                ch.edit_message(msg_id, f"❌ <b>Cancelled</b>\n\n<i>{html_escape(message)}</i>", buttons=[])
                _write_response(response_file, ELICIT_DECLINE, {})
                return

        elif update["type"] == "text":
            text_val = update["text"]
            for field in fields:
                if field["name"] in form_data or field.get("enum"):
                    continue
                ftype = field["type"]
                # String, integer, number fields accept text input.
                # Cast to the target type; on failure skip (user can retry).
                if ftype == "string":
                    form_data[field["name"]] = text_val
                elif ftype in ("integer", "number"):
                    try:
                        form_data[field["name"]] = (
                            int(text_val) if ftype == "integer"
                            else float(text_val)
                        )
                    except ValueError:
                        continue  # bad input, try next field or wait
                else:
                    continue
                _update_form(ch, msg_id, message, fields, form_data, timeout=timeout, show_more=more_shown)
                break


def _wait_for_local_done(ch, msg_id, done_file, message=""):
    """After timeout, wait for user to fill form locally."""
    title = f"\n\n<i>{html_escape(message)}</i>" if message else ""
    for _ in range(300):  # max 5 minutes
        if os.path.exists(done_file):
            _log("Local done signal received")
            ch.edit_message(msg_id, f"🖥 <b>Handled locally</b>{title}", buttons=[])
            return
        time.sleep(1)
    ch.edit_message(msg_id, f"💤 <b>Expired</b>{title}", buttons=[])


# ---------------------------------------------------------------- signal files

def _write_response(path, action, content):
    """Atomically write response file for parent to read."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"action": action, "content": content}, f)
    os.rename(tmp, path)


# ---------------------------------------------------------------- terminal

def _activate_terminal():
    """Bring terminal window to front on macOS."""
    term = os.environ.get("TERM_PROGRAM", "")
    app_map = {
        "Apple_Terminal": "Terminal",
        "iTerm.app": "iTerm",
        "iTerm2.app": "iTerm2",
        "WarpTerminal": "Warp",
        "vscode": "Visual Studio Code",
        "ghostty": "Ghostty",
    }
    app_name = app_map.get(term)
    if app_name:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'],
                timeout=3, capture_output=True)
            _log(f"Activated terminal: {app_name}")
        except Exception:
            pass


# ---------------------------------------------------------------- message helpers

def _edit_terminal_fallback(ch, msg_id, message, fields, form_data):
    """Update message: timeout, please fill on terminal."""
    text = (
        f"⏰ <b>Please fill the form in Claude Code</b>\n\n"
        f"The form is now showing in Claude Code.\n\n"
        f"<i>{html_escape(message)}</i>"
    )
    if form_data:
        text += "\n\nFilled:"
        for name, val in form_data.items():
            text += f"\n  • {html_escape(name)}: <code>{html_escape(str(val))}</code>"
    # Form now lives in the local terminal — clear the channel buttons so
    # users don't tap them expecting remote-side action.
    ch.edit_message(msg_id, text, buttons=[])


# ---------------------------------------------------------------- form helpers

def _parse_fields(schema):
    props = schema.get("properties", {})
    required = schema.get("required", [])
    fields = []
    for name, spec in props.items():
        fields.append({
            "name": name,
            "type": spec.get("type", "string"),
            "title": spec.get("title", name),
            "required": name in required,
            "enum": spec.get("enum"),
            "default": spec.get("default"),
        })
    return fields


def _build_field_buttons(fields, skip_filled=None, show_more=True):
    """Build inline-keyboard rows for form fields. Shared by _build_form_message
    and _update_form — enum fields get one button per value, booleans get a
    ✅/⬜ pair, and string fields have no buttons (handled via text reply).

    skip_filled: optional set of field names to skip (already answered)."""
    skip_filled = skip_filled or set()
    buttons = []
    for fi, f in enumerate(fields):
        if f["name"] in skip_filled:
            continue
        title = f["title"]
        if f.get("enum"):
            for ei, val in enumerate(f["enum"]):
                buttons.append([{"text": f"{title}: {val}",
                                 "callback_data": f"f:{fi}:e:{ei}"}])
        elif f["type"] == "boolean":
            buttons.append([
                {"text": f"✅ {title}", "callback_data": f"f:{fi}:b:1"},
                {"text": f"⬜ {title}", "callback_data": f"f:{fi}:b:0"},
            ])
    buttons.append([
        {"text": "✅ Submit", "callback_data": "submit"},
        {"text": "❌ Cancel", "callback_data": "cancel"},
    ])
    if show_more:
        buttons.append([{"text": "📖 Full context", "callback_data": "more"}])
    return buttons


def _build_form_message(message, fields, timeout=None, show_more=True):
    # Truncate cleanly at a paragraph/line/word boundary before HTML-escaping
    # so the final message stays under TG's 4096-char limit even when the
    # MCP server sends a very long elicitation prompt.
    message = smart_truncate(message or "", 2000, marker="\n\n…truncated")
    text = f"📋 <b>Form</b>\n"
    if timeout:
        text += f"⏳ Respond within {timeout}s, or it will fall back to local form\n"
    text += f"\n{html_escape(message)}\n"
    # Fields that need text input (no buttons): string, integer, number.
    _TEXT_INPUT_TYPES = {"string", "integer", "number"}
    for f in fields:
        if f["type"] in _TEXT_INPUT_TYPES and not f.get("enum"):
            hint = "number" if f["type"] in ("integer", "number") else "text"
            text += f"\n💬 <i>{html_escape(f['title'])}: type {hint} below</i>"
    return text, _build_field_buttons(fields, show_more=show_more)


def _update_form(ch, msg_id, message, fields, form_data, timeout=None, show_more=True):
    message = smart_truncate(message or "", 2000, marker="\n\n…truncated")
    text = f"📋 <b>Form</b>\n"
    if timeout:
        text += f"⏳ Respond within {timeout}s, or it will fall back to local form\n"
    text += f"\n{html_escape(message)}\n"
    for f in fields:
        name, title = f["name"], f["title"]
        if name in form_data:
            text += f"\n✅ {html_escape(title)}: <code>{html_escape(str(form_data[name]))}</code>"
        else:
            text += f"\n⬜ {html_escape(title)}: ..."
    ch.edit_message(msg_id, text,
                    buttons=_build_field_buttons(fields, skip_filled=set(form_data), show_more=show_more))


if __name__ == "__main__":
    main()
