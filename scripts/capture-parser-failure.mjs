#!/usr/bin/env node
/**
 * capture-parser-failure.mjs
 *
 * Captures a live terminal frame from a running CCC session and saves it as a
 * new parser fixture for regression testing.
 *
 * Usage:
 *   node scripts/capture-parser-failure.mjs <session-name> [--backend <backend>] [--description <desc>]
 *
 * Example:
 *   node scripts/capture-parser-failure.mjs my-session --backend opencode --description "opencode ready state misparse"
 *
 * This will:
 *   1. Run `ccc tail <session-name> --lines 50` to capture the current pane
 *   2. Save it to tests/fixtures/<NNN>-capture-<slug>/frames/01.txt
 *   3. Infer initial expected.json by running the parser on the captured frame
 *   4. Print a diff between parser output and what you should correct it to
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import { stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const __dir = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dir, "..");
const FIXTURES_DIR = path.join(ROOT, "tests", "fixtures");

// ---------------------------------------------------------------------------
// Parser — inline import
// ---------------------------------------------------------------------------

async function runParser(frameLines, backend) {
  // Import parser functions from built dist
  const parserPath = path.join(ROOT, "dist", "src", "parser.js");
  try {
    const mod = await import(parserPath);
    const ready = mod.detectReady(frameLines, null, 0, 0.8, backend);
    const response = mod.extractLastResponse(frameLines, backend);
    const permission = mod.detectPermission ? mod.detectPermission(frameLines) : null;
    return { ready, response, permission };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Next fixture number
// ---------------------------------------------------------------------------

async function nextFixtureNumber() {
  const dirs = await readdir(FIXTURES_DIR).catch(() => []);
  const nums = dirs
    .map((d) => parseInt(d.slice(0, 3), 10))
    .filter((n) => !isNaN(n));
  const max = nums.length ? Math.max(...nums) : 22;
  return String(max + 1).padStart(3, "0");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const args = process.argv.slice(2);
  const sessionName = args[0];
  if (!sessionName) {
    console.error("Usage: capture-parser-failure.mjs <session-name> [--backend <b>] [--description <d>]");
    process.exit(1);
  }

  let backend = "claude";
  let description = "";
  for (let i = 1; i < args.length; i++) {
    if (args[i] === "--backend" && args[i + 1]) backend = args[++i];
    if (args[i] === "--description" && args[i + 1]) description = args[++i];
  }

  console.log(`Capturing frame from session: ${sessionName} (backend: ${backend})`);

  // Capture frame via ccc tail
  let frameText;
  try {
    const { stdout } = await exec("ccc", ["tail", sessionName, "--lines", "50"], {
      timeout: 10_000,
    });
    frameText = stdout;
  } catch (err) {
    console.error(`Failed to capture frame: ${err.message}`);
    console.error("Make sure the session is running and ccc is on PATH.");
    process.exit(1);
  }

  const frameLines = frameText.split("\n");

  // Run parser to get current (possibly wrong) output
  const parserOut = await runParser(frameLines, backend);

  // Determine fixture name
  const num = await nextFixtureNumber();
  const slug = (description || sessionName)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
  const fixtureName = `${num}-capture-${slug}`;
  const fixtureDir = path.join(FIXTURES_DIR, fixtureName);
  const framesDir = path.join(fixtureDir, "frames");

  await mkdir(framesDir, { recursive: true });
  await writeFile(path.join(framesDir, "01.txt"), frameText);

  // Build expected.json from parser output (with review flags)
  const expected = {
    description: description || `Capture from session: ${sessionName}`,
    backend,
    _review: "NEEDS MANUAL REVIEW — verify and correct the expected values below",
    detectReady: parserOut
      ? { isReady: parserOut.ready.isReady, confidence: parserOut.ready.confidence }
      : { isReady: false, confidence: "not_ready" },
    extractLastResponse: parserOut?.response ?? "",
    detectPermission: parserOut?.permission ?? null,
  };

  await writeFile(
    path.join(fixtureDir, "expected.json"),
    JSON.stringify(expected, null, 2),
  );

  console.log(`\nFixture created: tests/fixtures/${fixtureName}/`);
  console.log(`  frames/01.txt       — raw terminal capture`);
  console.log(`  expected.json       — NEEDS REVIEW`);

  if (parserOut) {
    console.log(`\nParser currently reports:`);
    console.log(`  detectReady:         isReady=${parserOut.ready.isReady}, confidence=${parserOut.ready.confidence}`);
    console.log(`  extractLastResponse: ${JSON.stringify(parserOut.response?.slice(0, 80))}${parserOut.response?.length > 80 ? "..." : ""}`);
    console.log(`  detectPermission:    ${parserOut.permission ? JSON.stringify(parserOut.permission.type) : "null"}`);
  } else {
    console.log("\nParser dist not built — run `npm run build` first to get parser output.");
  }

  console.log(`\nNext steps:`);
  console.log(`  1. Open tests/fixtures/${fixtureName}/expected.json`);
  console.log(`  2. Correct any wrong expected values`);
  console.log(`  3. Remove the "_review" field`);
  console.log(`  4. Run: npm test -- --reporter=verbose tests/unit/ts/parser.fixtures.test.ts`);
  console.log(`  5. Fix parser.ts until the new fixture passes`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
