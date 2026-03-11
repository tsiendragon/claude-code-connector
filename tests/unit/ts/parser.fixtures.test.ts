/**
 * parser.fixtures.test.ts — Data-driven parser tests.
 *
 * Automatically discovers every folder under tests/fixtures/ and runs
 * parser functions against the frames, comparing results to expected.json.
 *
 * HOW TO ADD A NEW TEST CASE
 * ───────────────────────────
 * 1. Create a folder: tests/fixtures/<NNN>-<short-description>/
 *
 * 2. Add terminal capture frame(s):
 *      Single frame  → frames/01.txt
 *      Multi-frame   → frames/01.txt, frames/02.txt, frames/03.txt, ...
 *
 *    Content of each .txt is the raw terminal output exactly as captured,
 *    ANSI codes stripped. Easiest way to get it: `ccc tail <name> --lines 40`
 *
 *    For heartbeat tests (status confirmation), capture the same pane at
 *    multiple points in time and save each as a separate numbered .txt file.
 *    The test will simulate polling at 300ms intervals.
 *
 * 3. Add expected.json — see gen-fixture-expected skill for all keys.
 *
 * 4. Run tests: npm test
 *    The new case is picked up automatically — no code changes needed.
 *
 * FIXTURE FOLDER STRUCTURE
 * ─────────────────────────
 * tests/fixtures/
 * ├── 001-idle-fresh/
 * │   ├── frames/01.txt        ← single-frame: no heartbeat needed
 * │   └── expected.json
 * ├── 004-heartbeat-stable/
 * │   ├── frames/
 * │   │   ├── 01.txt           ← frame captured at t=0ms
 * │   │   ├── 02.txt           ← frame captured at t=300ms
 * │   │   ├── 03.txt           ← frame captured at t=600ms
 * │   │   └── 04.txt           ← frame captured at t=900ms (→ stable)
 * │   └── expected.json
 * └── ...
 */

import { describe, test, expect } from "vitest";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  detectReady,
  extractLastResponse,
  detectPermission,
  detectChoices,
  detectComposedInput,
  detectModelPicker,
  type ReadyResult,
  type ChoiceItem,
} from "../../../src/parser.js";
import {
  classifyWindow,
  type PaneState,
} from "../../../src/session.js";
import { simulateHeartbeat, simulateAwaitFrameMatch } from "./helpers.js";

// ---------------------------------------------------------------------------
// Fixture discovery
// ---------------------------------------------------------------------------

const FIXTURES_DIR = resolve("tests/fixtures");

/** Load all frame .txt files from a fixture's frames/ subdirectory, sorted. */
function loadFrames(fixtureDir: string): string[][] {
  const framesDir = join(fixtureDir, "frames");
  if (!existsSync(framesDir)) return [];
  return readdirSync(framesDir)
    .filter((f) => /^\d+\.txt$/.test(f))
    .sort()
    .map((f) => readFileSync(join(framesDir, f), "utf8").split("\n"));
}

interface PermissionExpected {
  type: string;
  tool?: string;
  options: Array<{ key: string; label: string; selected: boolean }>;
}

interface Expected {
  description: string;
  backend?: string;
  detectReady: { isReady: boolean; confidence: string };
  detectReadySingleFrame?: { isReady: boolean; confidence: string };
  extractLastResponse: string;
  detectPermission: PermissionExpected | null;
  // optional keys — only present when the fixture exercises the feature
  detectChoices?: ChoiceItem[] | null;
  detectComposedInput?: string | null;
  detectModelPicker?: ChoiceItem[] | null;
  classifyWindow?: { state: PaneState["state"] };
  awaitFrameMatch?: Array<{
    description: string;
    predicate: string;          // "ready" | "thinking" | "approval" | "choosing" | "any-change"
    beforeText?: "frame:NN";    // e.g. "frame:01" — use frame 01 as beforeText
    stableThreshold?: number;
    expectMatch: boolean;
    matchIndex?: number;        // 0-based frame index where predicate first matches
    matchState?: string;        // expected state at matchIndex
  }>;
}

/** Discover all fixture case directories, sorted by name. */
const fixtureCases = readdirSync(FIXTURES_DIR, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name)
  .sort();

// ---------------------------------------------------------------------------
// Data-driven test suite
// ---------------------------------------------------------------------------

describe.each(fixtureCases)("fixture: %s", (caseName) => {
  const caseDir = join(FIXTURES_DIR, caseName);
  const frames = loadFrames(caseDir);
  const expected: Expected = JSON.parse(
    readFileSync(join(caseDir, "expected.json"), "utf8"),
  );

  const backend = expected.backend ?? "claude";

  // The last frame is used for non-heartbeat tests (extractLastResponse, detectPermission, etc.)
  const lastFrame = frames[frames.length - 1] ?? [];

  // ── detectReady (heartbeat over all frames) ──────────────────────────────
  test("detectReady — heartbeat result", () => {
    const result = simulateHeartbeat(frames, 300, 0.8, backend);
    expect(result.isReady).toBe(expected.detectReady.isReady);
    expect(result.confidence).toBe(expected.detectReady.confidence);
  });

  // ── detectReady (single frame, no prevLines) ─────────────────────────────
  // Only asserted when expected.json includes "detectReadySingleFrame".
  // Use this to show that a single-frame check gives a DIFFERENT (wrong) result
  // compared to heartbeat — proving why heartbeat is necessary for status.
  if (expected.detectReadySingleFrame) {
    test("detectReady — single frame (no heartbeat)", () => {
      const result: ReadyResult = detectReady(frames[0], null, 0, 0.8, backend);
      expect(result.isReady).toBe(expected.detectReadySingleFrame!.isReady);
      expect(result.confidence).toBe(expected.detectReadySingleFrame!.confidence);
    });
  }

  // ── extractLastResponse (on the last frame) ──────────────────────────────
  test("extractLastResponse — last frame", () => {
    const result = extractLastResponse(lastFrame, backend);
    expect(result).toBe(expected.extractLastResponse);
  });

  // ── detectPermission (on the last frame) ─────────────────────────────────
  test("detectPermission — last frame", () => {
    const result = detectPermission(lastFrame);
    if (expected.detectPermission === null) {
      expect(result).toBeNull();
    } else {
      expect(result).not.toBeNull();
      expect(result?.type).toBe(expected.detectPermission.type);
      if (expected.detectPermission.tool !== undefined) {
        expect(result?.tool).toBe(expected.detectPermission.tool);
      }
      expected.detectPermission.options.forEach((exp, i) => {
        expect(result?.options[i]?.key).toBe(exp.key);
        expect(result?.options[i]?.label).toBe(exp.label);
        expect(result?.options[i]?.selected).toBe(exp.selected);
      });
    }
  });

  // ── detectChoices (on the last frame) ────────────────────────────────────
  if ("detectChoices" in expected) {
    test("detectChoices — last frame", () => {
      const result = detectChoices(lastFrame);
      if (expected.detectChoices === null) {
        expect(result).toBeNull();
      } else {
        expect(result).not.toBeNull();
        (expected.detectChoices as ChoiceItem[]).forEach((exp, i) => {
          expect(result![i]?.key).toBe(exp.key);
          expect(result![i]?.label).toBe(exp.label);
          expect(result![i]?.selected).toBe(exp.selected);
        });
      }
    });
  }

  // ── detectComposedInput (on the last frame) ──────────────────────────────
  if ("detectComposedInput" in expected) {
    test("detectComposedInput — last frame", () => {
      const result = detectComposedInput(lastFrame);
      expect(result).toBe(expected.detectComposedInput ?? null);
    });
  }

  // ── detectModelPicker (on the last frame) ────────────────────────────────
  if ("detectModelPicker" in expected) {
    test("detectModelPicker — last frame", () => {
      const result = detectModelPicker(lastFrame, backend);
      if (expected.detectModelPicker === null) {
        expect(result).toBeNull();
      } else {
        expect(result).not.toBeNull();
        (expected.detectModelPicker as ChoiceItem[]).forEach((exp, i) => {
          expect(result![i]?.key).toBe(exp.key);
          expect(result![i]?.label).toBe(exp.label);
          expect(result![i]?.selected).toBe(exp.selected);
        });
      }
    });
  }

  // ── classifyWindow — pure frame-window classification ────────────────────
  // Tests the core state machine logic of readState() and awaitFrameMatch().
  // Feeds all fixture frames (as pre-stripped strings) into classifyWindow.
  // Requires at least 2 frames to be meaningful (single-frame is trivially stable).
  if ("classifyWindow" in expected && frames.length >= 2) {
    test("classifyWindow — full frame sequence", () => {
      const frameTexts = frames.map((f) => f.join("\n"));
      const result = classifyWindow(frameTexts, backend);
      expect(result.state).toBe(expected.classifyWindow!.state);
    });
  }

  // ── awaitFrameMatch — sliding-window predicate matching ────────────────
  // Simulates awaitFrameMatch's core loop: sliding window + predicate.
  // Each case in the array specifies a predicate, optional beforeText,
  // and the expected match result.
  if (expected.awaitFrameMatch && frames.length >= 3) {
    const frameTexts = frames.map((f) => f.join("\n"));

    describe.each(expected.awaitFrameMatch.map((c, i) => [i, c] as const))(
      "awaitFrameMatch[%i] — %s",
      (_i, c) => {
        test(c.description, () => {
          // Resolve beforeText from "frame:NN" reference
          let beforeText: string | null = null;
          if (c.beforeText) {
            const m = c.beforeText.match(/^frame:(\d+)$/);
            if (m) {
              const idx = parseInt(m[1], 10) - 1; // 1-based to 0-based
              beforeText = frameTexts[idx] ?? null;
            }
          }

          // Build predicate from name
          const predicate = (s: PaneState, prev: string | null): boolean => {
            if (c.predicate === "any-change") {
              return prev !== null && s.lines.join("\n") !== prev;
            }
            return s.state === c.predicate;
          };

          const result = simulateAwaitFrameMatch(
            frameTexts,
            backend,
            predicate,
            beforeText,
            c.stableThreshold ?? 3,
          );

          if (!c.expectMatch) {
            expect(result).toBeNull();
          } else {
            expect(result).not.toBeNull();
            if (c.matchIndex !== undefined) {
              expect(result!.matchIndex).toBe(c.matchIndex);
            }
            if (c.matchState !== undefined) {
              expect(result!.state.state).toBe(c.matchState);
            }
          }
        });
      },
    );
  }
});
