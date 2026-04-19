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

    def test_returns_chunk_per_turn(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "first turn"}}),
            json.dumps({"message": {"role": "assistant", "content": "second turn"}}),
            json.dumps({"message": {"role": "user", "content": "third turn"}}),
        ]
        transcript.write_text("\n".join(lines))
        chunks = build_full_context_chunks(str(transcript), max_turns=3)
        assert len(chunks) == 3
        assert "first turn" in chunks[0]
        assert "second turn" in chunks[1]
        assert "third turn" in chunks[2]

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

