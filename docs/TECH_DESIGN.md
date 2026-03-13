# Technical Design Document
## claude-cli-connector

**版本**: v0.2
**更新日期**: 2026-03-06
**状态**: Draft
**关联文档**: [PRD.md](./PRD.md)

---

## 1. 架构总览

### 1.1 设计原则

- **tmux-first**: Claude CLI 永远运行在 tmux pane 里，Python 只管理 tmux，不直接接 PTY。这使得 session 的生命周期与 Python 进程解耦——Python 崩溃后 tmux session 依然存活。
- **分层清晰**: 每一层只做一件事，上层不绕过下层直接操作。
- **fail-loud**: 任何异常用明确的自定义异常类型抛出，不静默吞掉错误。
- **可测试**: transport 层用 libtmux 抽象，可 mock；parser 层是纯函数，无副作用，直接单测。
- **agent-friendly**: CLI 提供 `read`/`wait`/`input`/`key`/`approve` 等低层原语，便于外部 agent 框架驱动。

### 1.2 层次结构

```
┌──────────────────────────────────────────────────────────────┐
│            调用方（Python 脚本 / Agent 框架 / ccc CLI）        │
└───────────────────────────┬──────────────────────────────────┘
                            │  Python API / CLI
              ┌─────────────┴──────────────┐
              │                            │
┌─────────────▼──────────┐   ┌────────────▼───────────┐
│    ClaudeSession        │   │   SessionManager        │
│  (session.py)           │   │   (manager.py)          │
│  单会话封装              │   │  多会话编排              │
└─────────────┬───────────┘   └────────────┬───────────┘
              │                            │
    ┌─────────┴──────────────────────────┐ │
    │                                    │ │
┌───▼──────────────┐  ┌───────────────┐  └─┘
│  TmuxTransport   │  │  SessionStore  │
│  (transport.py)  │  │  (store.py)    │
│  libtmux 封装    │  │  JSON 持久化   │
└───┬──────────────┘  └───────────────┘
    │
┌───▼──────────────┐
│  parser.py        │
│  纯函数工具集     │
│  · ANSI strip     │
│  · ready detect   │
│  · choice detect  │
│  · permission det │
│  · model picker   │
│  · pane state     │
└───┬──────────────┘
    │  tmux send-keys / capture-pane
┌───▼──────────────────────────────────┐
│  tmux session: ccc-<name>            │
│  └─ pane 0: claude / cursor-agent    │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│  RelayOrchestrator (relay.py)        │
│  两个 Claude 实例的交互编排           │
│  · debate 模式: 辩论                 │
│  · collab 模式: 开发+评审迭代         │
└──────────────────────────────────────┘
```

### 1.3 demo 层定位

`demo/server.py` 是一个独立的 FastAPI 应用，用于端到端验证本包行为，**不是本包的一部分**。它通过相同的 Python API 调用本包，与任何普通脚本没有区别。

```
Browser ──SSE──► demo/server.py ──Python API──► claude-cli-connector
                 （仅开发验证用）
```

---

## 2. 模块设计

### 2.1 `exceptions.py`

所有自定义异常的根节点，调用方应 catch 具体子类而不是基类。

```
ConnectorError（基类）
├── SessionNotFoundError   session 在 store 或 tmux 中不存在
├── SessionAlreadyExistsError  创建时 name 冲突
├── SessionTimeoutError    wait_ready() 超时
└── TransportError         libtmux 底层操作失败
```

---

### 2.2 `transport.py` — TmuxTransport

**职责**: 封装所有 libtmux 调用，对上层暴露类型明确的接口，屏蔽 libtmux 版本差异。

**核心数据结构**:

```python
@dataclass
class PaneSnapshot:
    lines: list[str]      # capture-pane 返回的原始行（含 ANSI）
    timestamp: float      # monotonic time，用于 stability check
```

**关键方法**:

| 方法 | 说明 | 异常 |
|---|---|---|
| `TmuxTransport.create(name, cwd, command)` | 创建 tmux session，启动 claude | `TransportError` if session exists / command not found |
| `TmuxTransport.attach(name)` | attach 已有 session | `TransportError` if not found |
| `send_keys(text, enter)` | tmux send-keys | `TransportError` |
| `send_ctrl(key)` | 发送控制键，如 `C-c` | `TransportError` |
| `capture()` | capture-pane（可见区域），返回 `PaneSnapshot` | `TransportError` |
| `capture_full(scrollback)` | capture-pane + 滚动缓冲区（默认 5000 行） | `TransportError` |
| `is_alive()` | 检查 tmux session 是否存在 | 不抛出 |
| `kill()` | kill-session（兼容 libtmux ≥0.30 的 `kill()` 和旧版 `kill_session()`） | `TransportError` |
| `resize(width, height)` | 调整 pane 尺寸 | `TransportError` |

**tmux session 命名**: 统一加 `ccc-` 前缀（`ccc-<name>`），避免与用户自己的 session 冲突。

**pane 尺寸**: 默认 220×50。宽度 220 是为了避免 CJK 字符（占 2 列）在 80 列终端里换行导致输出乱序。

**scrollback 支持**: `capture_full()` 使用 `pane.cmd("capture-pane", "-p", "-S", "-N")` 直接调用 tmux 命令获取滚动缓冲区，解决了 `capture_pane()` 只能获取可见区域的限制。

---

### 2.3 `parser.py` — 纯函数工具集

**职责**: 所有输出解析逻辑。无副作用，不持有状态，全部是接受 `list[str]` 返回结果的纯函数，天然易测。

#### 2.3.1 ANSI 清洗

```python
def strip_ansi(text: str) -> str: ...
def strip_ansi_lines(lines: list[str]) -> list[str]: ...
```

处理：CSI 序列（颜色、光标移动）、OSC 序列（窗口标题）、裸 CR（`\r` 非 CRLF）。

#### 2.3.2 ready detection

```python
@dataclass
class ReadinessResult:
    is_ready: bool
    confidence: Literal["prompt", "stability", "busy"]
    snapshot_text: str
    elapsed: float

def detect_ready(
    lines: list[str],
    prev_lines: list[str] | None = None,
    elapsed: float = 0.0,
    min_stable_secs: float = 0.4,
) -> ReadinessResult: ...
```

**三层判断策略**（按优先级）:

```
① BUSY 检测（高置信）
   · spinner 字符: ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ●
   · 关键词: "Thinking...", "Generating", "Working..."
   · Claude CLI hint: "ESC to interrupt"
   → 命中则立即返回 is_ready=False

② READY 检测（高置信）
   · prompt pattern: "^\s*[╰>─]+\s*>?\s*$", "^\s*>\s*$", "Human:"
   · 在 pane 尾部 6 行内命中
   → 命中则立即返回 is_ready=True, confidence="prompt"

③ Stability 检测（中置信，fallback）
   · 当前 lines == prev_lines（内容无变化）
   · elapsed ≥ min_stable_secs
   → 满足则返回 is_ready=True, confidence="stability"

以上均未命中 → is_ready=False，继续 polling
```

#### 2.3.3 choice menu 检测

```python
@dataclass
class ChoiceItem:
    key: str           # 发给 claude 的响应键，如 "1", "2"
    label: str         # 显示文本
    selected: bool     # 当前光标是否在此项（箭头样式）

def detect_choices(lines: list[str]) -> list[ChoiceItem] | None: ...
```

识别两种格式：

```
# 数字列表（send key "1"/"2"/"3"）
1. claude-opus-4-5
2. claude-sonnet-4-5
3. claude-haiku-4-5

# 箭头光标（send Up/Down + Enter，或直接 send 数字）
❯ claude-sonnet-4-5
  claude-opus-4-5
  claude-haiku-4-5
```

返回 `None` 表示当前没有 choice menu。

#### 2.3.4 Permission 检测

```python
@dataclass
class PermissionPrompt:
    tool: str              # 工具名（如 "Bash", "Read"）
    action: str            # 动作描述（如 "Allow Bash?", "Do you want to proceed?"）
    options: list[ChoiceItem]  # 可选项

def detect_permission(
    lines: list[str],
    backend: str = "",
) -> PermissionPrompt | None: ...
```

识别两种 Claude Code CLI 的权限提示格式：

**Format A**（Allow once/always/Deny）:
```
⏺ Claude wants to run: Bash(date)
  Allow Bash?
❯ Allow once
  Allow always
  Deny
```

**Format B**（Do you want to proceed? + 数字列表）:
```
Bash command
   find /path -maxdepth 1 -type d | wc -l
Do you want to proceed?
❯ 1. Yes
  2. Yes, allow reading from repos/ from this project
  3. No
Esc to cancel · Tab to amend · ctrl+e to explain
```

返回 `None` 表示当前没有权限提示。

#### 2.3.5 Model Picker 检测

```python
def detect_model_picker(
    lines: list[str],
    backend: str = "",
) -> list[ChoiceItem] | None: ...
```

识别 `/model` 命令触发的模型选择列表。Claude 和 Cursor 有不同的 UI 格式。

#### 2.3.6 Pane State 综合检测（heartbeat 模式）

```python
@dataclass
class PaneState:
    state: str              # "ready" | "thinking" | "generating" | "approval" | "choosing"
    confidence: str
    permission: PermissionPrompt | None
    choices: list[ChoiceItem] | None
    activity: ActivityInfo | None

def detect_pane_state(
    capture_fn: Callable[[], list[str]],
    backend: str = "",
    activity_samples: int = 5,
    activity_interval: float = 0.2,
) -> PaneState: ...
```

`detect_pane_state()` 通过多次快照（heartbeat）区分 "thinking"（无输出）和 "generating"（输出正在流式生成），比单次快照的 pattern matching 更可靠。

#### 2.3.7 其他工具函数

```python
def diff_output(before: list[str], after: list[str]) -> list[str]: ...
# 返回 after 中相对 before 新增的行（suffix diff，用于增量输出）

def extract_last_response(lines: list[str], backend: str = "") -> str: ...
# 从全量 pane 内容提取最近一次 Claude 回复（best-effort）
# 支持 backend 参数区分 Claude 和 Cursor 的输出格式
```

---

### 2.4 `store.py` — SessionStore

**职责**: 持久化 session 元数据，使 Python 进程重启后可以 attach 已有 tmux session。

**存储位置**: `~/.local/share/claude-cli-connector/sessions.json`（可通过 `CCC_STORE_PATH` 覆盖）。

**数据模型**:

```python
class SessionRecord(BaseModel):
    name: str                  # 逻辑名称
    tmux_session_name: str     # ccc-<name>
    cwd: str
    command: str = "claude"
    backend: str = "claude"    # "claude" 或 "cursor"
    created_at: float          # unix timestamp
    last_seen_at: float
    extra: dict = {}           # 扩展字段
```

**写入策略**: atomic write（先写 `.tmp` 再 `rename`），避免写一半时进程崩溃导致 JSON 损坏。

---

### 2.5 `session.py` — ClaudeSession

**职责**: 本包的主要公开接口。组合 `TmuxTransport` + `parser` + `SessionStore`，实现高层语义。

**核心方法**:

| 方法 | 行为 |
|---|---|
| `ClaudeSession.create(name, cwd, command, backend)` | 创建 tmux session + 启动 claude/cursor + 写 store |
| `ClaudeSession.attach(name)` | 从 store 读记录 → tmux attach |
| `send(text, enter)` | 非阻塞，直接 `transport.send_keys()` |
| `send_and_wait(text, timeout)` | send → 等待内容变化 → `wait_ready()` → 检查 permission → `extract_last_response()` |
| `wait_ready(timeout)` | polling loop，调用 `detect_ready()`，超时抛 `SessionTimeoutError` |
| `capture()` | `transport.capture()` + ANSI strip，返回全量文本 |
| `tail(lines)` | 同上，只返回最后 N 行 |
| `new_output_since_last_capture()` | diff 上次 capture 游标，返回增量 |
| `detect_choices()` | 检测当前是否有交互选择菜单 |
| `interrupt()` | `send_ctrl('c')` |
| `is_ready()` | 非阻塞，`detect_ready(elapsed=999)` |
| `is_alive()` | `transport.is_alive()` |
| `kill()` | `transport.kill()` + `store.delete()` |

**`send_and_wait()` 内部流程（含 permission 检测）**:

```
capture(before)    # 记录发送前状态
send(text)
  ↓
等待 pane 内容变化   # 避免旧 prompt 误判 ready
  ↓
wait_ready(timeout)
  ↓
detect_permission()  # 检查是否是权限提示而非完成
  ↓ 如果是 permission
  返回 "[PERMISSION_REQUIRED] ..."
  ↓ 如果不是
extract_last_response()  # 提取 Claude 的回复
```

---

### 2.6 `manager.py` — SessionManager

**职责**: 多 session 生命周期管理，提供批量操作接口。

**核心方法**:

| 方法 | 说明 |
|---|---|
| `create(name, cwd, exist_ok)` | 创建并注册 session |
| `attach(name)` | attach 并注册到 in-process cache |
| `get(name)` | 返回已注册的 session |
| `kill(name)` / `kill_all()` | 终止 session |
| `collect_responses(timeout)` | 等待所有 session ready，返回 `{name: text}` |
| `prune_dead()` | 移除已死亡的 session（in-process cache + store） |
| `list_stored_sessions()` | 从 store 读取所有记录（含 offline） |

**in-process cache**: `dict[str, ClaudeSession]`，进程级单例。重启后需重新 attach。

---

### 2.7 `relay.py` — RelayOrchestrator

**职责**: 编排两个 Claude 实例之间的交互，支持两种模式。

**数据模型**:

```python
class RelayMode(Enum):
    DEBATE = "debate"    # 辩论模式
    COLLAB = "collab"    # 协作模式（开发+评审）

@dataclass
class RelayRole:
    name: str            # 角色名称
    system_prompt: str   # 角色 system prompt
    model: str = ""      # 可选模型

@dataclass
class RelayConfig:
    mode: RelayMode
    role_a: RelayRole
    role_b: RelayRole
    initial_topic: str = ""       # debate 模式的话题
    task_description: str = ""    # collab 模式的任务描述
    max_rounds: int = 5
    round_timeout: float = 300.0
    transport_mode: TransportMode = TransportMode.STREAM_JSON
    cwd: str = "."
    allowed_tools: list[str] = []
    verbose: bool = False

@dataclass
class RelayResult:
    mode: str
    rounds_completed: int
    final_state: str          # "completed" | "max_rounds" | "lgtm"
    transcript: list[RelayTurn]
    total_cost_usd: float
    start_time: float
    end_time: float
    history_path: str
```

**debate 流程**:
1. 给 A 发送话题 → 收到 A 的观点
2. 将 A 的观点作为上下文发给 B → 收到 B 的反驳
3. 将 B 的反驳发给 A → 收到 A 的回应
4. 重复直到达到 max_rounds

**collab 流程**:
1. 给 Developer 发送任务 → 收到代码
2. 将代码发给 Reviewer → 收到评审意见
3. 如果 Reviewer 说 "LGTM" → 结束
4. 否则将评审意见发回 Developer → 重复

支持 tmux 和 stream-json 两种 transport 模式。

---

### 2.8 `cli.py` — ccc CLI

`ccc` 命令行入口（Typer），对应核心包功能的 shell 级封装：

#### Session 生命周期

| 命令 | 对应 API | 说明 |
|---|---|---|
| `ccc run <name> --cwd DIR [--cursor] [--model M]` | `ClaudeSession.create()` | 支持 Claude 和 Cursor 后端 |
| `ccc attach <name>` | `ClaudeSession.attach()` | |
| `ccc send <name> "message" [--no-wait] [-A]` | `session.send_and_wait()` | `--auto-approve` 自动批准权限 |
| `ccc tail <name> -n 40 [--full]` | `session.tail()` | `--full` 包含滚动缓冲区 |
| `ccc last <name> [--raw]` | `extract_last_response()` | 使用 scrollback 提取 |
| `ccc ps` | `store.list_all()` | 显示 backend 和 alive 状态 |
| `ccc status <name> [--porcelain]` | `detect_ready()` + `detect_permission()` | 支持 approval/choosing 状态 |
| `ccc kill <name>` | `session.kill()` | |
| `ccc clean [--yes] [--dry-run]` | `store.delete()` | 清理已死 session 记录 |
| `ccc interrupt <name>` | `session.interrupt()` | |
| `ccc model <name> [model]` | `detect_model_picker()` | 列出或切换模型 |
| `ccc approve <name> [yes\|always\|no]` | `detect_permission()` | 处理两种权限格式 |

#### Agent 控制原语

| 命令 | 说明 |
|---|---|
| `ccc input <name> <text> [--no-enter]` | 发送任意文本（slash 命令、部分输入等） |
| `ccc key <name> <keys> [--repeat N]` | 发送特殊键（Enter, Escape, Tab, 方向键, C-c 等） |
| `ccc read <name> [--json] [--full] [--heartbeat]` | 结构化读取 pane 状态（state, permission, choices, lines） |
| `ccc wait <name> <state> [--timeout T] [--heartbeat]` | 阻塞等待目标状态（ready, approval, choosing, thinking, generating, any-change） |

**Agent loop 模式**:
```bash
# send → wait → read → decide → act → repeat
ccc send myproject "build the app" --no-wait
ccc wait myproject ready --timeout 300
ccc read myproject --json  # 检查结果 + 检测权限提示
```

#### Relay 子命令

| 命令 | 说明 |
|---|---|
| `ccc relay debate "topic" [--role-a] [--role-b] [-r N]` | 辩论模式 |
| `ccc relay collab "task" [--dev] [--reviewer] [-r N]` | 协作模式 |

#### Stream-json 模式

| 命令 | 说明 |
|---|---|
| `ccc stream "prompt" [--cwd] [--tools] [--model] [--raw]` | 单次查询，无需 tmux |

---

## 3. 关键设计决策

| 决策点 | 选择 | 理由 | 放弃的备选 |
|---|---|---|---|
| 终端控制层 | tmux | 成熟稳定，session 与 Python 进程生命周期解耦 | 直接 `pty.fork()`：需自己处理 PTY 读写、信号、回显，复杂度高 |
| Python tmux 绑定 | libtmux | 类型完善，`Pane.capture_pane()` 开箱即用 | 自己 subprocess tmux 命令：需自己解析输出 |
| ready detection | 三层叠加策略 | 单一策略鲁棒性不够，三层互补 | 仅靠 stability：延迟高；仅靠 prompt：跨版本脆 |
| 会话持久化 | JSON 文件 | 零依赖，atomic write，轻量 | SQLite：overkill；内存：进程重启丢失 |
| 输出协议（demo） | SSE | 单向流，浏览器原生支持，无需 WS 握手 | WebSocket：双向但过重；长轮询：延迟高 |
| 权限处理 | 格式匹配 + 按键模拟 | 两种格式各有不同的交互方式（单键 vs 数字导航） | 始终用 `y`/`n`：不兼容 Format B |
| Relay transport | 默认 stream-json | 单次交互更适合 relay 场景，无需持久 tmux session | 始终用 tmux：重且需要清理 |
| 活动检测 | heartbeat（多次快照） | 可区分 thinking vs generating，单次快照做不到 | 纯 regex：无法检测"输出正在生成" |

---

## 4. 单元测试设计

### 4.1 测试分层

本包的测试分为两层，有明确边界：

```
tests/
├── unit/                  # 单元测试（无需 tmux，CI 中运行）
│   ├── test_parser.py     # 纯函数，直接传 list[str] 测试
│   ├── test_transport.py  # mock libtmux
│   ├── test_session.py    # mock TmuxTransport
│   ├── test_store.py      # 使用 tmp_path fixture
│   └── test_manager.py    # mock ClaudeSession
└── integration/           # 集成测试（需要真实 tmux，本地运行）
    ├── test_real_session.py
    └── conftest.py        # 自动创建/清理 tmux session
```

**原则**:
- 单元测试 **不启动 tmux**，**不调用真实 claude CLI**，全部通过 mock 隔离外部依赖。
- 集成测试标记为 `@pytest.mark.integration`，默认跳过，需要显式 `--run-integration` 才执行。
- CI pipeline 只跑单元测试；集成测试在本地或有 tmux 的环境手动触发。

### 4.2 `test_parser.py` — 纯函数单测

parser 是纯函数，**最容易测，覆盖率最高**，测试直接传字符串列表即可。

**测试要点**:

```python
# ANSI 清洗
def test_strip_colour_codes():
    assert strip_ansi("\x1b[32mGreen\x1b[0m") == "Green"

def test_strip_csi_cursor_move():
    assert strip_ansi("\x1b[2J\x1b[H text") == " text"

def test_preserve_crlf():
    # \r\n 里的 \r 不应被删除
    assert "\r\n" in strip_ansi("line\r\n")

# ready detection
def test_spinner_is_busy():
    assert not detect_ready(["⠋ Thinking..."]).is_ready

def test_prompt_pattern_is_ready():
    result = detect_ready(["Hello!", ">"])
    assert result.is_ready and result.confidence == "prompt"

def test_stability_needs_min_time():
    lines = ["done"]
    # elapsed 不足，不应 ready
    assert not detect_ready(lines, prev_lines=lines, elapsed=0.1, min_stable_secs=0.5).is_ready
    # elapsed 足够，应 ready
    assert detect_ready(lines, prev_lines=lines, elapsed=1.0, min_stable_secs=0.5).is_ready

def test_busy_overrides_stability():
    # 即使内容稳定，只要有 spinner，仍是 busy
    lines = ["⠋"]
    assert not detect_ready(lines, prev_lines=lines, elapsed=5.0).is_ready

# choice menu 检测
def test_numeric_choice_menu():
    lines = ["Which model?", "1. claude-opus-4-5", "2. claude-sonnet-4-5"]
    choices = detect_choices(lines)
    assert choices is not None and len(choices) == 2
    assert choices[0].key == "1"

def test_arrow_choice_menu():
    lines = ["❯ claude-sonnet-4-5", "  claude-opus-4-5"]
    choices = detect_choices(lines)
    assert choices is not None
    assert choices[0].selected is True

def test_no_choice_menu_returns_none():
    assert detect_choices(["Hello, how can I help?"]) is None

# permission 检测
def test_permission_format_a():
    lines = ["⏺ Claude wants to run: Bash(date)", "  Allow Bash?",
             "❯ Allow once", "  Allow always", "  Deny"]
    perm = detect_permission(lines)
    assert perm is not None
    assert perm.tool == "Bash"

def test_permission_format_b():
    lines = ["Bash command", "   date", "Do you want to proceed?",
             "❯ 1. Yes", "  2. No"]
    perm = detect_permission(lines)
    assert perm is not None

# diff_output
def test_diff_output_append():
    assert diff_output(["a", "b"], ["a", "b", "c"]) == ["c"]

def test_diff_output_identical():
    assert diff_output(["a"], ["a"]) == []
```

### 4.3 `test_transport.py` — mock libtmux

目标：覆盖 `TmuxTransport` 的所有公共方法，**不启动真实 tmux**。

**mock 策略**: 用 `pytest-mock` 的 `mocker.patch` 替换 `libtmux.Server`，注入预设返回值。

```python
@pytest.fixture
def mock_server(mocker):
    server = mocker.MagicMock(spec=libtmux.Server)
    server.find_where.return_value = None   # 默认：session 不存在
    return server

@pytest.fixture
def mock_pane(mocker):
    pane = mocker.MagicMock(spec=libtmux.Pane)
    pane.capture_pane.return_value = ["line1", ">"]
    return pane

# 测试 create()
def test_create_raises_if_command_not_found(mock_server, mocker):
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(TransportError, match="not found in PATH"):
        TmuxTransport.create("t", ".", server=mock_server)

def test_create_raises_if_session_exists(mock_server, mocker):
    mock_server.find_where.return_value = MagicMock()  # session 已存在
    mocker.patch("shutil.which", return_value="/usr/bin/claude")
    with pytest.raises(TransportError, match="already exists"):
        TmuxTransport.create("t", ".", server=mock_server)

def test_create_success(mock_server, mock_session, mocker):
    mock_server.new_session.return_value = mock_session
    mocker.patch("shutil.which", return_value="/usr/bin/claude")
    t = TmuxTransport.create("mytest", "/tmp", server=mock_server)
    assert t.logical_name == "mytest"
    assert t.tmux_session_name == "ccc-mytest"

# 测试 capture()
def test_capture_returns_snapshot(mock_server, mock_session, mock_pane):
    mock_server.find_where.return_value = mock_session
    t = TmuxTransport.attach("s", server=mock_server)
    snap = t.capture()
    assert "line1" in snap.lines

def test_capture_raises_transport_error_on_failure(mock_server, mock_session, mock_pane):
    mock_pane.capture_pane.side_effect = RuntimeError("tmux gone")
    mock_server.find_where.return_value = mock_session
    t = TmuxTransport.attach("s", server=mock_server)
    with pytest.raises(TransportError):
        t.capture()

# 测试 is_alive()
def test_is_alive_true(mock_server, mock_session):
    mock_server.find_where.return_value = mock_session
    t = TmuxTransport.attach("s", server=mock_server)
    assert t.is_alive() is True

def test_is_alive_false_after_kill(mock_server, mock_session):
    mock_server.find_where.return_value = mock_session
    t = TmuxTransport.attach("s", server=mock_server)
    mock_server.find_where.return_value = None
    assert t.is_alive() is False
```

### 4.4 `test_session.py` — mock TmuxTransport

目标：测试 `ClaudeSession` 的状态机和流程逻辑，**不依赖 tmux**。

**mock 策略**: 用 `mocker.patch.object` 替换 `TmuxTransport`，控制 `capture()` 和 `send_keys()` 的返回序列。

```python
@pytest.fixture
def mock_transport(mocker):
    t = mocker.MagicMock(spec=TmuxTransport)
    t.logical_name = "test"
    t.tmux_session_name = "ccc-test"
    t.is_alive.return_value = True
    return t

# send_and_wait：正常路径（第二次 capture 到 prompt）
def test_send_and_wait_returns_on_prompt(mock_transport, tmp_path):
    mock_transport.capture.side_effect = [
        PaneSnapshot(["⠋ Thinking..."]),           # 第1次：busy
        PaneSnapshot(["Claude: Hello!", ">"]),     # 第2次：ready
    ]
    session = ClaudeSession(mock_transport, store=SessionStore(tmp_path / "s.json"))
    # 绕过 wait_ready 的 sleep（monkeypatching time.sleep）
    with patch("time.sleep"):
        resp = session.send_and_wait("Hi")
    assert "Hello" in resp

# send_and_wait：超时
def test_send_and_wait_raises_on_timeout(mock_transport, tmp_path):
    mock_transport.capture.return_value = PaneSnapshot(["⠋ Thinking..."])
    session = ClaudeSession(mock_transport, store=SessionStore(tmp_path / "s.json"))
    with patch("time.sleep"), pytest.raises(SessionTimeoutError):
        session.send_and_wait("Hi", timeout=0.01)

# interrupt
def test_interrupt_calls_send_ctrl_c(mock_transport, tmp_path):
    session = ClaudeSession(mock_transport, store=SessionStore(tmp_path / "s.json"))
    session.interrupt()
    mock_transport.send_ctrl.assert_called_once_with("c")

# tail
def test_tail_returns_last_n_lines(mock_transport, tmp_path):
    mock_transport.capture.return_value = PaneSnapshot(
        ["line1", "line2", "line3", "line4", "line5"]
    )
    session = ClaudeSession(mock_transport, store=SessionStore(tmp_path / "s.json"))
    assert session.tail(lines=3) == "line3\nline4\nline5"
```

### 4.5 `test_store.py` — 文件系统单测

目标：测试 JSON 持久化逻辑，使用 pytest 的 `tmp_path` fixture 避免污染用户目录。

```python
def test_save_and_get(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    record = SessionRecord(name="foo", tmux_session_name="ccc-foo", cwd="/tmp")
    store.save(record)
    got = store.get("foo")
    assert got is not None and got.cwd == "/tmp"

def test_get_returns_none_if_not_exists(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("nope") is None

def test_delete(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.save(SessionRecord(name="bar", tmux_session_name="ccc-bar", cwd="."))
    assert store.delete("bar") is True
    assert store.get("bar") is None

def test_list_all(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.save(SessionRecord(name="a", tmux_session_name="ccc-a", cwd="."))
    store.save(SessionRecord(name="b", tmux_session_name="ccc-b", cwd="."))
    assert {r.name for r in store.list_all()} == {"a", "b"}

def test_backend_field_persisted(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.save(SessionRecord(name="c", tmux_session_name="ccc-c", cwd=".", backend="cursor"))
    got = store.get("c")
    assert got.backend == "cursor"

def test_atomic_write_survives_concurrent_read(tmp_path):
    # 写完后文件应是合法 JSON，不应出现半写状态
    store = SessionStore(tmp_path / "sessions.json")
    for i in range(20):
        store.save(SessionRecord(name=f"s{i}", tmux_session_name=f"ccc-s{i}", cwd="."))
    import json
    with open(tmp_path / "sessions.json") as f:
        data = json.load(f)
    assert len(data) == 20
```

### 4.6 集成测试（`tests/integration/`）

集成测试需要真实 tmux 环境，**不在 CI 中运行**，仅本地手动触发。

```python
# conftest.py
import pytest

def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true")

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip = pytest.mark.skip(reason="need --run-integration flag")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
```

```python
# test_real_session.py
@pytest.mark.integration
def test_create_attach_kill(tmp_path):
    """Full lifecycle: create → send → capture → kill."""
    store = SessionStore(tmp_path / "s.json")
    session = ClaudeSession.create(name="itest", cwd="/tmp", store=store)
    assert session.is_alive()
    session.wait_ready(timeout=10)
    session.kill()
    assert not session.is_alive()

@pytest.mark.integration
def test_attach_survives_process_restart(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    s1 = ClaudeSession.create(name="persist", cwd="/tmp", store=store)
    s1.wait_ready(timeout=10)
    # 模拟进程重启：创建新的 session 对象，通过 store attach
    s2 = ClaudeSession.attach(name="persist", store=store)
    assert s2.is_alive()
    s2.kill()
```

### 4.7 覆盖率目标

| 模块 | 目标行覆盖率 | 说明 |
|---|---|---|
| `parser.py` | ≥ 95% | 纯函数，最容易达到 |
| `store.py` | ≥ 90% | 文件操作，tmp_path 覆盖全部路径 |
| `transport.py` | ≥ 80% | libtmux mock 覆盖主流程；部分错误路径依赖集成测试 |
| `session.py` | ≥ 80% | mock transport 覆盖主流程 |
| `manager.py` | ≥ 75% | mock session 覆盖主流程 |
| `relay.py` | ≥ 70% | mock transport 覆盖两种模式的主流程 |
| `cli.py` | ≥ 60% | Typer CLI，可用 `CliRunner` 补充 |

运行方式：
```bash
pytest tests/unit/ --cov=claude_cli_connector --cov-report=term-missing
```

---

## 5. 已知限制与风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| `capture-pane` 是屏幕快照，非原始字节流 | prompt pattern 可能随 Claude CLI 版本变化失效 | `_PROMPT_PATTERNS` 设为可配置，用户可自定义 |
| `send-keys` 与 `capture-pane` 之间存在 race condition | 偶发性读到旧输出 | `initial_delay` + stability 双重保障 + 发送后等待内容变化 |
| CJK 字符渲染宽度与 ASCII 不同 | 输出在窄终端里 wrap 导致乱序 | pane width 默认 220，文档注明 |
| Claude CLI 版本更新后 prompt 格式变化 | ready detection 失效 | CI 中跑集成测试检测回归 |
| tmux 版本差异（< 3.0） | `capture-pane` 参数不兼容 | 文档注明最低版本，启动时检查 |
| 权限提示格式可能随版本变化 | `detect_permission()` 失效 | 同时支持 Format A 和 Format B，新格式出现时可扩展 |
| Relay 长时间运行可能累积 cost | 未预期的 API 费用 | `max_rounds` 限制 + `RelayResult` 返回总 cost |
| libtmux API 不稳定 | `kill_session()` → `kill()`, `find_where()` 移除 | 使用 `getattr` 降级和 `sessions.get()` 替代 |
