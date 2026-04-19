#!/usr/bin/env python3
"""
PermissionRequest hook — escalate to Telegram after local timeout.
Parallel: native dialog shows immediately, Telegram fires after N seconds.
"""
import json
import os
import signal
import sys
import time

from utils.common import (load_config, html_escape, make_logger,
                     mask_secrets, check_local_response, format_context_lines,
                     format_context_block, smart_truncate, POLL_TIMEOUT_SECONDS,
                     send_full_context,
                     session_tag as common_session_tag)
from utils.channel import create_channel

_log = make_logger("permission_request")


def format_tool_display(tool_name, tool_input):
    """Clean display of what the tool wants to do. Masks sensitive info.
    Truncates at line/word boundaries so multi-line commands don't show
    a mid-word cut in the middle of an argument."""
    if tool_name == "Bash":
        text = smart_truncate(tool_input.get("command", str(tool_input)), 300)
    elif tool_name in ("Edit", "Write"):
        text = smart_truncate(tool_input.get("file_path", str(tool_input)), 300)
    elif tool_name == "WebFetch":
        text = smart_truncate(tool_input.get("url", str(tool_input)), 300)
    elif tool_name in ("EnterPlanMode", "ExitPlanMode"):
        return ""
    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        return questions[0].get("question", "") if questions else ""
    else:
        raw = json.dumps(tool_input, ensure_ascii=False)
        text = smart_truncate(raw, 300) if raw != "{}" else ""
    return mask_secrets(text)


# Keycap emoji for option indices 1-10. AskUserQuestion schema currently
# caps at 4 options, so even 10 is generous headroom. Beyond 10 falls
# back to "(N)" — functional, just no special glyph.
_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _format_option_body(options, multi, selected=None):
    """Render the option list as a text block for the message body.
    Each option shows its emoji number + full label + optional description."""
    selected = selected or set()
    lines = []
    for i, opt in enumerate(options):
        marker = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"{i+1}."
        label = opt.get("label", f"Option {i+1}")
        desc = opt.get("description", "")
        if multi:
            check = "✅ " if i in selected else "⬜ "
            line = f"{check}{marker} <b>{html_escape(label)}</b>"
        else:
            line = f"{marker} <b>{html_escape(label)}</b>"
        if desc:
            line += f"\n   <i>{html_escape(desc)}</i>"
        lines.append(line)
    return "\n".join(lines)


def build_ask_user_question_message(ch, tool_input, context_lines, session_tag="", show_more=True):
    """For AskUserQuestion: render options inline in the message body and
    use compact emoji-number buttons so long option labels don't get cut."""
    questions = tool_input.get("questions", [])
    if not questions:
        return None, None, None, False

    q = questions[0]
    # Cap question text before HTML assembly — if it's huge (AI-generated
    # questions can be), truncate cleanly at a paragraph boundary instead
    # of letting the final HTML-level hard cut slice through a tag.
    question_text = smart_truncate(q.get("question", "?"), 2000)
    options = q.get("options", [])
    multi = q.get("multiSelect", False)

    context_text = format_context_block(context_lines)

    options_block = _format_option_body(options, multi, selected=set())
    multi_hint = "\n\n<i>Multi-select: tap to toggle, then ✅ Submit</i>" if multi else ""
    tag = f" · <code>{html_escape(session_tag)}</code>" if session_tag else ""
    text = (
        f"❓ <b>{html_escape(question_text)}</b>{tag}\n\n"
        f"{options_block}"
        f"{multi_hint}"
        f"{context_text}"
    )
    text = smart_truncate(text, 4000, marker="\n\n<i>…truncated</i>")

    buttons = _build_question_keyboard(options, multi, selected=set(), show_more=show_more)
    msg_id = ch.send_message(text, buttons=buttons)
    return msg_id, question_text, options, multi


def _build_question_keyboard(options, multi, selected=None, show_more=True):
    """Build inline keyboard. Keycap emoji per option; labels live in the
    message body. Rows of up to 5 buttons to keep things compact on mobile."""
    selected = selected or set()
    row = []
    rows = []
    for i, _ in enumerate(options):
        marker = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"({i+1})"
        # In multi-select we prefix a check so the button reflects toggled state.
        text = f"{'✅ ' if i in selected else ''}{marker}" if multi else marker
        row.append({"text": text, "callback_data": f"opt:{i}"})
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if multi:
        rows.append([{"text": "✅ Submit", "callback_data": "opt:submit"}])
    rows.append([{"text": "✏️ Other", "callback_data": "opt:other"}])
    if show_more:
        rows.append([{"text": "📖 Full context", "callback_data": "opt:more"}])
    return rows


def poll_question_answer(ch, message_id, options, multi=False, transcript_path="", poll_start_size=0, prompt_ids=None, on_more=None):
    """Poll for AskUserQuestion answer via channel.

    prompt_ids: optional list, mutated in place as transient ForceReply
    prompts are created. Callers share this list with their cleanup
    handlers so stale prompts are deleted on any exit path (normal
    resolve, signal, atexit).

    on_more: optional callable invoked when user taps More. Called in-loop."""
    selected = set()
    # Msg ids we accept replies on. Starts with the question msg_id; the
    # "Other" branch appends the ForceReply prompt msg_id so TG clients
    # auto-quoting the prompt (the natural UX target) still route here.
    accepted_ids = [message_id]
    if prompt_ids is None:
        prompt_ids = []
    more_shown = True
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local", None

        update = ch.poll(accepted_ids)
        if update is None:
            time.sleep(1)
            continue

        # Close the TOCTOU window: transcript may have grown between the
        # last check and now. If the user responded locally, don't overwrite
        # their decision with the channel callback that arrived in parallel.
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local", None

        if update["type"] == "callback":
            data = update["data"]
            if not data.startswith("opt:"):
                continue
            action = data.split(":", 1)[1] if ":" in data else ""

            if action == "more":
                if on_more and more_shown:
                    # Flip first so rapid-duplicate callbacks drop, then
                    # restore on failure so the user can retry.
                    more_shown = False
                    if on_more(selected, multi) is False:
                        more_shown = True
                continue

            if action == "other":
                prompt_id = ch.send_reply_prompt(
                    message_id,
                    "✏️ Reply to this message with your custom answer "
                    "— your input box is locked to reply mode."
                )
                if prompt_id is not None and prompt_id not in accepted_ids:
                    accepted_ids.append(prompt_id)
                    prompt_ids.append(prompt_id)
                continue

            if action == "submit":
                labels = [options[i].get("label", "") for i in sorted(selected)]
                return "option", ", ".join(labels) if labels else "Other"

            idx = int(action) if action.isdigit() else -1
            if idx < 0 or idx >= len(options):
                continue

            if multi:
                if idx in selected:
                    selected.discard(idx)
                else:
                    selected.add(idx)
                buttons = _build_question_keyboard(options, multi, selected, show_more=more_shown)
                ch.edit_buttons(message_id, buttons)
                continue
            else:
                # Single select — look up label from options by index
                return "option", options[idx].get("label", f"Option {idx+1}")

        elif update["type"] == "text":
            text = update["text"]
            if text:
                return "text", text

    return "timeout", None


def build_approval_buttons(permission_suggestions=None, show_more=True):
    """Assemble inline-keyboard rows for an approval message. Separate
    helper so we can rebuild without More after the user taps it."""
    row = [{"text": "✅ Allow", "callback_data": "allow"}]
    if permission_suggestions:
        row.append({"text": "✅ Always", "callback_data": "always"})
    row.append({"text": "❌ Deny", "callback_data": "deny"})
    rows = [row]
    if show_more:
        rows.append([{"text": "📖 Full context", "callback_data": "more"}])
    return rows


def send_approval_message(ch, tool_name, tool_display, context_lines, permission_suggestions=None, session_tag="", show_more=True):
    context_text = format_context_block(context_lines)

    cmd_block = f"\n\n<pre>{html_escape(tool_display)}</pre>" if tool_display else ""
    tag = f" · <code>{html_escape(session_tag)}</code>" if session_tag else ""
    text = (
        f"🔔 <b>Permission Request: {html_escape(tool_name)}</b>{tag}"
        f"{cmd_block}"
        f"{context_text}"
    )
    text = smart_truncate(text, 4000, marker="\n\n<i>…truncated</i>")
    return ch.send_message(text, buttons=build_approval_buttons(permission_suggestions, show_more=show_more))



def poll_callback(ch, message_id, transcript_path="", poll_start_size=0,
                  on_more=None):
    """Poll channel for callback only (ignores text messages).
    Runs until response, local action, or POLL_TIMEOUT_SECONDS (3 days).

    on_more: optional callable invoked when the user taps "More". Called
    at most once — duplicate clicks arriving before the button is edited
    away are ignored so we don't send the full context twice."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    more_shown = True

    while time.monotonic() < deadline:
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local"

        update = ch.poll(message_id)
        if update and update["type"] == "callback":
            # Re-check to catch local activity that happened in parallel
            # with the channel callback.
            if check_local_response(transcript_path, poll_start_size, threshold=100):
                return "local"
            if update["data"] == "more":
                if on_more and more_shown:
                    # Flip first so rapid-duplicate callbacks drop, then
                    # restore on failure so the user can retry.
                    more_shown = False
                    if on_more() is False:
                        more_shown = True
                continue
            return update["data"]

        time.sleep(1)

    return "timeout"


def edit_message_resolved(ch, message_id, status, tool_name, tool_display):
    """Edit message after resolution — just icon + title + command, clean and short."""
    icons = {"allow": "✅", "always": "✅", "deny": "❌", "timeout": "⏰", "local": "🖥", "expired": "💤"}
    labels = {"allow": "Allowed", "always": "Always allowed", "deny": "Denied", "timeout": "Timeout", "local": "Handled locally", "expired": "Session ended"}
    icon = icons.get(status, "✅")
    label = labels.get(status, status)

    cmd_block = f"\n\n<pre>{html_escape(tool_display)}</pre>" if tool_display else ""
    text = (
        f"{icon} <b>{label}: {html_escape(tool_name)}</b>"
        f"{cmd_block}"
    )

    # buttons=[] clears the inline keyboard so users can't tap Allow/Deny
    # on an already-resolved message (callback would land in pending and
    # TTL-expire silently).
    ch.edit_message(message_id, text, buttons=[])



# ---------------------------------------------------------------- hook response

def respond_allow(permission_suggestions=None):
    decision = {"behavior": "allow"}
    if permission_suggestions:
        decision["updatedPermissions"] = permission_suggestions
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": decision}}, sys.stdout)
    sys.stdout.flush()


def respond_allow_with_input(updated_input):
    """Allow + pre-fill tool input (used for AskUserQuestion answers)."""
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow", "updatedInput": updated_input}}}, sys.stdout)
    sys.stdout.flush()

def respond_deny(message=""):
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": message}}}, sys.stdout)
    sys.stdout.flush()

# ---------------------------------------------------------------- main

def main():

    # State shared with signal handler and atexit.
    # prompt_ids: transient ForceReply prompts from AskUserQuestion's "Other"
    # branch. Deleted on any exit path so TG users don't see a dangling
    # "Reply to this message" lock on an already-resolved request.
    state = {"ch": None, "msg_id": None, "tool_name": "", "tool_display": "",
             "resolved": False, "prompt_ids": []}

    def _cleanup_prompts():
        if not state["ch"]:
            return
        for pid in state["prompt_ids"]:
            try:
                state["ch"].delete_message(pid)
            except Exception:
                pass
        state["prompt_ids"] = []

    def on_signal(sig, frame):
        _log(f"SIGNAL received: {sig}")
        state["resolved"] = True
        if state["msg_id"] and state["ch"]:
            import signal as _sig
            status = "expired" if sig == _sig.SIGPIPE else "local"
            edit_message_resolved(
                state["ch"], state["msg_id"],
                status, state["tool_name"], state["tool_display"],
            )
        _cleanup_prompts()
        sys.exit(0)

    import atexit

    def on_exit():
        """When hook dies for ANY reason, clean up stale Telegram buttons."""
        if state["msg_id"] and state["ch"] and not state["resolved"]:
            edit_message_resolved(
                state["ch"], state["msg_id"],
                "expired", state["tool_name"], state["tool_display"],
            )
        _cleanup_prompts()

    atexit.register(on_exit)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(sig, on_signal)

    try:
        raw = sys.stdin.read()
        _log(f"START, stdin={len(raw)} bytes")
        event = json.loads(raw)
    except Exception as e:
        _log(f"STDIN ERROR: {e}")
        sys.exit(0)

    cfg = load_config()
    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        sys.exit(0)
    _log(f"CONFIG: channel={cfg['channel_type']}, escalation={cfg['escalation_seconds']}")

    tool_name = event.get("tool_name", "?")
    tool_input = event.get("tool_input", {})
    transcript_path = event.get("transcript_path", "")
    tool_display = format_tool_display(tool_name, tool_input)
    session_tag = common_session_tag(event)

    state["ch"] = ch
    state["tool_name"] = tool_name
    state["tool_display"] = tool_display

    context_lines = format_context_lines(transcript_path, max_turns=cfg["context_turns"], max_chars=cfg["context_max_chars"])

    # Record transcript size BEFORE sleep
    poll_start_size = 0
    if transcript_path:
        try:
            poll_start_size = os.path.getsize(transcript_path)
        except OSError:
            pass

    # Phase 1: Wait locally, checking for local response every 0.5s
    deadline = time.monotonic() + cfg["escalation_seconds"]
    while time.monotonic() < deadline:
        try:
            time.sleep(0.5)
        except SystemExit:
            sys.exit(0)
        if check_local_response(transcript_path, poll_start_size):
            _log("User responded locally during sleep, skipping Telegram")
            sys.exit(0)

    # Re-record transcript size AFTER sleep (fresh baseline for polling phase)
    if transcript_path:
        try:
            poll_start_size = os.path.getsize(transcript_path)
        except OSError:
            pass

    # Phase 2 & 3: depends on tool type
    _log(f"ESCALATING: {tool_name} / {tool_display[:50]}")

    if tool_name == "AskUserQuestion":
        try:
            msg_id, question_text, options, multi = build_ask_user_question_message(
                ch, tool_input, context_lines, session_tag=session_tag,
                show_more=bool(transcript_path) and cfg["context_turns"] > 0)
            state["msg_id"] = msg_id
            _log(f"SENT question msg_id={msg_id} multi={multi}")
        except Exception as e:
            _log(f"SEND FAILED: {e}")
            sys.exit(0)

        def _on_more_question(selected, multi_state):
            _log("User clicked More")
            sent, total = send_full_context(ch, msg_id, transcript_path, cfg["context_turns"])
            if sent == total:  # includes 0==0 — nothing to show, drop button
                if total == 0:
                    _log("No full context to expand")
                ch.edit_buttons(msg_id, _build_question_keyboard(
                    options, multi_state, selected, show_more=False))
                return True
            _log(f"Full context incomplete ({sent}/{total}); keeping button")
            return False

        answer_type, answer_value = poll_question_answer(
            ch, msg_id, options, multi, transcript_path, poll_start_size,
            prompt_ids=state["prompt_ids"], on_more=_on_more_question)

        state["resolved"] = True
        if answer_type == "local":
            edit_message_resolved(ch, msg_id, "local", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)
        elif answer_type == "timeout":
            _log("AskUserQuestion timeout, no response")
            edit_message_resolved(ch, msg_id, "timeout", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)
        elif answer_type in ("option", "text"):
            selected = answer_value or ""
            edit_message_resolved(ch, msg_id, "allow", tool_name, f"{question_text}: {selected}")
            updated_input = dict(tool_input)
            updated_input["answers"] = {question_text: selected}
            respond_allow_with_input(updated_input)
            _cleanup_prompts()
        else:
            edit_message_resolved(ch, msg_id, "expired", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)

    else:
        permission_suggestions = event.get("permission_suggestions")
        try:
            msg_id = send_approval_message(
                ch, tool_name, tool_display, context_lines,
                permission_suggestions, session_tag=session_tag,
                show_more=bool(transcript_path) and cfg["context_turns"] > 0)
            state["msg_id"] = msg_id
            _log(f"SENT msg_id={msg_id}")
        except Exception as e:
            _log(f"SEND FAILED: {e}")
            sys.exit(0)

        def _on_more():
            _log("User clicked More")
            sent, total = send_full_context(ch, msg_id, transcript_path, cfg["context_turns"])
            if sent == total:  # includes 0==0 — nothing to show, drop button
                if total == 0:
                    _log("No full context to expand")
                ch.edit_buttons(msg_id, build_approval_buttons(
                    permission_suggestions, show_more=False))
                return True
            _log(f"Full context incomplete ({sent}/{total}); keeping button")
            return False

        answer = poll_callback(ch, msg_id, transcript_path, poll_start_size,
                               on_more=_on_more)

        state["resolved"] = True
        if answer == "local":
            edit_message_resolved(ch, msg_id, "local", tool_name, tool_display)
            sys.exit(0)
        elif answer == "timeout":
            _log("Telegram timeout, no response")
            edit_message_resolved(ch, msg_id, "timeout", tool_name, tool_display)
            sys.exit(0)
        elif answer == "allow":
            edit_message_resolved(ch, msg_id, "allow", tool_name, tool_display)
            respond_allow()
        elif answer == "always":
            edit_message_resolved(ch, msg_id, "always", tool_name, tool_display)
            respond_allow(permission_suggestions=permission_suggestions)
        elif answer == "deny":
            edit_message_resolved(ch, msg_id, "deny", tool_name, tool_display)
            respond_deny("User denied via Telegram")

if __name__ == "__main__":
    main()
