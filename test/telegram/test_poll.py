"""Tests for channels/telegram/poll.py — coordinated Telegram polling."""
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from telegram.conftest import FakeTelegram


# --- Helpers ---

def make_callback(update_id, msg_id, data, cb_id="cb1"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cb_id,
            "data": data,
            "message": {"message_id": msg_id},
        },
    }


def make_text(update_id, chat_id, text, reply_to_msg_id=None):
    msg = {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id},
        },
    }
    if reply_to_msg_id is not None:
        msg["message"]["reply_to_message"] = {"message_id": reply_to_msg_id}
    return msg


@pytest.fixture
def poll_dir(tmp_path):
    """Override TG_POLL_DIR to use a temp directory."""
    import channels.telegram.poll as tg_poll
    orig_lock = tg_poll.LOCK_PATH
    orig_offset = tg_poll.OFFSET_PATH
    orig_pending = tg_poll.PENDING_PATH

    tg_poll.LOCK_PATH = str(tmp_path / "poll.lock")
    tg_poll.OFFSET_PATH = str(tmp_path / "offset")
    tg_poll.PENDING_PATH = str(tmp_path / "pending.json")

    # Also patch TG_POLL_DIR for makedirs
    import utils.common as common
    orig_dir = common.TG_POLL_DIR
    common.TG_POLL_DIR = str(tmp_path)

    yield tmp_path

    tg_poll.LOCK_PATH = orig_lock
    tg_poll.OFFSET_PATH = orig_offset
    tg_poll.PENDING_PATH = orig_pending
    common.TG_POLL_DIR = orig_dir


# --- Tests ---

class TestPollOnceCallback:
    """Callback query routing by msg_id."""

    def test_returns_matching_callback(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_callback(1, msg_id=100, data="allow")])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is not None
        assert result["type"] == "callback"
        assert result["data"] == "allow"

    def test_ignores_callback_for_different_msg_id(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_callback(1, msg_id=200, data="deny")])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is None

    def test_non_matching_callback_goes_to_pending(self, poll_dir):
        from channels.telegram.poll import poll_once, _load_pending
        fake = FakeTelegram([make_callback(1, msg_id=200, data="deny")])

        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        pending = _load_pending()

        assert len(pending) == 1
        assert pending[0]["callback_query"]["message"]["message_id"] == 200

    def test_second_hook_picks_up_from_pending(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_callback(1, msg_id=200, data="deny")])

        # Hook A doesn't match
        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        # Hook B matches
        result = poll_once("tok", my_msg_id=200, chat_id="999", tg_request_fn=fake)

        assert result is not None
        assert result["data"] == "deny"


class TestPollOnceText:
    """Text message routing by reply_to_message."""

    def test_returns_text_reply_to_my_message(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_text(1, chat_id=999, text="hello", reply_to_msg_id=100)])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is not None
        assert result["type"] == "text"
        assert result["text"] == "hello"

    def test_ignores_text_without_reply_to(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_text(1, chat_id=999, text="hello")])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is None

    def test_ignores_text_reply_to_different_message(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_text(1, chat_id=999, text="hello", reply_to_msg_id=200)])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is None

    def test_text_reply_to_wrong_msg_stays_in_pending(self, poll_dir):
        from channels.telegram.poll import poll_once, _load_pending
        fake = FakeTelegram([make_text(1, chat_id=999, text="for hook B", reply_to_msg_id=200)])

        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        pending = _load_pending()

        assert len(pending) == 1


class TestConcurrent:
    """Two hooks polling simultaneously don't lose messages."""

    def test_two_callbacks_for_different_hooks(self, poll_dir):
        from channels.telegram.poll import poll_once
        # Both callbacks arrive in one getUpdates batch
        fake = FakeTelegram([
            make_callback(1, msg_id=100, data="allow", cb_id="cb1"),
            make_callback(2, msg_id=200, data="deny", cb_id="cb2"),
        ])

        # Hook A gets its callback
        r1 = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        assert r1["data"] == "allow"

        # Hook B gets its callback from pending
        r2 = poll_once("tok", my_msg_id=200, chat_id="999", tg_request_fn=fake)
        assert r2["data"] == "deny"

    def test_two_text_replies_for_different_hooks(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([
            make_text(1, chat_id=999, text="answer A", reply_to_msg_id=100),
            make_text(2, chat_id=999, text="answer B", reply_to_msg_id=200),
        ])

        r1 = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        assert r1["text"] == "answer A"

        r2 = poll_once("tok", my_msg_id=200, chat_id="999", tg_request_fn=fake)
        assert r2["text"] == "answer B"

    def test_mixed_callback_and_text(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([
            make_callback(1, msg_id=100, data="allow", cb_id="cb1"),
            make_text(2, chat_id=999, text="my answer", reply_to_msg_id=200),
        ])

        r1 = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)
        assert r1["type"] == "callback"

        r2 = poll_once("tok", my_msg_id=200, chat_id="999", tg_request_fn=fake)
        assert r2["type"] == "text"
        assert r2["text"] == "my answer"


class TestPollOnceMultipleMsgIds:
    """my_msg_id accepts a list — supports AskUserQuestion "Other" where
    both the question msg_id and the follow-up ForceReply prompt msg_id
    count as legitimate reply anchors."""

    def test_callback_matches_first_of_list(self, poll_dir):
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_callback(1, msg_id=100, data="opt:0")])

        result = poll_once("tok", my_msg_id=[100, 101], chat_id="999",
                           tg_request_fn=fake)

        assert result is not None
        assert result["type"] == "callback"
        assert result["data"] == "opt:0"

    def test_text_reply_matches_second_of_list(self, poll_dir):
        """The real "Other" scenario: user's text replies to the prompt
        (the second id), not the original question."""
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_text(1, chat_id=999, text="my answer",
                                        reply_to_msg_id=101)])

        result = poll_once("tok", my_msg_id=[100, 101], chat_id="999",
                           tg_request_fn=fake)

        assert result is not None
        assert result["type"] == "text"
        assert result["text"] == "my answer"

    def test_none_of_list_matches_stays_in_pending(self, poll_dir):
        from channels.telegram.poll import poll_once, _load_pending
        fake = FakeTelegram([make_text(1, chat_id=999, text="not for us",
                                        reply_to_msg_id=999)])

        result = poll_once("tok", my_msg_id=[100, 101], chat_id="999",
                           tg_request_fn=fake)

        assert result is None
        assert len(_load_pending()) == 1

    def test_single_int_still_works(self, poll_dir):
        """Back-compat: callers passing a bare int (not a list) keep working."""
        from channels.telegram.poll import poll_once
        fake = FakeTelegram([make_callback(1, msg_id=100, data="allow")])

        result = poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert result is not None
        assert result["data"] == "allow"


class TestOffset:
    """Offset advances correctly across polls."""

    def test_offset_persists_between_polls(self, poll_dir):
        from channels.telegram.poll import poll_once, _load_offset
        fake = FakeTelegram([make_callback(5, msg_id=100, data="allow")])

        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        assert _load_offset() == 6

    def test_offset_sent_in_getUpdates(self, poll_dir):
        from channels.telegram.poll import poll_once, _save_offset
        _save_offset(10)
        fake = FakeTelegram([])

        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        get_updates_call = [c for c in fake.calls if c[0] == "getUpdates"]
        assert get_updates_call[0][1]["offset"] == 10


class TestCorruptStateRecovery:
    """When pending.json or offset is corrupted, archive and start fresh
    rather than silently overwriting (so we can postmortem the cause)."""

    def test_corrupt_pending_json_archived(self, poll_dir):
        import glob
        from channels.telegram.poll import _load_pending, PENDING_PATH

        with open(PENDING_PATH, "w") as f:
            f.write("{not valid json")

        result = _load_pending()
        assert result == []
        # Original file got renamed aside with .corrupt-<ts> suffix
        corrupts = glob.glob(f"{PENDING_PATH}.corrupt-*")
        assert len(corrupts) == 1
        with open(corrupts[0]) as f:
            assert f.read() == "{not valid json"
        # And the original path is gone (ready for fresh writes)
        assert not os.path.exists(PENDING_PATH)

    def test_missing_pending_returns_empty_without_archiving(self, poll_dir):
        import glob
        from channels.telegram.poll import _load_pending, PENDING_PATH
        # Ensure no pending file exists
        if os.path.exists(PENDING_PATH):
            os.remove(PENDING_PATH)

        result = _load_pending()
        assert result == []
        corrupts = glob.glob(f"{PENDING_PATH}.corrupt-*")
        assert len(corrupts) == 0  # missing != corrupt

    def test_corrupt_offset_archived(self, poll_dir):
        import glob
        from channels.telegram.poll import _load_offset, OFFSET_PATH

        with open(OFFSET_PATH, "w") as f:
            f.write("not an integer")

        result = _load_offset()
        assert result == 0
        corrupts = glob.glob(f"{OFFSET_PATH}.corrupt-*")
        assert len(corrupts) == 1

    def test_schema_valid_but_type_invalid_pending_archived(self, poll_dir):
        """JSON-valid-but-type-invalid pending.json — {}, [1, 2], null —
        would crash the TTL prune step on u.get(). Treat as corrupt."""
        import glob
        import json
        from channels.telegram.poll import _load_pending, PENDING_PATH

        # dict instead of list of dicts
        with open(PENDING_PATH, "w") as f:
            json.dump({"key": "value"}, f)
        assert _load_pending() == []
        assert len(glob.glob(f"{PENDING_PATH}.corrupt-*")) == 1

    def test_list_of_non_dicts_archived(self, poll_dir):
        import glob
        import json
        from channels.telegram.poll import _load_pending, PENDING_PATH

        with open(PENDING_PATH, "w") as f:
            json.dump([1, 2, 3], f)
        assert _load_pending() == []
        assert len(glob.glob(f"{PENDING_PATH}.corrupt-*")) >= 1

    def test_null_pending_archived(self, poll_dir):
        import glob
        import json
        from channels.telegram.poll import _load_pending, PENDING_PATH

        with open(PENDING_PATH, "w") as f:
            json.dump(None, f)
        assert _load_pending() == []
        assert len(glob.glob(f"{PENDING_PATH}.corrupt-*")) >= 1

    def test_valid_empty_list_not_archived(self, poll_dir):
        """An empty list is valid state — don't archive it."""
        import glob
        import json
        from channels.telegram.poll import _load_pending, PENDING_PATH

        with open(PENDING_PATH, "w") as f:
            json.dump([], f)
        assert _load_pending() == []
        assert len(glob.glob(f"{PENDING_PATH}.corrupt-*")) == 0

    def test_poll_survives_corrupted_pending(self, poll_dir):
        """End-to-end: poll_once() does not crash when pending.json is
        type-invalid at startup — archives and starts fresh."""
        import json
        from channels.telegram.poll import poll_once, PENDING_PATH

        with open(PENDING_PATH, "w") as f:
            json.dump({"key": "value"}, f)  # would crash TTL prune

        fake = FakeTelegram([])
        # Must not raise — critical for production polling reliability.
        result = poll_once("tok", 100, "999", fake)
        assert result is None


class TestPendingTTL:
    """Regression: stale entries in pending.json must be pruned."""

    def test_stale_entries_pruned(self, poll_dir):
        """Entries with _ts older than 5 minutes are dropped."""
        import time
        from channels.telegram.poll import poll_once, _save_pending, _load_pending

        stale_update = make_callback(1, msg_id=999, data="old")
        stale_update["_ts"] = time.time() - 700  # 11+ minutes ago (TTL is 10 min)

        fresh_update = make_callback(2, msg_id=888, data="new")
        fresh_update["_ts"] = time.time() - 10  # 10 seconds ago

        _save_pending([stale_update, fresh_update])

        fake = FakeTelegram([])
        poll_once("tok", my_msg_id=777, chat_id="999", tg_request_fn=fake)

        pending = _load_pending()
        # Stale entry gone, fresh entry remains
        assert len(pending) == 1
        assert pending[0]["callback_query"]["data"] == "new"

    def test_entries_without_ts_are_pruned(self, poll_dir):
        """Legacy entries without _ts are treated as stale and pruned."""
        from channels.telegram.poll import poll_once, _save_pending, _load_pending

        no_ts_update = make_callback(1, msg_id=999, data="legacy")
        # No _ts field — defaults to epoch 0, always older than 10 min

        _save_pending([no_ts_update])

        fake = FakeTelegram([])
        poll_once("tok", my_msg_id=777, chat_id="999", tg_request_fn=fake)

        pending = _load_pending()
        assert len(pending) == 0  # pruned (no _ts = stale)

    def test_new_pending_entries_get_ts(self, poll_dir):
        """Updates that go to pending from getUpdates should have _ts."""
        from channels.telegram.poll import poll_once, _load_pending

        # Callback for msg_id=200, but we're polling for msg_id=100
        fake = FakeTelegram([make_callback(1, msg_id=200, data="other")])

        poll_once("tok", my_msg_id=100, chat_id="999", tg_request_fn=fake)

        pending = _load_pending()
        assert len(pending) == 1
        assert "_ts" in pending[0]
