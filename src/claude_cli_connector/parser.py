"""
parser.py
---------
Output parsing utilities for Claude CLI terminal output.

Two main responsibilities:
  1. ANSI / VT100 escape code stripping.
  2. "Ready detection" – determining whether Claude has finished its current
     response and is waiting for the next user input.
  3. Choice menu detection – identifying interactive selection prompts.

Ready detection uses a layered heuristic strategy:
  ① BUSY check:     spinner chars / "Thinking..." → definitely not ready
  ② PROMPT check:   standalone "> " prompt pattern → definitely ready
  ③ STABILITY check: pane content unchanged for N seconds → assume ready
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

# Matches ESC + any terminal control sequence (CSI, OSC, or single-byte Fe).
#
# IMPORTANT: CSI (\x1b[) and OSC (\x1b]) must come BEFORE the Fe catch-all
# (\x1b + [@-Z\\-_]) because ] (0x5D) falls inside the Fe range \\-_ (0x5C-0x5F).
# If the Fe branch ran first it would consume \x1b] and leave the OSC payload.
_ANSI_RE = re.compile(
    r"""
    \x1b                     # ESC
    (?:
        \[                   # CSI — ESC [
        [0-?]*               #   parameter bytes  (0x30-0x3F)
        [ -/]*               #   intermediate bytes (0x20-0x2F)
        [@-~]                #   final byte        (0x40-0x7E)
      | \]                   # OSC — ESC ]
        [^\x07\x1b]*         #   payload (anything except BEL or ESC)
        (?:\x07|\x1b\\)      #   ST: BEL (0x07) or ESC \
      | [@-Z\\^_]            # Fe single-byte sequences (0x40-0x5F), excluding [ and ]
    )
    """,
    re.VERBOSE,
)

# Strip bare CR (\r not followed by \n) – used by some terminal progress bars.
_BARE_CR_RE = re.compile(r"\r(?!\n)")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences and bare CR from *text*."""
    text = _ANSI_RE.sub("", text)
    text = _BARE_CR_RE.sub("", text)
    return text


def strip_ansi_lines(lines: list[str]) -> list[str]:
    """Apply :func:`strip_ansi` to each line in *lines*."""
    return [strip_ansi(line) for line in lines]


# ---------------------------------------------------------------------------
# Claude CLI ready-state detection
# ---------------------------------------------------------------------------

# Patterns that match a STANDALONE input prompt line (the idle cursor).
# Checked against trimmed individual lines near the bottom of the pane.
_PROMPT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^[╰>─]+\s*>?\s*$"),     # ">" / "╰─>" prompt
    re.compile(r"^>\s*$"),                # bare ">"
    re.compile(r"^❯\s*$"),               # bare "❯" (Claude Code CLI idle prompt)
    re.compile(r"^Human\s*:?\s*$"),       # "Human:" label
    re.compile(r"^\?\s+.+$"),            # inquirer-style "? ..." prompt
    re.compile(r"→\s*Add a follow-up"),  # Cursor Agent idle prompt
]

# Pattern that matches a user-input line: "❯ <text>" (Claude Code CLI).
_USER_INPUT_RE = re.compile(r"^❯\s+.+$")

# Pattern for Claude response marker: "⏺" (Claude Code CLI).
_RESPONSE_MARKER_RE = re.compile(r"^⏺\s*")

# Separator line (the horizontal rules in Claude Code CLI).
_SEPARATOR_RE = re.compile(r"^[─━═▪\s]{10,}$")

# --- Cursor Agent patterns ---
# Input box top/bottom borders: ┌───...───┐ / └───...───┘
_CURSOR_BOX_RE = re.compile(r"^[┌└][─┐┘\s]+$")
# The idle prompt inside the input box: "→ Add a follow-up"
_CURSOR_FOLLOWUP_RE = re.compile(r"→\s*Add a follow-up")
# Cursor header line: "Cursor Agent v..."
_CURSOR_HEADER_RE = re.compile(r"^\s*Cursor Agent\s+v")
# Cursor footer: "/ commands · @ files · ! shell" or model info
_CURSOR_FOOTER_RE = re.compile(r"^\s*(/\s*commands|Claude\s+\d|[○●]\s)")

# Patterns indicating Claude is actively generating (present in tail lines).
_BUSY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"),    # braille spinner
    re.compile(r"Thinking\.\.\."),
    re.compile(r"Generating"),
    re.compile(r"Working\.\.\."),
    re.compile(r"●\s*$"),               # solid-dot spinner
    re.compile(r"\bESC\b.*to interrupt", re.IGNORECASE),
]

# How many trailing lines to inspect for prompt / busy patterns.
_PROMPT_CHECK_LINES = 6


def _is_prompt_line(line: str) -> bool:
    """Return True if *line* (already ANSI-stripped) looks like an idle prompt."""
    s = line.strip()
    if not s:
        return False
    return any(p.fullmatch(s) for p in _PROMPT_PATTERNS)


@dataclass
class ReadinessResult:
    """Result from a single call to :func:`detect_ready`."""

    is_ready: bool
    confidence: Literal["prompt", "stability", "busy"]
    snapshot_text: str
    elapsed: float


def detect_ready(
    lines: list[str],
    prev_lines: Optional[list[str]] = None,
    elapsed: float = 0.0,
    min_stable_secs: float = 0.4,
    stability_delay: float = 0.0,
) -> ReadinessResult:
    """
    Heuristic ready-state detection for a Claude CLI pane.

    Parameters
    ----------
    lines:
        Current pane lines (raw, ANSI will be stripped internally).
    prev_lines:
        Pane lines from a previous capture (for stability check).
    elapsed:
        Seconds since we started polling.
    min_stable_secs:
        Minimum elapsed time before stability check is trusted.
    """
    clean = strip_ansi_lines(lines)
    snapshot_text = "\n".join(clean)
    tail = clean[-_PROMPT_CHECK_LINES:]
    tail_text = "\n".join(tail)

    # ① BUSY: any spinner or progress indicator → definitely not ready.
    for bp in _BUSY_PATTERNS:
        if bp.search(tail_text):
            return ReadinessResult(
                is_ready=False, confidence="busy",
                snapshot_text=snapshot_text, elapsed=elapsed,
            )

    # ② PROMPT: standalone idle-cursor line near the bottom → ready.
    for line in reversed(tail):
        if _is_prompt_line(line):
            return ReadinessResult(
                is_ready=True, confidence="prompt",
                snapshot_text=snapshot_text, elapsed=elapsed,
            )

    # ③ STABILITY: unchanged content + enough time → assume ready.
    if prev_lines is not None and elapsed >= min_stable_secs:
        prev_clean = strip_ansi_lines(prev_lines)
        if clean == prev_clean:
            return ReadinessResult(
                is_ready=True, confidence="stability",
                snapshot_text=snapshot_text, elapsed=elapsed,
            )

    return ReadinessResult(
        is_ready=False, confidence="busy",
        snapshot_text=snapshot_text, elapsed=elapsed,
    )


# ---------------------------------------------------------------------------
# Choice menu detection
# ---------------------------------------------------------------------------

@dataclass
class ChoiceItem:
    """A single item in an interactive selection list."""

    key: str
    """The value to send to Claude to select this item (e.g. ``"1"``, ``"2"``)."""

    label: str
    """Human-readable option text."""

    selected: bool = False
    """True when the cursor is currently on this item (arrow-style menus)."""


# Numbered list:  "1. some option"  or  "1) some option"
_NUM_LIST_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")

# Arrow cursor:   "❯ some option"  or  "> option"  or  "► option"
_ARROW_RE = re.compile(r"^\s*[❯►▶>]\s+(.+)$")

# Bullet items:   "○ option"  "● option"  "◉ option"  "◎ option"
_BULLET_RE = re.compile(r"^\s*[○●◉◎✓✗•]\s+(.+)$")


def detect_choices(lines: list[str]) -> list[ChoiceItem] | None:
    """
    Detect an interactive selection menu in the pane output.

    Recognises two formats:

    Numbered list::

        1. claude-opus-4-5
        2. claude-sonnet-4-5
        3. claude-haiku-4-5

    Arrow / bullet cursor (ink / inquirer TUI)::

        ❯ claude-sonnet-4-5   ← selected
          claude-opus-4-5
          claude-haiku-4-5

    Returns a list of :class:`ChoiceItem` when ≥ 2 choices are found,
    or ``None`` if no selection menu is detected.
    """
    clean = strip_ansi_lines(lines)
    tail = clean[-20:]   # inspect last 20 lines

    # --- Try numbered list first (most common) ---
    # Real choice menus (model picker, tool selector) are always a contiguous
    # block of items numbered sequentially from 1.  A numbered list in Claude's
    # response text (e.g. "4. Use script …  5. Pipe through tee") should NOT
    # be detected as a choice menu.
    numbered: list[ChoiceItem] = []
    numbered_block: list[ChoiceItem] = []
    for line in tail:
        m = _NUM_LIST_RE.match(line)
        if m:
            numbered_block.append(ChoiceItem(key=m.group(1), label=m.group(2).strip()))
        else:
            if numbered_block:
                numbered = numbered_block
                numbered_block = []
    if numbered_block:
        numbered = numbered_block

    # Accept only if ≥2 items and first item starts at "1"
    if len(numbered) >= 2 and numbered[0].key == "1":
        return numbered

    # --- Try arrow / bullet cursor style (consecutive lines only) ---
    # Real choice menus (e.g. model picker) have arrow/bullet lines back-to-back.
    # User-input echoes also start with ❯ but are scattered throughout the pane
    # with Claude responses between them — so we only accept a CONTIGUOUS block.
    last_block: list[ChoiceItem] = []
    current_block: list[ChoiceItem] = []

    for line in tail:
        ma = _ARROW_RE.match(line)
        if ma:
            current_block.append(ChoiceItem(
                key=str(len(current_block) + 1),
                label=ma.group(1).strip(),
                selected=True,
            ))
            continue
        mb = _BULLET_RE.match(line)
        if mb:
            current_block.append(ChoiceItem(
                key=str(len(current_block) + 1),
                label=mb.group(1).strip(),
            ))
            continue
        # Any non-arrow/bullet line breaks the block.
        if current_block:
            last_block = current_block
            current_block = []

    if current_block:
        last_block = current_block

    return last_block if len(last_block) >= 2 else None


# ---------------------------------------------------------------------------
# Output extraction helpers
# ---------------------------------------------------------------------------

def diff_output(before: list[str], after: list[str]) -> list[str]:
    """
    Return the lines in *after* that are new compared to *before*.

    Uses a simple longest-common-prefix diff – works well for terminal
    output that only ever appends.
    """
    common = 0
    for a, b in zip(before, after):
        if a == b:
            common += 1
        else:
            break
    return after[common:]


def _extract_cursor_response(clean: list[str]) -> str | None:
    """
    Extract the last response from Cursor Agent pane output.

    Cursor Agent format::

        Cursor Agent v2026.02.27-e7d2ef6
        ~/path/to/project · main

        what is the date today              ← user input (text block)

        Today is Friday, March 6, 2026.    ← response (text block)

        ┌───────────────────────────────┐
        │ → Add a follow-up             │   ← idle input box
        └───────────────────────────────┘
        Claude 4.6 Opus (Thinking) · 7.5%  ← footer
        / commands · @ files · ! shell

    Strategy: find the ``→ Add a follow-up`` input box, then walk upward
    to collect text blocks.  The conversation is a sequence of text blocks
    separated by blank lines.  The last block before the input box is the
    response; the one before that is the user input.
    """
    # Find the input box region.
    followup_idx = None
    for i in range(len(clean) - 1, -1, -1):
        if _CURSOR_FOLLOWUP_RE.search(clean[i]):
            followup_idx = i
            break

    if followup_idx is None:
        return None

    # Find the top border of the input box (┌───).
    box_top = followup_idx
    for i in range(followup_idx - 1, max(followup_idx - 5, -1), -1):
        if _CURSOR_BOX_RE.match(clean[i].strip()):
            box_top = i
            break

    # Collect content above the box, split into text blocks by blank lines.
    content_lines = clean[:box_top]

    # Split into blocks (non-empty line groups separated by blank lines).
    blocks: list[list[str]] = []
    current_block: list[str] = []
    for ln in content_lines:
        if ln.strip():
            current_block.append(ln)
        else:
            if current_block:
                blocks.append(current_block)
                current_block = []
    if current_block:
        blocks.append(current_block)

    if not blocks:
        return None

    # Filter out the header block (starts with "Cursor Agent v..." or path info).
    filtered: list[list[str]] = []
    for block in blocks:
        first = block[0].strip()
        if _CURSOR_HEADER_RE.match(first):
            continue
        # Skip path lines like "~/path · branch"
        if first.startswith("~/") or first.startswith("/"):
            continue
        filtered.append(block)

    if not filtered:
        return None

    # The last block is the response.
    response_block = filtered[-1]
    return "\n".join(response_block).strip()


def extract_last_response(lines: list[str], backend: str = "") -> str:
    """
    Extract the last assistant response from a full pane capture.

    Parameters
    ----------
    lines:
        Pane content (raw or ANSI-stripped).
    backend:
        Optional hint: ``"cursor"`` to use Cursor-specific extraction,
        ``"claude"`` for Claude Code CLI extraction, or ``""`` (default)
        for auto-detection across all strategies.

    Supports three formats:

    **Claude Code CLI (v2+)** — identified by ``❯`` prompts and ``⏺`` markers::

        ❯ user question here
        ⏺ Claude's response...
        ❯                          ← idle prompt

    **Cursor Agent** — identified by ``→ Add a follow-up`` input box::

        what is the date today
        Today is Friday, March 6, 2026.
        ┌──────────────────────────┐
        │ → Add a follow-up        │
        └──────────────────────────┘

    **Legacy / generic** — identified by ``>`` or ``╰─>`` prompts::

        > user input
        response text
        >                          ← idle prompt

    This is a best-effort heuristic and may not be perfect across all
    CLI versions.
    """
    clean = strip_ansi_lines(lines)

    # --- Backend-specific extraction ---
    if backend == "cursor":
        cursor_result = _extract_cursor_response(clean)
        if cursor_result:
            return cursor_result
        # Fallback: return everything above the input box as-is.
        return "\n".join(clean).strip()

    if backend == "claude":
        # Skip Cursor strategy, go straight to Claude Code CLI.
        pass
    else:
        # Auto-detect: try Cursor first.
        cursor_result = _extract_cursor_response(clean)
        if cursor_result:
            return cursor_result

    # --- Strategy 1: Claude Code CLI format (❯ / ⏺ markers) ---
    # Walk backward from the end to find the idle ❯ prompt, then find
    # the preceding user-input line (❯ <text>).  Everything between
    # them (excluding user-input, separators, and the idle prompt) is
    # the response.
    last_idle = None
    for i in range(len(clean) - 1, -1, -1):
        s = clean[i].strip()
        if s == "❯" or s == "":
            if s == "❯":
                last_idle = i
                break
        elif _SEPARATOR_RE.fullmatch(s) or s == "? for shortcuts":
            continue
        else:
            break

    if last_idle is not None:
        # Find the preceding user-input line: "❯ <text>"
        user_input_idx = None
        for i in range(last_idle - 1, -1, -1):
            s = clean[i].strip()
            if _USER_INPUT_RE.fullmatch(s):
                user_input_idx = i
                break

        if user_input_idx is not None:
            # Collect response lines between user input and idle prompt.
            response_lines = []
            for ln in clean[user_input_idx + 1 : last_idle]:
                s = ln.strip()
                # Skip separator lines, "? for shortcuts", empty lines at boundaries
                if _SEPARATOR_RE.fullmatch(s):
                    continue
                response_lines.append(ln)

            result = "\n".join(response_lines).strip()
            if result:
                return result

    # --- Strategy 2: Legacy / generic prompt-based extraction ---
    prompt_idx = [i for i, ln in enumerate(clean) if _is_prompt_line(ln)]

    if not prompt_idx:
        return "\n".join(clean).strip()

    last_prompt = prompt_idx[-1]

    if len(prompt_idx) >= 2:
        start = prompt_idx[-2] + 1
        response_lines = [
            ln for ln in clean[start:last_prompt]
            if not _is_prompt_line(ln)
        ]
    elif last_prompt < len(clean) - 1:
        response_lines = [
            ln for ln in clean[last_prompt + 1:]
            if not _is_prompt_line(ln)
        ]
    else:
        response_lines = [
            ln for ln in clean[:last_prompt]
            if not _is_prompt_line(ln)
        ]
    return "\n".join(response_lines).strip()
