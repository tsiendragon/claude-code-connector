/**
 * helpers.ts — Test utilities shared across TS unit tests.
 */

import { detectReady, type ReadyResult } from "../../../src/parser.js";
import { classifyWindow, type PaneState } from "../../../src/session.js";

/**
 * Simulate a heartbeat poll loop over a sequence of terminal frames.
 *
 * This mirrors what waitReady() does in session.ts: poll every `intervalMs`,
 * pass the previous frame as prevLines, and accumulate elapsed time.
 *
 * WHY HEARTBEAT MATTERS FOR STATUS
 * ─────────────────────────────────
 * A single-frame snapshot can be ambiguous:
 *   - Claude briefly shows ❯ between tool calls → false "ready"
 *   - A new session mid-splash → false "not_ready"
 *
 * Heartbeat requires the pane to be stable (unchanged) for at least
 * `minStableSecs` before reporting "stable" confidence, or finds the idle
 * ❯ prompt with "prompt" confidence.
 *
 * For `ccc status` to be accurate, use at least 3 frames (≈900ms) to
 * confirm stability before trusting a "ready" result that lacks a clear ❯.
 *
 * @param frames      Sequence of pane captures (pre-ANSI-stripped OK)
 * @param intervalMs  Time between polls in ms (default 300, matches prod)
 * @param minStableSecs  Stability threshold in seconds (default 0.8)
 * @returns           The last ReadyResult produced by the sequence
 */
export function simulateHeartbeat(
  frames: string[][],
  intervalMs = 300,
  minStableSecs = 0.8,
  backend = "claude",
): ReadyResult {
  let prevLines: string[] | null = null;
  let result: ReadyResult = { isReady: false, confidence: "not_ready", text: "" };

  for (let i = 0; i < frames.length; i++) {
    const elapsed = (i * intervalMs) / 1000;
    result = detectReady(frames[i], prevLines, elapsed, minStableSecs, backend);
    prevLines = frames[i];
  }

  return result;
}

/**
 * Build N identical frames of the same content, simulating a frozen pane.
 */
export function repeatFrame(lines: string[], count: number): string[][] {
  return Array.from({ length: count }, () => [...lines]);
}

/**
 * Simulate awaitFrameMatch's sliding-window logic over pre-captured frames.
 *
 * This replays the core loop of awaitFrameMatch() without any I/O or timers:
 *   1. Phase 1 (optional): skip frames identical to beforeText
 *   2. Accumulate frames into a sliding window of size stableThreshold
 *   3. At each step, classifyWindow(window, backend) and check predicate
 *   4. Return {matchIndex, state} when predicate first matches, or null if never
 *
 * @param frameTexts     All frames as joined strings (lines.join("\n"))
 * @param backend        Backend identifier
 * @param predicate      Same signature as awaitFrameMatch's predicate
 * @param beforeText     If set, skip frames identical to this (Phase 1)
 * @param stableThreshold  Window size (default 3, matches prod)
 */
export function simulateAwaitFrameMatch(
  frameTexts: string[],
  backend: string,
  predicate: (state: PaneState, prevLinesText: string | null) => boolean,
  beforeText: string | null = null,
  stableThreshold = 3,
): { matchIndex: number; state: PaneState } | null {
  const window: string[] = [];
  let seenChange = beforeText === null;
  let prevLinesText: string | null = null;

  for (let i = 0; i < frameTexts.length; i++) {
    const text = frameTexts[i];

    // Phase 1: skip until content differs from beforeText
    if (!seenChange) {
      if (text === beforeText) continue;
      seenChange = true;
    }

    // Sliding window
    window.push(text);
    if (window.length > stableThreshold + 1) window.shift();
    if (window.length < stableThreshold) continue;

    // Classify
    const state = classifyWindow(window.slice(-stableThreshold), backend);

    if (predicate(state, prevLinesText)) {
      return { matchIndex: i, state };
    }
    prevLinesText = state.lines.join("\n");
  }

  return null;
}
