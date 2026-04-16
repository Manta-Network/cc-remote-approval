"""
Telegram integration tests — inherits shared scenarios,
provides FakeTelegram fixture from conftest.
"""
import os
import sys
import time
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenarios import (ApprovalScenarios, AskUserQuestionScenarios,
                        LocalResponseScenarios, LongValueScenarios)
from telegram.conftest import FakeTelegram


def _make_fast_clock():
    counter = [0]
    def fast_monotonic():
        counter[0] += 1
        return counter[0] if counter[0] <= 50 else 99999
    return fast_monotonic


@pytest.fixture
def channel_env(tmp_path, monkeypatch):
    """Provide FakeTelegram as channel_env for shared scenarios."""
    fake = FakeTelegram()
    import channels.telegram.client as tg_client
    import channels.telegram.poll as tg_poll
    import utils.common as common
    monkeypatch.setattr(tg_client, "tg_request", fake)
    tg_poll.LOCK_PATH = str(tmp_path / "poll.lock")
    tg_poll.OFFSET_PATH = str(tmp_path / "offset")
    tg_poll.PENDING_PATH = str(tmp_path / "pending.json")
    monkeypatch.setattr(common, "TG_POLL_DIR", str(tmp_path))
    monkeypatch.setattr(time, "sleep", lambda x: None)
    monkeypatch.setattr(time, "monotonic", _make_fast_clock())
    return fake


# Inherit all shared scenarios — each gets channel_env from fixture above

class TestTelegramApproval(ApprovalScenarios):
    pass

class TestTelegramAskUserQuestion(AskUserQuestionScenarios):
    pass

class TestTelegramLocalResponse(LocalResponseScenarios):
    pass

class TestTelegramLongValues(LongValueScenarios):
    pass


# Telegram-specific tests (not shared with other channels)

class TestTelegramConcurrentPolling:
    """Tests specific to Telegram's getUpdates + pending queue mechanism."""

    def test_two_callbacks_dont_cross(self, channel_env, tmp_path, monkeypatch):
        from channels.telegram.poll import poll_once
        channel_env.queue_callback(msg_id=100, data="allow")
        channel_env.queue_callback(msg_id=200, data="deny")

        r1 = poll_once("tok", 100, "123", channel_env)
        assert r1["data"] == "allow"
        r2 = poll_once("tok", 200, "123", channel_env)
        assert r2["data"] == "deny"

    def test_text_replies_bound_to_correct_hook(self, channel_env, tmp_path, monkeypatch):
        from channels.telegram.poll import poll_once
        channel_env.queue_text(msg_id=100, text="answer A")
        channel_env.queue_text(msg_id=200, text="answer B")

        r1 = poll_once("tok", 100, "123", channel_env)
        assert r1["text"] == "answer A"
        r2 = poll_once("tok", 200, "123", channel_env)
        assert r2["text"] == "answer B"


class TestTelegramSendReplyPrompt:
    """TelegramChannel.send_reply_prompt sends with ForceReply markup
    and returns the prompt's new msg_id."""

    def test_sends_force_reply_markup_by_default(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        prompt_id = ch.send_reply_prompt(100, "Type answer")

        send_calls = fake.get_calls("sendMessage")
        assert len(send_calls) == 1
        data = send_calls[0][1]
        assert data["reply_to_message_id"] == 100
        assert data["reply_markup"]["force_reply"] is True
        assert prompt_id == fake.msg_counter  # the newly assigned id

    def test_can_disable_force_reply(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.send_reply_prompt(100, "Type answer", force_reply=False)

        data = fake.get_calls("sendMessage")[0][1]
        assert "reply_markup" not in data


class TestTelegramEditMessageButtons:
    """edit_message(buttons=[]) explicitly clears the inline keyboard;
    buttons=None keeps existing; buttons=[[...]] sets new.
    Clearing matters on resolve so users don't tap stale Allow/Deny
    buttons that would route nowhere."""

    def test_buttons_none_keeps_existing(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.edit_message(100, "new text")  # buttons=None (default)

        data = fake.get_calls("editMessageText")[0][1]
        assert "reply_markup" not in data  # absent = keep existing

    def test_buttons_empty_clears(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.edit_message(100, "resolved", buttons=[])

        data = fake.get_calls("editMessageText")[0][1]
        assert data["reply_markup"] == {"inline_keyboard": []}

    def test_buttons_new_replaces(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.edit_message(100, "updated", buttons=[[{"text": "ok", "callback_data": "x"}]])

        data = fake.get_calls("editMessageText")[0][1]
        assert data["reply_markup"]["inline_keyboard"][0][0]["text"] == "ok"


class TestTelegramChatIdAssertion:
    """TelegramChannel._send rejects any data that would target a chat_id
    other than the configured one. Defense-in-depth against future
    refactors that accidentally accept external chat_ids."""

    def test_matching_chat_id_ok(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.send_notification("hello")  # uses self.chat_id

        assert len(fake.get_calls("sendMessage")) == 1

    def test_mismatched_chat_id_raises(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        with pytest.raises(RuntimeError, match="chat_id mismatch"):
            ch._send("sendMessage", {"chat_id": "999", "text": "forbidden"})

    def test_numeric_and_string_chat_ids_compared_as_strings(self, monkeypatch):
        """Config stores chat_id as string; API responses may use int.
        String equality after str() coercion must treat 123 == '123'."""
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch._send("sendMessage", {"chat_id": 123, "text": "ok"})  # int, not str

        assert len(fake.get_calls("sendMessage")) == 1


class TestTelegramSendMessageErrors:
    """send_message intentionally propagates transport errors so the
    hook's SEND FAILED log captures the real cause (HTTPError 401,
    URLError, etc.) instead of a downstream 'NoneType' crash."""

    def test_send_message_raises_on_transport_failure(self, monkeypatch):
        import channels.telegram.client as tg_client

        def failing_request(token, method, data=None):
            raise RuntimeError("HTTPError 401 Unauthorized")

        monkeypatch.setattr(tg_client, "tg_request", failing_request)
        ch = tg_client.TelegramChannel("tok", "123")

        with pytest.raises(RuntimeError, match="HTTPError 401"):
            ch.send_message("hi")

    def test_send_reply_prompt_still_returns_none_on_failure(self, monkeypatch):
        """Fire-and-forget helpers keep swallowing: callers check for None
        rather than try/except. send_reply_prompt is in that category."""
        import channels.telegram.client as tg_client

        def failing_request(token, method, data=None):
            raise RuntimeError("network down")

        monkeypatch.setattr(tg_client, "tg_request", failing_request)
        ch = tg_client.TelegramChannel("tok", "123")
        assert ch.send_reply_prompt(100, "x") is None

    def test_edit_message_still_swallows(self, monkeypatch):
        """edit_message is fire-and-forget — transient failures must not
        bubble to the caller (no hook should fail its resolve path just
        because editing the status message timed out)."""
        import channels.telegram.client as tg_client

        def failing_request(token, method, data=None):
            raise RuntimeError("timeout")

        monkeypatch.setattr(tg_client, "tg_request", failing_request)
        ch = tg_client.TelegramChannel("tok", "123")
        ch.edit_message(100, "new text")  # must not raise
        ch.send_notification("hi")         # must not raise
        ch.delete_message(100)             # must not raise
        ch.edit_buttons(100, [])           # must not raise


class TestTelegramDeleteMessage:
    """TelegramChannel.delete_message calls deleteMessage with the right args."""

    def test_delete_message_calls_api(self, monkeypatch):
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client
        fake = FakeTelegram()
        monkeypatch.setattr(tg_client, "tg_request", fake)

        ch = TelegramChannel("tok", "123")
        ch.delete_message(42)

        delete_calls = fake.get_calls("deleteMessage")
        assert len(delete_calls) == 1
        assert delete_calls[0][1]["chat_id"] == "123"
        assert delete_calls[0][1]["message_id"] == 42

    def test_delete_message_swallows_errors(self, monkeypatch):
        """deleteMessage failure (e.g., message too old) must not propagate."""
        from channels.telegram.client import TelegramChannel
        import channels.telegram.client as tg_client

        def failing_request(*args, **kwargs):
            raise RuntimeError("API error")

        monkeypatch.setattr(tg_client, "tg_request", failing_request)
        ch = TelegramChannel("tok", "123")
        ch.delete_message(42)  # should not raise


class TestTelegramNotification:
    def test_idle_sends_message(self, monkeypatch):
        import utils.common as common
        import channels.telegram.client as tg_client
        import notification as n
        sent = []
        monkeypatch.setattr(tg_client, "tg_request",
            lambda *a, **kw: sent.append(a) or {"ok": True})
        monkeypatch.setattr(n, "load_config", lambda: {
            "bot_token": "tok", "chat_id": "123",
            **{k: v for k, v in common.DEFAULTS.items() if k not in ("bot_token", "chat_id")}})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"notification_type": "idle_prompt", "transcript_path": ""})))
        try:
            n.main()
        except SystemExit:
            pass
        assert len(sent) >= 1

    def test_logs_channel_unavailable_before_exit(self, monkeypatch, capsys):
        """Parity with permission_request / elicitation: misconfigured
        channel must leave a 'Channel unavailable' log so notification-only
        setup failures are debuggable from the log file."""
        import notification as n

        logs = []
        monkeypatch.setattr(n, "_log", lambda msg: logs.append(msg))
        # Empty credentials → create_channel returns (None, ChannelError).
        monkeypatch.setattr(n, "load_config", lambda: {
            "channel_type": "telegram", "bot_token": "", "chat_id": ""})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"notification_type": "idle_prompt", "transcript_path": ""})))

        with pytest.raises(SystemExit):
            n.main()

        assert any("Channel unavailable" in m for m in logs), \
            f"expected 'Channel unavailable' log, got: {logs}"
        # No stdout — notification is fire-and-forget
        assert capsys.readouterr().out == ""

    def test_notification_includes_session_tag_from_cwd(self, monkeypatch):
        """When multiple CC sessions share a single TG chat, the notification
        message needs a session tag so the reader can tell which project
        the 'agent idle' alert came from. We derive it from cwd basename."""
        import utils.common as common
        import channels.telegram.client as tg_client
        import notification as n
        sent_data = []
        monkeypatch.setattr(tg_client, "tg_request",
            lambda token, method, data=None: sent_data.append(data) or {"ok": True})
        monkeypatch.setattr(n, "load_config", lambda: {
            "bot_token": "tok", "chat_id": "123",
            **{k: v for k, v in common.DEFAULTS.items() if k not in ("bot_token", "chat_id")}})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"notification_type": "idle_prompt",
             "transcript_path": "",
             "cwd": "/Users/alice/code/cool-project"})))
        try:
            n.main()
        except SystemExit:
            pass
        assert any("cool-project" in d.get("text", "") for d in sent_data)

    def test_permission_prompt_is_suppressed(self, monkeypatch):
        """permission_prompt fires in parallel with PermissionRequest
        (which already sends an actionable button UI). Forwarding the
        plain-text Notification would duplicate with less detail, so
        notification.py intentionally does NOT send for this type."""
        import utils.common as common
        import channels.telegram.client as tg_client
        import notification as n
        sent = []
        logs = []
        monkeypatch.setattr(n, "_log", lambda msg: logs.append(msg))
        monkeypatch.setattr(tg_client, "tg_request",
            lambda *a, **kw: sent.append(a) or {"ok": True})
        monkeypatch.setattr(n, "load_config", lambda: {
            "bot_token": "tok", "chat_id": "123",
            **{k: v for k, v in common.DEFAULTS.items() if k not in ("bot_token", "chat_id")}})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"notification_type": "permission_prompt",
             "message": "Claude needs your permission to use Bash",
             "transcript_path": ""})))

        try:
            n.main()
        except SystemExit:
            pass

        # Must not send — PermissionRequest hook handles this path.
        assert not any(c[1] == "sendMessage" for c in sent), \
            "permission_prompt must not trigger a Telegram send"

    def test_logs_unknown_notification_type(self, monkeypatch):
        """An unknown notification_type shouldn't crash but also shouldn't
        be invisible — log so we can see if Claude Code adds a new type
        we haven't mapped."""
        import utils.common as common
        import channels.telegram.client as tg_client
        import notification as n
        logs = []
        monkeypatch.setattr(n, "_log", lambda msg: logs.append(msg))
        monkeypatch.setattr(tg_client, "tg_request",
            lambda *a, **kw: {"ok": True})
        monkeypatch.setattr(n, "load_config", lambda: {
            "bot_token": "tok", "chat_id": "123",
            **{k: v for k, v in common.DEFAULTS.items() if k not in ("bot_token", "chat_id")}})
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
            {"notification_type": "brand_new_type", "transcript_path": ""})))

        with pytest.raises(SystemExit):
            n.main()

        assert any("brand_new_type" in m for m in logs)
