/**
 * session.ts — high-level session API.
 *
 * Functional API (no class): all operations take session name as first arg.
 * Keeps code flat and avoids boilerplate.
 */

import {
  createSession as tmuxCreate,
  killSession as tmuxKill,
  sendText,
  sendKey,
  capturePane,
  captureFull,
  isAlive,
} from "./transport.js";
import {
  stripAnsiLines,
  detectReady,
  detectChoices,
  detectPermission,
  detectComposedInput,
  extractLastResponse,
  type PermissionPrompt,
  type ChoiceItem,
} from "./parser.js";
import {
  storeGet,
  storeSave,
  storeDelete,
  storeTouch,
  makeRecord,
} from "./store.js";
import { ConversationLogger } from "./history.js";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PaneState {
  state: "ready" | "thinking" | "typing" | "composed" | "approval" | "choosing" | "unknown" | "dead";
  permission?: PermissionPrompt;
  choices?: ChoiceItem[];
  lines: string[];
  lastResponse?: string;
  composedText?: string; // text present in input box (state === "composed")
}

export interface SessionConfig {
  name: string;
  cwd: string;
  command?: string | string[];
  backend?: string;
  env?: Record<string, string>;
  startupWaitMs?: number;
}

export interface SendOptions {
  timeout?: number;
  noWait?: boolean;
  autoApprove?: boolean;
  initialDelay?: number;
  backend?: string;
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

export async function runSession(
  name: string,
  cwd: string,
  command = "claude",
  backend = "claude",
  startupWait = 2000,
): Promise<void> {
  await tmuxCreate(name, cwd, command);
  storeSave(makeRecord(name, cwd, command, backend));
  if (startupWait > 0) await sleep(startupWait);
}

/** Idempotent: creates session only if it doesn't already exist. */
export async function ensureSession(config: SessionConfig): Promise<void> {
  const { name, cwd, command = "claude", backend = "claude", env, startupWaitMs = 2000 } = config;
  if (await isAlive(name)) return;
  await tmuxCreate(name, cwd, command, env);
  storeSave(makeRecord(name, cwd, Array.isArray(command) ? command[0] : command, backend));
  if (startupWaitMs > 0) await sleep(startupWaitMs);
}

export async function killSession(name: string): Promise<void> {
  await tmuxKill(name);
  storeDelete(name);
}

// ---------------------------------------------------------------------------
// Stable frame analysis — shared by readState and waitForResponse
// ---------------------------------------------------------------------------

// U+00A0 stripping inline (normTrim not exported from parser).
const stripInputLine = (text: string) =>
  text
    .split("\n")
    .filter((l) => {
      const t = l.replace(/\u00a0/g, " ").trim();
      return (
        t !== "\u276F" &&
        t !== "\u203A" &&
        t !== ">" &&
        !t.startsWith("\u276F ") &&
        !t.startsWith("\u203A ") &&
        !t.startsWith("> ")
      );
    })
    .join("\n");

/**
 * Classify a stable (non-changing) pane snapshot into a concrete state.
 * Called only after frame-diff confirms the pane has settled.
 */
export function analyzeStableLines(lines: string[], backend: string): PaneState {
  const perm = detectPermission(lines);
  if (perm) {
    return {
      state: "approval",
      permission: perm,
      lines,
      lastResponse: extractLastResponse(lines, backend),
    };
  }

  const choices = detectChoices(lines);
  if (choices) return { state: "choosing", choices, lines };

  // Must come before idle-❯ check: ❯ with text is NOT ready.
  const composedText = detectComposedInput(lines);
  if (composedText) return { state: "composed", composedText, lines };

  // Pass lines as prevLines — stability is already confirmed, elapsed=999
  // bypasses the time gate so detectReady triggers on the PROMPT check only.
  const ready = detectReady(lines, lines, 999, 0, backend);
  if (ready.isReady) {
    return { state: "ready", lines, lastResponse: extractLastResponse(lines, backend) };
  }

  // Stable but no recognisable idle prompt (mid-startup, unknown UI, etc.)
  return { state: "unknown", lines };
}

/**
 * Classify a window of pre-captured, ANSI-stripped pane frames into a PaneState.
 * Pure function — no I/O, no timers.
 *
 * This is the core logic shared by readState() and awaitFrameMatch(). Extracted
 * so it can be unit-tested directly against fixture frame sequences.
 *
 * @param frames     Array of stripped strings, each is stripAnsiLines(raw).join("\n"). At least 1 frame.
 * @param backend    Backend identifier ("claude", "cursor", "opencode", etc.)
 * @param rawFrames  Optional raw frames (with ANSI codes). When provided, frame equality
 *                   is checked against raw — so ANSI color changes (e.g. Codex "Working"
 *                   blinking) are detected as frame differences → "thinking".
 */
export function classifyWindow(frames: string[], backend: string, rawFrames?: string[]): PaneState {
  const lastLines = frames[frames.length - 1].split("\n");
  // Use raw frames for stability comparison when available — ANSI color
  // changes (blinking, highlighting) will make raw frames differ even when
  // stripped text is identical, correctly classifying as "thinking".
  const cmpFrames = rawFrames ?? frames;
  if (cmpFrames.every((f) => f === cmpFrames[0])) {
    return analyzeStableLines(lastLines, backend);
  }
  const nonInputAllSame = frames.every(
    (f) => stripInputLine(f) === stripInputLine(frames[0]),
  );
  return { state: nonInputAllSame ? "typing" : "thinking", lines: lastLines };
}

// ---------------------------------------------------------------------------
// Pane state
// ---------------------------------------------------------------------------

export async function readState(
  name: string,
  full = false,
  windowMs = 1000,
  intervalMs = 200,
): Promise<PaneState> {
  if (!(await isAlive(name))) return { state: "dead", lines: [] };

  const rec = storeGet(name);
  const backend = rec?.backend ?? "claude";
  const capture = (ansi: boolean) => (full ? captureFull(name, 5000, ansi) : capturePane(name, ansi));

  // Collect N frames within a fixed window.
  // Frame diff is the ground truth for "thinking" vs "stable".
  const samples = Math.max(2, Math.round(windowMs / intervalMs));
  const frames: string[] = [];
  const rawFrames: string[] = [];
  for (let i = 0; i < samples; i++) {
    const rawLines = await capture(true);
    rawFrames.push(rawLines.join("\n"));
    frames.push(stripAnsiLines(rawLines).join("\n"));
    if (i < samples - 1) await sleep(intervalMs);
  }

  return classifyWindow(frames, backend, rawFrames);
}

// ---------------------------------------------------------------------------
// Core: sliding-window frame accumulation
// ---------------------------------------------------------------------------

/**
 * Continuously capture pane frames using a sliding window, classify the
 * current state at each step, and return when `predicate` is satisfied.
 *
 * predicate(state, prevLinesText):
 *   state        — PaneState classified from the current window
 *   prevLinesText — lines.join("\n") from the previous classification (null on first)
 *
 * beforeText (optional): skip frames identical to this value before starting
 * accumulation — used by send() to ignore the pre-send snapshot (Phase 1).
 *
 * State classification per window:
 *   all N frames identical  → analyzeStableLines  (ready/approval/choosing/composed/unknown)
 *   frames differ, only ❯ line changes → typing
 *   frames differ, other content changes → thinking
 */
async function awaitFrameMatch(
  name: string,
  timeout: number,
  backend: string,
  predicate: (state: PaneState, prevLinesText: string | null) => boolean,
  beforeText: string | null = null,
  intervalMs = 200,
  stableThreshold = 3,
): Promise<PaneState> {
  const start = Date.now();
  const full = backend === "opencode";
  const frames: string[] = [];
  const rawFrames: string[] = [];
  let seenChange = beforeText === null;
  let prevLinesText: string | null = null;

  while (true) {
    if ((Date.now() - start) / 1000 > timeout)
      throw new Error(`Timeout on '${name}'`);

    if (!(await isAlive(name))) {
      const dead: PaneState = { state: "dead", lines: [] };
      if (predicate(dead, prevLinesText)) return dead;
      throw new Error(`Session '${name}' is dead`);
    }

    const rawLines = full ? await captureFull(name, 5000, true) : await capturePane(name, true);
    const rawText = rawLines.join("\n");
    const text = stripAnsiLines(rawLines).join("\n");

    // Phase 1: skip until content differs from beforeText.
    if (!seenChange) {
      if (text === beforeText) { await sleep(intervalMs); continue; }
      seenChange = true;
    }

    // Sliding window: keep only the last stableThreshold + 1 frames.
    frames.push(text);
    rawFrames.push(rawText);
    if (frames.length > stableThreshold + 1) { frames.shift(); rawFrames.shift(); }

    if (frames.length < stableThreshold) { await sleep(intervalMs); continue; }

    // Classify current window — raw frames used for stability comparison
    // so ANSI color changes (blinking text) are detected as "thinking".
    const state = classifyWindow(
      frames.slice(-stableThreshold), backend,
      rawFrames.slice(-stableThreshold),
    );

    if (predicate(state, prevLinesText)) return state;
    prevLinesText = state.lines.join("\n");
    await sleep(intervalMs);
  }
}

// ---------------------------------------------------------------------------
// Waiting — thin wrappers over awaitFrameMatch
// ---------------------------------------------------------------------------

/** Wait until the session is ready to accept input. */
export async function waitReady(
  name: string,
  timeout = 300,
  initialDelay = 500,
  backend?: string,
): Promise<string> {
  await sleep(initialDelay);
  const rec = storeGet(name);
  const resolvedBackend = backend ?? rec?.backend ?? "claude";
  const state = await awaitFrameMatch(
    name, timeout, resolvedBackend,
    (s) => s.state === "ready" || s.state === "approval" || s.state === "choosing",
  );
  return state.lines.join("\n");
}

/** Wait until the session reaches a target state. */
export async function waitState(
  name: string,
  target: string,
  timeout = 300,
): Promise<PaneState> {
  const rec = storeGet(name);
  const backend = rec?.backend ?? "claude";
  return awaitFrameMatch(
    name, timeout, backend,
    target === "any-change"
      ? (s, prev) => prev !== null && s.lines.join("\n") !== prev
      : (s) => s.state === target,
  );
}

/** Wait for response after send() — accumulate from beforeText change. */
async function waitForResponse(
  name: string,
  timeout: number,
  backend: string,
  beforeText: string,
): Promise<PaneState> {
  return awaitFrameMatch(
    name, timeout, backend,
    (s) => s.state !== "thinking" && s.state !== "unknown",
    beforeText,
  );
}

// ---------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------

export async function send(
  name: string,
  text: string,
  opts: SendOptions = {},
): Promise<PaneState> {
  const { timeout = 300, noWait = false, autoApprove = false } = opts;

  const record = storeGet(name);
  const backend = opts.backend ?? record?.backend ?? "claude";
  const logger = new ConversationLogger(name);

  // Snapshot before sending — waitForResponse uses this as the "change" baseline.
  const before = await capturePane(name);
  const beforeText = stripAnsiLines(before).join("\n");

  // Backend-specific submit:
  //   opencode → C-m (carriage return)
  //   codex    → text + sleep + Enter (two-step confirm)
  //   others   → text + Enter
  if (backend === "opencode") {
    await sendText(name, text, { submitKey: "C-m" });
  } else if (backend === "codex") {
    await sendText(name, text);
    await sleep(150);
    await sendKey(name, "Enter");
  } else {
    await sendText(name, text);
  }
  storeTouch(name);
  logger.logUser(text);

  if (noWait) return { state: "unknown", lines: [] };

  if (autoApprove) {
    return sendWithAutoApprove(name, timeout, backend, logger, beforeText);
  }

  // All other backends: accumulate frames from send-time until stable.
  const state = await waitForResponse(name, timeout, backend, beforeText);
  if (state.lastResponse) logger.logAssistant(state.lastResponse);
  return state;
}

async function sendWithAutoApprove(
  name: string,
  timeout: number,
  backend: string,
  logger: ConversationLogger,
  beforeText: string,
): Promise<PaneState> {
  const deadline = Date.now() + timeout * 1000;
  let currentBeforeText = beforeText;

  while (Date.now() < deadline) {
    const remaining = (deadline - Date.now()) / 1000;
    if (remaining <= 0) break;

    const state = await waitForResponse(name, remaining, backend, currentBeforeText);

    if (state.state === "approval" && state.permission) {
      await approvePermission(name, state.permission, "yes");
      // Update baseline so next waitForResponse waits for post-approval change.
      currentBeforeText = state.lines.join("\n");
      continue;
    }

    if (state.lastResponse) logger.logAssistant(state.lastResponse);
    return state;
  }

  throw new Error(`Auto-approve timeout on '${name}'`);
}

// ---------------------------------------------------------------------------
// Permission approval
// ---------------------------------------------------------------------------

export async function approvePermission(
  name: string,
  perm: PermissionPrompt,
  answer: "yes" | "always" | "no",
): Promise<void> {
  const selected = perm.options.find((o) => o.selected);
  const target = perm.options.find((o) => o.key === answer);
  if (!target) return;

  const from = selected?.index ?? 0;
  const to = target.index;
  await navigateAndSelect(name, from, to);
}

/**
 * Approve a generic choice menu (workspace trust, model picker, etc.).
 *
 * `answer` can be:
 *   - A 1-based number string ("1", "2") to select by position
 *   - "yes" (alias for "1") / "no" (alias for last option)
 *   - A substring to match against choice labels
 */
export async function approveChoice(
  name: string,
  choices: ChoiceItem[],
  answer: string,
): Promise<void> {
  const selectedIdx = choices.findIndex((c) => c.selected);
  const from = selectedIdx >= 0 ? selectedIdx : 0;

  // Resolve target index
  let to: number;
  if (/^\d+$/.test(answer)) {
    to = parseInt(answer, 10) - 1; // 1-based → 0-based
  } else if (answer === "yes") {
    to = 0;
  } else if (answer === "no") {
    to = choices.length - 1;
  } else {
    const lower = answer.toLowerCase();
    to = choices.findIndex((c) => c.label.toLowerCase().includes(lower));
  }

  if (to < 0 || to >= choices.length) {
    throw new Error(
      `No matching choice for '${answer}'. Options: ${choices.map((c, i) => `${i + 1}. ${c.label}`).join(", ")}`,
    );
  }

  await navigateAndSelect(name, from, to);
  console.log(`Selected: ${choices[to].label}`);
}

async function navigateAndSelect(
  name: string,
  from: number,
  to: number,
): Promise<void> {
  const delta = to - from;
  if (delta !== 0) {
    const key = delta > 0 ? "Down" : "Up";
    for (let i = 0; i < Math.abs(delta); i++) await sendKey(name, key);
    await sleep(100);
  }
  await sendKey(name, "Enter");
}
