/**
 * history.ts — JSONL conversation logger.
 *
 * Layout: $CCC_HISTORY_DIR/<session>/<timestamp>.jsonl
 * Format compatible with the Python version.
 */

import {
  appendFileSync,
  mkdirSync,
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from "fs";
import { join } from "path";
import { homedir } from "os";

const historyDir =
  process.env.CCC_HISTORY_DIR ??
  join(homedir(), ".local", "share", "claude-cli-connector", "history");

export interface HistoryEntry {
  ts: number;
  role: string;
  content: string;
  transport: string;
  event_type?: string;
  session_name?: string;
  metadata?: Record<string, unknown>;
}

export class ConversationLogger {
  private filePath: string;
  private transport: string;
  private sessionName: string;

  constructor(sessionName: string, transport = "tmux", runId?: string) {
    this.sessionName = sessionName;
    this.transport = transport;
    const id = runId ?? new Date().toISOString().slice(0, 19).replace(/:/g, "-");
    const dir = join(historyDir, sessionName);
    mkdirSync(dir, { recursive: true });
    this.filePath = join(dir, `${id}.jsonl`);
  }

  private append(entry: Omit<HistoryEntry, "ts" | "session_name" | "transport">): void {
    const line: HistoryEntry = {
      ts: Date.now() / 1000,
      session_name: this.sessionName,
      transport: this.transport,
      ...entry,
    };
    try {
      appendFileSync(this.filePath, JSON.stringify(line) + "\n");
    } catch {
      // Non-fatal: history write failures should not crash ccc
    }
  }

  logUser(content: string, meta?: Record<string, unknown>): void {
    this.append({ role: "user", content, event_type: "send", metadata: meta });
  }

  logAssistant(content: string, meta?: Record<string, unknown>): void {
    this.append({ role: "assistant", content, event_type: "response", metadata: meta });
  }

  logEvent(role: string, content: string, eventType = "", meta?: Record<string, unknown>): void {
    this.append({ role, content, event_type: eventType, metadata: meta });
  }

  read(): HistoryEntry[] {
    if (!existsSync(this.filePath)) return [];
    return readFileSync(this.filePath, "utf8")
      .split("\n")
      .filter(Boolean)
      .flatMap((line) => {
        try {
          return [JSON.parse(line) as HistoryEntry];
        } catch {
          return [];
        }
      });
  }
}

// ---------------------------------------------------------------------------
// Read helpers (for CLI history command)
// ---------------------------------------------------------------------------

export function getHistoryDir(): string {
  return historyDir;
}

export function listSessionsWithHistory(): string[] {
  if (!existsSync(historyDir)) return [];
  return readdirSync(historyDir)
    .filter((name) => {
      const dir = join(historyDir, name);
      return statSync(dir).isDirectory() && readdirSync(dir).some((f) => f.endsWith(".jsonl"));
    })
    .sort();
}

/** Returns run IDs (filenames without .jsonl), sorted chronologically. */
export function listSessionRuns(sessionName: string): string[] {
  const dir = join(historyDir, sessionName);
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".jsonl"))
    .sort()
    .map((f) => f.slice(0, -6)); // remove ".jsonl"
}

export function readHistoryFile(filePath: string): HistoryEntry[] {
  if (!existsSync(filePath)) return [];
  return readFileSync(filePath, "utf8")
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => {
      try { return [JSON.parse(line) as HistoryEntry]; }
      catch { return []; }
    });
}

export function readFullSessionHistory(sessionName: string): HistoryEntry[] {
  const runs = listSessionRuns(sessionName);
  const entries: HistoryEntry[] = [];
  for (const run of runs) {
    entries.push(...readHistoryFile(join(historyDir, sessionName, `${run}.jsonl`)));
  }
  entries.sort((a, b) => a.ts - b.ts);
  return entries;
}
