/**
 * approve.test.ts — Tests for approveChoice target resolution.
 *
 * approveChoice calls navigateAndSelect(name, from, to) which needs tmux,
 * so we test the resolution logic by extracting the same algorithm and
 * verifying it picks the correct index.
 *
 * Also tests the full approve flow by verifying that `readState` on
 * choosing fixtures produces the right state for `ccc approve` to handle.
 */

import { describe, test, expect } from "vitest";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { detectChoices, detectPermission, type ChoiceItem } from "../../../src/parser.js";
import { classifyWindow } from "../../../src/session.js";

// ---------------------------------------------------------------------------
// resolveChoiceTarget — extracted from approveChoice logic in session.ts
// ---------------------------------------------------------------------------

function resolveChoiceTarget(choices: ChoiceItem[], answer: string): number {
  if (/^\d+$/.test(answer)) return parseInt(answer, 10) - 1;
  if (answer === "yes") return 0;
  if (answer === "no") return choices.length - 1;
  const lower = answer.toLowerCase();
  return choices.findIndex((c) => c.label.toLowerCase().includes(lower));
}

// ---------------------------------------------------------------------------
// Unit tests for choice resolution
// ---------------------------------------------------------------------------

describe("resolveChoiceTarget", () => {
  const trustChoices: ChoiceItem[] = [
    { label: "1. Yes, I trust this folder", selected: true },
    { label: "2. No, exit", selected: false },
  ];

  test("'yes' resolves to first option (index 0)", () => {
    expect(resolveChoiceTarget(trustChoices, "yes")).toBe(0);
  });

  test("'no' resolves to last option (index 1)", () => {
    expect(resolveChoiceTarget(trustChoices, "no")).toBe(1);
  });

  test("'1' resolves to index 0 (1-based)", () => {
    expect(resolveChoiceTarget(trustChoices, "1")).toBe(0);
  });

  test("'2' resolves to index 1 (1-based)", () => {
    expect(resolveChoiceTarget(trustChoices, "2")).toBe(1);
  });

  test("substring 'trust' matches first option", () => {
    expect(resolveChoiceTarget(trustChoices, "trust")).toBe(0);
  });

  test("substring 'exit' matches second option", () => {
    expect(resolveChoiceTarget(trustChoices, "exit")).toBe(1);
  });

  test("no match returns -1", () => {
    expect(resolveChoiceTarget(trustChoices, "foobar")).toBe(-1);
  });

  // 3-option permission-style choices
  const permChoices: ChoiceItem[] = [
    { label: "1. Yes", selected: true },
    { label: "2. Yes, allow reading from repos/", selected: false },
    { label: "3. No", selected: false },
  ];

  test("'no' resolves to last of 3 options", () => {
    expect(resolveChoiceTarget(permChoices, "no")).toBe(2);
  });

  test("'3' resolves to index 2", () => {
    expect(resolveChoiceTarget(permChoices, "3")).toBe(2);
  });

  test("substring 'allow' matches second option", () => {
    expect(resolveChoiceTarget(permChoices, "allow")).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Fixture-based: verify trust dialog is classified as choosing, not approval
// ---------------------------------------------------------------------------

describe("workspace trust dialog classification", () => {
  const fixtureDir = resolve("tests/fixtures/005-claude-choosing");

  // Skip if fixture doesn't exist (e.g. CI without fixtures)
  const hasFixture = existsSync(join(fixtureDir, "frames"));
  if (!hasFixture) return;

  const framesDir = join(fixtureDir, "frames");
  const files = readdirSync(framesDir)
    .filter((f) => /^\d+\.txt$/.test(f))
    .sort();
  const frames = files.map((f) =>
    readFileSync(join(framesDir, f), "utf8").split("\n"),
  );
  const lastFrame = frames[frames.length - 1];

  test("detectPermission returns null (not a permission prompt)", () => {
    expect(detectPermission(lastFrame)).toBeNull();
  });

  test("detectChoices returns trust options", () => {
    const choices = detectChoices(lastFrame);
    expect(choices).not.toBeNull();
    expect(choices!.length).toBe(2);
    expect(choices![0].label).toContain("Yes, I trust this folder");
    expect(choices![1].label).toContain("No, exit");
  });

  test("classifyWindow returns state: choosing", () => {
    const frameTexts = frames.map((f) => f.join("\n"));
    const state = classifyWindow(frameTexts, "claude");
    expect(state.state).toBe("choosing");
  });

  test("ccc approve yes would select first option", () => {
    const choices = detectChoices(lastFrame)!;
    expect(resolveChoiceTarget(choices, "yes")).toBe(0);
  });

  test("ccc approve no would select last option", () => {
    const choices = detectChoices(lastFrame)!;
    expect(resolveChoiceTarget(choices, "no")).toBe(1);
  });
});
