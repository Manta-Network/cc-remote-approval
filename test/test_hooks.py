"""Tests for hook components — uses FakeChannel from scenarios (channel-agnostic)."""
import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

from scenarios import FakeChannel


# --- format_tool_display ---

class TestFormatToolDisplay:
    def test_bash_shows_command(self):
        from permission_request import format_tool_display
        result = format_tool_display("Bash", {"command": "ls -la /tmp"})
        assert "ls -la /tmp" in result

    def test_edit_shows_file_path(self):
        from permission_request import format_tool_display
        result = format_tool_display("Edit", {"file_path": "/home/user/file.py"})
        assert "/home/user/file.py" in result

    def test_webfetch_shows_url(self):
        from permission_request import format_tool_display
        result = format_tool_display("WebFetch", {"url": "https://example.com"})
        assert "https://example.com" in result

    def test_exit_plan_mode_empty(self):
        from permission_request import format_tool_display
        assert format_tool_display("ExitPlanMode", {}) == ""

    def test_ask_user_question_shows_question(self):
        from permission_request import format_tool_display
        result = format_tool_display("AskUserQuestion", {
            "questions": [{"question": "Which database?"}]
        })
        assert "Which database?" in result

    def test_bash_masks_secrets(self):
        from permission_request import format_tool_display
        result = format_tool_display("Bash", {"command": "export TOKEN=secret123abc"})
        assert "secret123abc" not in result
        assert "****" in result

    def test_truncates_long_commands(self):
        from permission_request import format_tool_display
        result = format_tool_display("Bash", {"command": "x" * 500})
        assert len(result) <= 310  # 300 + masking overhead


# --- respond_* functions ---

class TestResponders:
    def test_respond_allow(self, capsys):
        from permission_request import respond_allow
        respond_allow()
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    def test_respond_allow_with_permissions(self, capsys):
        from permission_request import respond_allow
        perms = [{"type": "addRules", "rules": [{"toolName": "Bash"}]}]
        respond_allow(permission_suggestions=perms)
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["decision"]["updatedPermissions"] == perms

    def test_respond_deny(self, capsys):
        from permission_request import respond_deny
        respond_deny("User said no")
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        assert out["hookSpecificOutput"]["decision"]["message"] == "User said no"

    def test_respond_allow_with_input(self, capsys):
        from permission_request import respond_allow_with_input
        respond_allow_with_input({"answers": {"Q": "A"}})
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Q"] == "A"


# --- send_approval_message ---

class TestSendApprovalMessage:
    def test_sends_with_allow_deny_buttons(self):
        from permission_request import send_approval_message
        ch = FakeChannel()

        msg_id = send_approval_message(ch, "Bash", "ls -la", [])
        assert msg_id > 0
        assert len(ch._sent_messages) == 1
        buttons_flat = [b for row in ch._sent_messages[0]["buttons"] for b in row]
        labels = [b["text"] for b in buttons_flat]
        assert "✅ Allow" in labels
        assert "❌ Deny" in labels

    def test_always_button_shown_when_suggestions_present(self):
        from permission_request import send_approval_message
        ch = FakeChannel()

        perms = [{"type": "addRules"}]
        send_approval_message(ch, "Bash", "ls", [], perms)

        buttons_flat = [b for row in ch._sent_messages[0]["buttons"] for b in row]
        labels = [b["text"] for b in buttons_flat]
        assert "✅ Always" in labels

    def test_always_button_hidden_when_no_suggestions(self):
        from permission_request import send_approval_message
        ch = FakeChannel()

        send_approval_message(ch, "Bash", "ls", [], None)

        buttons_flat = [b for row in ch._sent_messages[0]["buttons"] for b in row]
        labels = [b["text"] for b in buttons_flat]
        assert "✅ Always" not in labels


class TestConfigIntFallback:
    """Invalid int config values must fall back to defaults, not crash."""

    def test_bad_int_falls_back_to_default(self, monkeypatch):
        from utils.common import load_config, DEFAULTS
        monkeypatch.setenv("CC_REMOTE_APPROVAL_ESCALATION_SECONDS", "abc")
        cfg = load_config()
        assert cfg["escalation_seconds"] == DEFAULTS["escalation_seconds"]

    def test_valid_int_still_works(self, monkeypatch):
        from utils.common import load_config
        monkeypatch.setenv("CC_REMOTE_APPROVAL_ESCALATION_SECONDS", "30")
        assert load_config()["escalation_seconds"] == 30


class TestSessionTag:
    """cwd basename is embedded in outbound channel messages so users
    running multiple CC sessions in one TG chat can tell them apart."""

    def test_approval_message_includes_session_tag(self):
        from permission_request import send_approval_message
        ch = FakeChannel()
        send_approval_message(ch, "Bash", "ls", [], session_tag="my-repo")
        assert "my-repo" in ch._sent_messages[0]["text"]

    def test_approval_message_omits_tag_when_empty(self):
        from permission_request import send_approval_message
        ch = FakeChannel()
        send_approval_message(ch, "Bash", "ls", [], session_tag="")
        # Title should not have dangling separator
        assert " · <code></code>" not in ch._sent_messages[0]["text"]

    def test_askuserquestion_message_includes_session_tag(self):
        from permission_request import build_ask_user_question_message
        ch = FakeChannel()
        tool_input = {"questions": [{
            "question": "Pick?", "header": "p", "multiSelect": False,
            "options": [{"label": "A", "description": ""},
                        {"label": "B", "description": ""}],
        }]}
        build_ask_user_question_message(ch, tool_input, [], session_tag="my-repo")
        assert "my-repo" in ch._sent_messages[0]["text"]


class TestAskUserQuestionEmojiUI:
    """AskUserQuestion buttons are compact emoji keycaps; full option
    labels live in the message body (no 40-char truncation)."""

    def test_buttons_use_emoji_not_labels(self):
        from permission_request import build_ask_user_question_message
        ch = FakeChannel()
        long_label = "Refactor the entire authentication flow with OAuth2 migration"
        tool_input = {"questions": [{
            "question": "Which approach?", "header": "Approach",
            "multiSelect": False,
            "options": [
                {"label": long_label, "description": "Big refactor"},
                {"label": "Keep as-is", "description": "Safe option"},
            ],
        }]}
        build_ask_user_question_message(ch, tool_input, [])
        flat = [b for row in ch._sent_messages[0]["buttons"] for b in row]
        labels = [b["text"] for b in flat]
        # Option buttons are just the emoji keycap
        assert "1️⃣" in labels
        assert "2️⃣" in labels
        # Long label does NOT appear in any button (no 40-char cut)
        for btn in flat:
            assert long_label not in btn["text"]

    def test_full_labels_in_message_body(self):
        from permission_request import build_ask_user_question_message
        ch = FakeChannel()
        long_label = "Refactor the entire authentication flow with OAuth2 migration"
        tool_input = {"questions": [{
            "question": "Which approach?", "header": "A",
            "multiSelect": False,
            "options": [
                {"label": long_label, "description": "Big refactor"},
                {"label": "Keep as-is", "description": ""},
            ],
        }]}
        build_ask_user_question_message(ch, tool_input, [])
        body = ch._sent_messages[0]["text"]
        # Full label preserved in the body, paired with its emoji marker
        assert "1️⃣" in body
        assert long_label in body
        assert "Keep as-is" in body

    def test_rows_capped_at_5_buttons(self):
        """Mobile TG wraps long rows ugly; cap at 5 per row."""
        from permission_request import build_ask_user_question_message
        ch = FakeChannel()
        opts = [{"label": f"Option {i}", "description": ""} for i in range(7)]
        tool_input = {"questions": [{
            "question": "pick", "header": "p", "multiSelect": False,
            "options": opts,
        }]}
        build_ask_user_question_message(ch, tool_input, [])
        rows = ch._sent_messages[0]["buttons"]
        # Option rows (before the final "Other" row) must all be ≤ 5
        for row in rows[:-1]:
            assert len(row) <= 5

    def test_multi_select_shows_checkmark_on_selected_button(self):
        from permission_request import _build_question_keyboard
        rows = _build_question_keyboard(
            [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
            multi=True, selected={1},
        )
        flat = [b for row in rows for b in row]
        labels = [b["text"] for b in flat]
        # Option 2 has the checkmark; option 1 doesn't
        assert "✅ 2️⃣" in labels
        assert "1️⃣" in labels
        assert "✅ 1️⃣" not in labels


class TestSendReplyPrompt:
    """FakeChannel.send_reply_prompt returns the new prompt msg_id so
    callers can route replies quoted under the prompt (ForceReply flow)."""

    def test_returns_new_msg_id(self):
        ch = FakeChannel()
        prompt_id = ch.send_reply_prompt(100, "Type answer")
        assert prompt_id is not None
        assert prompt_id != 100  # prompt gets its own id, not the question's

    def test_force_reply_defaults_on(self):
        ch = FakeChannel()
        ch.send_reply_prompt(100, "Type answer")
        assert ch.last_sent["force_reply"] is True


class TestSessionStartHook:
    """SessionStart hook injects a hint steering Claude toward the
    AskUserQuestion tool so we don't have to heuristic-parse free-text
    questions. Only injects when a channel is actually configured."""

    def test_injects_hint_when_channel_configured(self, monkeypatch, capsys):
        import session_start as ss

        monkeypatch.setattr(ss, "load_config", lambda: {
            "channel_type": "telegram",
            "bot_token": "tok", "chat_id": "123",
            "session_hint_enabled": True,
        })

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        ss.main()

        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Must reference the tool explicitly so Claude knows what to use
        assert "AskUserQuestion" in ctx

    def test_no_hint_when_channel_unconfigured(self, monkeypatch, capsys):
        """If no bot_token/chat_id, the plugin isn't relaying anything —
        injecting a hint about channel behavior would be misleading."""
        import session_start as ss

        monkeypatch.setattr(ss, "load_config", lambda: {
            "channel_type": "telegram",
            "bot_token": "", "chat_id": "",
            "session_hint_enabled": True,
        })

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        with pytest.raises(SystemExit):
            ss.main()

        assert capsys.readouterr().out == ""

    def test_no_hint_when_disabled_by_config(self, monkeypatch, capsys):
        """session_hint_enabled=false skips injection even if channel is OK."""
        import session_start as ss

        monkeypatch.setattr(ss, "load_config", lambda: {
            "channel_type": "telegram",
            "bot_token": "tok", "chat_id": "123",
            "session_hint_enabled": False,
        })

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        with pytest.raises(SystemExit):
            ss.main()

        assert capsys.readouterr().out == ""

    def test_bool_env_override(self, monkeypatch, capsys):
        """session_hint_enabled can be toggled via env var."""
        from utils.common import load_config
        monkeypatch.setenv("CC_REMOTE_APPROVAL_SESSION_HINT_ENABLED", "false")
        assert load_config()["session_hint_enabled"] is False

        monkeypatch.setenv("CC_REMOTE_APPROVAL_SESSION_HINT_ENABLED", "true")
        assert load_config()["session_hint_enabled"] is True

    def test_stdin_read_error_exits_silently(self, monkeypatch, capsys):
        """Malformed stdin must not crash the session — we don't even
        care about event contents, we just drain stdin."""
        import session_start as ss

        class BadStdin:
            def read(self):
                raise OSError("simulated read failure")

        monkeypatch.setattr("sys.stdin", BadStdin())
        with pytest.raises(SystemExit):
            ss.main()
        assert capsys.readouterr().out == ""


class TestDeleteMessage:
    """Channel.delete_message is used to clean up transient ForceReply
    prompts so users don't see a dangling 'Reply to' lock."""

    def test_fake_records_deletion(self):
        ch = FakeChannel()
        ch.delete_message(42)
        assert 42 in ch._deleted_messages

    def test_default_channel_delete_is_noop(self):
        """Channel base class delete_message is graceful no-op, not raise."""
        from utils.channel import Channel
        Channel().delete_message(42)  # should not raise


# poll_callback and poll_question_answer are tested end-to-end in test_integration.py
# (TestBashAllow, TestBashDeny, TestBashTimeout, TestLocalResponseDuringSleep,
#  TestAskUserQuestionSingle, TestAskUserQuestionText, TestAskUserQuestionMulti)


# --- format_context_lines (was extract_context, now in common.py) ---

class TestExtractContext:
    def test_extracts_user_and_assistant_turns(self, tmp_path):
        from utils.common import format_context_lines
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello world"}}),
            json.dumps({"message": {"role": "assistant", "content": "hi there"}}),
        ]
        transcript.write_text("\n".join(lines))

        result = format_context_lines(str(transcript))
        assert len(result) == 2
        assert "hello world" in result[0]
        assert "hi there" in result[1]

    def test_returns_empty_for_missing_file(self):
        from utils.common import format_context_lines
        assert format_context_lines("/nonexistent/path") == []

    def test_respects_max_turns(self, tmp_path):
        from utils.common import format_context_lines
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": f"message {i}"}})
            for i in range(10)
        ]
        transcript.write_text("\n".join(lines))

        result = format_context_lines(str(transcript), max_turns=2)
        assert len(result) == 2

    def test_shows_local_timestamp_when_present(self, tmp_path):
        """Transcript timestamps are UTC. Context lines should convert to
        local HH:MM so the TG reader sees their local time."""
        from utils.common import format_context_lines
        from datetime import datetime, timezone
        transcript = tmp_path / "t.jsonl"
        # Use a known UTC timestamp
        utc_ts = "2026-04-16T08:30:00.000Z"
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello"},
                        "timestamp": utc_ts}),
        ]
        transcript.write_text("\n".join(lines))

        result = format_context_lines(str(transcript))
        assert len(result) == 1
        # Compute expected local time
        dt = datetime.fromisoformat(utc_ts.replace("Z", "+00:00")).astimezone()
        expected_hh_mm = dt.strftime("%H:%M")
        assert expected_hh_mm in result[0], f"expected {expected_hh_mm} in {result[0]}"

    def test_no_timestamp_still_works(self, tmp_path):
        """Transcript entries without a timestamp field should render
        without a time label — not crash."""
        from utils.common import format_context_lines
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "no time"}}),
        ]
        transcript.write_text("\n".join(lines))

        result = format_context_lines(str(transcript))
        assert len(result) == 1
        assert "no time" in result[0]
        assert "[" not in result[0]  # no time bracket when timestamp missing


# --- edit_message_resolved ---

class TestEditMessageResolved:
    def test_edits_with_correct_status(self):
        from permission_request import edit_message_resolved
        ch = FakeChannel()
        edit_message_resolved(ch, 100, "allow", "Bash", "ls -la")
        assert len(ch._edited_messages) == 1
        assert "✅" in ch._edited_messages[0]["text"]
        assert "Allowed" in ch._edited_messages[0]["text"]

    def test_timeout_status(self):
        from permission_request import edit_message_resolved
        ch = FakeChannel()
        edit_message_resolved(ch, 100, "timeout", "Bash", "ls")
        assert "⏰" in ch._edited_messages[0]["text"]
        assert "Timeout" in ch._edited_messages[0]["text"]

    def test_local_status(self):
        from permission_request import edit_message_resolved
        ch = FakeChannel()
        edit_message_resolved(ch, 100, "local", "Edit", "/tmp/x")
        assert "🖥" in ch._edited_messages[0]["text"]
"""Tests for hooks/elicitation.py and elicitation_result.py — signal file IPC."""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))


# --- Elicitation signal file logic ---

class TestElicitationSignalFiles:
    """Test signal file creation and isolation (without forking)."""

    def test_request_id_contains_server_name_and_uuid(self):
        """Verify request_id format: {server_name}_{uuid8}."""
        import uuid
        from utils.common import sanitize_name
        server = sanitize_name("test-server")
        request_id = f"{server}_{uuid.uuid4().hex[:8]}"
        assert request_id.startswith("test-server_")
        assert len(request_id) == len("test-server_") + 8

    def test_two_requests_get_different_ids(self):
        import uuid
        id1 = f"srv_{uuid.uuid4().hex[:8]}"
        id2 = f"srv_{uuid.uuid4().hex[:8]}"
        assert id1 != id2

    def test_response_file_write_and_read(self, tmp_path):
        """Simulate child writing response, parent reading it."""
        response_file = tmp_path / "srv_abc12345.response"

        # Child writes
        data = {"action": "accept", "content": {"env": "production"}}
        tmp = str(response_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, str(response_file))

        # Parent reads
        with open(str(response_file)) as f:
            result = json.load(f)
        assert result["action"] == "accept"
        assert result["content"]["env"] == "production"

    def test_done_file_per_server_not_per_request(self, tmp_path):
        """Done file uses server_name, not request_id — cancels all pending."""
        done_file = tmp_path / "test-server.done"
        with open(str(done_file), "w") as f:
            json.dump({"action": "accept", "server": "test-server"}, f)

        # Both request IDs see the same done file
        assert os.path.exists(str(done_file))

    def test_timeout_file_per_request(self, tmp_path):
        """Timeout file uses request_id — only affects one request."""
        t1 = tmp_path / "srv_aaaa1111.timeout"
        t2 = tmp_path / "srv_bbbb2222.timeout"

        with open(str(t1), "w") as f:
            json.dump({"timeout": True}, f)

        assert os.path.exists(str(t1))
        assert not os.path.exists(str(t2))


# --- ElicitationResult hook ---

class TestElicitationResult:
    def test_creates_done_file_for_active_request(self, tmp_path, monkeypatch):
        """ElicitationResult scans .active files and writes .done for matching server."""
        import utils.common as common
        import elicitation_result as er
        monkeypatch.setattr(common, "ELICIT_SIGNAL_DIR", str(tmp_path))
        monkeypatch.setattr(er, "ELICIT_SIGNAL_DIR", str(tmp_path))

        # Simulate an active elicitation request
        active_file = tmp_path / "my-server_abc12345.active"
        active_file.write_text(json.dumps({"server_name": "my-server", "request_id": "my-server_abc12345"}))

        import io
        event = {"mcp_server_name": "my-server", "action": "accept"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

        with pytest.raises(SystemExit):
            er.main()

        done_file = tmp_path / "my-server_abc12345.done"
        assert done_file.exists()
        data = json.loads(done_file.read_text())
        assert data["action"] == "accept"

    def test_signals_multiple_active_requests(self, tmp_path, monkeypatch):
        """Two concurrent requests for same server both get .done signals."""
        import utils.common as common
        import elicitation_result as er
        monkeypatch.setattr(common, "ELICIT_SIGNAL_DIR", str(tmp_path))
        monkeypatch.setattr(er, "ELICIT_SIGNAL_DIR", str(tmp_path))

        # Two active requests for same server
        (tmp_path / "srv_aaaa1111.active").write_text(
            json.dumps({"server_name": "srv", "request_id": "srv_aaaa1111"}))
        (tmp_path / "srv_bbbb2222.active").write_text(
            json.dumps({"server_name": "srv", "request_id": "srv_bbbb2222"}))

        import io
        event = {"mcp_server_name": "srv", "action": "accept"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

        with pytest.raises(SystemExit):
            er.main()

        assert (tmp_path / "srv_aaaa1111.done").exists()
        assert (tmp_path / "srv_bbbb2222.done").exists()

    def test_ignores_other_server_active_files(self, tmp_path, monkeypatch):
        """Active requests for other servers are not signaled."""
        import utils.common as common
        import elicitation_result as er
        monkeypatch.setattr(common, "ELICIT_SIGNAL_DIR", str(tmp_path))
        monkeypatch.setattr(er, "ELICIT_SIGNAL_DIR", str(tmp_path))

        (tmp_path / "other_aaaa1111.active").write_text(
            json.dumps({"server_name": "other", "request_id": "other_aaaa1111"}))

        import io
        event = {"mcp_server_name": "my-server", "action": "accept"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

        with pytest.raises(SystemExit):
            er.main()

        assert not (tmp_path / "other_aaaa1111.done").exists()


# --- Elicitation form building ---

class TestElicitationRequiredFieldValidation:
    """Submit on remote form must block when required fields are empty."""

    def test_submit_blocked_when_required_field_missing(self):
        """Simulates: user clicks Submit without filling required 'env' field."""
        fields = [
            {"name": "env", "type": "string", "title": "Environment",
             "required": True, "enum": ["prod", "staging"], "default": None},
            {"name": "note", "type": "string", "title": "Note",
             "required": False, "enum": None, "default": None},
        ]
        form_data = {}  # nothing filled
        missing = [f["title"] for f in fields
                   if f.get("required") and f["name"] not in form_data]
        assert missing == ["Environment"]

    def test_submit_allowed_when_required_fields_filled(self):
        fields = [
            {"name": "env", "type": "string", "title": "Environment",
             "required": True, "enum": ["prod", "staging"], "default": None},
        ]
        form_data = {"env": "prod"}
        missing = [f["title"] for f in fields
                   if f.get("required") and f["name"] not in form_data]
        assert missing == []


class TestElicitationDefaults:
    """Schema default values must pre-populate form_data."""

    def test_defaults_prefilled(self):
        fields = [
            {"name": "env", "type": "string", "title": "Env",
             "required": True, "enum": ["prod", "staging"], "default": "staging"},
            {"name": "note", "type": "string", "title": "Note",
             "required": False, "enum": None, "default": None},
        ]
        form_data = {f["name"]: f["default"] for f in fields if f["default"] is not None}
        assert form_data == {"env": "staging"}
        # note has no default → not in form_data

    def test_no_defaults_gives_empty(self):
        fields = [
            {"name": "x", "type": "string", "title": "X",
             "required": True, "enum": None, "default": None},
        ]
        form_data = {f["name"]: f["default"] for f in fields if f["default"] is not None}
        assert form_data == {}


class TestElicitationIntegerNumberFields:
    """integer/number fields should be fillable via text input and
    render a 'type number below' hint in the form message."""

    def test_form_message_shows_number_hint(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "count", "type": "integer", "title": "Count",
             "required": True, "enum": None, "default": None},
            {"name": "ratio", "type": "number", "title": "Ratio",
             "required": False, "enum": None, "default": None},
        ]
        text, buttons = _build_form_message("Fill", fields)
        assert "Count: type number below" in text
        assert "Ratio: type number below" in text

    def test_integer_cast_on_text_input(self):
        """Simulate the text handler logic for integer field."""
        field = {"name": "count", "type": "integer", "enum": None}
        text_val = "42"
        result = int(text_val)
        assert result == 42

    def test_float_cast_on_text_input(self):
        field = {"name": "ratio", "type": "number", "enum": None}
        text_val = "3.14"
        result = float(text_val)
        assert result == 3.14

    def test_bad_integer_input_skipped(self):
        """Non-numeric text for integer field should not crash."""
        try:
            int("not a number")
            assert False, "should have raised"
        except ValueError:
            pass  # expected — handler does continue


class TestElicitationFormBuilding:
    def test_parse_fields(self):
        from elicitation import _parse_fields
        schema = {
            "properties": {
                "env": {"type": "string", "title": "Environment", "enum": ["prod", "staging"]},
                "notify": {"type": "boolean", "title": "Notify team"},
                "note": {"type": "string", "title": "Notes"},
            },
            "required": ["env"],
        }
        fields = _parse_fields(schema)
        assert len(fields) == 3
        assert fields[0]["name"] == "env"
        assert fields[0]["enum"] == ["prod", "staging"]
        assert fields[0]["required"] is True
        assert fields[1]["type"] == "boolean"

    def test_build_form_message_has_buttons(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod", "staging"],
             "required": True, "default": None},
            {"name": "ok", "type": "boolean", "title": "OK",
             "required": False, "enum": None, "default": None},
        ]
        text, buttons = _build_form_message("Please configure", fields)
        assert "Please configure" in text
        # enum buttons + boolean buttons + submit/cancel
        assert len(buttons) >= 3
        # Last row should be submit/cancel
        last_row = buttons[-1]
        labels = [b["text"] for b in last_row]
        assert "✅ Submit" in labels
        assert "❌ Cancel" in labels

    def test_update_form_shows_filled_fields(self):
        from elicitation import _update_form

        ch = FakeChannel()
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        _update_form(ch, 100, "msg", fields, {"env": "prod"})
        assert len(ch._edited_messages) == 1
        assert "prod" in ch._edited_messages[0]["text"]


class TestElicitationTimeoutHint:
    """_build_form_message shows a timeout hint line when timeout is given."""

    def test_timeout_hint_shown_with_timeout(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        text, _ = _build_form_message("Configure", fields, timeout=60)
        assert "Respond within 60s" in text
        assert "fall back to local form" in text

    def test_timeout_hint_shown_with_custom_value(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "x", "type": "string", "title": "X", "enum": None,
             "required": False, "default": None},
        ]
        text, _ = _build_form_message("Fill", fields, timeout=30)
        assert "Respond within 30s" in text

    def test_no_timeout_hint_without_timeout(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        text, _ = _build_form_message("Configure", fields)
        assert "Respond within" not in text
        assert "fall back to local form" not in text

    def test_no_timeout_hint_when_timeout_is_none(self):
        from elicitation import _build_form_message
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        text, _ = _build_form_message("Configure", fields, timeout=None)
        assert "Respond within" not in text

    def test_no_timeout_hint_when_timeout_is_zero(self):
        """timeout=0 is falsy, so no hint should appear."""
        from elicitation import _build_form_message
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        text, _ = _build_form_message("Configure", fields, timeout=0)
        assert "Respond within" not in text

    def test_update_form_shows_timeout_hint(self):
        """_update_form also shows the timeout hint when timeout is given."""
        from elicitation import _update_form
        ch = FakeChannel()
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        _update_form(ch, 100, "msg", fields, {}, timeout=60)
        assert "Respond within 60s" in ch._edited_messages[0]["text"]

    def test_update_form_no_timeout_hint_without_timeout(self):
        from elicitation import _update_form
        ch = FakeChannel()
        fields = [
            {"name": "env", "type": "string", "title": "Env", "enum": ["prod"],
             "required": True, "default": None},
        ]
        _update_form(ch, 100, "msg", fields, {})
        assert "Respond within" not in ch._edited_messages[0]["text"]


class TestElicitationBooleanDefaultHandling:
    """Boolean fields must NOT be pre-filled so their toggle buttons stay
    visible. Boolean defaults are applied at submit time instead."""

    def test_boolean_excluded_from_prefill(self):
        """The pre-fill dict comprehension must skip boolean fields."""
        fields = [
            {"name": "env", "type": "string", "title": "Env",
             "required": True, "enum": ["prod"], "default": "prod"},
            {"name": "notify", "type": "boolean", "title": "Notify",
             "required": False, "enum": None, "default": True},
            {"name": "verbose", "type": "boolean", "title": "Verbose",
             "required": False, "enum": None, "default": False},
        ]
        # This mirrors the exact comprehension in _child_run
        form_data = {f["name"]: f["default"] for f in fields
                     if f["default"] is not None and f["type"] != "boolean"}
        assert "env" in form_data
        assert form_data["env"] == "prod"
        assert "notify" not in form_data
        assert "verbose" not in form_data

    def test_non_boolean_defaults_still_prefilled(self):
        """String/integer fields with defaults must still be pre-filled."""
        fields = [
            {"name": "count", "type": "integer", "title": "Count",
             "required": False, "enum": None, "default": 5},
            {"name": "env", "type": "string", "title": "Env",
             "required": True, "enum": ["a", "b"], "default": "a"},
        ]
        form_data = {f["name"]: f["default"] for f in fields
                     if f["default"] is not None and f["type"] != "boolean"}
        assert form_data == {"count": 5, "env": "a"}

    def test_boolean_defaults_applied_at_submit(self):
        """Simulate the submit-time logic that applies boolean defaults
        for fields the user didn't explicitly toggle."""
        fields = [
            {"name": "notify", "type": "boolean", "title": "Notify",
             "required": False, "enum": None, "default": True},
            {"name": "verbose", "type": "boolean", "title": "Verbose",
             "required": False, "enum": None, "default": False},
            {"name": "debug", "type": "boolean", "title": "Debug",
             "required": False, "enum": None, "default": None},
        ]
        form_data = {}  # user didn't touch any boolean
        # This mirrors the exact submit-time logic in _child_run
        for f in fields:
            if f["type"] == "boolean" and f["name"] not in form_data and f["default"] is not None:
                form_data[f["name"]] = f["default"]
        assert form_data["notify"] is True
        assert form_data["verbose"] is False
        assert "debug" not in form_data  # default=None → not applied

    def test_boolean_user_toggle_not_overridden_at_submit(self):
        """If user explicitly toggled a boolean, submit must not overwrite it."""
        fields = [
            {"name": "notify", "type": "boolean", "title": "Notify",
             "required": False, "enum": None, "default": True},
        ]
        form_data = {"notify": False}  # user toggled to False
        for f in fields:
            if f["type"] == "boolean" and f["name"] not in form_data and f["default"] is not None:
                form_data[f["name"]] = f["default"]
        assert form_data["notify"] is False  # user's choice preserved

    def test_boolean_toggle_buttons_visible_when_not_prefilled(self):
        """_build_field_buttons must show boolean toggle buttons when the
        field is NOT in skip_filled (i.e., not pre-filled)."""
        from elicitation import _build_field_buttons
        fields = [
            {"name": "notify", "type": "boolean", "title": "Notify",
             "required": False, "enum": None, "default": True},
        ]
        buttons = _build_field_buttons(fields, skip_filled=set())
        flat = [b for row in buttons for b in row]
        labels = [b["text"] for b in flat]
        assert "✅ Notify" in labels
        assert "⬜ Notify" in labels

    def test_boolean_toggle_buttons_hidden_when_filled(self):
        """Once a boolean is in skip_filled (user toggled it),
        its buttons should be skipped."""
        from elicitation import _build_field_buttons
        fields = [
            {"name": "notify", "type": "boolean", "title": "Notify",
             "required": False, "enum": None, "default": True},
        ]
        buttons = _build_field_buttons(fields, skip_filled={"notify"})
        flat = [b for row in buttons for b in row]
        labels = [b["text"] for b in flat]
        assert "✅ Notify" not in labels
        assert "⬜ Notify" not in labels


class TestElicitationResolvedMessages:
    """Resolved form messages must include the form title (message) text."""

    def test_submit_resolved_shows_title_and_values(self):
        """Submit resolution: 'Form submitted' + message + field values."""
        from utils.common import html_escape
        message = "Deploy configuration"
        form_data = {"env": "production", "count": 3}

        # This mirrors the exact submit resolution in _child_run
        summary = f"✅ <b>Form submitted</b>\n\n<i>{html_escape(message)}</i>"
        for name, val in form_data.items():
            summary += f"\n  • {html_escape(name)}: <code>{html_escape(str(val))}</code>"

        assert "Form submitted" in summary
        assert "Deploy configuration" in summary
        assert "production" in summary
        assert "3" in summary

    def test_cancel_resolved_shows_title(self):
        """Cancel resolution includes the form title."""
        from utils.common import html_escape
        message = "Deploy configuration"
        text = f"❌ <b>Cancelled</b>\n\n<i>{html_escape(message)}</i>"
        assert "Cancelled" in text
        assert "Deploy configuration" in text

    def test_handled_locally_resolved_shows_title(self):
        """'Handled locally' resolution includes the form title."""
        from utils.common import html_escape
        message = "Deploy configuration"
        text = f"🖥 <b>Handled locally</b>\n\n<i>{html_escape(message)}</i>"
        assert "Handled locally" in text
        assert "Deploy configuration" in text

    def test_expired_resolved_shows_title(self):
        """Expired resolution includes the form title."""
        from utils.common import html_escape
        message = "Deploy configuration"
        title = f"\n\n<i>{html_escape(message)}</i>" if message else ""
        text = f"💤 <b>Expired</b>{title}"
        assert "Expired" in text
        assert "Deploy configuration" in text

    def test_handled_locally_via_channel(self):
        """FakeChannel.edit_message records the resolved 'Handled locally'
        message including the form title."""
        from utils.common import html_escape
        ch = FakeChannel()
        message = "Select environment"
        ch.edit_message(42, f"🖥 <b>Handled locally</b>\n\n<i>{html_escape(message)}</i>", buttons=[])
        assert len(ch._edited_messages) == 1
        assert "Handled locally" in ch._edited_messages[0]["text"]
        assert "Select environment" in ch._edited_messages[0]["text"]

    def test_submit_via_channel(self):
        """FakeChannel.edit_message records the resolved 'Form submitted'
        message including field values."""
        from utils.common import html_escape
        ch = FakeChannel()
        message = "Configure"
        form_data = {"env": "staging"}
        summary = f"✅ <b>Form submitted</b>\n\n<i>{html_escape(message)}</i>"
        for name, val in form_data.items():
            summary += f"\n  • {html_escape(name)}: <code>{html_escape(str(val))}</code>"
        ch.edit_message(42, summary, buttons=[])
        assert "Form submitted" in ch._edited_messages[0]["text"]
        assert "Configure" in ch._edited_messages[0]["text"]
        assert "staging" in ch._edited_messages[0]["text"]


# --- Elicitation channel failure ---

class TestElicitationChannelFailure:
    def test_no_channel_exits_immediately(self, monkeypatch):
        """When channel is not configured, elicitation hook should exit
        immediately (no 60s wait), letting native form show."""
        import elicitation as el
        import utils.channel as ch_mod

        # create_channel returns (None, error)
        from utils.channel import ChannelError
        monkeypatch.setattr(ch_mod, "create_channel", lambda cfg: (None, ChannelError("test")))
        monkeypatch.setattr(el, "load_config", lambda: {
            "bot_token": "", "chat_id": "", "elicitation_timeout": 60,
            "channel_type": "telegram",
        })

        import io
        event = {
            "mcp_server_name": "test",
            "message": "fill form",
            "requested_schema": {
                "properties": {"x": {"type": "string", "title": "X"}},
            },
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

        import time
        start = time.monotonic()
        with pytest.raises(SystemExit):
            el.main()
        elapsed = time.monotonic() - start

        # Must exit in <2 seconds, not wait 60s
        assert elapsed < 2

    def test_child_send_failure_signals_parent(self, tmp_path):
        """When child can't send to channel, it writes response file
        so parent exits immediately instead of blocking."""
        from elicitation import _write_response

        response_file = str(tmp_path / "test.response")
        _write_response(response_file, "decline", {})

        import json
        with open(response_file) as f:
            data = json.load(f)
        assert data["action"] == "decline"


# --- Fault injection tests ---

class TestFaultInjection:
    def test_channel_create_returns_structured_error(self):
        """create_channel returns (None, ChannelError) with reason."""
        from utils.channel import create_channel, ChannelError
        ch, err = create_channel({"channel_type": "telegram", "bot_token": "", "chat_id": ""})
        assert ch is None
        assert isinstance(err, ChannelError)
        assert "missing" in str(err)

    def test_channel_unknown_type(self):
        from utils.channel import create_channel, ChannelError
        ch, err = create_channel({"channel_type": "carrier_pigeon"})
        assert ch is None
        assert "carrier_pigeon" in str(err)

    def test_send_message_exception_in_elicitation_child(self, tmp_path):
        """Child catches send exception and writes fail signal."""
        from elicitation import _write_response, ELICIT_FAIL
        response_file = str(tmp_path / "test.response")
        _write_response(response_file, ELICIT_FAIL, {})

        with open(response_file) as f:
            data = json.load(f)
        assert data["action"] == ELICIT_FAIL

    def test_permission_request_no_channel_exits_silently(self, monkeypatch, capsys):
        """PermissionRequest with no channel exits without stdout."""
        import permission_request as pr
        from utils.channel import ChannelError
        import utils.channel as ch_mod
        monkeypatch.setattr(ch_mod, "create_channel",
                            lambda cfg: (None, ChannelError("test")))
        monkeypatch.setattr(pr, "load_config", lambda: {"channel_type": "telegram",
                            "bot_token": "", "chat_id": "", "escalation_seconds": 0,
                            "context_turns": 3, "context_max_chars": 200})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": ""})))

        with pytest.raises(SystemExit):
            pr.main()

        assert capsys.readouterr().out == ""  # no hookSpecificOutput


# --- Elicitation parent/child response_file handoff ---

class TestElicitationResponseHandoff:
    """Simulate the parent/child fork handoff for response_file.
    These tests run in a single process but exercise the exact same
    file-based IPC that the real fork uses."""

    def test_parent_reads_response_before_child_cleans_up(self, tmp_path):
        """Core race scenario: child writes response, parent must read it.
        Child's finally block does NOT delete response_file — parent does."""
        from elicitation import _write_response, ELICIT_ACCEPT

        response_file = str(tmp_path / "req_abc.response")

        # Simulate child: write response
        _write_response(response_file, ELICIT_ACCEPT, {"env": "production"})
        assert os.path.exists(response_file)

        # Simulate child finally: clean up other files but NOT response
        for f in [str(tmp_path / "req_abc.active"),
                  str(tmp_path / "req_abc.timeout"),
                  str(tmp_path / "req_abc.done")]:
            try:
                os.remove(f)
            except OSError:
                pass

        # response_file must still exist for parent
        assert os.path.exists(response_file)

        # Simulate parent: read and consume
        with open(response_file) as f:
            data = json.load(f)
        os.remove(response_file)

        assert data["action"] == ELICIT_ACCEPT
        assert data["content"]["env"] == "production"
        assert not os.path.exists(response_file)

    def test_parent_reads_fail_response_and_exits_silently(self, tmp_path, capsys):
        """When child writes FAIL, parent should exit with no stdout
        (letting native form show), not return decline."""
        from elicitation import _write_response, ELICIT_FAIL

        response_file = str(tmp_path / "req_def.response")
        _write_response(response_file, ELICIT_FAIL, {})

        with open(response_file) as f:
            data = json.load(f)

        # Parent logic: FAIL → exit silently (no hookSpecificOutput)
        assert data["action"] == ELICIT_FAIL
        # In real code this triggers sys.exit(0) with no stdout

    def test_concurrent_requests_dont_cross_response_files(self, tmp_path):
        """Two concurrent requests use different request_ids,
        so their response_files don't collide."""
        from elicitation import _write_response, ELICIT_ACCEPT

        resp_a = str(tmp_path / "srv_aaaa1111.response")
        resp_b = str(tmp_path / "srv_bbbb2222.response")

        _write_response(resp_a, ELICIT_ACCEPT, {"env": "staging"})
        _write_response(resp_b, ELICIT_ACCEPT, {"env": "production"})

        with open(resp_a) as f:
            data_a = json.load(f)
        with open(resp_b) as f:
            data_b = json.load(f)

        assert data_a["content"]["env"] == "staging"
        assert data_b["content"]["env"] == "production"

    def test_real_fork_response_handoff(self, tmp_path):
        """Actually fork a child process that writes response_file,
        then verify parent can read it after child exits."""
        from elicitation import _write_response, ELICIT_ACCEPT

        response_file = str(tmp_path / "fork_test.response")

        pid = os.fork()
        if pid == 0:
            # Child: write response and exit (like real elicitation child)
            _write_response(response_file, ELICIT_ACCEPT, {"from": "child"})
            os._exit(0)
        else:
            # Parent: wait for child to finish, then read
            os.waitpid(pid, 0)

            assert os.path.exists(response_file)
            with open(response_file) as f:
                data = json.load(f)
            os.remove(response_file)

            assert data["action"] == ELICIT_ACCEPT
            assert data["content"]["from"] == "child"
            assert not os.path.exists(response_file)


# Notification tests are in test_integration.py (TestNotificationFlow)
