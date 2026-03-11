/**
 * parser.ts — ANSI stripping, ready-state detection, response extraction.
 *
 * Claude Code CLI output format:
 *   ❯ user question           ← user input marker
 *   ⏺ Claude response...     ← response marker
 *   ─────────── ▪▪▪ ─────    ← separator
 *   ❯                         ← idle prompt (ready for next input)
 */

// ANSI/VT100 escape code regex (CSI and OSC must come before Fe catch-all)
const ANSI_RE =
  /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])/g;

const SPINNERS = new Set([..."⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"]);

// Box-drawing chars and separator patterns.
// Also matches status-bar lines like "─── ▪▪▪ Medium /model ─" which contain
// box-drawing chars flanking ▪▪▪ (three or more ▪ in a row).
const SEP_RE = /^[\u2500-\u257F\s▪]+$|[\u2500-\u257F].*▪{2,}.*[\u2500-\u257F]/;

const TUI_HINTS = [
  "Esc to cancel",
  "Tab to amend",
  "ctrl+e to explain",
  "↑↓ to navigate",
  "↵ to confirm",
  "? for shortcuts",
  "Press enter to continue",
];

// Prompt arrow characters used by different backends.
// ❯ U+276F (Claude Code), › U+203A (Codex), > ASCII (fallback)
const ARROW_RE = /^[\u276F\u203A>]/;
const ARROW_SELECTED_RE = /^[\u276F\u203A>]\s+\S/;

// Strip U+00A0 (non-breaking space) that Claude Code CLI sometimes appends to
// the idle ❯ prompt, then trim. Regular trim() does not remove U+00A0.
function normTrim(s: string): string {
  return s.replace(/\u00a0/g, " ").trim();
}

// ---------------------------------------------------------------------------
// ANSI stripping
// ---------------------------------------------------------------------------

export function stripAnsi(s: string): string {
  return s.replace(ANSI_RE, "").replace(/\r/g, "");
}

export function stripAnsiLines(lines: string[]): string[] {
  return lines.map(stripAnsi);
}

// ---------------------------------------------------------------------------
// Ready detection
// ---------------------------------------------------------------------------

export interface ReadyResult {
  isReady: boolean;
  confidence: "prompt" | "stable" | "not_ready";
  text: string;
}

export function detectReady(
  lines: string[],
  prevLines: string[] | null,
  elapsed: number,
  minStableSecs = 0.8,
  backend = "claude",
): ReadyResult {
  const clean = stripAnsiLines(lines);
  const text = clean.join("\n");

  // ---------------------------------------------------------------------------
  // PROMPT detection — pure content check, no busy detection.
  // Busy/thinking is handled by frame stability (ANSI-aware) in classifyWindow.
  // Each backend has its own idle-prompt signature.
  // ---------------------------------------------------------------------------

  if (backend === "codex") {
    // Codex idle: › <suggestion text> at bottom, with response content above.
    // Two › lines with actual content between them = post-response ready.
    const arrowIndices: number[] = [];
    for (let i = 0; i < clean.length; i++) {
      if (clean[i].trim().startsWith("\u203A")) arrowIndices.push(i);
    }
    if (arrowIndices.length >= 2) {
      const lastArrow = arrowIndices[arrowIndices.length - 1];
      const prevArrow = arrowIndices[arrowIndices.length - 2];
      const between = clean.slice(prevArrow + 1, lastArrow);
      const hasContent = between.some((l) => {
        const t = l.trim();
        return t && !SEP_RE.test(t);
      });
      if (hasContent) return { isReady: true, confidence: "prompt", text };
    }
  } else if (backend === "opencode") {
    // Opencode idle: ▣ marker with completion duration (e.g. "▣  Build · model · 22.8s").
    // ▣ WITHOUT duration = still processing (safety net for frozen TUI).
    const oc = clean.map((l) => l.replace(/\u2588.*$/, "").trimEnd());
    for (let i = oc.length - 1; i >= 0; i--) {
      const s = oc[i].trim();
      if (s.startsWith("\u25A3")) {
        if (/\d+\.?\d*s/.test(s)) return { isReady: true, confidence: "prompt", text };
        return { isReady: false, confidence: "not_ready", text };
      }
    }
  } else {
    // Claude / Cursor: trailing idle ❯ (or › or >) prompt.
    // Skip empty lines, separators, and TUI hint lines below the prompt.
    for (let i = clean.length - 1; i >= 0; i--) {
      const t = normTrim(clean[i]);
      if (t === "\u276F" || t === "\u203A" || t === ">")
        return { isReady: true, confidence: "prompt", text };
      if (t === "") continue;
      if (SEP_RE.test(t)) continue;
      if (TUI_HINTS.some((h) => t.includes(h))) continue;
      break;
    }
  }

  // STABILITY fallback: content unchanged since last poll and enough time passed.
  // Catches initial sessions (no prompt yet) and unknown TUI states.
  if (prevLines !== null && elapsed > minStableSecs) {
    if (text === stripAnsiLines(prevLines).join("\n")) {
      return { isReady: true, confidence: "stable", text };
    }
  }

  return { isReady: false, confidence: "not_ready", text };
}

// ---------------------------------------------------------------------------
// Choice menu detection (model picker, Format A permissions, etc.)
// ---------------------------------------------------------------------------

export interface ChoiceItem {
  key?: string;
  label: string;
  selected: boolean;
}

export function detectChoices(lines: string[]): ChoiceItem[] | null {
  const clean = stripAnsiLines(lines);
  const choices: ChoiceItem[] = [];

  for (const line of clean) {
    const t = line.trimEnd();
    const trimmed = t.trimStart();
    // ❯ is always a choice marker; › and > only when followed by a number (to avoid
    // matching codex idle prompt like "› Find and fix a bug...")
    const isChoice = /^\u276F\s+\S/.test(trimmed) ||
      /^[\u203A>]\s+\d+\.\s/.test(trimmed);
    if (isChoice) {
      choices.push({ label: trimmed.replace(/^[\u276F\u203A>]\s+/, ""), selected: true });
    } else if (choices.length > 0 && /^\s{2,}\S/.test(t)) {
      const label = t.trim();
      if (!SEP_RE.test(label) && !TUI_HINTS.some((h) => label.startsWith(h)))
        choices.push({ label, selected: false });
    } else if (choices.length > 0 && t.trim() !== "") {
      break;
    }
  }

  return choices.length >= 2 ? choices : null;
}

// ---------------------------------------------------------------------------
// Permission prompt detection
// ---------------------------------------------------------------------------

export interface PermissionOption {
  key: "yes" | "always" | "no";
  label: string;
  selected: boolean;
  index: number; // 0-based position in list
}

export interface PermissionPrompt {
  type: "allow" | "proceed";
  tool: string;
  options: PermissionOption[];
}

export function detectPermission(lines: string[]): PermissionPrompt | null {
  const clean = stripAnsiLines(lines);

  // Format A: "Allow <Tool>?" followed by Allow once / Allow always / Deny
  for (let i = 0; i < clean.length; i++) {
    const m = clean[i].trim().match(/^Allow (\w+)\??$/);
    if (!m) continue;

    const tool = m[1];
    const keyMap: Record<string, PermissionOption["key"]> = {
      "Allow once": "yes",
      "Allow always": "always",
      Deny: "no",
    };
    const options: PermissionOption[] = [];

    for (let j = i + 1; j < Math.min(i + 6, clean.length); j++) {
      const line = clean[j];
      const label = line.trim().replace(/^[\u276F\u203A>]\s*/, "");
      const key = keyMap[label];
      if (key)
        options.push({
          key,
          label,
          selected: ARROW_RE.test(line.trimStart()),
          index: options.length,
        });
    }
    if (options.length > 0) return { type: "allow", tool, options };
  }

  // Format B: "Do you want to proceed?" followed by numbered list
  for (let i = 0; i < clean.length; i++) {
    if (!clean[i].trim().includes("Do you want to proceed?")) continue;

    const options: PermissionOption[] = [];
    for (let j = i + 1; j < Math.min(i + 8, clean.length); j++) {
      const line = clean[j];
      const m = line.trimStart().match(/^(?:[\u276F\u203A>]\s*)?(\d+)\.\s+(.+)/);
      if (!m) {
        if (line.trim() && !line.trim().startsWith("Esc")) break;
        continue;
      }
      const num = parseInt(m[1]);
      const key: PermissionOption["key"] =
        num === 1 ? "yes" : num === 2 ? "always" : "no";
      options.push({
        key,
        label: m[2].trim(),
        selected: ARROW_RE.test(line.trimStart()),
        index: options.length,
      });
    }

    if (options.length > 0) {
      let tool = "command";
      for (let k = Math.max(0, i - 4); k < i; k++) {
        const t = clean[k].trim();
        if (t && !SEP_RE.test(t)) tool = t.slice(0, 60);
      }
      return { type: "proceed", tool, options };
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// Response extraction
// ---------------------------------------------------------------------------

export function extractLastResponse(
  lines: string[],
  backend = "claude",
): string {
  const clean = stripAnsiLines(lines);

  if (backend === "codex") return extractCodexResponse(clean);
  if (backend === "opencode") return extractOpencodeResponse(clean);

  // --- Claude / Cursor ---
  // Find trailing idle ❯ (use normTrim to handle trailing U+00A0)
  let idleIdx = -1;
  for (let i = clean.length - 1; i >= 0; i--) {
    const t = normTrim(clean[i]);
    if (t === "\u276F" || t === "\u203A" || t === ">") {
      idleIdx = i;
      break;
    }
    // Skip empty lines, separator lines, and TUI hint lines that appear
    // after the idle ❯ prompt (e.g. ── ▪▪▪ ─, ? for shortcuts, etc.)
    if (t === "") continue;
    if (SEP_RE.test(t)) continue;
    if (TUI_HINTS.some((h) => t.includes(h))) continue;
    // Non-empty, non-hint, non-sep line — we've passed the idle area
    break;
  }

  if (idleIdx < 0) return clean.filter((l) => l.trim()).join("\n").trim();

  // Find previous ❯/›/> <user text> (user input marker)
  let userIdx = -1;
  for (let i = idleIdx - 1; i >= 0; i--) {
    const t = clean[i].trim();
    if (t.startsWith("\u276F ") || t.startsWith("\u203A ") || t.startsWith("> ")) {
      userIdx = i;
      break;
    }
  }

  // If user input line not found (scrolled out of buffer), return everything
  // above the idle ❯ — the response is still visible even if the prompt isn't.
  const startIdx = userIdx < 0 ? 0 : userIdx + 1;

  return clean
    .slice(startIdx, idleIdx)
    .filter((line) => {
      const t = line.trim();
      return t && !SEP_RE.test(t) && !TUI_HINTS.some((h) => t.includes(h));
    })
    .join("\n")
    .trim();
}

// ---------------------------------------------------------------------------
// Backend-specific response extraction
// ---------------------------------------------------------------------------

function extractCodexResponse(clean: string[]): string {
  // Codex format: › <user input> ... • <response lines> ... › <idle suggestion>
  // Find the LAST TWO › lines: last one is idle suggestion, second-to-last is user input.
  const arrowIndices: number[] = [];
  for (let i = 0; i < clean.length; i++) {
    if (clean[i].trim().startsWith("\u203A")) arrowIndices.push(i);
  }
  if (arrowIndices.length < 2) return "";

  const idleIdx = arrowIndices[arrowIndices.length - 1];
  const userIdx = arrowIndices[arrowIndices.length - 2];

  return clean
    .slice(userIdx + 1, idleIdx)
    .filter((line) => {
      const t = line.trim();
      return t && !SEP_RE.test(t);
    })
    .map((line) => line.trim().replace(/^\u2022\s*/, "")) // strip • prefix
    .join("\n")
    .trim();
}

function extractOpencodeResponse(clean: string[]): string {
  // Opencode uses a split-pane TUI (220 cols): sidebar (█ ...) appears after conversation content.
  // Strip sidebar content (everything from █ U+2588 FULL BLOCK onwards) before processing.
  const stripped = clean.map((l) => l.replace(/\u2588.*$/, "").trimEnd());

  // Opencode layout:
  //   ┃  <user message>           ← user input (inside ┃ box)
  //   ┃                           ← empty ┃
  //   ┃  # Wrote weather.py       ← response content (ALSO inside ┃ box!)
  //   ┃    1 import requests      ← code (inside ┃)
  //   ┃  Thinking: ...            ← thinking (inside ┃)
  //                               ← ┃ box ends
  //   Done. Run with:             ← response content (outside ┃)
  //   ▣  Build · model · 22.8s   ← completion marker

  // Find last COMPLETED ▣ marker (must have duration suffix like "· 4.3s")
  let endIdx = -1;
  for (let i = stripped.length - 1; i >= 0; i--) {
    const s = stripped[i].trim();
    if (s.startsWith("\u25A3") && /\d+\.?\d*s/.test(s)) { endIdx = i; break; } // ▣ with duration
  }
  if (endIdx < 0) return "";

  // Walk backward from ▣ to find the user message ┃ block.
  // The user message is the FIRST ┃ line (with content) in the contiguous ┃ block.
  // After it, everything until ▣ is the response — both inside and outside ┃.

  // 1. Find the top of the ┃ block: walk backward through all ┃ lines (and non-┃ gaps).
  //    The user message is the first ┃-with-content we hit when scanning from the top.
  let blockTop = endIdx;
  for (let i = endIdx - 1; i >= 0; i--) {
    const t = stripped[i].trim();
    if (t.startsWith("\u2503") || t === "") {
      blockTop = i;
    } else {
      // Non-┃, non-empty line between ┃ block and ▣ is response content (outside box).
      // Keep walking up.
      blockTop = i;
    }
  }
  // blockTop is now 0 (top of visible buffer) — find the LAST user ┃ block start.
  // Strategy: find the last run of ┃ lines that ends before the response.
  // Simpler approach: find the first ┃-with-non-code-content going backward from ▣,
  // where we cross from non-┃ back into ┃ territory — that's the bottom of the user msg box.

  // Actually, the user message is inside a ┃ box. After the user box, there may be
  // response lines also in ┃, then response lines outside ┃, then ▣.
  // We need to find where the USER message ends. The user box is typically:
  //   ┃  <user text>
  //   ┃
  // followed by response ┃ lines starting with different content markers (code, #, etc.)

  // Practical approach: collect ALL content between line 0 and ▣, strip ┃ prefix,
  // and return everything. The user message at top is short (1-2 lines) and
  // including it is acceptable — it's better than losing the full response.
  const lines: string[] = [];
  for (let i = 0; i < endIdx; i++) {
    let line = stripped[i];
    // Strip ┃ prefix (with optional leading spaces)
    const t = line.trim();
    if (t.startsWith("\u2503")) {
      line = t.slice(1); // remove ┃
    }
    const trimmed = line.trim();
    if (trimmed && !SEP_RE.test(trimmed) && !TUI_HINTS.some((h) => trimmed.includes(h))) {
      lines.push(trimmed);
    }
  }
  return lines.join("\n").trim();
}

// ---------------------------------------------------------------------------
// Composed input detection
// ---------------------------------------------------------------------------

/**
 * Detect text typed into the input box but not yet submitted (Enter not pressed).
 * Walks from the bottom, skipping empty lines, separators, and TUI hint lines.
 * Returns the text content after ❯ if found, null otherwise.
 */
export function detectComposedInput(lines: string[]): string | null {
  const clean = stripAnsiLines(lines);
  for (let i = clean.length - 1; i >= 0; i--) {
    const t = normTrim(clean[i]);
    if (t === "") continue;
    if (SEP_RE.test(t)) continue;
    if (TUI_HINTS.some((h) => t.includes(h))) continue;
    if (t.startsWith("\u276F ") || t.startsWith("\u203A ") || t.startsWith("> ")) {
      return t.slice(2);
    }
    break;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Model picker detection
// ---------------------------------------------------------------------------

// Claude Code CLI numbered picker:
//   1. Default (recommended)  Sonnet 4.6 · Best for everyday tasks
// ❯ 3. Opus ✔                 Opus 4.6 · Most capable for complex work
const MODEL_PICKER_RE =
  /^(?<prefix>\s*(?:[❯►▶>]\s+)?)(?<num>\d+)\.\s+(?<label>\S+(?:\s+\([^)]*\))?)(?<rest>.*)/;

// Cursor Agent autocomplete dropdown:
//   → /model Auto
//     /model Composer 1.5
const CURSOR_MODEL_RE = /^\s*(?<arrow>→)?\s*\/model\s+(?<name>.+?)\s*$/;

// "↓ more below" scroll indicator
const MORE_BELOW_RE = /^\s*↓\s*more below\s*$/;

export interface CursorDropdownResult {
  items: ChoiceItem[];
  hasMore: boolean;
}

export function detectCursorModelDropdown(tail: string[]): CursorDropdownResult | null {
  const items: ChoiceItem[] = [];
  let hasMore = false;

  for (const line of tail) {
    const m = line.match(CURSOR_MODEL_RE);
    if (m) {
      const name = (m.groups?.name ?? "").trim();
      const selected = m.groups?.arrow !== undefined;
      items.push({ key: String(items.length + 1), label: name, selected });
      continue;
    }
    if (MORE_BELOW_RE.test(line)) {
      hasMore = true;
      continue;
    }
    if (items.length > 0) break;
  }

  if (items.length === 0) return null;
  return { items, hasMore };
}

export function detectModelPicker(lines: string[], backend = ""): ChoiceItem[] | null {
  const clean = stripAnsiLines(lines);
  const tail = clean.slice(-30);

  // Cursor Agent dropdown
  if (backend !== "claude") {
    const result = detectCursorModelDropdown(tail);
    if (result) return result.items;
    if (backend === "cursor") return detectChoices(lines);
  }

  // Claude Code CLI numbered picker
  let items: ChoiceItem[] = [];
  let block: ChoiceItem[] = [];

  for (const line of tail) {
    // Strip leading box-drawing border chars (e.g. "│  " panel borders) so the
    // picker regex matches even when the TUI renders items inside a bordered box.
    const stripped = line.replace(/^[\u2500-\u257F]+\s*/, "");
    const m = stripped.match(MODEL_PICKER_RE);
    if (m) {
      const prefix = m.groups?.prefix ?? "";
      let label = (m.groups?.label ?? "").trim();
      const num = m.groups?.num ?? "";
      const rest = m.groups?.rest ?? "";
      const selected = /[❯►▶>]/.test(prefix);
      if (/[✔✓]/.test(rest)) label += " \u2714";
      block.push({ key: num, label, selected });
    } else {
      if (block.length) {
        items = [...block];
        block = [];
      }
    }
  }
  if (block.length) items = block;

  if (items.length >= 2) return items;
  return detectChoices(lines);
}
