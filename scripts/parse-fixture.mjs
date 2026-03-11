#!/usr/bin/env node
/**
 * parse-fixture.mjs — Run parser functions against fixture frame files.
 *
 * Usage:
 *   node scripts/parse-fixture.mjs <frame1.txt> [frame2.txt ...]
 *
 * Output: JSON with results of detectReady (heartbeat), detectReadySingleFrame,
 *         extractLastResponse, and detectPermission.
 *
 * Requires: npm run build  (dist/parser.js must exist)
 */

import { readFileSync } from "node:fs";
import { detectReady, extractLastResponse, detectPermission } from "../dist/parser.js";

const framePaths = process.argv.slice(2);
if (framePaths.length === 0) {
  process.stderr.write("Usage: node scripts/parse-fixture.mjs <frame1.txt> [frame2.txt ...]\n");
  process.exit(1);
}

// Load each frame as a string[]
const frames = framePaths.map((p) => readFileSync(p, "utf8").split("\n"));
const lastFrame = frames[frames.length - 1];

/**
 * Simulate the waitReady() polling loop: poll every intervalMs,
 * passing prevLines from the previous iteration and accumulating elapsed time.
 * This is the same logic used by `ccc status` and `ccc send` internally.
 */
function simulateHeartbeat(frames, intervalMs = 300, minStableSecs = 0.8) {
  let prevLines = null;
  let result = { isReady: false, confidence: "not_ready", text: "" };
  for (let i = 0; i < frames.length; i++) {
    const elapsed = (i * intervalMs) / 1000;
    result = detectReady(frames[i], prevLines, elapsed, minStableSecs);
    prevLines = frames[i];
  }
  return result;
}

const heartbeat = simulateHeartbeat(frames);
const singleFrame = detectReady(frames[0], null, 0);

const output = {
  detectReady: {
    isReady: heartbeat.isReady,
    confidence: heartbeat.confidence,
  },
  detectReadySingleFrame: {
    isReady: singleFrame.isReady,
    confidence: singleFrame.confidence,
  },
  extractLastResponse: extractLastResponse(lastFrame),
  detectPermission: detectPermission(lastFrame),
};

process.stdout.write(JSON.stringify(output, null, 2) + "\n");
