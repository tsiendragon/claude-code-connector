/**
 * groupchat.ts — Multi-agent moderated group chat.
 *
 * Turn-based model:
 *   1. System sends rules to all agents (only speak when asked).
 *   2. User posts a message → recorded as context for all.
 *   3. System calls on each agent one by one: "It's your turn, {name}".
 *   4. Agent responds → response shared to all others as context.
 *   5. Repeat until all agents have spoken, then back to user.
 */

import { createInterface, type Interface as ReadlineInterface } from "readline";
import {
  ensureSession,
  killSession,
  send,
  waitReady,
  readState,
  approvePermission,
  approveChoice,
} from "./session.js";
import { isAlive, sendText, sendKey } from "./transport.js";

// ---------------------------------------------------------------------------
// ANSI helpers (zero deps)
// ---------------------------------------------------------------------------

const ESC = "\x1b[";
const RESET = `${ESC}0m`;
const BOLD = `${ESC}1m`;
const DIM = `${ESC}2m`;

const COLORS: Record<string, string> = {
  cyan: `${ESC}36m`,
  magenta: `${ESC}35m`,
  yellow: `${ESC}33m`,
  green: `${ESC}32m`,
  blue: `${ESC}34m`,
  red: `${ESC}31m`,
  gray: `${ESC}90m`,
  white: `${ESC}37m`,
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentDef {
  name: string;
  sessionName: string;
  backend: string;        // claude | codex | opencode
  command: string;        // CLI binary
  color: string;          // key into COLORS
}

export interface ChatMessage {
  speaker: string;        // agent name, "User", or "System"
  content: string;
  ts: number;
}

export interface GroupChatConfig {
  topic?: string;
  cwd: string;
  agents?: AgentDef[];
  autoApprove?: boolean;
  timeout?: number;       // per-message timeout in seconds
  keepSessions?: boolean; // don't kill on exit
  rounds?: number;        // auto-run N rounds per user message (default 5)
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULT_AGENTS: AgentDef[] = [
  { name: "Claude", sessionName: "gc-claude", backend: "claude", command: "claude", color: "magenta" },
  { name: "Codex", sessionName: "gc-codex", backend: "codex", command: "codex", color: "yellow" },
  { name: "OpenCode", sessionName: "gc-opencode", backend: "opencode", command: "opencode", color: "green" },
];

// ---------------------------------------------------------------------------
// Printing
// ---------------------------------------------------------------------------

function colorize(text: string, color: string): string {
  return `${COLORS[color] ?? ""}${text}${RESET}`;
}

function printMsg(speaker: string, content: string, color: string) {
  const time = new Date().toLocaleTimeString("en-US", {
    hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const lines = content.split("\n");
  const header = `${DIM}${time}${RESET} ${COLORS[color] ?? ""}${BOLD}[${speaker}]${RESET}`;
  if (lines.length === 1) {
    console.log(`${header} ${lines[0]}`);
  } else {
    console.log(`${header}`);
    const indent = "         ";
    for (const line of lines) {
      console.log(`${indent}${line}`);
    }
  }
}

function printSystem(msg: string) {
  printMsg("System", msg, "gray");
}

// ---------------------------------------------------------------------------
// Rules & prompts
// ---------------------------------------------------------------------------

function buildRules(agent: AgentDef, allAgents: AgentDef[], topic?: string): string {
  const others = allAgents.filter((a) => a.name !== agent.name);
  const otherList = others.map((a) => `${a.name}`).join(", ");

  const langRule = "你必须全程使用中文回复。所有参与者都使用中文。";

  const rules = [
    `=== GROUP DISCUSSION RULES ===`,
    `You are "${agent.name}" in a moderated group discussion with ${otherList} and a human User.`,
    ``,
    `PROTOCOL:`,
    `1. This is MODERATED — only speak when you see "[System]: It's your turn, ${agent.name}".`,
    `2. Messages from others: [Name]: message. From moderator: [System]: message.`,
    `3. ${langRule}`,
    ``,
    `THINKING STANDARDS — this is critical:`,
    `4. KEEP IT SHORT: respond in 100 words or less. Be sharp, not verbose. One piercing insight beats three paragraphs.`,
    `5. Think DEEPLY and PHILOSOPHICALLY. Do NOT give generic, surface-level takes.`,
    `   Bad: "AI will change engineering." Good: "Engineering mastery shifts from memorizing APIs to developing taste — knowing which of AI's ten suggestions fits the system's long-term trajectory."`,
    `6. Every claim must be ACTIONABLE or INSIGHTFUL. If your point wouldn't surprise anyone, don't make it.`,
    `7. DO NOT agree with others just to be polite. Critically examine what they said:`,
    `   - Point out logical gaps. Challenge vague claims. Offer a genuinely different angle.`,
    `8. Have a STRONG POINT OF VIEW. "It depends" is lazy — say exactly what you'd recommend and why.`,
    `9. Use CONCRETE EXAMPLES, data, or analogies — not abstract platitudes.`,
    `10. You may search the web for real data or research to support your arguments.`,
    `11. Goal: produce USEFUL KNOWLEDGE the User can act on.`,
  ];

  if (topic) {
    rules.push(``, `Discussion topic: ${topic}`);
  }

  rules.push(``, `Acknowledge these rules in one sentence, then wait for the discussion to begin.`);
  return rules.join("\n");
}

// ---------------------------------------------------------------------------
// Response cleaning
// ---------------------------------------------------------------------------

/**
 * Strip the echoed prompt from a TUI response.
 *
 * Claude Code TUI: echoed input above ⏺ marker.
 * Codex: may include "Working (...)" indicator lines.
 * General: strip known noise patterns.
 */
function cleanResponse(raw: string, backend: string): string {
  let text = raw;

  // Claude: strip everything before ⏺ (U+23FA)
  const marker = text.indexOf("⏺");
  if (marker >= 0) {
    text = text.slice(marker + "⏺".length);
  }

  // Codex: strip "Working (Ns • esc to interrupt)" lines
  text = text
    .split("\n")
    .filter((line) => !/^Working \(\d+s\b/.test(line.trim()))
    .join("\n");

  // OpenCode: strip remaining noise (thinking, system echoes, sidebar leaks)
  if (backend === "opencode") {
    const sidebarKw = ["tokens", "used", "spent", "Context", "LSP", "LSPs will activate", "~/", "OpenCode"];
    text = text
      .split("\n")
      .filter((l) => {
        const t = l.trim();
        if (t.startsWith("Thinking:") || t.startsWith("System:")) return false;
        // Filter sidebar keywords that leaked through
        if (sidebarKw.some((kw) => t === kw || (t.includes(kw) && t.length < 40))) return false;
        return true;
      })
      .join("\n");
  }

  // Strip leading/trailing whitespace
  text = text.trim();

  // If empty after cleaning, return original trimmed
  return text || raw.trim();
}

// ---------------------------------------------------------------------------
// Transcript formatting
// ---------------------------------------------------------------------------

function formatTranscript(msgs: ChatMessage[]): string {
  return msgs.map((m) => `[${m.speaker}]: ${m.content}`).join("\n\n");
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Handle startup prompts (workspace trust, approval, etc.)
 * by auto-approving until we reach a true "ready" state.
 */
async function handleStartupPrompts(
  agent: AgentDef,
  maxAttempts = 5,
): Promise<boolean> {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const state = await readState(agent.sessionName);

    if (state.state === "ready") return true;

    if (state.state === "approval" && state.permission) {
      printSystem(`${colorize(agent.name, agent.color)} approval prompt → auto-approving`);
      await approvePermission(agent.sessionName, state.permission, "yes");
      await sleep(2000);
      continue;
    }

    if (state.state === "choosing" && state.choices) {
      const label = state.choices[0]?.label ?? "option 1";
      printSystem(`${colorize(agent.name, agent.color)} choice prompt → selecting: ${label}`);
      await approveChoice(agent.sessionName, state.choices, "1");
      await sleep(2000);
      continue;
    }

    if (state.state === "thinking") {
      printSystem(`${colorize(agent.name, agent.color)} still starting up...`);
      await sleep(3000);
      continue;
    }

    await sleep(2000);
  }

  const finalState = await readState(agent.sessionName);
  return finalState.state === "ready";
}

/**
 * Submit text to an agent's tmux pane (backend-aware, no waiting).
 *
 * All backends: send text first (no Enter), sleep, then send Enter separately.
 * This two-step approach ensures the Enter key is reliably delivered after
 * the text has been fully received by the TUI.
 *
 * Claude: flatten multi-line text to single line (newlines = Enter in TUI).
 */
async function submitText(agent: AgentDef, text: string): Promise<void> {
  if (agent.backend === "opencode") {
    // Step 1: send text only (no enter)
    await sendText(agent.sessionName, text, { enter: false });
    await sleep(300);
    // Step 2: send C-m to submit
    await sendKey(agent.sessionName, "C-m");
  } else if (agent.backend === "codex") {
    // Step 1: send text only (no enter)
    await sendText(agent.sessionName, text, { enter: false });
    await sleep(300);
    // Step 2: send Enter to submit, then confirm Enter
    await sendKey(agent.sessionName, "Enter");
    await sleep(150);
    await sendKey(agent.sessionName, "Enter");
  } else {
    // Claude: flatten multi-line text to avoid premature submission
    const flat = text.replace(/\n{2,}/g, " | ").replace(/\n/g, " ");
    // Step 1: send text only (no enter)
    await sendText(agent.sessionName, flat, { enter: false });
    await sleep(300);
    // Step 2: dismiss autocomplete popup (if any), then submit
    await sendKey(agent.sessionName, "Escape");
    await sleep(100);
    await sendKey(agent.sessionName, "Enter");
  }
}

/**
 * Send a message to an agent and poll readState until ready.
 *
 * Instead of relying on send()'s frame-diff detection (which can misfire
 * for Codex/OpenCode), we:
 *   1. Submit text directly to the tmux pane
 *   2. Wait a beat for the agent to start processing
 *   3. Poll readState every 2s until state === "ready"
 *   4. Extract lastResponse from the ready state
 *
 * Auto-approves permission prompts along the way.
 */
async function askAgent(
  agent: AgentDef,
  prompt: string,
  timeout: number,
  _autoApprove: boolean,
): Promise<string | null> {
  if (!(await isAlive(agent.sessionName))) return null;

  // 1. Snapshot current lastResponse BEFORE sending — we need to detect when
  //    the response actually CHANGES (avoids returning stale response from
  //    a previously pushed context message).
  const preSendState = await readState(agent.sessionName);
  const preSendResponse = preSendState.lastResponse ?? "";

  // 2. Submit the prompt
  await submitText(agent, prompt);

  // 3. Give the agent time to start processing (avoid reading stale "ready")
  await sleep(3000);

  // 4. Poll readState until ready WITH A DIFFERENT response (or timeout)
  const deadline = Date.now() + timeout * 1000;
  let seenThinking = false;
  while (Date.now() < deadline) {
    if (!(await isAlive(agent.sessionName))) return null;

    const state = await readState(agent.sessionName);

    if (state.state === "thinking" || state.state === "typing") {
      seenThinking = true;
      await sleep(2000);
      continue;
    }

    if (state.state === "ready") {
      const resp = state.lastResponse ?? "";
      // Only return if we've seen the agent think OR the response changed.
      // This prevents returning a stale response from a pushContext.
      if (seenThinking || resp !== preSendResponse) {
        if (resp) return cleanResponse(resp, agent.backend);
        return null;
      }
      // Response unchanged and haven't seen thinking — agent may not have
      // started yet. Wait and retry.
      await sleep(2000);
      continue;
    }

    // Auto-approve permission prompts
    if (state.state === "approval" && state.permission) {
      seenThinking = true;
      await approvePermission(agent.sessionName, state.permission, "yes");
      await sleep(1000);
      continue;
    }

    // Auto-select choices (e.g. workspace trust)
    if (state.state === "choosing" && state.choices) {
      seenThinking = true;
      await approveChoice(agent.sessionName, state.choices, "1");
      await sleep(1000);
      continue;
    }

    // Still unknown — wait and retry
    await sleep(2000);
  }

  // Timeout — try to extract whatever is there
  const finalState = await readState(agent.sessionName);
  if (finalState.lastResponse && finalState.lastResponse !== preSendResponse) {
    return cleanResponse(finalState.lastResponse, agent.backend);
  }
  return null;
}

/**
 * Push context to an agent without waiting (fire-and-forget).
 * The agent processes it in the background.
 */
async function pushContext(agent: AgentDef, text: string): Promise<void> {
  if (!(await isAlive(agent.sessionName))) return;
  await submitText(agent, text);
}

// ---------------------------------------------------------------------------
// Main group chat
// ---------------------------------------------------------------------------

export async function runGroupChat(config: GroupChatConfig): Promise<void> {
  const {
    cwd,
    topic,
    autoApprove = true,
    timeout = 120,
    keepSessions = false,
    rounds: autoRounds = 5,
  } = config;
  const agentDefs = config.agents ?? DEFAULT_AGENTS;

  // ---- Banner ----
  console.log(`\n${BOLD}═══ Multi-Agent Group Chat (Moderated) ═══${RESET}\n`);
  if (topic) printSystem(`Topic: ${topic}`);
  const nameList = agentDefs.map((a) => colorize(a.name, a.color)).join(", ");
  printSystem(`Participants: ${nameList}, ${colorize("You", "cyan")}`);
  console.log();

  // ---- Start sessions ----
  printSystem("Starting agent sessions...");
  for (const agent of agentDefs) {
    try {
      await ensureSession({
        name: agent.sessionName,
        cwd,
        command: agent.command,
        backend: agent.backend,
        startupWaitMs: 3000,
      });
      printSystem(`${colorize(agent.name, agent.color)} session started`);
    } catch (e) {
      printSystem(`Failed to start ${agent.name}: ${(e as Error).message}`);
    }
  }

  // ---- Wait for ready & handle startup prompts ----
  printSystem("Waiting for agents to become ready...");
  const liveAgents: AgentDef[] = [];
  for (const agent of agentDefs) {
    try {
      await waitReady(agent.sessionName, 90, 1000, agent.backend);
      const ok = await handleStartupPrompts(agent);
      if (ok) {
        liveAgents.push(agent);
        printSystem(`${colorize(agent.name, agent.color)} ready`);
      } else {
        printSystem(`${agent.name} stuck in startup — skipping`);
      }
    } catch {
      printSystem(`${agent.name} failed to become ready — skipping`);
    }
  }

  if (liveAgents.length === 0) {
    printSystem("No agents available. Exiting.");
    return;
  }

  // ---- Send rules to all agents (fire-and-forget, no waiting) ----
  const transcript: ChatMessage[] = [];
  const seenUpTo = new Map<string, number>();

  printSystem("Sending group chat rules to all agents...");
  for (const agent of liveAgents) {
    const rules = buildRules(agent, liveAgents, topic);
    pushContext(agent, rules).catch(() => {});
    seenUpTo.set(agent.name, 0);
  }
  printSystem("Rules sent. Agents will process them in the background.");

  // ---- Chat loop ----
  console.log(
    `\n${DIM}─── Type a message to set the topic. System will call on each agent in turn. ───${RESET}`,
  );
  console.log(
    `${DIM}─── Commands: /quit, /kick <name>, /status, /order <name1,name2,...> ───${RESET}\n`,
  );

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  let roundNum = 0;

  const chatLoop = (): void => {
    rl.question(`${COLORS.cyan}${BOLD}You > ${RESET}`, async (input) => {
      try {
        const text = input.trim();
        if (!text) { chatLoop(); return; }

        // ---- Commands ----
        if (text === "/quit" || text === "/exit") {
          await cleanup(rl, liveAgents, keepSessions);
          return;
        }
        if (text === "/status") {
          for (const a of liveAgents) {
            const alive = await isAlive(a.sessionName);
            printSystem(`${colorize(a.name, a.color)}: ${alive ? "alive" : "dead"}`);
          }
          chatLoop();
          return;
        }
        if (text.startsWith("/kick ")) {
          const target = text.slice(6).trim();
          const idx = liveAgents.findIndex(
            (a) => a.name.toLowerCase() === target.toLowerCase(),
          );
          if (idx >= 0) {
            const removed = liveAgents.splice(idx, 1)[0];
            try { await killSession(removed.sessionName); } catch {}
            printSystem(`${removed.name} kicked from chat.`);
          } else {
            printSystem(`No agent named "${target}"`);
          }
          chatLoop();
          return;
        }
        if (text.startsWith("/order ")) {
          const names = text.slice(7).split(",").map((n) => n.trim().toLowerCase());
          const reordered: AgentDef[] = [];
          for (const n of names) {
            const a = liveAgents.find((ag) => ag.name.toLowerCase() === n);
            if (a) reordered.push(a);
          }
          for (const a of liveAgents) {
            if (!reordered.includes(a)) reordered.push(a);
          }
          liveAgents.length = 0;
          liveAgents.push(...reordered);
          printSystem(`Turn order: ${liveAgents.map((a) => colorize(a.name, a.color)).join(" → ")}`);
          chatLoop();
          return;
        }

        // ---- Record user message ----
        const userMsg: ChatMessage = { speaker: "User", content: text, ts: Date.now() };
        transcript.push(userMsg);
        printMsg("User", text, "cyan");

        // ---- Remove dead agents ----
        for (let i = liveAgents.length - 1; i >= 0; i--) {
          if (!(await isAlive(liveAgents[i].sessionName))) {
            printSystem(`${liveAgents[i].name} is dead — removing`);
            liveAgents.splice(i, 1);
          }
        }

        if (liveAgents.length === 0) {
          printSystem("All agents are gone. Exiting.");
          rl.close();
          return;
        }

        // ---- Auto-run N rounds of discussion ----
        for (let round = 0; round < autoRounds; round++) {
          roundNum++;
          const startIdx = (roundNum - 1) % liveAgents.length;
          const order = [...liveAgents.slice(startIdx), ...liveAgents.slice(0, startIdx)];

          printSystem(`── Round ${round + 1}/${autoRounds} ──`);

          for (const agent of order) {
            if (!(await isAlive(agent.sessionName))) {
              printSystem(`${agent.name} died mid-round — skipping`);
              continue;
            }

            const lastSeen = seenUpTo.get(agent.name) ?? 0;
            const unseen = transcript.slice(lastSeen);
            const contextBlock = unseen.length > 0 ? formatTranscript(unseen) + "\n\n" : "";
            const turnPrompt = `${contextBlock}[System]: It's your turn, ${agent.name}. Please share your thoughts.`;

            printSystem(`${colorize(agent.name, agent.color)}'s turn...`);

            try {
              const resp = await askAgent(agent, turnPrompt, timeout, autoApprove);
              seenUpTo.set(agent.name, transcript.length);

              if (resp) {
                const msg: ChatMessage = {
                  speaker: agent.name,
                  content: resp,
                  ts: Date.now(),
                };
                transcript.push(msg);
                printMsg(agent.name, resp, agent.color);
                seenUpTo.set(agent.name, transcript.length);

                // Pre-push to other agents (fire-and-forget)
                for (const other of order) {
                  if (other.name === agent.name) continue;
                  try {
                    if (await isAlive(other.sessionName)) {
                      const syncText = `[System]: 以下是${agent.name}的发言记录，仅供你了解，请勿回复。等我说"It's your turn"时再发言。\n[${agent.name}]: ${resp}`;
                      pushContext(other, syncText).catch(() => {});
                    }
                  } catch {}
                }
              } else {
                printSystem(`${agent.name} gave no response`);
              }
            } catch (e) {
              printSystem(`${agent.name} error: ${(e as Error).message}`);
              seenUpTo.set(agent.name, transcript.length);
            }
          }

          // Check if all agents died
          for (let i = liveAgents.length - 1; i >= 0; i--) {
            if (!(await isAlive(liveAgents[i].sessionName))) {
              printSystem(`${liveAgents[i].name} is dead — removing`);
              liveAgents.splice(i, 1);
            }
          }
          if (liveAgents.length === 0) {
            printSystem("All agents are gone. Exiting.");
            rl.close();
            return;
          }

          console.log();
        }

        printSystem(`${autoRounds} rounds complete. Your turn.`);
        chatLoop();
      } catch (e) {
        // Safety net: ensure chatLoop always continues even on unexpected errors
        printSystem(`Unexpected error: ${(e as Error).message}`);
        chatLoop();
      }
    });
  };

  chatLoop();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function cleanup(
  rl: ReadlineInterface,
  agents: AgentDef[],
  keep: boolean,
): Promise<void> {
  printSystem("Ending group chat...");
  rl.close();
  if (!keep) {
    for (const agent of agents) {
      try {
        await killSession(agent.sessionName);
      } catch {}
    }
    printSystem("Sessions cleaned up.");
  } else {
    printSystem("Sessions kept alive (use `ccc ps` to see them).");
  }
  printSystem("Goodbye!");
}
