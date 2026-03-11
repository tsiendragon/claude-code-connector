# ccc-ts → npm 包改造需求文档

> **目标**：将 ccc-ts 改造为可发布到 npm 的 Node.js 包（包名 `ccc-ts`），供 NanoClaw 直接 `import` 使用，无需 Bun 运行时，无需复制代码。

---

## 现状分析

| 项目 | 当前状态 |
|------|---------|
| 运行时 | Bun（`$` shell API、`Bun.spawn`） |
| 模块系统 | `"module": "ESNext"` + `"moduleResolution": "Bundler"` |
| 入口 | `"module": "src/cli.ts"`（Bun 专属字段） |
| Session 前缀 | `"ccc-"` 硬编码在 `transport.ts` 和 `store.ts` |
| npm 发布 | 无 `exports` 字段，无 `dist`，无 `.d.ts` |

---

## 需要修改的文件（共 6 个）

---

### 1. `src/transport.ts` — Bun `$` → Node.js `execFile`，`sendText` 加日志回调

**当前代码（Bun 专属）：**
```typescript
import { $ } from "bun";
const PREFIX = "ccc-";

export async function createSession(name, cwd, command = "claude") {
  await $`tmux new-session -d -s ${fn(name)} -x 220 -y 50 -c ${cwd} ${command}`;
}
export async function isAlive(name) {
  const r = await $`tmux has-session -t ${fn(name)}`.nothrow();
  return r.exitCode === 0;
}
// ...其余函数同理
```

**改为（Node.js）：**
```typescript
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

export async function isAlive(name: string): Promise<boolean> {
  try {
    await execFileAsync("tmux", ["has-session", "-t", fn(name)]);
    return true;
  } catch { return false; }
}
// ...其余函数：capturePane, captureFull, sendText, sendKey, killSession, listSessions, resizePane
```

**新增改动：**
- `createSession` 支持 `command: string | string[]`（用于传 flag 数组）
- `createSession` 支持 `env?: Record<string, string>`（通过 tmux `-e` 注入环境变量）
- PREFIX 改为从 `getConfig()` 读取（见第 5 节）
- `sendText` 无需修改（见下方对话日志设计）

---

### 2. `src/relay.ts` — `Bun.spawn` → Node.js `spawn`

**当前代码（Bun 专属）：**
```typescript
const proc = Bun.spawn(cmd, {
  stdin: new TextEncoder().encode(prompt),
  stdout: "pipe",
  stderr: "pipe",
  cwd: opts.cwd ?? ".",
});
const [stdout] = await Promise.all([
  new Response(proc.stdout).text(),
  proc.exited,
]);
```

**改为（Node.js）：**
```typescript
import { spawn } from "child_process";

function spawnAsync(cmd: string[], input: string, cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd[0], cmd.slice(1), { cwd, stdio: ["pipe", "pipe", "pipe"] });
    const chunks: Buffer[] = [];
    proc.stdout.on("data", (d: Buffer) => chunks.push(d));
    proc.stdin.write(input);
    proc.stdin.end();
    proc.on("close", () => resolve(Buffer.concat(chunks).toString()));
    proc.on("error", reject);
  });
}
```

其余 relay 逻辑（debate/collab 模式、approval detection）**不变**。

---

### 3. `src/session.ts` — 新增 `ensureSession`，`send`/`waitReady` 支持直传 `backend`

**新增 `ensureSession`（幂等，已存在则跳过）：**
```typescript
export interface SessionConfig {
  name: string;
  cwd: string;
  command?: string | string[];
  backend?: string;
  env?: Record<string, string>;
  startupWaitMs?: number;
}

// 幂等：session 已存在则直接返回
export async function ensureSession(config: SessionConfig): Promise<void> {
  const { name, cwd, command = "claude", backend = "claude", env, startupWaitMs = 2000 } = config;
  if (await isAlive(name)) return;
  await tmuxCreate(name, cwd, command, env);
  storeSave(makeRecord(name, cwd, Array.isArray(command) ? command[0] : command, backend));
  if (startupWaitMs > 0) await sleep(startupWaitMs);
}
```

**`SendOptions` 新增 `backend` 字段：**
```typescript
export interface SendOptions {
  timeout?: number;
  noWait?: boolean;
  autoApprove?: boolean;
  initialDelay?: number;
  backend?: string;  // 新增：直接传入，不走 store 查询
}
```

**`send` 修改（backend 优先级：opts > store > 默认值）：**
```typescript
export async function send(name: string, text: string, opts: SendOptions = {}): Promise<string> {
  const { timeout = 300, noWait = false, autoApprove = false, initialDelay = 800 } = opts;
  const record = storeGet(name);
  const backend = opts.backend ?? record?.backend ?? "claude";  // 修改点
  // ...其余逻辑不变
}
```

**`waitReady` 新增可选 `backend` 参数：**
```typescript
export async function waitReady(
  name: string,
  timeout = 300,
  initialDelay = 500,
  backend?: string,  // 新增（可选，不传则走 store）
): Promise<string> {
  const rec = storeGet(name);
  const resolvedBackend = backend ?? rec?.backend ?? "claude";  // 修改点
  // ...其余逻辑不变
}
```

**所有 import 路径从 `.ts` 改为 `.js`：**
```typescript
// 改前
import { createSession as tmuxCreate } from "./transport.ts";
// 改后
import { createSession as tmuxCreate } from "./transport.js";
```

---

### 4. `src/store.ts` — session 前缀改为可配置

**当前代码：**
```typescript
export function makeRecord(name, cwd, command, backend): SessionRecord {
  return {
    tmux_session_name: "ccc-" + name,  // 硬编码
    ...
  };
}
```

**改为：**
```typescript
import { getConfig } from "./config.js";

export function makeRecord(name, cwd, command, backend): SessionRecord {
  return {
    tmux_session_name: getConfig().sessionPrefix + name,  // 可配置
    ...
  };
}
```

---

### 5. `src/config.ts` — **新建**，全局配置

```typescript
export interface CccConfig {
  /** tmux session 名前缀。默认 "ccc-" */
  sessionPrefix: string;
}

const _config: CccConfig = { sessionPrefix: "ccc-" };

export function configure(opts: Partial<CccConfig>): void {
  Object.assign(_config, opts);
}

export function getConfig(): CccConfig {
  return _config;
}
```

---

### 6. `src/index.ts` — **新建**，库的统一导出入口

```typescript
// 配置
export { configure } from "./config.js";
export type { CccConfig } from "./config.js";

// 低层 tmux 操作
export {
  createSession, ensureSession as ensureSessionRaw,
  killSession, isAlive, listSessions,
  sendText, sendKey, capturePane, captureFull, resizePane,
} from "./transport.js";

// 高层 session API
export {
  runSession, ensureSession, killSession as killManagedSession,
  readState, waitReady, waitState, send, approvePermission,
} from "./session.js";
export type { PaneState, SendOptions, SessionConfig } from "./session.js";

// Parser（供高级用户直接使用）
export {
  stripAnsi, detectReady, detectPermission, detectChoices, extractLastResponse,
} from "./parser.js";
export type { ReadyResult, PermissionPrompt, ChoiceItem, PermissionOption } from "./parser.js";

// Relay
export { runRelay } from "./relay.js";
export type { RelayConfig, RelayResult, RelayMode, RelayRole, RelayTurn } from "./relay.js";
```

---

## 需要修改的配置文件（共 3 个）

---

### 7. `tsconfig.json` — 从 Bundler/ESNext 改为 NodeNext

**当前：**
```json
{
  "compilerOptions": {
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "noEmit": true,
    "allowImportingTsExtensions": true
  }
}
```

**改为：**
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "declaration": true,
    "declarationMap": true,
    "strict": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

关键变化：
- 删除 `noEmit: true` → 允许编译输出
- 删除 `allowImportingTsExtensions: true` → NodeNext 不允许 `.ts` 后缀
- 所有相对 import 改为 `.js` 后缀（TypeScript NodeNext 规范）

---

### 8. `package.json` — 重写为 npm 发布格式

**当前：**
```json
{
  "name": "ccc",
  "module": "src/cli.ts",
  "bin": { "ccc": "src/cli.ts" },
  "scripts": { "build": "bun build src/cli.ts --compile --outfile ccc" },
  "devDependencies": { "@types/bun": "latest" }
}
```

**改为：**
```json
{
  "name": "ccc-ts",
  "version": "0.2.0",
  "description": "Manage Claude Code interactive sessions in tmux — Node.js library",
  "main": "dist/index.js",
  "types": "dist/index.d.ts",
  "exports": {
    ".": {
      "import": "./dist/index.js",
      "types": "./dist/index.d.ts"
    }
  },
  "bin": {
    "ccc": "dist/cli.js"
  },
  "files": ["dist", "README.md"],
  "scripts": {
    "build": "tsc && node scripts/fix-shebang.mjs",
    "prepublishOnly": "npm run build"
  },
  "dependencies": {
    "citty": "^0.1.6"
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "typescript": "^5.9.3"
  },
  "engines": { "node": ">=18" },
  "license": "MIT"
}
```

---

### 9. `scripts/fix-shebang.mjs` — **新建**，为 CLI 添加 shebang

TypeScript 编译时会丢弃 `#!/usr/bin/env node` shebang。需要 postbuild 脚本补回：

```javascript
// scripts/fix-shebang.mjs
import { readFileSync, writeFileSync, chmodSync } from "fs";

const file = "dist/cli.js";
const content = readFileSync(file, "utf8");
if (!content.startsWith("#!")) {
  writeFileSync(file, "#!/usr/bin/env node\n" + content);
  chmodSync(file, 0o755);
}
```

---

## 其他文件（无需修改）

| 文件 | 原因 |
|------|------|
| `src/parser.ts` | 纯 TypeScript 逻辑，无外部依赖，无 Bun API |
| `src/history.ts` | 只用 Node.js 内置 `fs`/`path`/`os`，已兼容 |
| `src/store.ts` | 只用 Node.js 内置 `fs`，import 路径需改 `.js` 后缀 |
| `src/cli.ts` | import 路径需改 `.js` 后缀，shebang 改 `#!/usr/bin/env node` |

---

## NanoClaw 的使用方式

```typescript
import { configure, ensureSession, send, isAlive, killSession } from "ccc-ts";

// 启动时配置（改前缀避免与用户自己的 tmux 会话冲突）
configure({ sessionPrefix: "nc-" });

// 每次收到消息（幂等，session 已存在则跳过创建）
await ensureSession({
  name: groupFolder,           // e.g. "whatsapp_family"
  cwd: groupFolderPath,
  command: [
    "claude",
    "--dangerously-skip-permissions",
    "--mcp-config", mcpConfigPath,
  ],
  env: {
    CLAUDE_CONFIG_DIR: sessionConfigDir,  // 隔离 ~/.claude 配置
  },
  backend: "claude",
  startupWaitMs: 2000,
});

// 发消息，等待 ❯ 出现（内部 poll）
const response = await send(groupFolder, userMessage, {
  timeout: 300,
  backend: "claude",
});

// 健康检查
const alive = await isAlive(groupFolder);

// 清理
await killSession(groupFolder);
```

---

## 发布步骤

```bash
cd ccc-ts
npm install          # 安装 @types/node、typescript
npm run build        # tsc + fix-shebang
npm publish          # 发布到 npm（需 npm login）
```

NanoClaw 安装：
```bash
npm install ccc-ts
```

---

## 改动规模估计

| 类型 | 文件数 | 改动量 |
|------|--------|--------|
| 重写（Bun→Node.js）+ 功能扩展 | 2 | `transport.ts`（~80行 + `sendText` onLogged 回调 +5行）、`relay.ts`（~30行函数体） |
| 功能扩展 | 1 | `session.ts`（+30行：ensureSession + backend 参数） |
| 小改（import路径 + prefix） | 2 | `store.ts`、`cli.ts` |
| 新建 | 3 | `config.ts`（~20行）、`index.ts`（~30行）、`fix-shebang.mjs`（~10行） |
| 配置文件重写 | 2 | `tsconfig.json`、`package.json` |

**总计：新增约 90 行，修改约 130 行，纯机械替换为主，无业务逻辑变动。**

---

## 对话历史记录设计决定

### 为什么不用 `extractLastResponse`

Claude Code 调用 MCP tool 时，pane 里显示的内容是：

```
⏺ nanoclaw:send_message({"chatJid": "120363...@g.us", "text": "明天北京天气晴..."})
  ✓ {"success": true}
```

消息内容有出现在 pane 里，但混杂着 Claude 的中间思考、其他 tool call 结果。提取后还需解析 JSON、过滤非 send_message 调用，脆弱易错。**NanoClaw 不使用它。**

---

### 记录点：完全在 NanoClaw 侧，不依赖 ccc-ts

```
用户消息
  ↓
channel.onInboundMessage(msg)
  ↓
storeMessage(msg)              ← 入向已在此持久化，messages 表 ✅
  ↓
sendText(session, prompt)      ← ccc-ts，无需修改
  ↓
claude 处理（tmux 内）
  ↓
claude 调 send_message MCP tool
  ↓
ipc.ts: deps.sendMessage(jid, text)   ← 出向在此记录，一行代码 ✅
  ↓
用户收到消息
```

**入向**：`storeMessage(msg)` 在 channel 收到消息时立刻执行，早于任何 tmux 调用。`messages` 表已有 `sender, sender_name, content, timestamp`——不需要新的记录逻辑。

**出向**：`ipc.ts` 第 83 行 `deps.sendMessage()` 之后加一行写 DB：

```typescript
// src/ipc.ts（NanoClaw，不涉及 ccc-ts）
await deps.sendMessage(data.chatJid, data.text);

// 新增：记录出向消息
db.logOutbound({
  group_folder: sourceGroup,
  chat_jid:     data.chatJid,
  content:      data.text,
  sent_at:      new Date().toISOString(),
});
```

**查询时合并两张表**：
```sql
-- 入向：来自 messages 表
SELECT 'in' AS direction, sender_name, content, timestamp AS ts
FROM messages WHERE chat_jid = ?

UNION ALL

-- 出向：来自 outbound_messages 表
SELECT 'out' AS direction, 'Claude' AS sender_name, content, sent_at AS ts
FROM outbound_messages WHERE chat_jid = ?

ORDER BY ts ASC;
```

---

### ccc-ts 需要做的改动

**对话日志功能：零改动。** 完全在 NanoClaw 侧实现，ccc-ts 不涉及。
