"""Shared fixtures for all Telegram tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scenarios import FakeChannel


class FakeTelegram(FakeChannel):
    """FakeChannel + Telegram Bot API simulation (__call__ for tg_request mock)."""

    def __init__(self, updates=None):
        super().__init__()
        self.update_counter = 100
        self._queued_updates = list(updates or [])
        self.calls = []

    def __call__(self, token, method, data=None):
        self.calls.append((method, data))
        if method == "sendMessage":
            self.msg_counter += 1
            msg_id = self.msg_counter
            self._do_send(msg_id, data.get("text", ""), reply_markup=data.get("reply_markup"))
            return {"result": {"message_id": msg_id}}
        elif method == "getUpdates":
            result = list(self._queued_updates)
            self._queued_updates.clear()
            return {"result": result}
        elif method == "answerCallbackQuery":
            return {"ok": True}
        elif method in ("editMessageText", "editMessageReplyMarkup"):
            self._edited_messages.append({"msg_id": data.get("message_id"), "text": data.get("text", "")})
            return {"ok": True}
        elif method == "deleteMessage":
            self._deleted_messages.append(data.get("message_id"))
            return {"ok": True}
        return {"ok": True}

    def queue_callback(self, msg_id, data, cb_id=None):
        self.update_counter += 1
        self._queued_updates.append({
            "update_id": self.update_counter,
            "callback_query": {"id": cb_id or f"cb_{self.update_counter}",
                               "data": data, "message": {"message_id": msg_id, "chat": {"id": 123}}},
        })

    def queue_text(self, msg_id, text, chat_id=123):
        self.update_counter += 1
        self._queued_updates.append({
            "update_id": self.update_counter,
            "message": {"text": text, "chat": {"id": chat_id},
                        "reply_to_message": {"message_id": msg_id}},
        })

    def get_calls(self, method):
        return [c for c in self.calls if c[0] == method]
