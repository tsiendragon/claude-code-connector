# ccc 命令逻辑文档

## 核心理念 — ANSI 感知的帧稳定性检测

ccc 通过 tmux 管理 AI CLI 会话，核心检测逻辑分两层：

1. **帧稳定性判断**（thinking vs 静止）— 比较 **带 ANSI 颜色码** 的原始帧
2. **静止后内容分类**（ready / composed / choosing / approval / unknown）— 在 **去 ANSI** 的纯文本上做结构解析

```
classifyWindow(strippedFrames, backend, rawFrames)
  │
  ├─ rawFrames 不同 → "thinking" 或 "typing"
  │   （ANSI 颜色变化也算不同 — 闪烁/动画/spinner 都能检测到）
  │   （只有 ❯ 输入行变化 → "typing"，否则 → "thinking"）
  │
  └─ rawFrames 完全相同（静止）→ analyzeStableLines(lines, backend)
        ├─ detectPermission    → "approval"
        ├─ detectChoices       → "choosing"
        ├─ detectComposedInput → "composed"
        ├─ detectReady         → "ready"    ← 纯 prompt 检测，不做 busy 判断
        └─ else                → "unknown"
```

**为什么用 ANSI 颜色而不是文本匹配来判断 busy？**

- 通用：不管 backend 把 busy 文本叫什么（"Working"、"Thinking..."、"▣ Build"），只要 TUI 在重绘/闪烁颜色，raw frames 就会不同
- 向前兼容：backend 改了 busy 文本名称也不会失效
- 统一：所有 backend 共用同一套帧比较逻辑，不需要各自硬编码 busy 关键字

**`detectReady` 不做 busy 检测**，只回答一个问题："这个稳定的内容是不是 idle/ready 状态？"

---

## 架构分层

```
Layer 0  tmux 原语          transport.ts
Layer 1  单帧解析            parser.ts
Layer 2  帧合成 / 状态读取   session.ts  ← readState / classifyWindow
Layer 3  轮询等待            session.ts  ← waitReady / waitState
Layer 4  高级操作            session.ts  ← send / approve
Layer 5  CLI 命令            cli.ts
```

---

## Layer 0 — tmux 原语

所有 tmux 操作都通过 `execFile("tmux", [...])` 执行，会话名加前缀 `ccc-`（可配置），窗口尺寸固定 220×50（减少 Claude 自动换行）。

### `capturePane(name, ansi=false)`

```
tmux capture-pane -p [-e] -t ccc-<name>
→ 返回当前可见 pane 内容（50 行）
→ ansi=true 时加 -e flag，保留 ANSI 转义码（颜色/粗体等）
```

### `captureFull(name, scrollback=5000, ansi=false)`

```
tmux capture-pane -p -S -5000 [-e] -t ccc-<name>
→ 返回含滚动缓冲区的完整历史
→ ansi=true 时保留 ANSI 转义码
```

`-e` flag 的作用：保留 `\e[1m`（bold）、`\e[32m`（green）等 ANSI escape codes。
用于帧间颜色变化检测 — 即使纯文本相同，颜色变化也意味着 TUI 在重绘（busy）。

用于 opencode（响应可能已滚出可见区域）和 `ccc last --full`。

### `sendText(name, text, opts?)`

```
tmux send-keys -t ccc-<name> -l <text>   ← -l 禁止 tmux 解释特殊字符
tmux send-keys -t ccc-<name> Enter        ← 默认提交键
                                          ← opencode 用 C-m（carriage return）
```

### `sendKey(name, ...keys)`

```
tmux send-keys -t ccc-<name> <key1> [key2 ...]
```

发送特殊键序列（Enter、Escape、Up/Down、C-c 等）。

### `isAlive(name)`

```
tmux has-session -t ccc-<name>
→ true / false（catch 即为 false）
```

### `createSession(name, cwd, command, env?)`

```
tmux new-session -d -s ccc-<name> -x 220 -y 50 -c <cwd> [env] <command>
```

### `listSessions()`

```
tmux list-sessions -F "#S"
→ 过滤出 ccc- 前缀，去掉前缀后返回
```

---

## Layer 1 — 单帧解析

所有函数接收 `string[]`（已过 ANSI stripping），不做 IO。

### `stripAnsi(lines)`

用正则剥离 ANSI/VT100 转义码（CSI、OSC、Fe 序列）和 `\r`。

### `detectReady(lines, prevLines, elapsed, minStableSecs, backend)` → `ReadyResult`

**纯 prompt 检测 — 不做 busy 判断。** 只回答："这个内容是不是 idle/ready 状态？"

Busy/thinking 检测由 Layer 2 的帧稳定性比较（带 ANSI）负责。`detectReady` 只在帧已确认稳定后被 `analyzeStableLines` 调用。

**各 backend 的 idle prompt 特征：**

| Backend | Idle 信号 | 示例 |
|---------|----------|------|
| Claude / Cursor | 末尾空 `❯`（或 `›`/`>`），跳过空行/分隔线/TUI hint | `❯\u00a0` |
| Codex | 两个 `›` 之间有实际内容（回复完成后的 idle suggestion） | `› singapore` ... content ... `› Find and fix...` |
| Opencode | `▣` 标记带时间后缀 | `▣  Build · model · 22.8s` |

**Codex 细节：**
```
› singapore            ← 用户输入
  • response text...   ← 回复内容
› Find and fix bugs... ← idle suggestion（两个 › 之间有内容 = ready）
```

**Opencode 细节：**
```
▣  Build · model         ← 无时间 = 还在处理（not_ready，frozen TUI 安全网）
▣  Build · model · 22.8s ← 有时间 = 完成了（ready）
```

**Stability fallback：** 如果 prompt 检测都没命中（比如刚启动、没有 ▣/❯），但 `prevLines === lines` 且 `elapsed > minStableSecs`，返回 `{ isReady: true, confidence: "stable" }`。

### `detectPermission(lines)`

扫描两种格式：

**Format A（Allow once / Allow always / Deny）：**

```
⏺ Claude wants to run: Bash(date)
  Allow Bash?
❯ Allow once          ← selected
  Allow always
  Deny
```

返回 `{ type: "allow", tool: "Bash", options: [...] }`

**Format B（Do you want to proceed? + 数字列表）：**

```
Do you want to proceed?
❯ 1. Yes
  2. Yes, allow reading from repos/
  3. No
```

返回 `{ type: "proceed", tool: "<context from lines above>", options: [...] }`

### `detectChoices(lines)`

找以 `❯ <text>` 开头的选中项，后跟至少 2 个缩进选项。用于非权限类菜单（workspace trust、model picker 等）。返回 `ChoiceItem[]` 或 `null`。

支持的箭头字符：`❯`（Claude）、`›`（Codex，仅匹配 `› N.` 模式以避免误匹配 idle prompt）、`>`。

### `detectComposedInput(lines)`

从末尾向前遍历（跳过空行、分隔线、TUI hint 行），找到 `❯ <text>`（❯/›/> 后有内容）则返回文字，否则返回 `null`。

```
❯ some typed text but not submitted   ← 返回 "some typed text but not submitted"
❯                                     ← 空 idle prompt，返回 null
```

与 `detectChoices` / `detectPermission` 的区别：后两者要求有多个选项或特定关键词，`detectComposedInput` 只关心输入框里有没有文字。

### `extractLastResponse(lines, backend)`

**Claude / Cursor：**

```
从末尾向前，跳过空行 / 分隔线 / TUI hint → 找 idle ❯ (idleIdx)
从 idleIdx 向前 → 找 ❯ <user text> (userIdx)
返回 lines[userIdx+1 .. idleIdx] 去掉分隔线和 hint 行
若 user text 已滚出缓冲区（userIdx < 0），返回 lines[0..idleIdx] 全部内容
```

**Codex：** 取最后两个 `›` 之间的内容，去掉 `•` 前缀。

**Opencode：** 找最后一个带时长的 `▣`，收集 `▣` 上方全部内容（包含 `┃` 框内的代码、思考过程），去掉 `┃` 前缀和 sidebar（`█` 后内容）。

---

## Layer 2 — 帧合成 / 状态读取

### `classifyWindow(strippedFrames, backend, rawFrames?)` → `PaneState`

**核心纯函数**，被 `readState` 和 `awaitFrameMatch` 共用。

接收两组帧：
- `strippedFrames` — 去 ANSI 的纯文本（用于内容分析）
- `rawFrames`（可选）— 带 ANSI 颜色码的原始帧（用于稳定性比较）

```
1. 稳定性判断 — 用 rawFrames（有的话）比较
   rawFrames 全相同？
   │
   ├─ NO（有差异）— TUI 在重绘（颜色闪烁、spinner、内容变化）
   │   ├─ 去掉 ❯ 输入行后 strippedFrames 仍一致？
   │   │   → YES → { state: "typing" }     ← 仅输入框文字在变
   │   │   → NO  → { state: "thinking" }   ← 内容或颜色在变
   │
   └─ YES（稳定）→ analyzeStableLines(lastLines, backend)
```

**为什么用 rawFrames 而不是 strippedFrames 比较稳定性？**

某些 backend（如 Codex "Working"）在处理时文本不变但颜色在闪烁。
去 ANSI 后帧完全相同 → 误判为 stable → 误判为 ready。
保留 ANSI 后帧不同 → 正确判断为 thinking。

### `analyzeStableLines(lines, backend)` → `PaneState`

帧已确认稳定后的内容分类，优先级从高到低：

```
detectPermission    → { state: "approval", permission, lastResponse }
detectChoices       → { state: "choosing", choices }
detectComposedInput → { state: "composed", composedText }
detectReady(lines, lines, 999, 0, backend)
  → ready  → { state: "ready", lastResponse }
  → 否则   → { state: "unknown" }（稳定但无 idle prompt）
```

### `readState(name, full, windowMs=1000, intervalMs=200)`

多帧差异检测，用于 `ccc status` / `ccc read`。

```
1. isAlive? → NO → { state: "dead" }

2. 采集 N 帧（N = windowMs / intervalMs，默认 5 帧）
   每帧：capturePane(name, ansi=true) → 同时保存 raw 和 stripped
   帧间 sleep(intervalMs)

3. classifyWindow(strippedFrames, backend, rawFrames)
```

---

## Layer 3 — 核心帧循环 + 等待封装

### `awaitFrameMatch(name, timeout, backend, predicate, beforeText?, intervalMs?, stableThreshold?)` （内部函数）

所有等待逻辑的唯一底层。持续采帧（**带 ANSI**），用滑动窗口分类当前 state，predicate 满足时返回。

```
每次采帧:
  rawLines = capturePane(name, ansi=true)   ← 保留 ANSI 颜色码
  rawText  = rawLines.join("\n")            ← 用于帧间比较（颜色变化 = 不同帧）
  text     = stripAnsi(rawLines).join("\n") ← 用于内容分析

Phase 1（仅 beforeText 不为 null）:
  跳过 stripped text 与 beforeText 相同的帧，直到内容发生变化

Phase 2 — 滑动窗口:
  每 intervalMs(200ms) 采一帧，保留最近 stableThreshold+1 帧（raw + stripped 各一组）
  取末尾 stableThreshold(3) 帧:
    classifyWindow(strippedFrames, backend, rawFrames)
      → raw 帧全相同 → analyzeStableLines → ready/approval/choosing/composed/unknown
      → raw 帧有差异，仅 ❯ 行变化 → typing
      → raw 帧有差异，其他内容或颜色变化 → thinking

  predicate(state, prevLinesText)?
    YES → return state
    NO  → prevLinesText = state.lines，继续

session 挂掉:
  predicate({ state:"dead" })? → return dead
  否则 → throw Error
```

**三个 wrapper 对比：**

| 函数 | beforeText | predicate |
|------|-----------|-----------|
| `waitForResponse` | 有（send 前快照） | `s.state !== "thinking" && !== "unknown"` |
| `waitReady` | 无 | `s.state === "ready" \| "approval" \| "choosing"` |
| `waitState` | 无 | `s.state === target` 或 `lines 与上次不同`（any-change）|

### `waitReady(name, timeout, initialDelay, backend)` → `string`

```
sleep(initialDelay)
awaitFrameMatch(name, timeout, backend,
  s => s.state === "ready" || "approval" || "choosing")
→ return state.lines.join("\n")
```

保持 `string` 返回类型兼容外部 API。

### `waitState(name, target, timeout)` → `PaneState`

支持目标：`ready | thinking | typing | composed | approval | choosing | unknown | dead | any-change`

```
awaitFrameMatch(name, timeout, backend,
  target === "any-change"
    ? (s, prev) => prev !== null && s.lines !== prev   ← 相邻两次分类结果不同
    : (s) => s.state === target
)
```

### `waitForResponse(name, timeout, backend, beforeText)` → `PaneState`

```
awaitFrameMatch(name, timeout, backend,
  s => s.state !== "thinking" && s.state !== "unknown",
  beforeText   ← Phase 1：跳过 send 前的旧状态
)
```

---

## Layer 4 — 高级操作

### `waitForResponse(name, timeout, backend, beforeText)` （内部函数）

`awaitFrameMatch` 的薄封装，从 `send` 触发时刻起累积 ANSI 感知帧，直到内容稳定后返回。

```
awaitFrameMatch(name, timeout, backend,
  s => s.state !== "thinking" && s.state !== "unknown",
  beforeText   ← Phase 1：跳过 send 前的旧状态
)
```

**与 `readState` 固定窗口的区别：**

| | `readState` | `waitForResponse` |
|--|--|--|
| 帧来源 | 固定 1s 窗口采集 | 从 send 时刻起持续累积 |
| 稳定判断 | 全部 N 帧相同 | 末尾连续 3 帧 raw(ANSI) 相同 |
| 误判风险 | 窗口可能跨越变化/稳定边界 | 早期变化帧不影响稳定判断 |
| 速度 | 固定 ~1s | 稳定即返回，无固定开销 |

### `send(name, text, opts)` → `PaneState`

```
1. 快照 beforeText（Phase 1 的基准）

2. 发送文字
   · claude/cursor: sendText → Enter
   · opencode:      sendText → C-m
   · codex:         sendText → sleep(150) → Enter

3. noWait → return { state: "unknown", lines: [] }

4a. autoApprove → sendWithAutoApprove

4b. 所有 backend（统一路径）：
    waitForResponse(name, timeout, backend, beforeText)
    → ANSI 感知帧累积，直到稳定 → return PaneState
```

**返回值：** `PaneState`，调用方通过 `state.state` 判断结束原因，`state.lastResponse` 取响应文字。可能返回的 state：

| state | 含义 |
|-------|------|
| `ready` | 正常完成，`lastResponse` 有响应 |
| `approval` | Claude 等待权限授权 |
| `choosing` | Claude 显示选择菜单 |
| `unknown` | noWait / 奇怪状态 |
| `dead` | session 挂了 |

### `sendWithAutoApprove(name, timeout, backend, logger, beforeText)` （内部函数）

`--auto-approve` 时调用，循环 `waitForResponse` + 自动授权。

```
loop（deadline = timeout）:
  state = waitForResponse(name, remaining, backend, currentBeforeText)
  · state === "approval" → approvePermission → currentBeforeText = state.lines → continue
  · 其他                 → log lastResponse → return state
```

每次 approve 后更新 `currentBeforeText`，使下一次 `waitForResponse` 从授权后的状态起重新等待变化。

### `approvePermission(name, perm, answer)`

响应权限提示（Format A / Format B），通过方向键导航到目标选项后按 Enter。

```
from = currently selected option index
to   = target option index (yes=0, always=1, no=2)
→ navigateAndSelect(name, from, to)
```

### `approveChoice(name, choices, answer)`

响应通用选择菜单（workspace trust、model picker 等），支持多种 answer 格式：

| answer | 行为 |
|--------|------|
| `"1"`, `"2"` | 1-based 位置选择 |
| `"yes"` | 选第一个 |
| `"no"` | 选最后一个 |
| `"substring"` | 模糊匹配 label |

```
from = currently selected choice index
to   = resolved target index
→ navigateAndSelect(name, from, to)
```

### `navigateAndSelect(name, from, to)` （内部函数）

`approvePermission` 和 `approveChoice` 共用的导航辅助函数。

```
delta = to - from
delta > 0: sendKey("Down") × delta
delta < 0: sendKey("Up")   × |delta|
sleep(100)
sendKey("Enter")
```

---

## Layer 5 — CLI 命令

### 会话生命周期

#### `ccc run <name> [--cwd] [--cursor|--codex|--opencode] [--startup N]`

```
runSession(name, cwd, command, backend, startupMs)
  = tmuxCreate + storeSave + sleep(startupMs)
```

`startupMs` 默认 2000ms，给 Claude TUI 渲染时间。

#### `ccc kill <name>`

```
killSession(name) = tmuxKill + storeDelete
```

#### `ccc ps [--json]`

```
storeList() → 并发 isAlive() → 输出

--json → JSON 数组，每条记录附加 alive 字段
else   → 对齐表格：

  NAME                  BACKEND  LAST SEEN  CWD
  ────────────────────  ───────  ─────────  ────────────────────
● my-project            claude   2m ago     /Users/mj/repos/foo
○ old-session           claude   3d ago     /tmp/test

● = 存活  ○ = dead
列宽根据内容自适应（最小宽度 = 列头宽度）
LAST SEEN 显示相对时间：Xs ago / Xm ago / Xh ago / Xd ago
```

#### `ccc clean [--yes] [--dry-run]`

```
storeList() → 过滤 isAlive()=false → 可选确认 → storeDelete
```

#### `ccc attach <name>`

```
requireSession + isAlive → 仅验证存活，不做任何操作
```

---

### 原语直通命令

#### `ccc input <name> <text> [--no-enter]`

```
sendText(name, text, { enter: !noEnter })
```

用于发送 slash 命令（`/model`、`/help`）或部分输入，不等待响应。

#### `ccc key <name> <keys> [--repeat N]`

```
for i in 0..N: sendKey(name, ...keys.split(","))
```

发送特殊键序列，支持逗号分隔多键、重复。

#### `ccc interrupt <name>`

```
sendKey(name, "C-c")
```

打断 Claude 当前执行，不等待状态变化。

---

### 读取 / 观测命令

#### `ccc tail <name> [--lines N] [--full]`

```
capturePane（或 captureFull）→ stripAnsi → slice(-N) → 去尾部空行 → print
```

单帧快照，纯展示，无状态解析。

#### `ccc last <name> [--raw] [--full]`

```
capturePane（或 captureFull）→ stripAnsi
--raw → print as-is
else  → extractLastResponse → print
```

单帧快照，适合 Claude 已 ready 后立即调用。

#### `ccc status <name> [--json] [--porcelain]`

```
readState(name)   ← 多帧差异检测，默认 1000ms/200ms
--porcelain → print state string only
--json      → print JSON
else        → "state: <s>\npermission: ...\n\n<lastResponse>"
```

#### `ccc read <name> [--json] [--full] [--window N] [--interval N]`

```
readState(name, full, window, interval)   ← 参数可调
printState(state, json)
```

与 `status` 相同逻辑，额外支持 `--full`（全缓冲区）和自定义观测窗口。

---

### 等待命令

#### `ccc wait <name> <state> [--timeout T] [--json]`

```
waitState(name, target, timeout)   ← 300ms 轮询
printState(result, json)
```

支持的目标：`ready | thinking | typing | composed | approval | choosing | dead | any-change`

`any-change` 特殊：不看 state 类型，只要 pane 内容有任何变化就返回。

---

### 操作命令

#### `ccc send <name> <msg> [--no-wait] [--auto-approve] [--timeout T] [--json]`

```
send(name, msg, { noWait, autoApprove, timeout }) → PaneState
--json → print full JSON state
else   → print state.lastResponse（默认，向下兼容）
```

#### `ccc approve <name> [yes|always|no|<number>|<substring>]`

```
readState(name) → state
  state === "approval"  → approvePermission(name, perm, answer)
  state === "choosing"  → approveChoice(name, choices, answer)
  其他                   → die("No permission prompt or choice menu detected")
```

同时支持权限提示和通用选择菜单（workspace trust、model picker 等）。

#### `ccc model <name> [<model>] [--list]`

```
1. sendKey(name, "/") → sleep(200ms)
2. 轮询最多 5 秒（500ms 间隔）等待 model picker 出现
3. --list → 打印所有可用模型
4. 找到目标 model（模糊匹配）
5. cursor backend：C-c → sendText("/model <name>")
   claude backend：导航 Up/Down → Enter
```

---

### 历史记录命令

#### `ccc history [<name>] [--last N] [--run ID] [--json]`

```
无 name → 列出所有有历史的 session（listSessionsWithHistory）
有 name：
  --run ID → 读指定 run 的 JSONL 文件
  else     → readFullSessionHistory（合并所有 run）
  --last N → slice(-N)
  --json   → JSONL 输出
  else     → 格式化打印（时间戳 | 角色 | 内容前500字）
```

历史文件路径：`~/.local/share/ccc/history/<name>/<timestamp>.jsonl`
每次 `send` 调用时由 `ConversationLogger` 自动追加写入。

---

### 独立模式命令（不依赖 tmux）

#### `ccc stream <prompt> [--cwd] [--tools] [--model] [--timeout] [--raw]`

```
claude -p --output-format stream-json --verbose [--model] [--allowedTools]
  → stdin: prompt
  → 解析 stream-json 事件流：
      content_block_delta.text_delta → 累积文字
      assistant.content[].text       → 累积文字（备用格式）
      result.result                  → 最终结果
  → print 累积内容
  → stderr: session_id, cost
```

完全绕过 tmux，适合一次性查询。删除 `CLAUDECODE` 环境变量防止嵌套调用被拒。

#### `ccc relay debate <topic> [--role-a] [--role-b] [--rounds N] [--model]`

#### `ccc relay collab <task>  [--developer] [--reviewer] [--rounds N] [--model]`

```
两种模式均调用 runRelay → 多轮 claudeOneShot（claude -p --output-format json）

debate：A 先发言，B 反驳，交替进行 N 轮
collab：A(Developer) 实现，B(Reviewer) 审查，
        B 输出含 "LGTM"/"approved" 等关键词 → 提前终止

claudeOneShot = spawn("claude", ["-p", "--output-format", "json"])
  → stdin: prompt → 解析 JSON 输出 → 返回 { content, costUsd }
```

同样删除 `CLAUDECODE` 环境变量。每轮将对方上一轮输出拼入 prompt 上下文。

---

## 状态说明


| state      | 帧间变化     | 触发条件                                    |
| ---------- | -------- | --------------------------------------- |
| `ready`    | 稳定       | 空 idle ❯，无文字，无菜单                        |
| `thinking` | 变化（非输入行） | spinner / token 数增加 / 响应文字累积            |
| `typing`   | 仅 ❯ 行变化  | ❯ 行文字在增加，其余内容不变                         |
| `composed` | 稳定       | ❯ 行有文字，未提交 Enter                        |
| `approval` | 稳定       | 出现 Allow once/always/Deny 或数字确认菜单       |
| `choosing` | 稳定       | 出现 ❯ 选中项 + 缩进选项列表                       |
| `unknown`  | 稳定       | pane 静止但无法识别当前界面（启动中、未知 UI、Ctrl-C 后等） |
| `dead`     | —        | tmux session 不存在                        |


## 状态机总览

```
                         ┌──────────┐
                         │   dead   │  isAlive() = false
                         └──────────┘
                               ↑
                   kill / 进程崩溃

  ccc run
     ↓
┌──────────┐  send msg   ┌────────────┐
│  ready   │ ──────────► │  thinking  │  spinner/token/响应文字变化
│ (idle ❯) │             └────────────┘
└──────────┘                   │
      ↑   ↑        waitReady   │
      │   │                    │ 出现权限/选择菜单
      │   │    approve/        ▼
      │   │    选择后  ┌──────────────┐
      │   └─────────── │  approval /  │  pane 稳定
      │                │  choosing    │
      │                └──────────────┘
      │
      │  ccc input / send --no-wait
      │         ↓
      │   ┌──────────┐
      │   │  typing  │  ❯ 行文字变化，其余稳定
      │   └──────────┘
      │         │ 停止输入（文字停止变化）
      │         ▼
      │   ┌──────────┐
      │   │ composed │  ❯ 行有文字，稳定，未 Enter
      │   └──────────┘
      │         │  Enter / 清空
      └─────────┘

  ccc run（启动中）/ Ctrl-C 后 / 未知 UI
         ↓
   ┌──────────┐
   │ unknown  │  pane 稳定但无 idle ❯
   └──────────┘
```

