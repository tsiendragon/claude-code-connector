"""
parser.py
---------
Output parsing utilities for Claude CLI terminal output.

Two main responsibilities:
  1. ANSI / VT100 escape code stripping.
  2. "Ready detection" – determining whether Claude has finished its current
     response and is waiting for the next user input.

Ready detection is the trickiest part of the tmux-first approach.
Claude CLI does not emit a machine-readable sentinel when it's done.
We therefore rely on heuristic pattern matching + output stability.

Strategy (layered, in order of confidence):
  ① Prompt pattern:  Claude CLI shows a distinctive input prompt when idle,
     e.g.  "> " or "Human: " at the bottom of the pane (high confidence).
  ② Spinner/progress disappears:  During generation, Claude shows a spinner
     or "Thinking…" indicator.  When it disappears, generation is likely done
     (medium confidence).
  ③ Output stability:  Capture the pane twice with a short delay.  If the
     content is identical, Claude has stopped writing (low confidence, used
     as a fallback after a minimum wait).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

# Matches ESC + any terminal control sequence (CSI, OSC, etc.).
_ANSI_RE = re.compile(
    r"""
    \x1b          # ESC
    (?:
        [@-Z\\-_]              # Fe sequences (ESC + single byte)
      | \[                     # CSI sequences: ESC [
        [0-?]*                 #   parameter bytes
        [ -/]*                 #   intermediate bytes
        [@-~]                  #   final byte
      | \]                     # OSC sequences: ESC ]
        [^\x07\x1b]*           #   payload
        (?:\x07|\x1b\\)        #   ST (BEL or ESC \)
    )
    """,
    re.VERBOSE,
)

# Also strip bare CR that are not part of CRLF (used by some terminal apps).
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

# Patterns that indicate Claude's interactive input prompt is visible.
# These are checked against the *last few lines* of the pane.
#
# Claude Code CLI (as of early 2025) shows a prompt that looks like:
#   ╭─ Human
#   ╰─> <cursor>
# or a simpler "> " prompt depending on context / version.
_PROMPT_PATTERNS: list[re.Pattern[str]] = [
    # Arrow-style prompt: "> " or "╰─> " at line start
    re.compile(r"^\s*[╰>─]+\s*>\s*$"),
    re.compile(r"^\s*>\s*$"),
    # Explicit "Human:" label that Claude Code uses
    re.compile(r"^\s*Human\s*:?\s*$"),
    # The input cursor placeholder (empty prompt line after Claude finishes)
    re.compile(r"^\s*\?\s+.*$"),  # interactive prompt "? " (inquirer-style)
]

# Patterns that indicate Claude is still actively generating.
_BUSY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"),           # braille spinner characters
    re.compile(r"Thinking\.\.\."),
    re.compile(r"Generating"),
    re.compile(r"Working\.\.\."),
    re.compile(r"●\s*$"),                        # solid dot spinner
    re.compile(r"\bESC\b.*to interrupt"),        # "ESC to interrupt" hint
]

# Number of trailing lines to check for prompt patterns.
_PROMPT_CHECK_LINES = 6


@dataclass
class ReadinessResult:
    """Result from a single call to :func:`detect_ready`."""

    is_ready: bool
    """Whether Claude appears to be ready for the next input."""

    confidence: str
    """One of: ``'prompt'``, ``'stability'``, ``'timeout'``, ``'busy'``."""

    snapshot_text: str
    """The cleaned pane text that was analysed."""

    elapsed: float
    """Seconds elapsed since observation started."""


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
        Current pane lines (already ANSI-stripped).
    prev_lines:
        Pane lines from a previous capture (used for stability check).
    elapsed:
        Seconds since we started waiting.
    min_stable_secs:
        Minimum time that must have passed before stability check is trusted.
    stability_delay:
        Gap between prev and current capture (used for context only).

    Returns
    -------
    ReadinessResult
    """
    clean = strip_ansi_lines(lines)
    snapshot_text = "\n".join(clean)

    # ① Check for active generation indicators first (most reliable "not ready").
    tail = clean[-_PROMPT_CHECK_LINES:]
    tail_text = "\n".join(tail)

    for bp in _BUSY_PATTERNS:
        if bp.search(tail_text):
            return ReadinessResult(
                is_ready=False,
                confidence="busy",
                snapshot_text=snapshot_text,
                elapsed=elapsed,
            )

    # ② Check for prompt patterns in the last few lines.
    for line in reversed(tail):
        stripped = line.strip()
        if not stripped:
            continue
        for pp in _PROMPT_PATTERNS:
            if pp.match(stripped):
                return ReadinessResult(
                    is_ready=True,
                    confidence="prompt",
                    snapshot_text=snapshot_text,
                    elapsed=elapsed,
                )

    # ③ Stability check: if output hasn't changed since previous capture
    #    and enough time has passed, assume generation is done.
    if prev_lines is not None and elapsed >= min_stable_secs:
        prev_clean = strip_ansi_lines(prev_lines)
        if clean == prev_clean:
            return ReadinessResult(
                is_ready=True,
                confidence="stability",
                snapshot_text=snapshot_text,
                elapsed=elapsed,
            )

    return ReadinessResult(
        is_ready=False,
        confidence="busy",
        snapshot_text=snapshot_text,
        elapsed=elapsed,
    )


# ---------------------------------------------------------------------------
# Output diffing helpers
# ---------------------------------------------------------------------------

def diff_output(before: list[str], after: list[str]) -> list[str]:
    """
    Return the *new* lines that appeared in *after* compared to *before*.

    This is a simple suffix-diff: find the longest common prefix and return
    whatever follows it in *after*.  Works well for terminal output that only
    ever appends.
    """
    # Find the common prefix length.
    common = 0
    for a, b in zip(before, after):
        if a == b:
            common += 1
        else:
            break
    return after[common:]


def extract_last_response(lines: list[str], prompt_marker: str = "> ") -> str:
    """
    Extract the last assistant response from a full pane capture.

    Looks for the last occurrence of *prompt_marker* and returns everything
    between the line *before* that marker (exclusive) and the *current*
    prompt marker (exclusive).

    This is a best-effort heuristic and may not be perfect across all
    Claude CLI versions.
    """
    clean = strip_ansi_lines(lines)
    # Find all lines that look like an input prompt.
    prompt_indices = [
        i for i, line in enumerate(clean)
        if line.strip().endswith(">") or line.strip() == ">"
    ]
    if len(prompt_indices) < 2:
        # Can't find two prompts: return everything after the first prompt.
        if prompt_indices:
            return "\n".join(clean[prompt_indices[0] + 1:]).strip()
        return "\n".join(clean).strip()

    # The response is between the second-to-last and last prompt.
    start = prompt_indices[-2] + 1
    end = prompt_indices[-1]
    response_lines = clean[start:end]
    return "\n".join(response_lines).strip()
