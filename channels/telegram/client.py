"""
Telegram channel implementation — Bot API via urllib.

All Telegram-specific logic lives here and in poll.py.
Hooks never import from this module directly — they use the Channel interface.
"""
import json
import urllib.request

from utils.channel import Channel



def tg_request(token, method, data=None):
    """Call Telegram Bot API via urllib. Token stays in-process (not visible in ps)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TelegramChannel(Channel):
    """Telegram Bot API implementation."""

    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _send(self, method, data):
        """Defense-in-depth: chat-scoped API calls must only target the
        configured chat_id. Catches accidental cross-chat sends from
        future refactors or new Channel-level helpers.

        Does NOT swallow transport errors — HTTPError / URLError propagate
        so the caller can log the real cause. Fire-and-forget methods
        (edit/delete/notification) wrap their own try/except."""
        chat_id = data.get("chat_id")
        if chat_id is not None and str(chat_id) != str(self.chat_id):
            raise RuntimeError(
                f"outbound chat_id mismatch: "
                f"requested={chat_id!r}, configured={self.chat_id!r}"
            )
        return tg_request(self.bot_token, method, data)

    def send_message(self, text, buttons=None, parse_mode="HTML"):
        """Raises the underlying transport error on failure — the hook
        wrapping this call is the right layer to log and decide whether
        to exit. Swallowing here would mask HTTPError 401 / URLError as a
        downstream NoneType crash."""
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        result = self._send("sendMessage", data)
        return result["result"]["message_id"]

    def edit_message(self, msg_id, text, buttons=None, parse_mode="HTML"):
        """Edit a message. buttons=None keeps existing inline keyboard;
        buttons=[] explicitly removes it (pass this on resolve so users
        don't see stale clickable buttons on a message that's already
        been handled); buttons=[[...]] replaces with new keyboard."""
        data = {"chat_id": self.chat_id, "message_id": msg_id,
                "text": text, "parse_mode": parse_mode}
        if buttons is not None:
            data["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self._send("editMessageText", data)
        except Exception:
            pass

    def edit_buttons(self, msg_id, buttons):
        try:
            self._send("editMessageReplyMarkup", {
                "chat_id": self.chat_id, "message_id": msg_id,
                "reply_markup": {"inline_keyboard": buttons},
            })
        except Exception:
            pass

    def delete_message(self, msg_id):
        try:
            self._send("deleteMessage", {
                "chat_id": self.chat_id, "message_id": msg_id,
            })
        except Exception:
            pass

    def poll(self, msg_id):
        from channels.telegram.poll import poll_once
        return poll_once(self.bot_token, msg_id, self.chat_id, tg_request)

    def send_notification(self, text, parse_mode="HTML"):
        try:
            self._send("sendMessage", {
                "chat_id": self.chat_id, "text": text, "parse_mode": parse_mode})
        except Exception:
            pass

    def send_reply_prompt(self, msg_id, text, force_reply=True):
        """Send a prompt asking user to type a text reply.

        When force_reply=True, TG clients auto-lock the user's input box to
        'Reply to this message' mode — their next message gets
        reply_to_message_id set automatically, even from notification quick
        reply or Apple Watch where swipe-to-quote isn't available.

        Returns the new prompt's message_id so the caller can accept replies
        threaded under it (in addition to the original message)."""
        data = {"chat_id": self.chat_id, "text": text,
                "reply_to_message_id": msg_id}
        if force_reply:
            data["reply_markup"] = {
                "force_reply": True,
                "input_field_placeholder": "Type your answer...",
            }
        try:
            resp = self._send("sendMessage", data)
            return resp["result"]["message_id"]
        except Exception:
            return None

    def answer_callback(self, callback_id, text="✅"):
        # answer_callback uses callback_query_id, not chat_id — skip the
        # chat assertion and call tg_request directly.
        try:
            tg_request(self.bot_token, "answerCallbackQuery", {
                "callback_query_id": callback_id, "text": text})
        except Exception:
            pass

    def send_reply(self, reply_to_msg_id, text, parse_mode="HTML"):
        """Send a new message anchored as a reply to reply_to_msg_id.
        Swallows transport errors — caller is typically sending a batch
        of context messages where one failure shouldn't abort the rest."""
        try:
            result = self._send("sendMessage", {
                "chat_id": self.chat_id, "text": text, "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_msg_id,
            })
            return result["result"]["message_id"]
        except Exception:
            return None
