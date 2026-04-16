"""
Shared integration test scenarios + FakeChannel mock.

FakeChannel: generic mock that any channel fixture can use or extend.
Scenario base classes: inherit + provide `channel_env` fixture.
"""
import json
import io
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))


# ================================================================
# FakeChannel — generic mock for all channels
# ================================================================

class FakeChannel:
    """In-memory channel mock. Use directly or extend for channel-specific behavior."""

    def __init__(self):
        self.msg_counter = 1000
        self._sent_messages = []
        self._edited_messages = []
        self._deleted_messages = []
        self._on_send_callback = None

    def on_send(self, callback):
        """Register callback(msg_id, data) fired when a message is sent."""
        self._on_send_callback = callback

    def _do_send(self, msg_id, text, **extra):
        """Record a sent message and fire on_send callback."""
        entry = {"msg_id": msg_id, "text": text, **extra}
        self._sent_messages.append(entry)
        if self._on_send_callback:
            self._on_send_callback(msg_id, entry)

    def send_message(self, text, buttons=None, parse_mode="HTML"):
        """Simulate sending a message. Returns msg_id."""
        self.msg_counter += 1
        self._do_send(self.msg_counter, text, buttons=buttons)
        return self.msg_counter

    def edit_message(self, msg_id, text, buttons=None, parse_mode="HTML"):
        """Simulate editing a message."""
        self._edited_messages.append({"msg_id": msg_id, "text": text})

    def edit_buttons(self, msg_id, buttons):
        """Simulate editing message buttons."""
        self._edited_messages.append({"msg_id": msg_id, "buttons": buttons})

    def delete_message(self, msg_id):
        """Simulate deleting a message."""
        self._deleted_messages.append(msg_id)

    def poll(self, msg_id):
        return None

    def send_notification(self, text, parse_mode="HTML"):
        self._do_send(0, text, notification=True)

    def send_reply_prompt(self, msg_id, text, force_reply=True):
        self.msg_counter += 1
        prompt_id = self.msg_counter
        self._do_send(prompt_id, text, reply_to=msg_id, force_reply=force_reply)
        return prompt_id

    def queue_callback(self, msg_id, data):
        """Override in subclass to route callbacks to the right polling mechanism."""
        raise NotImplementedError("Subclass must implement queue_callback")

    def queue_text(self, msg_id, text):
        """Override in subclass to route text replies to the right polling mechanism."""
        raise NotImplementedError("Subclass must implement queue_text")

    @property
    def last_sent(self):
        return self._sent_messages[-1] if self._sent_messages else None

    @property
    def last_edit(self):
        return self._edited_messages[-1] if self._edited_messages else None

    @property
    def sent_count(self):
        return len(self._sent_messages)


def _setup_hook(monkeypatch, event, **cfg_overrides):
    import permission_request as pr
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(pr, "load_config", lambda: _test_config(**cfg_overrides))


def _run_hook():
    import permission_request as pr
    try:
        pr.main()
    except SystemExit:
        pass


def _test_config(**overrides):
    from utils.common import DEFAULTS
    cfg = dict(DEFAULTS)
    cfg["bot_token"] = "test-token"
    cfg["chat_id"] = "123"
    cfg["escalation_seconds"] = 0
    cfg.update(overrides)
    return cfg


# ================================================================
# Approval scenarios (Bash / Edit / Write / WebFetch)
# ================================================================

class ApprovalScenarios:
    """Inherit and provide `channel_env` fixture to test any channel."""

    def test_bash_allow(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "allow"))
        _setup_hook(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls -la"},
                                   "transcript_path": "", "permission_suggestions": [{"type": "addRules"}]})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert channel_env.last_sent is not None
        assert "ls -la" in channel_env.last_sent["text"]

    def test_bash_deny(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "deny"))
        _setup_hook(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
                                   "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        assert "Denied" in channel_env.last_edit["text"]

    def test_bash_always(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "always"))
        perms = [{"type": "addRules", "rules": [{"toolName": "Bash"}]}]
        _setup_hook(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                   "transcript_path": "", "permission_suggestions": perms})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert result["hookSpecificOutput"]["decision"]["updatedPermissions"] == perms

    def test_bash_timeout(self, channel_env, monkeypatch, capsys):
        # No callback queued — fast clock jumps past POLL_TIMEOUT_SECONDS
        import utils.common as common
        import permission_request as pr
        monkeypatch.setattr(common, "POLL_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(pr, "POLL_TIMEOUT_SECONDS", 1)
        times = iter([0] * 5 + [0, 0] + [99999] * 50)
        monkeypatch.setattr(time, "monotonic", lambda: next(times, 99999))
        _setup_hook(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                   "transcript_path": ""}, escalation_seconds=0)
        _run_hook()
        out = capsys.readouterr().out
        assert out == "" or "timeout" not in out

    def test_edit_allow(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "allow"))
        _setup_hook(monkeypatch, {"tool_name": "Edit", "tool_input": {"file_path": "/home/user/app.py"},
                                   "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert "/home/user/app.py" in channel_env.last_sent["text"]


# ================================================================
# AskUserQuestion scenarios
# ================================================================

class AskUserQuestionScenarios:
    """Inherit and provide `channel_env` fixture."""

    def test_single_select(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "opt:0"))
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which database?", "header": "DB",
                                          "multiSelect": False,
                                          "options": [{"label": "PostgreSQL", "description": ""},
                                                      {"label": "MongoDB", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert "PostgreSQL" in json.dumps(result)

    def test_text_reply(self, channel_env, monkeypatch, capsys):
        channel_env.on_send(lambda msg_id, data: channel_env.queue_text(msg_id, "Redis"))
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which database?", "header": "DB",
                                          "multiSelect": False,
                                          "options": [{"label": "PostgreSQL", "description": ""},
                                                      {"label": "MySQL", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        assert "Redis" in json.dumps(result)

    def test_other_button_then_text_reply_to_prompt(self, channel_env, monkeypatch, capsys):
        """Regression: user clicks 'Other' → we send a ForceReply prompt;
        user's reply quotes the prompt (not the original question), and
        we still route it correctly.

        Before the ForceReply fix, the prompt's msg_id wasn't tracked, so a
        natural swipe-reply on the prompt was dropped by _extract_text."""
        state = {"other_sent": False, "prompt_id": None}

        def on_send(msg_id, data):
            # First send is the question — respond with "Other" button click.
            if not state["other_sent"]:
                state["other_sent"] = True
                channel_env.queue_callback(msg_id, "opt:other")
            else:
                # Second send is our ForceReply prompt — remember its id and
                # queue the user's text as a quote-reply to THIS prompt,
                # which is what a real TG client does under ForceReply.
                state["prompt_id"] = msg_id
                channel_env.queue_text(msg_id, "Custom DB Name")

        channel_env.on_send(on_send)
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which database?", "header": "DB",
                                          "multiSelect": False,
                                          "options": [{"label": "PostgreSQL", "description": ""},
                                                      {"label": "MySQL", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        # The text reply — bound to the prompt's msg_id, not the question's —
        # must still make it back as the answer.
        assert "Custom DB Name" in json.dumps(result)
        assert state["prompt_id"] is not None, "ForceReply prompt was never sent"
        # Prompt must be cleaned up on resolve so it doesn't linger with a
        # dangling ForceReply lock.
        assert state["prompt_id"] in channel_env._deleted_messages

    def test_other_prompt_cleaned_up_on_timeout(self, channel_env, monkeypatch, capsys):
        """If user clicks Other then never replies, the prompt still gets
        deleted when the hook times out."""
        # Force a short poll timeout so the timeout branch fires quickly.
        import utils.common as common
        import permission_request as pr
        monkeypatch.setattr(common, "POLL_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(pr, "POLL_TIMEOUT_SECONDS", 1)
        # Slow first few ticks so setup runs, then jump past deadline.
        times = iter([0] * 10 + [99999] * 100)
        monkeypatch.setattr(time, "monotonic", lambda: next(times, 99999))

        state = {"other_sent": False, "prompt_id": None}

        def on_send(msg_id, data):
            if not state["other_sent"]:
                state["other_sent"] = True
                channel_env.queue_callback(msg_id, "opt:other")
            else:
                # Remember prompt id but queue NO reply — hook will time out.
                state["prompt_id"] = msg_id

        channel_env.on_send(on_send)
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which database?", "header": "DB",
                                          "multiSelect": False,
                                          "options": [{"label": "PostgreSQL", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        assert state["prompt_id"] is not None, "ForceReply prompt was never sent"
        assert state["prompt_id"] in channel_env._deleted_messages

    def test_multi_select_submit(self, channel_env, monkeypatch, capsys):
        def on_send(msg_id, data):
            channel_env.queue_callback(msg_id, "opt:0")
            channel_env.queue_callback(msg_id, "opt:2")
            channel_env.queue_callback(msg_id, "opt:submit")
        channel_env.on_send(on_send)
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Features?", "header": "F",
                                          "multiSelect": True,
                                          "options": [{"label": "A", "description": ""},
                                                      {"label": "B", "description": ""},
                                                      {"label": "C", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        answer = json.dumps(result)
        assert "A" in answer and "C" in answer


# ================================================================
# Long value round-trip (callback short ID regression)
# ================================================================

class LongValueScenarios:
    """Verify that options/enums with long names survive the callback round-trip.
    Regression: old design put real values in callback_data, got truncated at 64 bytes."""

    def test_long_option_label_survives_roundtrip(self, channel_env, monkeypatch, capsys):
        """Option label > 64 bytes must come back intact via index lookup."""
        long_label = "PostgreSQL Enterprise Edition with Advanced Security Pack and Monitoring v12.3.1"
        assert len(long_label.encode("utf-8")) > 64  # confirm it would have been truncated under old design

        channel_env.on_send(lambda msg_id, data: channel_env.queue_callback(msg_id, "opt:0"))
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which DB?", "header": "DB",
                                          "multiSelect": False,
                                          "options": [{"label": long_label, "description": ""},
                                                      {"label": "SQLite", "description": ""}]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        # Full label must be in the response, not a truncated version
        assert long_label in json.dumps(result)

    def test_long_option_multiselect_roundtrip(self, channel_env, monkeypatch, capsys):
        """Multi-select with long labels — indices map back to full values."""
        labels = [
            "Enable comprehensive logging and monitoring for all services",
            "Short",
            "Configure automated database backup with point-in-time recovery",
        ]
        def on_send(msg_id, data):
            channel_env.queue_callback(msg_id, "opt:0")  # long label
            channel_env.queue_callback(msg_id, "opt:2")  # long label
            channel_env.queue_callback(msg_id, "opt:submit")
        channel_env.on_send(on_send)
        _setup_hook(monkeypatch, {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Features?", "header": "F",
                                          "multiSelect": True,
                                          "options": [{"label": l, "description": ""} for l in labels]}]},
            "transcript_path": ""})
        _run_hook()
        result = json.loads(capsys.readouterr().out)
        answer = json.dumps(result)
        assert labels[0] in answer
        assert labels[2] in answer


# ================================================================
# Local response scenario
# ================================================================

class LocalResponseScenarios:
    """Inherit and provide `channel_env` fixture."""

    def test_transcript_growth_skips_channel(self, channel_env, monkeypatch, capsys, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 100)

        import permission_request as pr
        monkeypatch.setattr(pr, "load_config", lambda: _test_config(escalation_seconds=10))

        # Slow clock so escalation loop actually runs and checks transcript
        counter = [0]
        monkeypatch.setattr(time, "monotonic", lambda: counter[0])

        sleep_count = [0]
        def fake_sleep(n):
            sleep_count[0] += 1
            counter[0] += 1  # advance clock
            if sleep_count[0] >= 2:
                transcript.write_text("x" * 500)
        monkeypatch.setattr(time, "sleep", fake_sleep)

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"},
             "transcript_path": str(transcript)})))
        _run_hook()
        assert channel_env.sent_count == 0
