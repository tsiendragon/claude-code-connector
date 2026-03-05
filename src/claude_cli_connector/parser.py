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
    re.compile(r"^Human\s*:?\s*$"),       # "Human:" label
    re.compile(r"^\?\s+.+$"),            # inquirer-style "? ..." prompt
]

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
    numbered: list[ChoiceItem] = []
    for line in tail:
        m = _NUM_LIST_RE.match(line)
        if m:
            numbered.append(ChoiceItem(key=m.group(1), label=m.group(2).strip()))
    if len(numbered) >= 2:
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


def extract_last_response(lines: list[str]) -> str:
    """
    Extract the last assistant response from a full pane capture.

    Strategy: find the last idle-prompt line, then collect all non-prompt,
    non-user-input lines that appear *before* it (reading upward until we
    hit another prompt or the top of the pane).

    This is a best-effort heuristic and may not be perfect across all
    Claude CLI versions.
    """
    clean = strip_ansi_lines(lines)

    # Find indices of all idle-prompt lines.
    prompt_idx = [i for i, ln in enumerate(clean) if _is_prompt_line(ln)]

    if not prompt_idx:
        # No prompt visible at all: return the whole pane.
        return "\n".join(clean).strip()

    last_prompt = prompt_idx[-1]

    if len(prompt_idx) >= 2:
        # Response is the content between the second-to-last and last prompt.
        start = prompt_idx[-2] + 1
        response_lines = [
            ln for ln in clean[start:last_prompt]
            if not _is_prompt_line(ln)
        ]
    elif last_prompt < len(clean) - 1:
        # Single prompt NOT at the end of the pane: the response comes after it
        # (e.g. first capture right after the user's very first question).
        response_lines = [
            ln for ln in clean[last_prompt + 1:]
            if not _is_prompt_line(ln)
        ]
    else:
        # Single prompt at the end: response is everything before it.
        response_lines = [
            ln for ln in clean[:last_prompt]
            if not _is_prompt_line(ln)
        ]
    return "\n".join(response_lines).strip()
