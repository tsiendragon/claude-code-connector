"""Tests for parser.py – ANSI stripping and ready detection."""

import pytest
from claude_cli_connector.parser import (
    strip_ansi,
    strip_ansi_lines,
    detect_ready,
    extract_last_response,
    diff_output,
)


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------

class TestStripAnsi:
    def test_plain_text_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_colour_code_removed(self):
        assert strip_ansi("\x1b[32mGreen\x1b[0m") == "Green"

    def test_csi_sequence_removed(self):
        assert strip_ansi("\x1b[1;31mBold Red\x1b[m") == "Bold Red"

    def test_osc_sequence_removed(self):
        text = "\x1b]0;window title\x07hello"
        assert strip_ansi(text) == "hello"

    def test_bare_cr_removed(self):
        # \r not followed by \n should be stripped
        assert strip_ansi("line1\rline2") == "line1line2"

    def test_crlf_preserved_cr(self):
        # \r\n: the \r should NOT be stripped (it's part of CRLF)
        result = strip_ansi("line1\r\nline2")
        assert "line1" in result
        assert "line2" in result

    def test_braille_spinner_stripped_of_escapes(self):
        # Spinner char is not an escape – it should remain
        assert "⠋" in strip_ansi("\x1b[1m⠋\x1b[0m")

    def test_strip_lines(self):
        lines = ["\x1b[32mfoo\x1b[0m", "\x1b[1mbar\x1b[m"]
        assert strip_ansi_lines(lines) == ["foo", "bar"]


# ---------------------------------------------------------------------------
# detect_ready
# ---------------------------------------------------------------------------

class TestDetectReady:
    def test_busy_spinner_not_ready(self):
        lines = ["Some output", "⠋ Thinking..."]
        result = detect_ready(lines, elapsed=1.0)
        assert not result.is_ready
        assert result.confidence == "busy"

    def test_busy_thinking_not_ready(self):
        lines = ["partial response…", "Thinking..."]
        result = detect_ready(lines, elapsed=1.0)
        assert not result.is_ready

    def test_prompt_pattern_ready(self):
        lines = ["Hello, how can I help?", ">"]
        result = detect_ready(lines, elapsed=0.5)
        assert result.is_ready
        assert result.confidence == "prompt"

    def test_stability_ready(self):
        lines = ["Some response text", "done"]
        result = detect_ready(
            lines,
            prev_lines=lines,   # same as current -> stable
            elapsed=1.0,
            min_stable_secs=0.5,
        )
        assert result.is_ready
        assert result.confidence == "stability"

    def test_stability_not_ready_if_too_soon(self):
        lines = ["Some response text"]
        result = detect_ready(
            lines,
            prev_lines=lines,
            elapsed=0.1,         # less than min_stable_secs
            min_stable_secs=0.5,
        )
        # No prompt, no spinner visible, but elapsed < min_stable_secs
        # -> should fall through to busy
        assert not result.is_ready

    def test_no_prev_lines_not_stable(self):
        lines = ["partial output"]
        result = detect_ready(lines, prev_lines=None, elapsed=2.0, min_stable_secs=0.5)
        assert not result.is_ready


# ---------------------------------------------------------------------------
# extract_last_response
# ---------------------------------------------------------------------------

class TestExtractLastResponse:
    def test_extracts_between_prompts(self):
        lines = [
            "> hello",
            "Hi there! I am Claude.",
            "How can I help you?",
            ">",
        ]
        response = extract_last_response(lines)
        assert "Hi there" in response
        assert "How can I help" in response

    def test_single_prompt_returns_remainder(self):
        lines = ["> ", "Just one response."]
        response = extract_last_response(lines)
        assert "Just one response" in response


# ---------------------------------------------------------------------------
# diff_output
# ---------------------------------------------------------------------------

class TestDiffOutput:
    def test_new_lines_appended(self):
        before = ["line1", "line2"]
        after  = ["line1", "line2", "line3", "line4"]
        assert diff_output(before, after) == ["line3", "line4"]

    def test_identical_no_diff(self):
        lines = ["a", "b", "c"]
        assert diff_output(lines, lines) == []

    def test_empty_before(self):
        after = ["x", "y"]
        assert diff_output([], after) == ["x", "y"]
