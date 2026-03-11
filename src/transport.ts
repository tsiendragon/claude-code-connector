/**
 * transport.ts — low-level tmux operations via Node.js child_process.
 *
 * All tmux sessions are namespaced via configurable prefix (default "ccc-").
 * Default terminal size: 220×50 (wide to minimise Claude's line wrapping).
 */

import { execFile } from "child_process";
import { promisify } from "util";
import { getConfig } from "./config.js";

const execFileAsync = promisify(execFile);
const fn = (name: string) => getConfig().sessionPrefix + name;

export async function createSession(
  name: string,
  cwd: string,
  command: string | string[] = "claude",
  env?: Record<string, string>,
): Promise<void> {
  const args = ["new-session", "-d", "-s", fn(name), "-x", "220", "-y", "50", "-c", cwd];
  if (env) {
    for (const [k, v] of Object.entries(env)) args.push("-e", `${k}=${v}`);
  }
  const cmdParts = Array.isArray(command) ? command : [command];
  await execFileAsync("tmux", [...args, ...cmdParts]);
}

export async function killSession(name: string): Promise<void> {
  try {
    await execFileAsync("tmux", ["kill-session", "-t", fn(name)]);
  } catch { /* ignore */ }
}

export interface SendTextOptions {
  enter?: boolean;    // default true
  submitKey?: string; // default "Enter"
  onLogged?: (text: string, timestamp: string) => void;
}

/** Send literal text, optionally followed by a submit key.
 *  submitKey defaults to "Enter"; use "C-m" for opencode. */
export async function sendText(
  name: string,
  text: string,
  opts: SendTextOptions = {},
): Promise<void> {
  await execFileAsync("tmux", ["send-keys", "-t", fn(name), "-l", text]);
  if (opts.enter !== false) {
    await execFileAsync("tmux", ["send-keys", "-t", fn(name), opts.submitKey ?? "Enter"]);
  }
  opts.onLogged?.(text, new Date().toISOString());
}

/** Send special key(s), e.g. "Enter", "Escape", "Up", "Down", "C-c". */
export async function sendKey(
  name: string,
  ...keys: string[]
): Promise<void> {
  await execFileAsync("tmux", ["send-keys", "-t", fn(name), ...keys]);
}

export async function capturePane(name: string, ansi = false): Promise<string[]> {
  const args = ["capture-pane", "-p"];
  if (ansi) args.push("-e");
  args.push("-t", fn(name));
  const { stdout } = await execFileAsync("tmux", args);
  return stdout.split("\n");
}

export async function captureFull(
  name: string,
  scrollback = 5000,
  ansi = false,
): Promise<string[]> {
  const args = ["capture-pane", "-p", "-S", String(-scrollback)];
  if (ansi) args.push("-e");
  args.push("-t", fn(name));
  const { stdout } = await execFileAsync("tmux", args);
  return stdout.split("\n");
}

export async function isAlive(name: string): Promise<boolean> {
  try {
    await execFileAsync("tmux", ["has-session", "-t", fn(name)]);
    return true;
  } catch { return false; }
}

export async function listSessions(): Promise<string[]> {
  try {
    const prefix = getConfig().sessionPrefix;
    const { stdout } = await execFileAsync("tmux", ["list-sessions", "-F", "#S"]);
    return stdout
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.startsWith(prefix))
      .map((s) => s.slice(prefix.length));
  } catch { return []; }
}

export async function resizePane(
  name: string,
  width: number,
  height: number,
): Promise<void> {
  try {
    await execFileAsync("tmux", [
      "resize-window", "-t", fn(name), "-x", String(width), "-y", String(height),
    ]);
  } catch { /* ignore */ }
}
