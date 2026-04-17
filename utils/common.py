"""
Shared utilities for all cc-remote-approval hooks.

Channel-agnostic: config loading, text processing, logging, IPC helpers.
Channel-specific code lives in channels/<name>/.
"""
import json
import os
import re
import tempfile
import time

CONFIG_PATH = os.path.expanduser("~/.cc-remote-approval/config.json")
RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "cc-remote-approval")
TG_POLL_DIR = os.path.join(RUNTIME_DIR, "tg")
ELICIT_SIGNAL_DIR = os.path.join(RUNTIME_DIR, "elicit")
STOP_SIGNAL_DIR = os.path.join(RUNTIME_DIR, "stop")
LOG_DIR = os.path.join(RUNTIME_DIR, "logs")

DEFAULTS = {
    "channel_type": "telegram",
    "bot_token": "",
    "chat_id": "",
    "escalation_seconds": 20,
    "elicitation_timeout": 60,
    "context_turns": 3,
    "context_max_chars": 200,
    "stop_hook_enabled": True,
    "session_hint_enabled": True,
}

# Internal: how long poll loops wait before giving up (matches hook timeout)
POLL_TIMEOUT_SECONDS = 259200  # 3 days


def load_config():
    """Load config from file, with env var overrides for key fields."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            pass

    result = {}
    for key, default in DEFAULTS.items():
        val = cfg.get(key, default)
        # Allow env var overrides for key fields
        env_key = f"CC_REMOTE_APPROVAL_{key.upper()}"
        val = os.environ.get(env_key, val)
        # Cast. bool is a subclass of int, so check bool FIRST.
        if isinstance(default, bool):
            if isinstance(val, str):
                val = val.strip().lower() in ("1", "true", "yes", "on")
            else:
                val = bool(val)
        elif isinstance(default, int):
            try:
                val = int(val)
            except (ValueError, TypeError):
                val = default
        result[key] = val

    return result



def html_escape(s):
    """Escape HTML special characters for messaging platforms."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def smart_truncate(text, limit, marker="…"):
    """Truncate at paragraph / line / space boundary to avoid ugly mid-word cuts.

    Operate on plain text only — callers must escape HTML *after* truncation
    (otherwise cutting across an HTML tag leaves unclosed markup).

    If no boundary lands above limit // 2, falls back to a hard cut with
    the marker appended.
    """
    if len(text) <= limit:
        return text
    room = max(1, limit - len(marker))
    for sep in ("\n\n", "\n", " "):
        cut = text.rfind(sep, 0, room)
        if cut > limit // 2:
            return text[:cut].rstrip() + marker
    return text[:room].rstrip() + marker



def sanitize_name(name):
    """Sanitize a name for use in filenames — alphanumerics, hyphens, underscores only."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:100]


# Patterns that look like secrets — matched case-insensitively
_SECRET_PATTERNS = [
    # Key=value patterns: TOKEN=xxx, password=xxx, secret=xxx, etc.
    re.compile(r'(?i)(token|password|passwd|secret|api_key|apikey|access_key|private_key|auth)\s*[=:]\s*\S+'),
    # Authorization headers (captures "Authorization: Bearer <token>" as one match)
    re.compile(r'(?i)Authorization\s*[:=]\s*(Bearer|Basic)?\s*\S+'),
    re.compile(r'(?i)Bearer\s+\S+'),
    # AWS keys
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'(?i)aws_secret_access_key\s*[=:]\s*\S+'),
    # Cookie values
    re.compile(r'(?i)(cookie|set-cookie)\s*[=:]\s*\S+'),
    # Long query strings (often contain tokens)
    re.compile(r'\?[^"\s]{80,}'),
    # Hex/base64 strings that look like keys (32+ chars)
    re.compile(r'(?<=[=: ])[A-Za-z0-9+/]{40,}={0,2}(?=\s|$)'),
]


def mask_secrets(text):
    """Mask sensitive patterns in text before sending to any channel."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group()[:8] + "****", text)
    return text


def check_local_response(transcript_path, baseline_size, threshold=200):
    """Check if transcript grew enough to indicate a local response."""
    if not transcript_path or not baseline_size:
        return False
    try:
        return os.path.getsize(transcript_path) > baseline_size + threshold
    except OSError:
        return False



def extract_last_messages(transcript_path, max_messages=3, max_chars=200):
    """Read last N user/assistant messages from a transcript JSONL file.
    Returns list of raw text strings (no HTML, no masking — caller handles that)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50000))
            chunk = f.read().decode("utf-8", errors="replace")

        messages = []
        for line in chunk.strip().split("\n"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                text = " ".join(parts).strip()
            else:
                continue
            if not text or len(text) < 3 or role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "text": text[:max_chars],
                             "timestamp": obj.get("timestamp", "")})
        return messages[-max_messages:]
    except Exception:
        return []


def format_context_lines(transcript_path, max_turns=3, max_chars=200):
    """Extract and format last N conversation turns for display in channel messages.
    Returns list of HTML-safe, masked strings like '👤 12:34 user said...' / '🤖 12:35 agent said...'."""
    import re
    messages = extract_last_messages(transcript_path, max_messages=max_turns, max_chars=max_chars)
    lines = []
    for msg in messages:
        prefix = "👤" if msg["role"] == "user" else "🤖"
        # Short time label from ISO timestamp, converted to local timezone.
        # Transcript timestamps are UTC (Z suffix): "2026-04-16T01:30:45.123Z"
        # → local "09:30" for UTC+8.
        ts = msg.get("timestamp", "")
        time_label = ""
        if ts:
            try:
                from datetime import datetime, timezone
                # Parse ISO format, strip sub-second precision for compat
                clean = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean).astimezone()
                time_label = f"[<code>{dt.strftime('%H:%M')}</code>] "
            except (ValueError, TypeError):
                pass
        text = msg["text"]
        if msg["role"] == "assistant":
            # Strip markdown: code blocks, bold, italic, headers, tables, links
            text = re.sub(r'```[\s\S]*?```', '', text)
            text = re.sub(r'`([^`]+)`', r'\1', text)
            text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
            text = re.sub(r'\*([^*]+)\*', r'\1', text)
            text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
            text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE)
            text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
            text = re.sub(r'\n{2,}', '\n', text)
            # Take first meaningful line
            text_lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
            text = text_lines[0] if text_lines else ""
            if not text or len(text) < 3:
                continue
        lines.append(f"{time_label}{prefix} {html_escape(mask_secrets(text))}")
    return lines


def format_context_block(context_lines):
    """Render context turns with visual separators so consecutive turns
    don't visually bleed into each other on mobile Telegram."""
    if not context_lines:
        return ""
    sep = "\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
    body = sep.join(context_lines)
    return f"\n\n📋 <b>Context:</b>\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n{body}"


MAX_LOG_SIZE = 1024 * 1024  # 1 MB


def make_logger(name):
    """Create a log function that writes to LOG_DIR/{name}.log.
    Includes PID to distinguish multiple concurrent sessions.
    Auto-rotates when file exceeds MAX_LOG_SIZE."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{name}.log")
    pid = os.getpid()
    def _log(msg):
        # Rotate: if file too large, keep last half
        try:
            if os.path.getsize(log_path) > MAX_LOG_SIZE:
                with open(log_path, "rb") as f:
                    f.seek(MAX_LOG_SIZE // 2)
                    tail = f.read()
                with open(log_path, "wb") as f:
                    f.write(tail)
        except OSError:
            pass
        with open(log_path, "a") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} [{pid}] {msg}\n")
    return _log
