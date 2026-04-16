"""
Channel abstraction — decouples hooks from specific messaging platforms.

Hooks call channel methods instead of platform API directly. To add a new
channel (Slack, Discord, etc.), implement Channel in channels/<name>/.
"""


class Channel:
    """Base channel interface. All messaging platforms implement these methods.

    Assumed capabilities (hooks depend on these):
      - Buttons: send_message with inline buttons, user clicks → callback data
      - Message editing: update message text/buttons after sending
      - Polling: non-blocking check for user response (callback or text)
      - Reply-to routing: text replies bound to specific message (for concurrent hooks)
      - Notification: fire-and-forget one-way message

    Optional (graceful degradation if missing):
      - edit_buttons: edit only buttons without changing text
      - send_reply_prompt: prompt user to type text (for "Other" option)
      - answer_callback: acknowledge button click (platform-specific UX)

    When implementing a new channel, if a capability is not supported:
      - Buttons not supported → flatten options into numbered text, parse text replies
      - Message editing not supported → send new message instead, ignore edit calls
      - Reply-to not supported → text routing becomes best-effort (first match)
    """

    def send_message(self, text, buttons=None, parse_mode="HTML"):
        """Send a message with optional inline buttons. Returns message ID."""
        raise NotImplementedError

    def edit_message(self, msg_id, text, buttons=None, parse_mode="HTML"):
        """Edit an existing message. Silently ignores errors."""
        raise NotImplementedError

    def edit_buttons(self, msg_id, buttons):
        """Edit only the buttons of an existing message."""
        raise NotImplementedError

    def delete_message(self, msg_id):
        """Delete a previously-sent message. Silently ignores errors.

        Used for transient UI like ForceReply prompts that should vanish
        once the hook resolves, so the user doesn't see a dangling
        'Reply to this message' lock on an already-handled request.
        """
        pass

    def poll(self, msg_id):
        """Check for a user response to msg_id. Non-blocking.

        Args:
            msg_id: int or list[int]. A response whose target matches ANY
                    of the given ids is claimed by this caller. Multiple
                    ids support the AskUserQuestion "Other" flow, where the
                    original question msg_id AND the follow-up force-reply
                    prompt msg_id are both legitimate reply anchors.

        Returns:
            {"type": "callback", "data": "..."} — button click
            {"type": "text", "text": "..."} — text reply
            None — no response yet
        """
        raise NotImplementedError

    def send_notification(self, text, parse_mode="HTML"):
        """Send a one-way notification. Fire-and-forget."""
        raise NotImplementedError

    def send_reply_prompt(self, msg_id, text, force_reply=True):
        """Send a prompt asking user to type a text reply.

        Implementations should, where supported, set a platform-specific
        flag that auto-locks the user's input to 'reply to this message'
        mode (Telegram: ForceReply). This makes the user's next message
        carry a reply anchor even when they type from a notification or
        a wearable UI that can't quote-reply by swipe.

        Returns the new prompt's message id (caller tracks it so replies
        threaded under the prompt also route to the caller) or None on
        failure.
        """
        raise NotImplementedError

    def answer_callback(self, callback_id, text=""):
        """Acknowledge a button click (platform-specific, optional)."""
        pass


class ChannelError:
    """Structured error when channel creation fails."""
    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return self.reason


def create_channel(cfg):
    """Factory: create the appropriate channel based on config.

    Returns:
        (Channel, None) — success
        (None, ChannelError) — failure with reason

    Usage:
        ch, err = create_channel(cfg)
        if err:
            _log(f"Channel unavailable: {err}")
            sys.exit(0)
    """
    channel_type = cfg.get("channel_type", "telegram")

    if channel_type == "telegram":
        bot_token = cfg.get("bot_token", "")
        chat_id = cfg.get("chat_id", "")
        if not bot_token or not chat_id:
            return None, ChannelError(
                f"Telegram config incomplete "
                f"(bot_token={'set' if bot_token else 'missing'}, "
                f"chat_id={'set' if chat_id else 'missing'}). "
                f"Run /cc-remote-approval:setup to configure.")
        from channels.telegram.client import TelegramChannel
        return TelegramChannel(bot_token, chat_id), None

    return None, ChannelError(f"Unknown channel_type: '{channel_type}'")
