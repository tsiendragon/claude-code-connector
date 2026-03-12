#!/usr/bin/env node
/**
 * cli.ts — ccc CLI entry point.
 *
 * Commands (tmux mode):
 *   run      <name> [--cwd] [--cursor]          Start a session
 *   attach   <name>                             Attach (verify alive)
 *   send     <name> <msg> [--no-wait] [--auto-approve] [--timeout]
 *   tail     <name> [--lines N] [--full]        Print last N lines
 *   last     <name> [--raw] [--full]            Extract last response
 *   status   <name> [--json] [--porcelain]      Show session state
 *   ps       [--json]                           List all sessions
 *   kill     <name>                             Kill a session
 *   clean    [--yes] [--dry-run]               Remove dead sessions
 *   interrupt <name>                            Send Ctrl-C
 *   approve  <name> [yes|always|no]             Respond to permission prompt
 *   input    <name> <text> [--no-enter]        Type arbitrary text
 *   key      <name> <keys...> [--repeat N]     Send special keys
 *   read     <name> [--json] [--full]          Read structured pane state
 *   wait     <name> <state> [--timeout T] [--json]  Block until state
 *   relay    debate|collab ...                  Two-Claude relay
 */

import { createInterface } from "readline";
import { spawn as nodeSpawn } from "child_process";
import { defineCommand, runMain } from "citty";
import {
  runSession,
  killSession,
  send,
  waitReady,
  waitState,
  readState,
  approvePermission,
  approveChoice,
  type PaneState,
} from "./session.js";
import {
  isAlive,
  listSessions,
  capturePane,
  captureFull,
  sendText,
  sendKey,
} from "./transport.js";
import {
  stripAnsiLines,
  extractLastResponse,
  detectModelPicker,
  detectCursorModelDropdown,
} from "./parser.js";
import { storeGet, storeList, storeDelete } from "./store.js";
import {
  getHistoryDir,
  listSessionsWithHistory,
  listSessionRuns,
  readHistoryFile,
  readFullSessionHistory,
} from "./history.js";
import { runRelay } from "./relay.js";
import { runGroupChat } from "./groupchat.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function die(msg: string): never {
  console.error(`Error: ${msg}`);
  process.exit(1);
}

function requireSession(name: string) {
  const rec = storeGet(name);
  if (!rec) die(`No session '${name}'. Run: ccc run ${name}`);
  return rec;
}

function printState(state: PaneState, asJson: boolean) {
  if (asJson) {
    console.log(JSON.stringify(state, null, 2));
  } else {
    console.log(`state: ${state.state}`);
    if (state.permission) console.log(`permission: ${state.permission.tool}`);
    if (state.choices) console.log(`choices: ${state.choices.map((c) => c.label).join(", ")}`);
    if (state.lastResponse) console.log(`\n${state.lastResponse}`);
  }
}

/** Capture pane (or full scrollback) and strip ANSI codes. */
const captureClean = (name: string, full: boolean): Promise<string[]> =>
  (full ? captureFull(name) : capturePane(name)).then(stripAnsiLines);

function printRelayTurn(turn: { round: number; speaker: string; content: string; costUsd: number }, truncate = false) {
  console.log(`\n[Round ${turn.round}] ${turn.speaker}:`);
  if (truncate && turn.content.length > 500) {
    console.log(turn.content.slice(0, 500));
    console.log("...(truncated)");
  } else {
    console.log(turn.content);
  }
  if (turn.costUsd) console.log(`(cost: $${turn.costUsd.toFixed(4)})`);
}

function printRelayResult(result: { finalState: string; totalCostUsd: number }) {
  console.log(`\nFinal state: ${result.finalState}`);
  console.log(`Total cost: $${result.totalCostUsd.toFixed(4)}`);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

const run = defineCommand({
  meta: { description: "Start a new Claude, Cursor, Codex, or Opencode session" },
  args: {
    name: { type: "positional", required: true, description: "Session name" },
    cwd: { type: "string", default: process.cwd(), description: "Working directory" },
    cursor: { type: "boolean", default: false, description: "Use Cursor agent" },
    codex: { type: "boolean", default: false, description: "Use OpenAI Codex" },
    opencode: { type: "boolean", default: false, description: "Use opencode" },
    startup: { type: "string", default: "2000", description: "Startup wait ms" },
  },
  async run({ args }) {
    let command = "claude";
    let backend = "claude";
    if (args.cursor) { command = "cursor-agent"; backend = "cursor"; }
    else if (args.codex) { command = "codex"; backend = "codex"; }
    else if (args.opencode) { command = "opencode"; backend = "opencode"; }
    await runSession(args.name, args.cwd as string, command, backend, parseInt(args.startup as string));
    console.log(`Session '${args.name}' started (${command})`);
  },
});

const attach = defineCommand({
  meta: { description: "Check that an existing session is alive" },
  args: {
    name: { type: "positional", required: true },
  },
  async run({ args }) {
    requireSession(args.name);
    const alive = await isAlive(args.name);
    if (!alive) die(`Session '${args.name}' tmux session not found`);
    console.log(`Session '${args.name}' is alive`);
  },
});

const sendCmd = defineCommand({
  meta: { description: "Send a message and optionally wait for response" },
  args: {
    name: { type: "positional", required: true },
    message: { type: "positional", required: true },
    "no-wait": { type: "boolean", default: false },
    "auto-approve": { type: "boolean", default: false },
    timeout: { type: "string", default: "300" },
    json: { type: "boolean", default: false },
  },
  async run({ args }) {
    requireSession(args.name);
    const state = await send(args.name, args.message as string, {
      noWait: args["no-wait"] as boolean,
      autoApprove: args["auto-approve"] as boolean,
      timeout: parseInt(args.timeout as string),
    });
    if (args.json) {
      console.log(JSON.stringify(state, null, 2));
    } else if (state.lastResponse) {
      console.log(state.lastResponse);
    }
  },
});

const tail = defineCommand({
  meta: { description: "Print last N lines of pane output" },
  args: {
    name: { type: "positional", required: true },
    lines: { type: "string", default: "40" },
    full: { type: "boolean", default: false },
  },
  async run({ args }) {
    requireSession(args.name);
    const clean = await captureClean(args.name, args.full as boolean);
    const n = parseInt(args.lines as string);
    // Drop trailing blank lines so output is non-empty when pane has content
    const trimmed = clean.slice(-n);
    let end = trimmed.length;
    while (end > 0 && trimmed[end - 1].trim() === "") end--;
    console.log(trimmed.slice(0, end).join("\n"));
  },
});

const last = defineCommand({
  meta: { description: "Extract the last Claude response" },
  args: {
    name: { type: "positional", required: true },
    raw: { type: "boolean", default: false },
    full: { type: "boolean", default: false },
  },
  async run({ args }) {
    const rec = requireSession(args.name);
    const lines = await captureClean(args.name, args.full as boolean);
    if (args.raw) {
      console.log(lines.join("\n"));
    } else {
      console.log(extractLastResponse(lines, rec.backend));
    }
  },
});

const status = defineCommand({
  meta: { description: "Show session state (alias for: ccc read --porcelain)" },
  args: {
    name: { type: "positional", required: true },
    json: { type: "boolean", default: false },
    porcelain: { type: "boolean", default: false },
  },
  async run({ args }) {
    requireSession(args.name);
    const state = await readState(args.name);
    if (args.porcelain) {
      console.log(state.state);
    } else {
      printState(state, args.json as boolean);
    }
  },
});

function relativeTime(epochSecs: number): string {
  const diff = Math.floor(Date.now() / 1000 - epochSecs);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

const ps = defineCommand({
  meta: { description: "List all known sessions" },
  args: {
    json: { type: "boolean", default: false },
  },
  async run({ args }) {
    const records = storeList();
    const withStatus = await Promise.all(
      records.map(async (r) => ({ ...r, alive: await isAlive(r.name) })),
    );

    if (args.json) {
      console.log(JSON.stringify(withStatus, null, 2));
      return;
    }

    if (withStatus.length === 0) {
      console.log("No sessions.");
      return;
    }

    // Column widths (at least header width)
    const W = {
      name:    Math.max(4,  ...withStatus.map((r) => r.name.length)),
      backend: Math.max(7,  ...withStatus.map((r) => r.backend.length)),
      seen:    Math.max(9,  ...withStatus.map((r) => relativeTime(r.last_seen_at).length)),
    };

    const header =
      `  ${"NAME".padEnd(W.name)}  ${"BACKEND".padEnd(W.backend)}  ${"LAST SEEN".padEnd(W.seen)}  CWD`;
    const divider =
      `  ${"─".repeat(W.name)}  ${"─".repeat(W.backend)}  ${"─".repeat(W.seen)}  ${"─".repeat(20)}`;

    console.log(header);
    console.log(divider);
    for (const r of withStatus) {
      const marker = r.alive ? "●" : "○";
      const seen   = relativeTime(r.last_seen_at);
      console.log(
        `${marker} ${r.name.padEnd(W.name)}  ${r.backend.padEnd(W.backend)}  ${seen.padEnd(W.seen)}  ${r.cwd}`,
      );
    }
  },
});

const kill = defineCommand({
  meta: { description: "Kill a session" },
  args: {
    name: { type: "positional", required: true },
  },
  async run({ args }) {
    requireSession(args.name);
    await killSession(args.name);
    console.log(`Session '${args.name}' killed.`);
  },
});

const clean = defineCommand({
  meta: { description: "Remove dead session records from the store" },
  args: {
    yes: { type: "boolean", default: false, description: "Skip confirmation" },
    "dry-run": { type: "boolean", default: false },
  },
  async run({ args }) {
    const records = storeList();
    const dead: string[] = [];
    for (const r of records) {
      if (!(await isAlive(r.name))) dead.push(r.name);
    }
    if (dead.length === 0) {
      console.log("No dead sessions found.");
      return;
    }
    console.log(`Dead sessions: ${dead.join(", ")}`);
    if (args["dry-run"]) return;
    if (!args.yes) {
      const rl = createInterface({ input: process.stdin, output: process.stdout });
      const answer = await new Promise<string>((resolve) =>
        rl.question("Remove them? [y/N] ", resolve),
      );
      rl.close();
      if (answer.trim().toLowerCase() !== "y") return;
    }
    for (const name of dead) storeDelete(name);
    console.log(`Removed ${dead.length} record(s).`);
  },
});

const interrupt = defineCommand({
  meta: { description: "Send Ctrl-C to a session" },
  args: {
    name: { type: "positional", required: true },
  },
  async run({ args }) {
    requireSession(args.name);
    await sendKey(args.name, "C-c");
    console.log("Ctrl-C sent.");
  },
});

const approve = defineCommand({
  meta: { description: "Respond to a permission/approval prompt" },
  args: {
    name: { type: "positional", required: true },
    answer: {
      type: "positional",
      required: false,
      description: "yes | always | no (default: yes)",
    },
  },
  async run({ args }) {
    requireSession(args.name);
    const answer = (args.answer as string | undefined) ?? "yes";

    const state = await readState(args.name);

    // Handle permission prompts (Format A / Format B)
    if (state.state === "approval" && state.permission) {
      if (!["yes", "always", "no"].includes(answer))
        die(`Invalid answer '${answer}'. Use: yes | always | no`);
      await approvePermission(args.name, state.permission, answer as "yes" | "always" | "no");
      console.log(`Approved: ${answer}`);
      return;
    }

    // Handle generic choice menus (workspace trust, model picker, etc.)
    if (state.state === "choosing" && state.choices) {
      await approveChoice(args.name, state.choices, answer);
      return;
    }

    die("No permission prompt or choice menu detected");
  },
});

const input = defineCommand({
  meta: { description: "Type arbitrary text (slash commands, partial input, etc.)" },
  args: {
    name: { type: "positional", required: true },
    text: { type: "positional", required: true },
    "no-enter": { type: "boolean", default: false },
  },
  async run({ args }) {
    requireSession(args.name);
    await sendText(args.name, args.text as string, { enter: !(args["no-enter"] as boolean) });
  },
});

const key = defineCommand({
  meta: { description: "Send special key(s): Enter, Escape, Up, Down, C-c, etc." },
  args: {
    name: { type: "positional", required: true },
    keys: { type: "positional", required: true },
    repeat: { type: "string", default: "1" },
  },
  async run({ args }) {
    requireSession(args.name);
    const n = parseInt(args.repeat as string);
    const keyList = (args.keys as string).split(",").map((k) => k.trim());
    for (let i = 0; i < n; i++) {
      await sendKey(args.name, ...keyList);
    }
  },
});

const read = defineCommand({
  meta: { description: "Read structured pane state" },
  args: {
    name: { type: "positional", required: true },
    json: { type: "boolean", default: false },
    full: { type: "boolean", default: false },
    porcelain: { type: "boolean", default: false, description: "Print only the state name" },
    window: { type: "string", default: "1000", description: "Observation window in ms" },
    interval: { type: "string", default: "200", description: "Poll interval in ms" },
  },
  async run({ args }) {
    requireSession(args.name);
    const state = await readState(
      args.name,
      args.full as boolean,
      parseInt(args.window as string),
      parseInt(args.interval as string),
    );
    if (args.porcelain) {
      console.log(state.state);
    } else {
      printState(state, args.json as boolean);
    }
  },
});

const wait = defineCommand({
  meta: { description: "Block until target state: ready | thinking | typing | composed | approval | choosing | unknown | dead | any-change" },
  args: {
    name: { type: "positional", required: true },
    state: { type: "positional", required: true },
    timeout: { type: "string", default: "300" },
    json: { type: "boolean", default: false },
  },
  async run({ args }) {
    requireSession(args.name);
    const state = await waitState(
      args.name,
      args.state as string,
      parseInt(args.timeout as string),
    );
    printState(state, args.json as boolean);
  },
});

// ---------------------------------------------------------------------------
// model
// ---------------------------------------------------------------------------

const modelCmd = defineCommand({
  meta: { description: "List or switch models in a running session" },
  args: {
    name: { type: "positional", required: true },
    select: {
      type: "positional",
      required: false,
      description: "Model to switch to (omit to list available models)",
    },
    timeout: { type: "string", default: "15", description: "Max seconds to wait for model list" },
  },
  async run({ args }) {
    const rec = requireSession(args.name);
    const backend = rec.backend ?? "claude";
    const timeout = parseInt(args.timeout as string) * 1000;

    const alive = await isAlive(args.name);
    if (!alive) die(`Session '${args.name}' is not alive.`);

    // Trigger the model picker
    if (backend === "cursor") {
      await sendText(args.name, "/model", { enter: false });
    } else {
      await sendText(args.name, "/model");
    }

    // Poll for the model picker to appear
    const deadline = Date.now() + timeout;
    let choices = null;
    while (Date.now() < deadline) {
      await sleep(500);
      const raw = await capturePane(args.name);
      choices = detectModelPicker(raw, backend);
      if (choices) break;
    }

    // Cursor Agent: scroll through dropdown to collect ALL models
    if (choices && backend === "cursor") {
      const allLabels = new Map<string, boolean>();
      for (const c of choices) allLabels.set(c.label, c.selected);

      for (let i = 0; i < 50; i++) {
        const raw = await capturePane(args.name);
        const clean = stripAnsiLines(raw);
        const dd = detectCursorModelDropdown(clean.slice(-30));
        if (!dd || !dd.hasMore) {
          if (dd) {
            for (const c of dd.items) if (!allLabels.has(c.label)) allLabels.set(c.label, c.selected);
          }
          break;
        }
        for (const c of dd.items) if (!allLabels.has(c.label)) allLabels.set(c.label, c.selected);
        await sendKey(args.name, "Down");
        await sleep(150);
      }

      let idx = 1;
      choices = Array.from(allLabels.entries()).map(([label, selected]) => ({
        key: String(idx++),
        label,
        selected,
      }));
    }

    if (!choices) {
      console.error("Could not detect model picker. The session may not support /model or it timed out.");
      await sendKey(args.name, "C-c");
      process.exit(1);
    }

    if (!args.select) {
      // List mode: display and cancel
      console.log(`Available models in '${args.name}':`);
      for (const c of choices) {
        const marker = c.selected ? "●" : " ";
        console.log(`  ${marker} ${c.label}`);
      }
      await sendKey(args.name, "C-c");
      return;
    }

    // Selection mode
    const selectLower = (args.select as string).toLowerCase();
    const matched = choices.find((c) => c.label.toLowerCase().includes(selectLower));

    if (!matched) {
      console.error(`No model matching '${args.select}' found.`);
      console.error(`Available: ${choices.map((c) => c.label).join(", ")}`);
      await sendKey(args.name, "C-c");
      process.exit(1);
    }

    if (backend === "cursor") {
      await sendKey(args.name, "C-c");
      await sleep(200);
      await sendText(args.name, `/model ${matched.label}`);
    } else {
      const currentIdx = choices.findIndex((c) => c.selected);
      const targetIdx = choices.indexOf(matched);
      const delta = targetIdx - (currentIdx >= 0 ? currentIdx : 0);
      if (delta > 0) {
        for (let i = 0; i < delta; i++) { await sendKey(args.name, "Down"); await sleep(50); }
      } else if (delta < 0) {
        for (let i = 0; i < -delta; i++) { await sendKey(args.name, "Up"); await sleep(50); }
      }
      await sleep(100);
      await sendKey(args.name, "Enter");
    }

    console.log(`Switching to ${matched.label}`);
  },
});

// ---------------------------------------------------------------------------
// history
// ---------------------------------------------------------------------------

const historyCmd = defineCommand({
  meta: { description: "View conversation history for a session" },
  args: {
    name: {
      type: "positional",
      required: false,
      description: "Session name (omit to list all sessions with history)",
    },
    last: { type: "string", default: "0", description: "Show last N entries" },
    run: { type: "string", description: "Specific run ID" },
    json: { type: "boolean", default: false, description: "Output as JSON lines" },
  },
  async run({ args }) {
    if (!args.name) {
      const sessions = listSessionsWithHistory();
      if (sessions.length === 0) {
        console.log("No conversation history found.");
        return;
      }
      console.log("Sessions with history:");
      for (const sname of sessions) {
        const runs = listSessionRuns(sname);
        const latest = runs.at(-1) ?? "";
        console.log(`  ${sname.padEnd(24)} ${runs.length} run(s)  ${latest}`);
      }
      return;
    }

    let entries;
    if (args.run) {
      const historyDir = getHistoryDir();
      const { join } = await import("path");
      entries = readHistoryFile(join(historyDir, args.name as string, `${args.run}.jsonl`));
    } else {
      entries = readFullSessionHistory(args.name as string);
    }

    if (entries.length === 0) {
      console.log(`No history for session '${args.name}'.`);
      const runs = listSessionRuns(args.name as string);
      if (runs.length > 0) {
        console.log("Available runs:");
        for (const r of runs) console.log(`  ${r}`);
      }
      return;
    }

    const n = parseInt(args.last as string);
    if (n > 0) entries = entries.slice(-n);

    if (args.json) {
      for (const entry of entries) console.log(JSON.stringify(entry));
      return;
    }

    const ROLE_LABEL: Record<string, string> = {
      user: "USER     ",
      assistant: "CLAUDE   ",
      system: "SYSTEM   ",
      tool: "TOOL     ",
    };
    for (const entry of entries) {
      const ts = new Date(entry.ts * 1000).toTimeString().slice(0, 8);
      const role = ROLE_LABEL[entry.role] ?? entry.role.padEnd(9);
      const content = entry.content.length > 500 ? entry.content.slice(0, 500) + "…" : entry.content;
      console.log(`${ts} ${role} ${content}`);
    }
  },
});

// ---------------------------------------------------------------------------
// stream
// ---------------------------------------------------------------------------

const streamCmd = defineCommand({
  meta: { description: "One-shot query using stream-json mode (no tmux required)" },
  args: {
    prompt: { type: "positional", required: true, description: "Prompt to send to Claude" },
    cwd: { type: "string", default: ".", description: "Working directory" },
    cmd: { type: "string", default: "claude", description: "Claude CLI executable" },
    tools: { type: "string", default: "", description: "Comma-separated allowed tools" },
    model: { type: "string", default: "", description: "Model name" },
    timeout: { type: "string", default: "300", description: "Max seconds to wait" },
    raw: { type: "boolean", default: false, description: "Print raw JSON events" },
  },
  async run({ args }) {
    const command = [args.cmd as string, "-p", "--output-format", "stream-json", "--verbose"];
    if (args.model) command.push("--model", args.model as string);
    if (args.tools) command.push("--allowedTools", args.tools as string);

    const env = { ...process.env } as Record<string, string>;
    delete env.CLAUDECODE;

    const proc = nodeSpawn(command[0], command.slice(1), {
      cwd: args.cwd as string,
      stdio: ["pipe", "pipe", "pipe"],
      env,
    });

    proc.stdin.write(args.prompt as string);
    proc.stdin.end();

    const rl = createInterface({ input: proc.stdout });
    const textParts: string[] = [];
    let costUsd = 0;
    let sessionId = "";

    const timeoutMs = parseInt(args.timeout as string) * 1000;
    const timer = setTimeout(() => { proc.kill(); process.stderr.write("Stream timeout.\n"); process.exit(1); }, timeoutMs);

    await new Promise<void>((resolve, reject) => {
      rl.on("line", (line) => {
        if (!line.trim()) return;
        try {
          const evt = JSON.parse(line) as Record<string, unknown>;
          if (args.raw) { console.log(JSON.stringify(evt)); return; }

          if (evt.type === "content_block_delta") {
            const delta = evt.delta as Record<string, unknown> | undefined;
            if (delta?.type === "text_delta") textParts.push((delta.text as string) ?? "");
          } else if (evt.type === "assistant") {
            const msg = evt.message as Record<string, unknown> | undefined;
            for (const block of (msg?.content as Array<Record<string, unknown>>) ?? []) {
              if (block.type === "text") textParts.push((block.text as string) ?? "");
            }
          } else if (evt.type === "result") {
            if (evt.result) textParts.push(evt.result as string);
            costUsd = (evt.cost_usd as number) ?? 0;
            sessionId = (evt.session_id as string) ?? "";
          }
        } catch { /* ignore parse errors */ }
      });
      rl.on("close", resolve);
      proc.on("error", reject);
    });

    clearTimeout(timer);

    if (!args.raw) {
      const content = textParts.join("").trim();
      if (content) console.log(content);
      else process.stderr.write("No text content in response.\n");
      if (sessionId) process.stderr.write(`\nsession: ${sessionId}\n`);
      if (costUsd > 0) process.stderr.write(`cost: $${costUsd.toFixed(4)}\n`);
    }
  },
});

// ---------------------------------------------------------------------------
// Relay subcommands
// ---------------------------------------------------------------------------

const relayDebate = defineCommand({
  meta: { description: "Two Claude instances debate a topic" },
  args: {
    topic: { type: "positional", required: true },
    "role-a": { type: "string", default: "Proponent", description: "Name of role A" },
    "role-b": { type: "string", default: "Skeptic", description: "Name of role B" },
    rounds: { type: "string", default: "3" },
    model: { type: "string", default: "" },
    cwd: { type: "string", default: process.cwd() },
  },
  async run({ args }) {
    const result = await runRelay({
      mode: "debate",
      roleA: { name: args["role-a"] as string, model: args.model as string },
      roleB: { name: args["role-b"] as string, model: args.model as string },
      topic: args.topic as string,
      maxRounds: parseInt(args.rounds as string),
      cwd: args.cwd as string,
      onTurn: (turn) => printRelayTurn(turn),
    });
    printRelayResult(result);
  },
});

const relayCollab = defineCommand({
  meta: { description: "Developer + Reviewer iterate on a coding task" },
  args: {
    task: { type: "positional", required: true },
    developer: { type: "string", default: "Developer" },
    reviewer: { type: "string", default: "Reviewer" },
    rounds: { type: "string", default: "3" },
    model: { type: "string", default: "" },
    cwd: { type: "string", default: process.cwd() },
  },
  async run({ args }) {
    const result = await runRelay({
      mode: "collab",
      roleA: { name: args.developer as string, model: args.model as string },
      roleB: { name: args.reviewer as string, model: args.model as string },
      task: args.task as string,
      maxRounds: parseInt(args.rounds as string),
      cwd: args.cwd as string,
      onTurn: (turn) => printRelayTurn(turn, true),
    });
    printRelayResult(result);
  },
});

const relay = defineCommand({
  meta: { description: "Claude-to-Claude relay orchestration" },
  subCommands: {
    debate: relayDebate,
    collab: relayCollab,
  },
});

// ---------------------------------------------------------------------------
// Group Chat
// ---------------------------------------------------------------------------

const groupchat = defineCommand({
  meta: { description: "Multi-agent group chat (Claude + Codex + OpenCode)" },
  args: {
    topic: { type: "positional", required: false, description: "Discussion topic or task" },
    cwd: { type: "string", default: process.cwd(), description: "Working directory" },
    timeout: { type: "string", default: "120", description: "Per-message timeout in seconds" },
    "keep-sessions": { type: "boolean", default: false, description: "Keep sessions alive on exit" },
    "no-claude": { type: "boolean", default: false, description: "Exclude Claude" },
    "no-codex": { type: "boolean", default: false, description: "Exclude Codex" },
    "no-opencode": { type: "boolean", default: false, description: "Exclude OpenCode" },
    rounds: { type: "string", default: "5", description: "Auto-run N rounds per user message" },
  },
  async run({ args }) {
    const agents = [];
    if (!args["no-claude"])
      agents.push({ name: "Claude", sessionName: "gc-claude", backend: "claude", command: "claude", color: "magenta" });
    if (!args["no-codex"])
      agents.push({ name: "Codex", sessionName: "gc-codex", backend: "codex", command: "codex", color: "yellow" });
    if (!args["no-opencode"])
      agents.push({ name: "OpenCode", sessionName: "gc-opencode", backend: "opencode", command: "opencode", color: "green" });

    if (agents.length === 0) die("At least one agent must be enabled");

    await runGroupChat({
      topic: args.topic as string | undefined,
      cwd: args.cwd as string,
      agents,
      timeout: parseInt(args.timeout as string),
      keepSessions: args["keep-sessions"] as boolean,
      rounds: parseInt(args.rounds as string),
    });
  },
});

// ---------------------------------------------------------------------------
// Root command
// ---------------------------------------------------------------------------

const main = defineCommand({
  meta: {
    name: "ccc",
    description: "Claude CLI Connector — manage Claude Code sessions in tmux",
    version: "0.3.0",
  },
  subCommands: {
    run,
    attach,
    send: sendCmd,
    tail,
    last,
    status,
    ps,
    kill,
    clean,
    interrupt,
    approve,
    input,
    key,
    read,
    wait,
    model: modelCmd,
    history: historyCmd,
    stream: streamCmd,
    relay,
    groupchat,
  },
});

runMain(main);
