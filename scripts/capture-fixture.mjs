#!/usr/bin/env node
/**
 * capture-fixture.mjs — Capture N frames from a live ccc session as a fixture.
 *
 * Usage:
 *   node scripts/capture-fixture.mjs <session> <fixture-dir> [options]
 *
 * Options:
 *   --frames N        Number of frames to capture (default: 5)
 *   --interval MS     Milliseconds between frames (default: 200)
 *   --lines N         Lines per frame (default: 40)
 *   --backend STR     Backend name for manifest (default: "claude")
 *   --scenario STR    Scenario name for manifest (default: "ready")
 *   --description STR Description for manifest (default: auto-generated)
 *
 * Examples:
 *   # Capture 5 frames at 200ms intervals from session "fix-cursor"
 *   node scripts/capture-fixture.mjs fix-cursor tests/fixtures/007-cursor-ready
 *
 *   # Capture 3 frames at 100ms from "fix-cursor", backend=cursor
 *   node scripts/capture-fixture.mjs fix-cursor tests/fixtures/008-cursor-thinking \
 *     --frames 3 --interval 100 --backend cursor --scenario thinking
 *
 * Requires: ccc binary in PATH (or /opt/homebrew/bin/ccc)
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

// ── Parse args ───────────────────────────────────────────────────────────────

const args = process.argv.slice(2);
const flags = {};
const positional = [];

for (let i = 0; i < args.length; i++) {
  if (args[i].startsWith("--") && i + 1 < args.length) {
    flags[args[i].slice(2)] = args[++i];
  } else {
    positional.push(args[i]);
  }
}

const session = positional[0];
const fixtureDir = positional[1];

if (!session || !fixtureDir) {
  process.stderr.write(
    "Usage: node scripts/capture-fixture.mjs <session> <fixture-dir> [--frames N] [--interval MS] [--lines N] [--backend STR] [--scenario STR] [--description STR]\n",
  );
  process.exit(1);
}

const numFrames = parseInt(flags.frames ?? "5", 10);
const intervalMs = parseInt(flags.interval ?? "200", 10);
const numLines = parseInt(flags.lines ?? "40", 10);
const backend = flags.backend ?? "claude";
const scenario = flags.scenario ?? "ready";
const description =
  flags.description ?? `${backend} ${scenario} — captured by capture-fixture.mjs`;

// ── Find ccc binary ──────────────────────────────────────────────────────────

function findCcc() {
  for (const p of ["/opt/homebrew/bin/ccc"]) {
    if (existsSync(p)) return p;
  }
  try {
    return execFileSync("which", ["ccc"], { encoding: "utf8" }).trim();
  } catch {
    process.stderr.write("Error: ccc binary not found\n");
    process.exit(1);
  }
}

const CCC = findCcc();

// ── Capture frames ───────────────────────────────────────────────────────────

const framesDir = join(fixtureDir, "frames");
mkdirSync(framesDir, { recursive: true });

const manifest = {
  backend,
  scenario,
  description,
  captured_at: new Date().toISOString(),
  ccc_session: session,
  frame_interval_ms: intervalMs,
  frames: [],
};

function sleep(ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    /* busy-wait for sub-second precision */
  }
}

for (let i = 0; i < numFrames; i++) {
  const frameNum = String(i + 1).padStart(2, "0");
  const fileName = `${frameNum}.txt`;
  const elapsedMs = i * intervalMs;

  try {
    const content = execFileSync(
      CCC,
      ["tail", session, "--lines", String(numLines)],
      { encoding: "utf8", timeout: 5000 },
    );
    writeFileSync(join(framesDir, fileName), content);
    manifest.frames.push({
      file: fileName,
      elapsed_ms: elapsedMs,
      label: i === 0 ? `t=0` : `t+${elapsedMs}ms`,
    });
    process.stderr.write(`  captured ${fileName} (${elapsedMs}ms)\n`);
  } catch (err) {
    process.stderr.write(`  FAILED ${fileName}: ${err.message}\n`);
    break;
  }

  if (i < numFrames - 1) {
    sleep(intervalMs);
  }
}

// ── Write manifest ───────────────────────────────────────────────────────────

writeFileSync(
  join(fixtureDir, "manifest.json"),
  JSON.stringify(manifest, null, 2) + "\n",
);

// ── Write stub expected.json ─────────────────────────────────────────────────

const stub = {
  description,
  backend,
  detectReady: { isReady: null, confidence: null },
  extractLastResponse: null,
  detectPermission: null,
};

writeFileSync(
  join(fixtureDir, "expected.json"),
  JSON.stringify(stub, null, 2) + "\n",
);

process.stderr.write(
  `\nDone: ${manifest.frames.length} frames → ${fixtureDir}\n`,
);
process.stderr.write(`Run /gen-fixture-expected to fill expected.json\n`);
