#!/usr/bin/env python3
"""
Coordinated Telegram update polling — safe for concurrent hook instances.

Problem: multiple hook processes poll getUpdates simultaneously, each advancing
the global offset and consuming updates meant for other hooks.

Solution: file lock + shared pending queue. Each hook:
  1. Acquires lock
  2. Checks pending queue for matching callback_query (by msg_id)
  3. If not found → calls getUpdates(timeout=0) non-blocking
  4. Keeps matching updates, stores non-matching in pending for other hooks
  5. Releases lock

All hooks share (via common.TG_POLL_DIR = $TMPDIR/cc-remote-approval/tg/):
  poll.lock    — flock for mutual exclusion
  offset       — current getUpdates offset
  pending.json — updates not yet claimed by any hook
"""
import fcntl
import json
import os

from utils.common import TG_POLL_DIR

LOCK_PATH = os.path.join(TG_POLL_DIR, "poll.lock")
OFFSET_PATH = os.path.join(TG_POLL_DIR, "offset")
PENDING_PATH = os.path.join(TG_POLL_DIR, "pending.json")

def poll_once(token, my_msg_id, chat_id, tg_request_fn):
    """
    Check for one update matching my_msg_id (callback) or chat_id (text).

    Args:
      my_msg_id: int or list[int]. When a list, a callback on ANY of these
                 msg_ids counts as ours, and a text reply whose
                 reply_to_message points at ANY of them counts as ours.
                 This is how the AskUserQuestion "Other" flow routes: the
                 original question msg_id plus the force-reply prompt msg_id
                 are both accepted, because TG clients attach reply_to to
                 whichever bot message the user replies to.

    Returns:
      {"type": "callback", "data": "allow", "callback_query": {...}}
      {"type": "text", "text": "user input"}
      None  (nothing found this round)

    Process-safe via file locking. Non-blocking (timeout=0 for getUpdates).
    Caller should sleep ~1s between calls.
    """
    import time as _time
    os.makedirs(TG_POLL_DIR, exist_ok=True)

    my_msg_ids = _coerce_ids(my_msg_id)

    with _file_lock(LOCK_PATH):
        pending = _load_pending()

        # Prune stale entries (older than 10 minutes, or missing _ts from old versions)
        now = _time.time()
        pending = [u for u in pending if now - u.get("_ts", 0) < 600]

        # 1. Check pending for callback matching my_msg_id
        result, pending = _extract_callback(pending, my_msg_ids, token, tg_request_fn)
        if result:
            _save_pending(pending)
            return result

        # 2. Check pending for text message (must be reply to one of my_msg_ids)
        result, pending = _extract_text(pending, my_msg_ids, chat_id)
        if result:
            _save_pending(pending)
            return result

        # 3. Poll Telegram (non-blocking)
        offset = _load_offset()
        try:
            resp = tg_request_fn(token, "getUpdates", {
                "offset": offset, "timeout": 0,
                "allowed_updates": ["callback_query", "message"],
            })
        except Exception:
            _save_pending(pending)
            return None

        my_result = None
        new_pending = []

        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            u["_ts"] = now  # timestamp for TTL pruning

            cb = u.get("callback_query")
            if cb and cb.get("message", {}).get("message_id") in my_msg_ids:
                if my_result is None:
                    _answer_callback(token, cb, tg_request_fn)
                    my_result = {"type": "callback", "data": cb.get("data", ""),
                                 "callback_query": cb}
                else:
                    new_pending.append(u)
                continue

            msg = u.get("message")
            if (msg and msg.get("text")
                    and str(msg.get("chat", {}).get("id", "")) == str(chat_id)
                    and msg.get("reply_to_message", {}).get("message_id") in my_msg_ids):
                if my_result is None:
                    my_result = {"type": "text", "text": msg["text"].strip()}
                else:
                    new_pending.append(u)
                continue

            # Not for me — save for other hooks
            new_pending.append(u)

        _save_offset(offset)
        _save_pending(pending + new_pending)
        return my_result


def _coerce_ids(msg_id):
    """Normalize int or list[int] to a list. None is empty."""
    if msg_id is None:
        return []
    if isinstance(msg_id, (list, tuple, set)):
        return list(msg_id)
    return [msg_id]


def _extract_callback(pending, msg_ids, token, tg_request_fn):
    """Find and remove first callback matching any of msg_ids from pending."""
    for i, u in enumerate(pending):
        cb = u.get("callback_query")
        if cb and cb.get("message", {}).get("message_id") in msg_ids:
            pending.pop(i)
            _answer_callback(token, cb, tg_request_fn)
            return {"type": "callback", "data": cb.get("data", ""),
                    "callback_query": cb}, pending
    return None, pending


def _extract_text(pending, msg_ids, chat_id):
    """Find and remove first text message that is a reply to any of msg_ids.
    Only consumes messages with reply_to_message pointing at one of our ids,
    preventing concurrent hooks from stealing each other's text replies."""
    for i, u in enumerate(pending):
        msg = u.get("message")
        if not msg or not msg.get("text"):
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        reply_to = msg.get("reply_to_message", {}).get("message_id")
        if reply_to in msg_ids:
            pending.pop(i)
            return {"type": "text", "text": msg["text"].strip()}, pending
    return None, pending


def _answer_callback(token, cb, tg_request_fn):
    try:
        tg_request_fn(token, "answerCallbackQuery", {
            "callback_query_id": cb["id"], "text": "✅"})
    except Exception:
        pass


# ---------------------------------------------------------------- file I/O

class _file_lock:
    """flock-based file lock context manager."""
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.fd = open(self.path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *args):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _archive_corrupt(path):
    """Move a corrupt state file aside so it survives for postmortem
    debugging instead of being silently overwritten on next save."""
    import time as _t
    try:
        os.rename(path, f"{path}.corrupt-{int(_t.time())}")
    except OSError:
        pass  # already gone or unwritable — nothing useful to do


def _load_pending():
    try:
        with open(PENDING_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        _archive_corrupt(PENDING_PATH)
        return []
    # Schema-valid but type-invalid JSON (e.g. {}, [1, 2], null) would
    # crash the TTL prune step on u.get(), taking down polling until the
    # state file is manually cleaned. Treat that as corrupt too.
    if not isinstance(data, list) or any(not isinstance(u, dict) for u in data):
        _archive_corrupt(PENDING_PATH)
        return []
    return data


def _save_pending(data):
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.rename(tmp, PENDING_PATH)


def _load_offset():
    try:
        with open(OFFSET_PATH) as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return 0
    except (ValueError, OSError):
        _archive_corrupt(OFFSET_PATH)
        return 0


def _save_offset(offset):
    with open(OFFSET_PATH, "w") as f:
        f.write(str(offset))
