/**
 * relay.ts — Claude-to-Claude relay (debate / collab modes).
 *
 * Uses `claude -p --output-format json` (one-shot subprocess per turn).
 * Much simpler than the Python tmux relay — no TUI scraping needed.
 */

import { spawn } from "child_process";

export type RelayMode = "debate" | "collab";

export interface RelayRole {
  name: string;
  systemPrompt?: string;
  model?: string;
}

export interface RelayTurn {
  round: number;
  speaker: string;
  content: string;
  costUsd: number;
}

export interface RelayResult {
  mode: RelayMode;
  transcript: RelayTurn[];
  finalState: "max_rounds" | "approved" | "error";
  totalCostUsd: number;
}

// ---------------------------------------------------------------------------
// One-shot Claude call
// ---------------------------------------------------------------------------

function spawnAsync(
  cmd: string[],
  input: string,
  cwd: string,
  env: NodeJS.ProcessEnv,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd[0], cmd.slice(1), { cwd, stdio: ["pipe", "pipe", "pipe"], env });
    const chunks: Buffer[] = [];
    proc.stdout.on("data", (d: Buffer) => chunks.push(d));
    proc.stdin.write(input);
    proc.stdin.end();
    proc.on("close", () => resolve(Buffer.concat(chunks).toString()));
    proc.on("error", reject);
  });
}

async function claudeOneShot(
  prompt: string,
  opts: {
    systemPrompt?: string;
    model?: string;
    cwd?: string;
    command?: string;
    allowedTools?: string[];
  } = {},
): Promise<{ content: string; costUsd: number }> {
  const cmd = [opts.command ?? "claude", "-p", "--output-format", "json"];
  if (opts.systemPrompt) cmd.push("--append-system-prompt", opts.systemPrompt);
  if (opts.model) cmd.push("--model", opts.model);
  if (opts.allowedTools?.length)
    cmd.push("--allowedTools", opts.allowedTools.join(","));

  // Unset CLAUDECODE so `claude -p` doesn't refuse to run inside a Claude session.
  const env = { ...process.env };
  delete env.CLAUDECODE;

  const stdout = await spawnAsync(cmd, prompt, opts.cwd ?? ".", env);

  const text = stdout.trim();
  if (!text) return { content: "", costUsd: 0 };

  try {
    const data = JSON.parse(text);
    const content: string =
      data.result ??
      (Array.isArray(data.content)
        ? data.content
            .filter((b: { type: string }) => b.type === "text")
            .map((b: { text: string }) => b.text)
            .join("\n")
        : data.content ?? "");
    return { content: content.trim(), costUsd: data.cost_usd ?? 0 };
  } catch {
    return { content: text, costUsd: 0 };
  }
}

// ---------------------------------------------------------------------------
// Approval detection (collab mode)
// ---------------------------------------------------------------------------

const APPROVAL_SIGNALS = [
  "lgtm",
  "looks good to me",
  "looks good",
  "approved",
  "i approve",
  "ship it",
  "no further changes",
  "no issues found",
];

function isApproved(text: string): boolean {
  const lower = text.toLowerCase();
  return APPROVAL_SIGNALS.some((s) => lower.includes(s));
}

// ---------------------------------------------------------------------------
// Relay orchestrator
// ---------------------------------------------------------------------------

export interface RelayConfig {
  mode: RelayMode;
  roleA: RelayRole;
  roleB: RelayRole;
  topic?: string;        // debate mode
  task?: string;         // collab mode
  maxRounds?: number;
  cwd?: string;
  command?: string;
  allowedTools?: string[];
  onTurn?: (turn: RelayTurn) => void;
}

export async function runRelay(config: RelayConfig): Promise<RelayResult> {
  const { mode, roleA, roleB, maxRounds = 5, cwd, command, allowedTools, onTurn } = config;
  const transcript: RelayTurn[] = [];

  const callA = (prompt: string) =>
    claudeOneShot(prompt, { systemPrompt: roleA.systemPrompt, model: roleA.model, cwd, command, allowedTools });
  const callB = (prompt: string) =>
    claudeOneShot(prompt, { systemPrompt: roleB.systemPrompt, model: roleB.model, cwd, command, allowedTools });

  const addTurn = (round: number, speaker: string, content: string, costUsd: number) => {
    const turn: RelayTurn = { round, speaker, content, costUsd };
    transcript.push(turn);
    onTurn?.(turn);
  };

  try {
    if (mode === "debate") {
      const topic = config.topic ?? "No topic specified";

      for (let r = 1; r <= maxRounds; r++) {
        const lastB = transcript.at(-1)?.content ?? "";

        const promptA =
          r === 1
            ? `Topic: ${topic}\n\nPresent the strongest arguments from the "${roleA.name}" perspective with concrete examples.`
            : `Topic: ${topic}\n\n"${roleB.name}" argued:\n${lastB}\n\nRespond from the "${roleA.name}" perspective. Round ${r}/${maxRounds}.`;

        const { content: cA, costUsd: costA } = await callA(promptA);
        addTurn(r, roleA.name, cA, costA);

        const promptB =
          r === 1
            ? `Topic: ${topic}\n\n"${roleA.name}" argued:\n${cA}\n\nPresent counter-arguments from the "${roleB.name}" perspective.`
            : `Topic: ${topic}\n\n"${roleA.name}" argued:\n${cA}\n\nRespond from the "${roleB.name}" perspective. Round ${r}/${maxRounds}.`;

        const { content: cB, costUsd: costB } = await callB(promptB);
        addTurn(r, roleB.name, cB, costB);
      }
    } else {
      // collab mode
      const task = config.task ?? "No task specified";

      for (let r = 1; r <= maxRounds; r++) {
        const lastFeedback = transcript.at(-1)?.content ?? "";

        const promptDev =
          r === 1
            ? `Task: ${task}\n\nImplement a solution. Write clean, well-documented code.`
            : `Task: ${task}\n\nReviewer feedback:\n${lastFeedback}\n\nRevise the implementation. Iteration ${r}/${maxRounds}.`;

        const { content: cDev, costUsd: costDev } = await callA(promptDev);
        addTurn(r, roleA.name, cDev, costDev);

        const promptReview =
          `Task: ${task}\n\nDeveloper submitted (iteration ${r}):\n${cDev}\n\n` +
          `Review the code. Point out bugs, suggest improvements.\n` +
          `If good enough, say "LGTM" or "approved".`;

        const { content: cRev, costUsd: costRev } = await callB(promptReview);
        addTurn(r, roleB.name, cRev, costRev);

        if (isApproved(cRev)) {
          return {
            mode,
            transcript,
            finalState: "approved",
            totalCostUsd: transcript.reduce((s, t) => s + t.costUsd, 0),
          };
        }
      }
    }
  } catch {
    return {
      mode,
      transcript,
      finalState: "error",
      totalCostUsd: transcript.reduce((s, t) => s + t.costUsd, 0),
    };
  }

  return {
    mode,
    transcript,
    finalState: "max_rounds",
    totalCostUsd: transcript.reduce((s, t) => s + t.costUsd, 0),
  };
}
