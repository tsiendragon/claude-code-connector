/**
 * store.ts — JSON session metadata store.
 *
 * Compatible with the Python version's JSON format (snake_case keys).
 * Location: $CCC_STORE_PATH or ~/.local/share/claude-cli-connector/sessions.json
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, renameSync } from "fs";
import { join, dirname } from "path";
import { homedir } from "os";
import { getConfig } from "./config.js";

const storePath =
  process.env.CCC_STORE_PATH ??
  join(homedir(), ".local", "share", "claude-cli-connector", "sessions.json");

// JSON shape matches the Python Pydantic model (snake_case for compatibility)
export interface SessionRecord {
  name: string;
  tmux_session_name: string;
  cwd: string;
  command: string;
  backend: string;
  created_at: number;
  last_seen_at: number;
  extra: Record<string, unknown>;
}

function load(): Record<string, SessionRecord> {
  if (!existsSync(storePath)) return {};
  try {
    return JSON.parse(readFileSync(storePath, "utf8"));
  } catch {
    return {};
  }
}

function save(data: Record<string, SessionRecord>): void {
  mkdirSync(dirname(storePath), { recursive: true });
  const tmp = storePath + ".tmp";
  writeFileSync(tmp, JSON.stringify(data, null, 2));
  renameSync(tmp, storePath);
}

export function storeGet(name: string): SessionRecord | null {
  return load()[name] ?? null;
}

export function storeSave(record: SessionRecord): void {
  const data = load();
  data[record.name] = record;
  save(data);
}

export function storeDelete(name: string): boolean {
  const data = load();
  if (!(name in data)) return false;
  delete data[name];
  save(data);
  return true;
}

export function storeList(): SessionRecord[] {
  return Object.values(load());
}

export function storeTouch(name: string): void {
  const data = load();
  if (data[name]) {
    data[name].last_seen_at = Date.now() / 1000;
    save(data);
  }
}

export function makeRecord(
  name: string,
  cwd: string,
  command: string,
  backend: string,
): SessionRecord {
  const now = Date.now() / 1000;
  return {
    name,
    tmux_session_name: getConfig().sessionPrefix + name,
    cwd,
    command,
    backend,
    created_at: now,
    last_seen_at: now,
    extra: {},
  };
}
