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
    "stop_hook_enabled": False,
    "stop_wait_seconds": 180,
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


def session_tag(event):
    """Short project identifier from event cwd basename — helps readers
    tell multiple CC sessions apart when they share one TG chat."""
    cwd = event.get("cwd") or ""
    return os.path.basename(cwd.rstrip("/"))


# Patterns that look like secrets — matched case-insensitively
_SECRET_PATTERNS = [
    # Key=value patterns (English + Chinese keywords)
    re.compile(r'(?i)(token|password|passwd|secret|api_key|apikey|access_key|private_key|auth|credential|mnemonic|seed_phrase|recovery_phrase)\s*[=:]\s*\S+'),
    re.compile(r'(密码|密钥|口令|凭据|助记词|私钥)\s*[=:：]\s*\S+'),
    # Authorization headers (captures "Authorization: Bearer <token>" as one match)
    re.compile(r'(?i)Authorization\s*[:=]\s*(Bearer|Basic)?\s*\S+'),
    re.compile(r'(?i)Bearer\s+\S+'),
    # AWS keys
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'(?i)aws_secret_access_key\s*[=:]\s*\S+'),
    # GitHub personal access tokens + app tokens
    re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{60,}\b'),
    # Slack tokens
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),
    # Stripe, Twilio, SendGrid prefixes
    re.compile(r'\b(sk|rk|pk)_(live|test)_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b'),
    # JWT (three dot-separated base64url segments)
    re.compile(r'\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{10,}\b'),
    # PEM blocks (private key / cert bodies)
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'),
    # SSH private key header (in case END block is missing)
    re.compile(r'ssh-(rsa|ed25519|dss|ecdsa)\s+[A-Za-z0-9+/=]{50,}'),
    # Cookie values
    re.compile(r'(?i)(cookie|set-cookie)\s*[=:]\s*\S+'),
    # Long query strings (often contain tokens)
    re.compile(r'\?[^"\s]{80,}'),
    # URL path segments that look like token paths
    re.compile(r'(?i)/(token|key|secret|session)/[A-Za-z0-9_-]{20,}'),
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



# Claude Code wraps slash commands, caveats, reminders, and other
# plumbing in XML-style tags inside transcript entries. Strip just the
# tags (<tag> and </tag>) in channel context previews — inner content
# is left intact.
_XML_TAG = re.compile(r"</?\w[\w-]*[^>]*/?>")


def _strip_system_tags(text):
    """Remove XML-style tags, preserving the inner text."""
    return _XML_TAG.sub("", text)


def extract_last_messages(transcript_path, max_messages=3, max_chars=200,
                          full_scan=False):
    """Read last N user/assistant messages from a transcript JSONL file.
    Returns list of raw text strings (no HTML, no masking — caller handles that).

    full_scan=True reads the entire file instead of just the 50KB tail.
    Use when you need complete messages (e.g. the Full context feature) —
    a single huge turn larger than the tail window would otherwise be
    dropped because the seek lands mid-entry and the partial JSON line
    fails to parse."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    if max_messages <= 0:
        # Python's messages[-0:] == messages[0:] (full list), so short-circuit.
        return []
    try:
        with open(transcript_path, "rb") as f:
            if full_scan:
                chunk = f.read().decode("utf-8", errors="replace")
            else:
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
            text = _strip_system_tags(text).strip()
            if not text or len(text) < 3 or role not in ("user", "assistant"):
                continue
            truncated = text if max_chars is None else text[:max_chars]
            messages.append({"role": role, "text": truncated,
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
        # Collapse whitespace (newlines, tabs, repeated spaces) to single
        # spaces so each context line renders as a clean one-liner.
        text = re.sub(r'\s+', ' ', text).strip()
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


def send_full_context(ch, reply_to_msg_id, transcript_path, max_turns):
    """Send the last N transcript turns as reply-anchored messages.
    Each turn becomes one (or more) TG messages threaded under the
    original. Used by every hook's "Full context" button.

    Returns (sent_count, total_count). Callers should only remove the
    Full context button when sent_count == total_count — a partial
    success leaves the user with a truncated prefix and no retry path
    if we claim success too early."""
    chunks = build_full_context_chunks(transcript_path, max_turns=max_turns)
    sent = 0
    for chunk in chunks:
        if ch.send_reply(reply_to_msg_id, chunk):
            sent += 1
    return sent, len(chunks)


def _split_escaped_at_boundaries(raw_text, limit):
    """Split raw text into pieces whose HTML-escaped size is ≤ limit.
    Prefers to cut at paragraph / newline / space boundaries so we don't
    slice words in half. Returns a list of already-escaped strings."""
    parts = []
    remaining = raw_text
    while remaining:
        escaped = html_escape(remaining)
        if len(escaped) <= limit:
            parts.append(escaped)
            break
        # Estimate how many raw chars fit: if the full remainder inflated
        # to N chars but we can only afford `limit`, keep roughly
        # limit / expansion_ratio raw chars. Factor 0.9 as safety margin.
        ratio = max(1.0, len(escaped) / max(1, len(remaining)))
        target_raw = max(100, int(limit * 0.9 / ratio))
        target_raw = min(target_raw, len(remaining))
        window = remaining[:target_raw]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > target_raw // 2:
                cut = idx + len(sep)
                break
        if cut == -1:
            cut = target_raw
        piece = remaining[:cut].rstrip()
        piece_escaped = html_escape(piece)
        if len(piece_escaped) > limit:
            # Worst-case pathological content (e.g. pure ampersands) —
            # shrink until it fits.
            while len(piece_escaped) > limit and len(piece) > 1:
                piece = piece[: len(piece) * limit // max(1, len(piece_escaped))]
                piece_escaped = html_escape(piece)
            cut = len(piece)
        parts.append(piece_escaped)
        remaining = remaining[cut:]
    return parts


def build_full_context_chunks(transcript_path, max_turns=3, chunk_limit=3900):
    """Return the last N user/assistant turns as HTML-safe message chunks
    (oldest → newest). Each chunk fits under Telegram's 4096-char limit
    with headroom.

    Packing strategy: complete turns are greedy-packed into a single chunk
    when they fit together; only turns larger than `chunk_limit` stand
    alone and split across multiple chunks with "(part i/N)" markers."""
    messages = extract_last_messages(
        transcript_path, max_messages=max_turns, max_chars=None, full_scan=True)
    blocks = []  # (rendered_text, is_atomic) — atomic blocks can be packed together
    for idx, msg in enumerate(messages, start=1):
        prefix = "👤" if msg["role"] == "user" else "🤖"
        ts = msg.get("timestamp", "")
        time_label = ""
        if ts:
            try:
                from datetime import datetime
                clean = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean).astimezone()
                time_label = f"[<code>{dt.strftime('%H:%M')}</code>] "
            except (ValueError, TypeError):
                pass
        header = f"{time_label}{prefix} <b>Turn {idx}/{len(messages)}</b>\n"
        raw_body = mask_secrets(msg["text"])
        body_limit = chunk_limit - len(header) - 40
        escaped_parts = _split_escaped_at_boundaries(raw_body, body_limit)
        if len(escaped_parts) == 1:
            blocks.append((header + escaped_parts[0], True))
        else:
            total = len(escaped_parts)
            for i, part in enumerate(escaped_parts, start=1):
                blocks.append((f"{header}<i>(part {i}/{total})</i>\n{part}", False))

    # Pack atomic blocks greedily; oversized (non-atomic) blocks stand alone.
    chunks = []
    current = ""
    sep = "\n\n"
    for text, atomic in blocks:
        if not atomic:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(text)
            continue
        if not current:
            current = text
        elif len(current) + len(sep) + len(text) <= chunk_limit:
            current += sep + text
        else:
            chunks.append(current)
            current = text
    if current:
        chunks.append(current)
    return chunks


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
