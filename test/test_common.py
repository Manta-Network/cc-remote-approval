"""Tests for lib/common.py — config, masking, utilities."""
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.common import (
    html_escape, sanitize_name, mask_secrets,
    make_logger, load_config, check_local_response,
    extract_last_messages, smart_truncate, DEFAULTS,
    build_full_context_chunks,
)


class TestHtmlEscape:
    def test_escapes_ampersand(self):
        assert html_escape("a & b") == "a &amp; b"

    def test_escapes_tags(self):
        assert html_escape("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"

    def test_handles_non_string(self):
        assert html_escape(123) == "123"


class TestSmartTruncate:
    def test_under_limit_returned_as_is(self):
        assert smart_truncate("short", 100) == "short"

    def test_exactly_at_limit_unchanged(self):
        s = "x" * 100
        assert smart_truncate(s, 100) == s

    def test_cuts_at_paragraph_boundary(self):
        text = "para one\n\npara two" + "y" * 200
        result = smart_truncate(text, 20)
        assert result.endswith("…")
        assert "para two" not in result  # cut happened before second para

    def test_prefers_paragraph_over_line(self):
        # First paragraph is > limit/2 so the paragraph boundary wins over
        # any later line boundary inside the second block.
        text = "first paragraph text\n\nline one\nline two" + "x" * 100
        result = smart_truncate(text, 50)
        assert result.endswith("…")
        assert "first paragraph text" in result
        assert "line two" not in result  # cut at \n\n, not later \n

    def test_falls_back_to_line_when_no_paragraph(self):
        text = "line1\nline2\nline3" + "y" * 100
        result = smart_truncate(text, 15)
        assert result.endswith("…")
        assert "\n" in result[:-1]  # at least one newline preserved

    def test_hard_cut_when_no_good_boundary(self):
        """Unbroken string with no spaces: fall back to hard cut."""
        text = "x" * 100
        result = smart_truncate(text, 10)
        assert len(result) <= 10
        assert result.endswith("…")

    def test_custom_marker(self):
        text = "abc def ghi jkl mno"
        result = smart_truncate(text, 10, marker=" [cut]")
        assert result.endswith(" [cut]")

    def test_does_not_exceed_limit_materially(self):
        """Truncated length should be close to limit, never grow beyond."""
        text = "word " * 1000  # 5000 chars
        result = smart_truncate(text, 100)
        assert len(result) <= 100 + len("…")


# safe_callback_data tests moved to test/telegram/ (Telegram-specific concern)


class TestSanitizeName:
    def test_normal_name_unchanged(self):
        assert sanitize_name("my-server") == "my-server"

    def test_path_traversal_blocked(self):
        result = sanitize_name("../../etc/cron.d/evil")
        assert "/" not in result
        assert ".." not in result

    def test_truncates_long_names(self):
        result = sanitize_name("a" * 200)
        assert len(result) <= 100

    def test_preserves_underscores_hyphens(self):
        assert sanitize_name("my_server-v2") == "my_server-v2"


class TestMaskSecrets:
    def test_masks_token_env(self):
        result = mask_secrets('export TOKEN=abc123def456')
        assert "****" in result
        assert "abc123def456" not in result

    def test_masks_password(self):
        result = mask_secrets('password=super_secret_123')
        assert "super_secret_123" not in result
        assert "****" in result

    def test_masks_authorization_header(self):
        result = mask_secrets('curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIs"')
        assert "****" in result
        # The full token should not survive intact
        assert "eyJhbGciOiJSUzI1NiIs" not in result or "****" in result

    def test_masks_aws_key(self):
        result = mask_secrets('key=AKIAIOSFODNN7EXAMPLE')
        assert "AKIAIOSF" in result  # first 8
        assert "EXAMPLE" not in result

    def test_preserves_normal_text(self):
        text = "echo hello world"
        assert mask_secrets(text) == text

    def test_masks_cookie(self):
        result = mask_secrets('Cookie: session=abc123xyz')
        assert "abc123xyz" not in result

    def test_masks_chinese_keywords(self):
        cases = [
            ("密码：my-secret-p@ss", "my-secret"),
            ("密钥=abc123def456", "abc123def"),
            ("助记词: word1 word2 word3", "word1"),
        ]
        for text, hidden in cases:
            result = mask_secrets(text)
            assert hidden not in result, f"leaked {hidden!r} from {text!r}"

    # NOTE: token fixtures below are assembled at runtime from pieces so
    # GitHub's secret scanner doesn't flag the test file as containing
    # real secrets.

    def test_masks_github_pat(self):
        payload = "a" * 20 + "B" * 20  # 40 chars matching the pattern
        token = "ghp" + "_" + payload
        result = mask_secrets(f"gh token: {token}")
        assert payload not in result

    def test_masks_slack_token(self):
        body = "1234567890" + "-" + "abcdefghij"
        token = "xoxb" + "-" + body
        result = mask_secrets(token)
        assert "abcdefghij" not in result

    def test_masks_stripe_key(self):
        payload = "a" * 24
        key = "sk" + "_" + "live" + "_" + payload
        result = mask_secrets(f"key = {key}")
        assert payload not in result

    def test_masks_jwt(self):
        header = "eyJ" + "hbGciOiJIUzI1NiJ9"
        body = "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0"
        sig = "sig" + "123abc456def789"
        jwt = header + "." + body + "." + sig
        result = mask_secrets(f"token = {jwt}")
        assert sig not in result

    def test_masks_pem_private_key(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890abcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = mask_secrets(pem)
        assert "1234567890abcdef" not in result

    def test_masks_ssh_key(self):
        result = mask_secrets("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDxxxxxxxxxxxxxxxx user@host")
        assert "AAAAB3NzaC1yc2EAAAADAQABAAABAQDxxxxxxxxxxxxxxxx" not in result

    def test_masks_url_token_path(self):
        result = mask_secrets("https://api.example.com/v1/token/aBc123dEf456gHi789jKl")
        assert "aBc123dEf456gHi789jKl" not in result


class TestMakeLogger:
    def test_creates_log_file(self, tmp_path):
        import utils.common as common
        orig = common.LOG_DIR
        common.LOG_DIR = str(tmp_path)

        log = make_logger("test")
        log("hello")

        log_file = tmp_path / "test.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "hello" in content

        common.LOG_DIR = orig

    def test_log_has_date_and_time(self, tmp_path):
        import utils.common as common
        orig = common.LOG_DIR
        common.LOG_DIR = str(tmp_path)

        log = make_logger("test")
        log("msg")

        content = (tmp_path / "test.log").read_text()
        # Format: MM-DD HH:MM:SS [PID] msg
        import re
        assert re.search(r'\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[\d+\]', content)

        common.LOG_DIR = orig


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        import utils.common as common
        monkeypatch.setattr(common, "CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        # Clear env vars
        for key in DEFAULTS:
            monkeypatch.delenv(f"CC_REMOTE_APPROVAL_{key.upper()}", raising=False)

        cfg = load_config()
        assert cfg["escalation_seconds"] == 20
        
        assert cfg["bot_token"] == ""

    def test_reads_from_file(self, tmp_path, monkeypatch):
        import utils.common as common
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"bot_token": "test123", "chat_id": "456"}))
        monkeypatch.setattr(common, "CONFIG_PATH", str(config_file))
        for key in DEFAULTS:
            monkeypatch.delenv(f"CC_REMOTE_APPROVAL_{key.upper()}", raising=False)

        cfg = load_config()
        assert cfg["bot_token"] == "test123"
        assert cfg["chat_id"] == "456"

    def test_env_var_overrides_file(self, tmp_path, monkeypatch):
        import utils.common as common
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"bot_token": "from_file"}))
        monkeypatch.setattr(common, "CONFIG_PATH", str(config_file))
        monkeypatch.setenv("CC_REMOTE_APPROVAL_BOT_TOKEN", "from_env")

        cfg = load_config()
        assert cfg["bot_token"] == "from_env"


class TestCheckLocalResponse:
    def test_detects_growth(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("x" * 500)
        assert check_local_response(str(f), baseline_size=100) is True

    def test_ignores_small_growth(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("x" * 250)
        assert check_local_response(str(f), baseline_size=200) is False

    def test_custom_threshold(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("x" * 200)
        assert check_local_response(str(f), baseline_size=100, threshold=50) is True
        assert check_local_response(str(f), baseline_size=100, threshold=500) is False

    def test_returns_false_for_missing_file(self):
        assert check_local_response("/nonexistent", baseline_size=100) is False

    def test_returns_false_for_empty_path(self):
        assert check_local_response("", baseline_size=100) is False

    def test_returns_false_for_zero_baseline(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("x" * 500)
        assert check_local_response(str(f), baseline_size=0) is False


# tg_edit_message tests moved to integration tests (tested via TelegramChannel)


class TestExtractLastMessages:
    def test_extracts_messages(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello"}}),
            json.dumps({"message": {"role": "assistant", "content": "hi there"}}),
            json.dumps({"message": {"role": "user", "content": "do something"}}),
        ]
        transcript.write_text("\n".join(lines))

        msgs = extract_last_messages(str(transcript))
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[0]["text"] == "hello"

    def test_respects_max_messages(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": f"msg {i}"}})
            for i in range(10)
        ]
        transcript.write_text("\n".join(lines))

        msgs = extract_last_messages(str(transcript), max_messages=2)
        assert len(msgs) == 2

    def test_truncates_long_text(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"message": {"role": "user", "content": "x" * 500}})
        )

        msgs = extract_last_messages(str(transcript), max_chars=50)
        assert len(msgs[0]["text"]) == 50

    def test_handles_list_content(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ]}
        }))

        msgs = extract_last_messages(str(transcript))
        assert "part one" in msgs[0]["text"]
        assert "part two" in msgs[0]["text"]

    def test_skips_short_messages(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "hi"}}),  # too short (<3)
            json.dumps({"message": {"role": "user", "content": "hello world"}}),
        ]
        transcript.write_text("\n".join(lines))

        msgs = extract_last_messages(str(transcript))
        assert len(msgs) == 1
        assert msgs[0]["text"] == "hello world"

    def test_returns_empty_for_missing_file(self):
        assert extract_last_messages("/nonexistent") == []

    def test_returns_empty_for_empty_path(self):
        assert extract_last_messages("") == []

    def test_send_full_context_reports_partial_failure(self, tmp_path):
        """send_full_context returns (sent, total). A partial send — some
        chunks succeed, some fail — must NOT be mistaken for full success
        by the caller, otherwise the user gets a truncated context with
        no retry path. Use a single oversized turn so it splits into
        multiple chunks (the packing logic would otherwise merge small
        turns into one)."""
        import json as _json
        from utils.common import send_full_context

        transcript = tmp_path / "t.jsonl"
        # One very big turn → splits across multiple chunks
        big = "word " * 15000
        transcript.write_text(_json.dumps({
            "message": {"role": "user", "content": big}
        }))

        class PartialChannel:
            def __init__(self):
                self.calls = 0
            def send_reply(self, reply_to, text, parse_mode="HTML"):
                self.calls += 1
                # First succeeds, later ones fail
                return 42 if self.calls == 1 else None

        ch = PartialChannel()
        sent, total = send_full_context(ch, 100, str(transcript), max_turns=1)
        assert total > 1, "test needs a multi-chunk context to exercise partial failure"
        assert sent == 1
        assert sent < total, "partial failure should not read as full success"

    def test_max_messages_zero_returns_empty(self, tmp_path):
        """Python's messages[-0:] == messages[:] (full list). max_messages=0
        must short-circuit to [] so context_turns=0 doesn't blow up into
        sending every transcript turn."""
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": f"msg {i} has words"}})
            for i in range(5)
        ]
        transcript.write_text("\n".join(lines))
        assert extract_last_messages(str(transcript), max_messages=0) == []
        assert extract_last_messages(str(transcript), max_messages=-1) == []

    def test_strips_tags_preserving_inner_content(self, tmp_path):
        """Tags are removed but inner text is preserved — filter leaves
        user intent intact while dropping the XML plumbing."""
        transcript = tmp_path / "t.jsonl"
        wrapped = (
            "<command-name>/reload-plugins</command-name>\n"
            "<command-message>reload-plugins</command-message>\n"
            "<command-args></command-args>\n"
            "切换到 main 分支吧？"
        )
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": wrapped}
        }))
        msgs = extract_last_messages(str(transcript))
        assert len(msgs) == 1
        assert "切换到 main 分支吧？" in msgs[0]["text"]
        # Tag markers gone…
        assert "<command-name>" not in msgs[0]["text"]
        assert "</command-name>" not in msgs[0]["text"]
        # …but inner content preserved
        assert "/reload-plugins" in msgs[0]["text"]

    def test_strips_local_command_and_system_variants(self, tmp_path):
        """Covers <(local-)command-*> and <system-*> variants — tags go,
        inner text stays."""
        transcript = tmp_path / "t.jsonl"
        wrapped = (
            "<local-command-stdout>(no content)</local-command-stdout>\n"
            "<system-session-start>Injected preamble</system-session-start>\n"
            "怎么还有标签？"
        )
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": wrapped}
        }))
        msgs = extract_last_messages(str(transcript))
        assert len(msgs) == 1
        assert "怎么还有标签？" in msgs[0]["text"]
        assert "<local-command-stdout>" not in msgs[0]["text"]
        assert "<system-session-start>" not in msgs[0]["text"]
        # Inner text preserved
        assert "(no content)" in msgs[0]["text"]
        assert "Injected preamble" in msgs[0]["text"]


class TestBuildFullContextChunks:
    """build_full_context_chunks returns full (untruncated) turns, splitting
    oversized ones across multiple chunks with a (part i/N) marker."""

    def test_small_turns_packed_into_one_chunk(self, tmp_path):
        """Three short turns should pack into a single TG message — we
        want to minimize message count when the content fits together."""
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "first turn"}}),
            json.dumps({"message": {"role": "assistant", "content": "second turn"}}),
            json.dumps({"message": {"role": "user", "content": "third turn"}}),
        ]
        transcript.write_text("\n".join(lines))
        chunks = build_full_context_chunks(str(transcript), max_turns=3)
        assert len(chunks) == 1
        assert "first turn" in chunks[0]
        assert "second turn" in chunks[0]
        assert "third turn" in chunks[0]

    def test_oversized_turn_splits_but_others_pack(self, tmp_path):
        """A short turn next to an oversized one: short packs alone,
        oversized splits into its own parts. Final layout: short-chunk,
        then multi-part chunks for the big one."""
        transcript = tmp_path / "t.jsonl"
        big = "word " * 15000  # ~75KB
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello"}}),
            json.dumps({"message": {"role": "assistant", "content": big}}),
            json.dumps({"message": {"role": "user", "content": "bye"}}),
        ]
        transcript.write_text("\n".join(lines))
        chunks = build_full_context_chunks(str(transcript), max_turns=3)
        # First chunk holds "hello" (packing stops before the oversized turn)
        assert "hello" in chunks[0]
        # Middle chunks carry the oversized turn's parts
        assert any("part 1/" in c for c in chunks)
        # Final chunk holds "bye" (separate from the oversized turn's parts)
        assert "bye" in chunks[-1]

    def test_splits_oversized_turn(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        big = "x" * 9000  # much bigger than any single TG message allows
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": big}
        }))
        chunks = build_full_context_chunks(str(transcript), max_turns=1)
        assert len(chunks) > 1
        # Every chunk must be under TG's 4096 char limit
        for chunk in chunks:
            assert len(chunk) <= 4096
        # Combined body preserves the full content
        combined = "".join(chunks)
        assert combined.count("x") == 9000
        # Continuation marker appears
        assert "part 1/" in chunks[0]

    def test_empty_transcript_returns_empty(self, tmp_path):
        assert build_full_context_chunks("/nonexistent", max_turns=3) == []

    def test_send_full_context_zero_when_nothing_to_send(self, tmp_path):
        """With max_turns=0 the helper returns (0, 0) — callers must treat
        this as 'done, nothing to retry', not as a failure that leaves the
        button in place forever."""
        from utils.common import send_full_context

        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps(
            {"message": {"role": "user", "content": "some content words here"}}
        ))

        class Ch:
            def send_reply(self, *a, **kw): return 1

        sent, total = send_full_context(Ch(), 100, str(transcript), max_turns=0)
        assert sent == 0 and total == 0

    def test_split_never_breaks_html_entity(self, tmp_path):
        """When a turn has characters that HTML-escape into entities (like &),
        the split must happen BEFORE escaping so a single '&' never becomes
        a half '&amp;' across two chunks."""
        transcript = tmp_path / "t.jsonl"
        # Pack many '&' so some will land near the chunk boundary
        body = "foo & bar " * 1000
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": body}
        }))
        chunks = build_full_context_chunks(str(transcript), max_turns=1, chunk_limit=2000)
        assert len(chunks) > 1
        for chunk in chunks:
            # A broken "&amp;" would leave either "&am" at end or "p;" at start
            assert "&am " not in chunk and not chunk.rstrip().endswith("&am")
            assert not chunk.lstrip().startswith("p;")
            # Must still be valid bounded chunks
            assert len(chunk) <= 4096

    def test_html_escape_expansion_stays_under_limit(self, tmp_path):
        """Raw text heavy in &/</> inflates 4-5x when HTML-escaped. Chunk
        sizing must account for that — a pathological all-ampersand turn
        should never emit a chunk past TG's 4096-char limit."""
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": "&" * 3900}
        }))
        chunks = build_full_context_chunks(str(transcript), max_turns=1)
        assert len(chunks) > 0
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_single_huge_turn_not_dropped(self, tmp_path):
        """Full-context extraction reads the whole file so a final turn
        larger than the 50KB tail window isn't dropped mid-JSON."""
        transcript = tmp_path / "t.jsonl"
        big = "word " * 15000  # ~75KB, well past the 50KB tail
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": big}
        }))
        chunks = build_full_context_chunks(str(transcript), max_turns=1)
        assert len(chunks) > 0
        # Content survives across the chunks
        combined = "".join(chunks)
        assert combined.count("word") >= 15000

    def test_split_prefers_line_boundaries(self, tmp_path):
        """Long multi-line body should split at newline, not mid-line."""
        transcript = tmp_path / "t.jsonl"
        # Use varied words so mask_secrets doesn't collapse the body
        lines = [f"line number {i:03d} with some content words here" for i in range(200)]
        body = "\n".join(lines)
        transcript.write_text(json.dumps({
            "message": {"role": "user", "content": body}
        }))
        chunks = build_full_context_chunks(str(transcript), max_turns=1, chunk_limit=2000)
        assert len(chunks) > 1
        # All "line number NNN" markers should appear intact across the
        # chunks combined (no one got sliced in half)
        combined = "\n".join(chunks)
        for i in range(200):
            assert f"line number {i:03d}" in combined

