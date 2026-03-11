# Orchestrator TUI Demo — Design Document

## 概述

`demos/orchestrator.py` — 一个轻量 TUI 工具，让用户通过可视化界面与"老板"对话，老板可以创建并指挥多个子 agent（Claude Code / Cursor tmux 会话），用户也可以直接与子 agent 沟通。整个系统基于 `ccc` 工具构建。

---

## 核心原则

1. **老板只负责协调**，不直接写代码。它的工作是：拆解任务、分配给子 agent、汇总进度。
2. **TUI 是消息总线**，负责：解析老板指令、执行 `ccc` 操作、把子 agent 状态回流给老板。
3. **用户与任何 agent 的直接沟通，老板都知道**（TUI 自动转发通知）。
4. **单文件实现**，`~350行 Python`，依赖只需 `textual` + `ccc`。
5. **子 agent 的文件读写限制在沙箱目录内**，防止误操作影响其他项目。

---

## Agent 命名规则

子 agent 使用**有意义的人名**，而非 `agent-1`、`agent-2` 这类机械编号。

**名称由老板在 `[CREATE]` 指令中指定**，老板会根据任务性质或自己的偏好起名。名字可以是：
- 中文名：`小红`、`李华`、`阿强`
- 英文名：`MUSK`、`Alan`、`Grace`
- 绰号：`后端王`、`测试狗`

**好处**：
- 对话中更自然（"小红完成了登录模块" vs "agent-1完成了..."）
- 多 agent 并行时更容易区分
- 老板可以给不同性格/专长的 agent 起符合其角色的名字

**命名约束**：名称作为 tmux session 名，只能包含字母、数字、中文、连字符，不能含空格或特殊字符。TUI 在执行 `[CREATE]` 时需校验，非法名称拒绝并通知老板重新命名。

---

## TUI 布局

```
┌──────────────────────────────────────────────────────────────────┐
│ Orchestrator   [老板 ●]  [小红 ●]  [MUSK ○]                     │  ← Tab 栏
├──────────────┬───────────────────────────────────────────────────┤
│              │  [老板 视图]                                      │
│  Sessions    │  user   > 帮我重构认证模块，加上OAuth支持          │
│  ─────────── │  老板   > 好的，我派小红做OAuth，MUSK重构旧代码    │
│  ● 老板      │         > [CREATE]                                │
│  ● 小红      │         > name: 小红                              │
│  ○ MUSK      │         > task: 实现OAuth登录                     │
│              │         > [/CREATE]                               │
│              │  system > 小红 已创建                             │
│              │  system > MUSK 已创建                             │
│              │  老板   > 两人已就绪，开始监控进度...              │
│              │                                                   │
│              ├───────────────────────────────────────────────────┤
│              │  > _                             [ESC 取消焦点]   │  ← 输入框
└──────────────┴───────────────────────────────────────────────────┘
```

### 按键说明

| 按键 | 作用 |
|------|------|
| `Tab` / `Shift+Tab` | 在 Tab 栏中依次切换到下一个 / 上一个 session |
| `1` `2` `3` … | 直接跳到第 N 个 session（老板=1，之后按创建顺序） |
| `↑` `↓` | 在左侧 Sessions 列表中移动光标，`Enter` 确认切换 |
| `i` 或 `/` | 聚焦输入框（类 vim 风格，随时开始输入） |
| `ESC` | 取消输入框焦点，回到导航模式 |
| `PgUp` / `PgDn` | 滚动当前对话区 |
| `q` 或 `Ctrl+C` | 退出（tmux 会话保留） |

> **焦点逻辑**：默认启动时输入框已聚焦，直接输入即可发送消息给当前 session。按 `ESC` 进入导航模式后，数字键和方向键才生效。

> **Tab 冲突处理**：Textual 框架默认用 `Tab` 切换组件焦点。实现时需覆盖此行为：
> ```python
> def on_key(self, event: Key) -> None:
>     if event.key == "tab" and not self.query_one(Input).has_focus:
>         # 导航模式：Tab 切换 session
>         self.switch_to_next_session()
>         event.prevent_default()
>     # 否则 Textual 默认处理（输入框聚焦时 Tab 正常工作）
> ```

切换到子 agent 视图时，对话显示三种来源：

```
┌──────────────────────────────────────────────────────────────────┐
│ Orchestrator   [老板 ●]  [小红 ●]  [MUSK ○]                     │
├──────────────┬───────────────────────────────────────────────────┤
│              │  [小红]                                           │
│  Sessions    │  老板   > 实现OAuth登录，使用Google和GitHub        │
│  ─────────── │  小红   > 好的，我先安装依赖...                   │
│  ● 老板      │  小红   > (thinking...)                           │
│  ● 小红  ◀   │  user   > 记得加refresh token支持                 │
│  ○ MUSK      │  小红   > 收到，正在添加refresh token逻辑...      │
│              │                                                   │
│              ├───────────────────────────────────────────────────┤
│              │  > _                                              │
└──────────────┴───────────────────────────────────────────────────┘
```

> 左侧列表中 `◀` 标记表示当前选中的 session。状态颜色：
> - `●` 绿色 = `ready`
> - `●` 黄色 = `thinking` / `generating`
> - `●` 橙色 = `approval`（高亮横幅）
> - `○` 灰色 = `dead` / 未知

---

## 消息类型与来源标签

每条消息有明确的来源标签：

| 标签 | 颜色 | 含义 |
|------|------|------|
| `user` | 绿色 | 用户直接输入 |
| `老板` | 蓝色 | 老板输出 |
| `<agent名称>` | 黄色 | 子 agent 输出（显示 agent 自己的名字，如 `小红`、`MUSK`）|
| `system` | 灰色 | TUI 系统事件（创建、销毁、超时、错误等）|

---

## 老板指令协议

给老板的 system prompt 中约定以下标记语法，TUI 解析执行。

> **格式常量**（代码中定义为常量，与 system prompt 保持一致）：
> ```python
> TAG_CREATE  = "CREATE"
> TAG_SEND    = "SEND"
> TAG_STATUS  = "STATUS"
> TAG_KILL    = "KILL"
> NOTIFY_UPDATE   = "[AGENT_UPDATE name={name}]"
> NOTIFY_USER_MSG = "[USER_TO_AGENT name={name}]"
> ```

### 创建子 agent

多行格式，避免 task 内容含引号时解析失败：

```
[CREATE]
name: 小红
backend: claude
workdir: /tmp/sandbox/小红
task: 实现OAuth登录模块，支持Google和GitHub
[/CREATE]

[CREATE]
name: MUSK
backend: cursor
workdir: /tmp/sandbox/MUSK
task: 重构现有auth代码，整理模块结构
[/CREATE]
```

`workdir` 字段可选，省略时使用全局 `SANDBOX_DIR/<name>` 作为默认值（TUI 自动创建该目录）。

TUI 执行：
- `backend: claude` → `ccc run 小红 --cwd <workdir>`（默认）
- `backend: cursor` → `ccc run 小红 --cursor --cwd <workdir>`

`--cwd` 的作用：Claude Code 启动时将工作目录设为 `workdir`，默认的文件读写权限边界即为该目录（访问目录外的路径会触发权限提示）。这是 ccc 原生支持的轻量沙箱机制，无需额外配置。

创建后 TUI 执行 `ccc wait 小红 ready`（阻塞在后台线程），就绪后再通过 `ccc send 小红 <task> --no-wait` 发送首条任务消息。

**session 名冲突处理**：TUI 在执行前先用 `ccc ps --json` 解析现有会话列表，若同名 session 已存在，通过 system 消息告知老板，老板需改名后重新 `[CREATE]`。

### 向子 agent 发消息

```
[SEND]
name: 小红
msg: 记得处理token过期的edge case，同时加上refresh token轮换策略
[/SEND]
```

TUI 执行：`ccc send 小红 "<msg>" --no-wait`

### 请求子 agent 状态

```
[STATUS name=小红]
[STATUS all]
```

TUI 执行：
- `[STATUS name=小红]` → `ccc read 小红 --json`，把结果注入回老板
- `[STATUS all]` → 依次查询所有子 agent，汇总后一次性注入老板

### 终止子 agent

```
[KILL name=MUSK]
```

TUI 执行：`ccc kill MUSK`，从 sidebar 移除，停止轮询。

---

## 消息流转逻辑

### 用户 → 老板 / 子 agent

所有 `ccc send` 调用均使用 `--no-wait`，由轮询机制检测响应：

```python
async def send_user_message(session_name: str, text: str):
    add_message(session_name, source="user", text=text)
    await asyncio.to_thread(run_ccc, "send", session_name, text, "--no-wait")

    # 如果是子 agent，顺带通知老板
    if session_name != BOSS_NAME:
        await asyncio.to_thread(
            run_ccc, "send", BOSS_NAME,
            f"[USER_TO_AGENT name={session_name}]\n{text}",
            "--no-wait"
        )
```

### 老板输出解析

```
轮询 ccc last 老板 → 检测新响应 →
  正则扫描 [CREATE/SEND/STATUS/KILL] 标记 → 执行对应 ccc 命令
  普通文本 → 追加到老板对话视图
```

### 子 Agent 状态回流（节流）

```
后台轮询每个子 agent（1秒间隔）→ 检测 ready 状态变化 →
  追加完整响应到该 agent 的对话视图
  节流：仅在 agent 状态从 thinking/generating → ready 时才通知老板（避免刷屏）
    通知内容（截断至500字，使用 --no-wait）:
    "[AGENT_UPDATE name=小红]\n<最新响应摘要>"
```

> **为什么只在 ready 时通知**：子 agent thinking/generating 期间输出是流式的、不完整的，转为 ready 才是一次完整回复，此时通知老板才有意义。

### 用户直接与子 agent 沟通

```
用户在 小红 视图输入 → ccc send 小红 "<message>" --no-wait
同时通知老板: "[USER_TO_AGENT name=小红]\n<message>" --no-wait
```

### Approval 处理

```
轮询检测到子 agent 状态 = "approval" →
  在该 agent 视图顶部显示高亮横幅: "⚠ 小红 请求工具权限: <tool名称>"
  用户可按 y 一键批准（ccc approve 小红 yes，默认参数即为 yes）
  或按 n 拒绝（ccc approve 小红 no）
  超时策略：approval 状态持续 60 秒无用户操作 →
    自动执行 ccc approve 小红 no
    显示 system 消息："小红 工具权限请求超时，已自动拒绝"
```

### 往已死亡的 agent 发消息

若用户在 dead 状态的 session 视图中输入，TUI 不执行 `ccc send`，而是显示 system 消息：
"`<name>` 已死亡，消息未发送。可让老板重新 `[CREATE]` 该 agent。"

---

## 老板的 System Prompt

启动老板后，等待 `ccc wait 老板 ready` 就绪，再通过 `ccc send --no-wait` 发送初始化指令：

```
你是一个 AI 工程团队的老板。你的职责是协调多个子 agent 完成复杂任务。
你不直接写代码，而是将任务分解并分配给子 agent。

可用指令（每个指令独占多行，放在回复末尾）：

[CREATE]
name: <名称>
backend: claude|cursor
workdir: <工作目录路径，可选，省略则使用默认沙箱>
task: <任务描述，可以是任意文本，无需引号>
[/CREATE]

[SEND]
name: <名称>
msg: <消息内容，可以是任意文本>
[/SEND]

[STATUS name=<名称>|all]

[KILL name=<名称>]

名称规则：只能用字母、数字、中文、连字符，不能有空格。

重要约束：
- 每个子 agent 只能在其分配的工作目录内读写文件，不要让 agent 操作工作目录以外的路径
- 如需访问共享资源，应在任务描述中明确指定路径，并确认该路径在 agent 的 workdir 范围内

规则：
- 收到用户任务后，先分析并制定计划，再创建 agent
- 收到 [AGENT_UPDATE] 时，评估进度，决定是否需要补充指令或介入
- 收到 [USER_TO_AGENT] 时，了解情况但不必重复用户已说的话
- 若 [AGENT_UPDATE] 显示 agent 报错或长时间无进展，评估是否 [KILL] 后重建
- 若收到 system 通知某 agent 已死亡，评估是否需要重新分配其任务给新 agent
- 保持回复简洁，行动导向
```

---

## 状态管理

### 状态机

```
starting → ready
         → thinking → generating → ready
                    → approval   → ready（批准后）
                                 → ready（拒绝后）
         → dead
```

`generating` 状态表示 agent 当前有输出活跃变化中（output 仍在流式写入），区别于 `thinking`（等待 Claude 思考，尚无输出）。

### 内存数据结构

```python
@dataclass
class AgentState:
    name: str                    # session 名称（也是 tmux session 名）
    backend: str                 # "claude" | "cursor"
    task: str                    # 分配的任务描述
    workdir: str                 # 工作目录（沙箱路径，ccc run --cwd 使用）
    status: str                  # "starting"|"thinking"|"generating"|"ready"|"approval"|"dead"
    last_response: str           # 上一次 ready 时的完整响应（字符串，用于变化检测）
    last_seen: float             # 最后活跃时间戳
    approval_since: float        # approval 状态开始时间戳（0 表示未在 approval 中）

@dataclass
class Message:
    source: str                  # "user" | "老板" | "<agent名称>" | "system"
    text: str
    timestamp: float

# 全局状态
BOSS_NAME = "老板"
sessions: dict[str, AgentState]          # name -> AgentState（BOSS_NAME 是特殊 key）
conversations: dict[str, list[Message]]  # session_name -> messages
```

### 会话视图映射

- `conversations["老板"]` — 老板视图显示的消息（user + 老板 + system）
- `conversations["小红"]` — 小红视图显示的消息（老板指令 + 小红回复 + user直接消息）

---

## 轮询机制

每个 session 一个 `asyncio` task，间隔 1 秒轮询。**所有 subprocess 调用必须用 `asyncio.to_thread` 包裹**，避免阻塞事件循环：

```python
async def poll_session(name: str):
    while name in sessions:
        try:
            # subprocess 必须用 to_thread 包裹，避免阻塞 Textual 事件循环
            result = await asyncio.to_thread(run_ccc, "read", name, "--json")
            state = json.loads(result)
            new_status = state["state"]  # "ready"|"thinking"|"generating"|"approval"|"choosing"|"dead"
            sessions[name].status = new_status

            if new_status == "ready":
                # ccc last 返回字符串（自动含 scrollback），无需 --full
                response = await asyncio.to_thread(run_ccc, "last", name)
                if response and response != sessions[name].last_response:
                    sessions[name].last_response = response
                    sessions[name].approval_since = 0
                    add_message(name, source=name, text=response)

                    if name != BOSS_NAME:
                        # --no-wait：不阻塞，靠下一轮轮询检测老板响应
                        await asyncio.to_thread(
                            run_ccc, "send", BOSS_NAME,
                            f"[AGENT_UPDATE name={name}]\n{response[:500]}",
                            "--no-wait"
                        )
                    else:
                        parse_and_execute_commands(response)

            elif new_status == "approval":
                perm = state.get("permission", {})
                show_approval_banner(name, perm)
                # 超时自动拒绝
                if sessions[name].approval_since == 0:
                    sessions[name].approval_since = time.time()
                elif time.time() - sessions[name].approval_since > 60:
                    await asyncio.to_thread(run_ccc, "approve", name, "no")
                    add_message(name, source="system",
                                text=f"{name} 工具权限请求超时，已自动拒绝")
                    sessions[name].approval_since = 0

        except Exception as e:
            sessions[name].status = "dead"
            add_message(name, source="system", text=f"{name} 无响应: {e}")

        await asyncio.sleep(1)
```

> **为什么用 `ccc last` 而非 diff `state["lines"]`**：`ccc read --json` 返回的 `lines` 是列表（当前可见 pane 快照），直接比较两次快照难以可靠提取"新增内容"。`ccc last` 专门提取上一次完整响应（字符串），语义更清晰，变化检测更稳定。

> **`ccc wait --json` 输出格式**（供参考，`ccc wait` 内部使用）：
> ```json
> {"reached": true, "state": "ready", "elapsed": 3.2}
> ```

---

## 文件结构

```
demos/                  ← 新建目录，放 TUI orchestrator
└── orchestrator.py     # 全部逻辑，单文件，~350行

demo/                   ← 已有目录，FastAPI web UI，不动
├── server.py
└── ...
```

> `demos/`（复数）是新目录，与已有的 `demo/`（单数，FastAPI web UI）区分开来。

### 依赖

```
textual>=0.50.0         # TUI 框架
ccc                     # 本项目，必须已安装并在 PATH 中
```

安装：
```bash
pip install textual
python demos/orchestrator.py
```

---

## 沙箱与权限限制

### 机制

两层防护叠加：

1. **`--cwd <workdir>`**：`ccc run` 将 Claude Code 的工作目录设为 `workdir`，Claude Code 的权限提示系统以此为默认边界。
2. **`PreToolUse` hook**：在 `workdir/.claude/settings.json` 注入一个 hook 脚本，Claude 每次调用文件写入工具前，hook 先检查目标路径是否在 `workdir` 内。路径越界时返回 `permissionDecision: "deny"`，Claude Code 直接阻止该工具调用并把原因反馈给 Claude。

这是 **Claude Code 应用层的拦截**，不是 OS 沙箱，但对意外越界写入有实际阻止效果。

### Hook 工作原理

Claude Code 在执行工具前将参数 JSON 通过 stdin 传给 hook 脚本：

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Write",
  "tool_input": { "file_path": "/abs/path/to/file.txt", "content": "..." },
  "cwd": "/sandbox/小红"
}
```

Hook 脚本检查 `tool_input.file_path`，若在沙箱外则 exit 0 + stdout JSON 拒绝：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "路径在沙箱目录之外，操作已阻止"
  }
}
```

### 配置

`orchestrator.py` 顶部声明全局沙箱根目录：

```python
SANDBOX_DIR = os.path.expanduser("~/orchestrator-sandbox")
```

创建 agent 时，`resolve_workdir()` 建目录并注入 hook：

```python
def resolve_workdir(name: str, explicit_workdir: str | None) -> str:
    workdir = explicit_workdir or os.path.join(SANDBOX_DIR, name)
    os.makedirs(workdir, exist_ok=True)
    inject_sandbox_hook(workdir)
    return workdir

def inject_sandbox_hook(workdir: str) -> None:
    """在 workdir/.claude/ 写入沙箱 hook 配置和检查脚本。"""
    dot_claude = os.path.join(workdir, ".claude")
    os.makedirs(dot_claude, exist_ok=True)

    # hook 检查脚本
    hook_script = os.path.join(dot_claude, "sandbox-check.py")
    with open(hook_script, "w") as f:
        f.write(f"""\
#!/usr/bin/env python3
import json, os, sys

data = json.load(sys.stdin)
file_path = data.get("tool_input", {{}}).get("file_path", "")

if file_path:
    sandbox = os.path.realpath("{workdir}")
    target  = os.path.realpath(os.path.abspath(file_path))
    if not target.startswith(sandbox + os.sep) and target != sandbox:
        print(json.dumps({{
            "hookSpecificOutput": {{
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"路径 {{file_path}} 在沙箱目录之外，操作已阻止"
            }}
        }}))
        sys.exit(0)

sys.exit(0)
""")
    os.chmod(hook_script, 0o755)

    # settings.json：注册 hook，拦截所有文件写入工具
    settings_path = os.path.join(dot_claude, "settings.json")
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": hook_script}]
                }
            ]
        }
    }
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
```

调用：

```python
workdir = resolve_workdir(name, create_cmd.get("workdir"))
run_ccc("run", name, "--cwd", workdir, *(["--cursor"] if backend == "cursor" else []))
```

### 局限性

- **Bash 工具不拦截**：`Bash` 的 `tool_input.command` 是任意 shell 命令，静态解析容易漏判，因此 hook 只覆盖明确的文件写入工具（`Write`/`Edit`/`MultiEdit`/`NotebookEdit`）。通过 Bash 的重定向（`echo x > /outside/file`）仍可绕过。
- **不是 OS 沙箱**：应用层拦截，Claude Code 本身或用户点击 Allow 后可覆盖。
- 如需更强隔离，需配合 OS 方案（macOS `sandbox-exec` / Linux `firejail` / 容器），超出本 demo 范围。

---

## 实现阶段

### Phase 1 — TUI 骨架（~80行）
- Textual App：sidebar + chat area + input box
- Tab 切换逻辑（`on_key` 覆盖 Textual 默认 Tab 行为）
- 静态 mock 数据验证布局

### Phase 2 — 老板会话（~60行）
- `ccc run 老板` 启动老板
- `ccc wait 老板 ready` 等待就绪（在后台线程中）
- 发送 system prompt（`--no-wait`）
- 轮询输出，显示在老板视图

### Phase 3 — 指令解析与子 agent 管理（~80行）
- 多行正则解析 `[CREATE]` `[SEND]` `[STATUS]` `[KILL]`
- 名称校验 + 用 `ccc ps --json` 检测 session 冲突
- backend 参数映射（`claude` vs `cursor --cursor`）
- 创建子 agent 后自动添加到 sidebar 并启动轮询

### Phase 4 — 状态回流与三方视图（~60行）
- 子 agent ready 时才通知老板（节流，截断500字，`--no-wait`）
- 用户直接输入子 agent 时同步通知老板（`--no-wait`）
- agent 视图显示三种来源消息

### Phase 5 — 打磨（~70行）
- Approval 横幅 + `y`/`n` 快捷键 + 60 秒超时自动拒绝
- agent 状态颜色指示（含 `generating` 状态）
- 死亡 session 检测与 system 提示，阻止往死亡 agent 发消息
- `Ctrl+C` 优雅退出，保留 tmux 会话

---

## 已知限制与 TODO

- **不持久化**：重启 orchestrator 后对话历史清空（可后续接 `ccc history`）
- **老板指令解析是单向的**：老板必须遵守标记格式；未来可改为工具调用风格
- **轮询延迟**：最多 1 秒延迟，对实时性要求不高的场景够用
- **单用户**：没有多用户支持，当前是本地单机工具
- **死亡恢复**：子 agent 死掉后目前只显示提示，不自动重启（可手动让老板重新 `[CREATE]`）

---

## 使用示例

```bash
# 启动
python demos/orchestrator.py

# TUI 内操作：
# 1. 在老板视图输入任务（启动时已自动聚焦输入框）
> 帮我实现一个用户认证系统，包括OAuth和JWT

# 2. 老板自动拆解并创建子 agent，名字由老板自己取
# Sidebar 出现: ● 小红  ● MUSK

# 3. 按 Tab 或数字键 2 切换到 小红 查看工作进度
# 4. 按 i 聚焦输入框，直接在 小红 视图发消息给她
# 5. 老板自动收到 [USER_TO_AGENT name=小红] 通知

# 6. 若小红请求工具权限，顶部出现高亮横幅
# ⚠ 小红 请求权限: Bash(npm install)   [y 批准 / n 拒绝 / 60秒后自动拒绝]

# 退出：ESC → q，或 Ctrl+C（tmux 会话保留）
```
