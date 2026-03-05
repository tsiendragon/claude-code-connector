# Product Requirements Document
## claude-cli-connector

**版本**: v0.1
**作者**: Lilong Qian
**更新日期**: 2026-03-05
**状态**: Draft

---

## 1. 背景与问题定义

### 1.1 背景

Claude Code CLI（以下简称 "claude CLI"）是 Anthropic 提供的命令行交互式 AI 助手。它本质上是一个运行在终端伪终端（PTY）里的交互式进程：接受用户文本输入，流式输出 AI 回复，并在特定场景（如需要用户确认、选择模型、执行命令前等）弹出选项让用户选择。

直接使用 claude CLI 的方式只有终端。但在工程实践中，我们经常需要：

- 在 Python 脚本/自动化流程里，让 claude CLI 作为一个可编程的 AI 后端。
- 在 Agent 框架（如 eagleeye-task-platform）里，把 claude CLI 作为一个可调用的工具节点。
- 管理多个并发的 claude CLI 会话，比如每个项目/仓库开一个独立 session。
- Python 进程重启后能恢复已有的 claude CLI 会话，不丢失上下文。

目前没有官方的 Python SDK 能直接控制 claude CLI 进程（与 Anthropic Python SDK 不同，后者是直接调 API 而非 CLI）。

### 1.2 问题

| 需要解决的问题 | 现状 |
|---|---|
| Python 代码如何给 claude CLI 发送输入 | 无标准方案，只能终端手敲 |
| Python 代码如何读取 claude CLI 的输出 | 无标准方案 |
| 如何检测 claude CLI 是否在"思考中"还是"等待输入" | 无 |
| Python 进程重启后如何恢复已有的 claude CLI 会话 | 无 |
| 如何管理多个并发的 claude CLI 会话 | 无 |
| 如何响应 claude CLI 弹出的交互式选项（模型选择、命令确认等） | 无 |

---

## 2. 目标（Goals）

**核心目标（本包）**: 提供一个稳定、可靠的 Python 低层包，使任何 Python 程序都能以编程方式与一个正在运行的 claude CLI 进程双向交互。

> **说明**: `demo/` 目录下的 FastAPI Web UI **不是本包的核心功能**，仅作为端到端验证工具存在。它的用途是帮助开发者直观地确认 `claude-cli-connector` 能否正常工作，降低调试门槛。实际集成场景中，上层应用（Agent 框架、脚本等）直接调用本包的 Python API，不依赖 demo 层。

### 2.1 核心指标（针对 Python 包本身）

| 指标 | 目标值 |
|---|---|
| `send_and_wait()` ready detection 误差（响应截断/过早返回） | ≤ 500ms |
| Python 进程重启后重连已有 session 的成功率 | ≥ 99% |
| 多 session 并发数 | ≥ 10 |
| `capture()` 调用延迟（P99） | ≤ 50ms |

### 2.2 非目标（Out of Scope）

- 不实现 token 级流式精确解析（`capture-pane` 是屏幕快照，非原始字节流）。
- 不支持 Windows（tmux 依赖 POSIX）。
- 不重新实现 Anthropic API SDK（本包是 CLI 层封装，不走 API 直连）。
- demo Web UI 不做用户鉴权、不做生产级部署，仅供本地开发验证。

---

## 3. 用户与使用场景（Use Cases）

### 3.1 用户画像

| 角色 | 描述 |
|---|---|
| **AI 工程师** | 写 Python 脚本，需要把 claude CLI 作为 AI 后端嵌入自动化流程 |
| **平台开发者** | 在 eagleeye 等 agent 平台里集成 claude CLI 节点 |
| **包开发者自身** | 需要端到端验证 claude-cli-connector 是否工作正常（使用 demo） |

### 3.2 Use Case 详细描述

#### UC-1: Python 脚本中调用 Claude CLI 完成任务

**角色**: AI 工程师
**场景**: 工程师在 Python 里启动一个 claude CLI 会话，发送代码分析任务，等待完整响应后处理结果。

```
用户故事: 作为 AI 工程师，我希望用 Python 调用 claude CLI 分析一个 git diff，
          并拿到结构化的结果，以便接入 CI/CD 流程。
```

**典型流程**:
```python
session = ClaudeSession.create(name="ci-review", cwd="/repo")
diff = subprocess.check_output(["git", "diff", "HEAD~1"])
response = session.send_and_wait(f"Review this diff:\n{diff}")
# 解析 response，写入 PR comment
session.kill()
```

**关键需求**:
- `send_and_wait()` 必须在 Claude 完整输出后才返回，不能过早截断。
- 支持设置超时（大型代码库分析可能需要 5 分钟）。
- 异常（超时、session 死亡）必须以明确的异常类型抛出，不能静默失败。

---

#### UC-2: 多会话并发管理

**角色**: 平台开发者
**场景**: eagleeye-task-platform 里，每个任务对应一个 claude CLI 会话。任务调度器需要动态创建/销毁会话，并能在平台重启后恢复。

**典型流程**:
```python
mgr = SessionManager()

# 任务派发（并发）
for task in tasks:
    session = mgr.create(name=task.id, cwd=task.repo_path)
    session.send(task.prompt)

# 等待全部完成
results = mgr.collect_responses(timeout=300)

# 平台重启后恢复：session 仍在 tmux 里，直接 attach 回来
mgr2 = SessionManager()
for name in mgr2.list_stored_sessions():
    s = mgr2.attach(name)
    print(s.tail())
```

**关键需求**:
- 每个 session 独立隔离，互不干扰。
- 会话元数据持久化到本地（JSON），重启后可恢复。
- `prune_dead()` 能自动清理已死亡的 session。

---

#### UC-3: 响应 claude CLI 的交互式提示

**角色**: AI 工程师
**场景**: claude CLI 在某些操作前会弹出交互式确认框或选择菜单，需要 Python 侧以编程方式识别并响应。

```
Which model do you want to use?
  1. claude-opus-4-5
  2. claude-sonnet-4-5   ← default
  3. claude-haiku-4-5
```

```
Do you want to run this command? (y/n)
> git reset --hard HEAD~1
```

**典型流程**:
```python
session.send("Some task that triggers a choice")
# parser 识别到选项菜单
choices = session.detect_choices()   # → [{"key":"1","label":"claude-opus-4-5"}, ...]
# 程序根据业务逻辑自动选择
session.send("2")
```

**关键需求**:
- `parser.py` 要能识别数字列表和箭头光标两种选项菜单格式。
- 返回结构化的选项列表，供上层逻辑判断。

---

#### UC-4: 端到端验证（使用 demo）

**角色**: 包开发者自身 / 集成调试
**场景**: 在开发或调试 claude-cli-connector 时，需要快速验证包的核心行为是否符合预期，而不必写大量测试代码。启动 demo Web UI，通过浏览器手动操作，可以直观地观察 send/capture/ready-detection 的实际表现。

> 这是 `demo/` 的唯一定位：**开发阶段的调试与验证工具**，不是面向最终用户的功能。

**典型验证场景**:
- 发送一条消息，确认 ready detection 不会过早截断或超时。
- 触发模型选择菜单，确认 choice detection 能正确识别选项。
- Kill 一个 session 后重建，确认 store 持久化和 attach 逻辑正常。
- 发送 Ctrl-C，确认 interrupt 生效且 session 恢复到 ready 状态。

---

## 4. 技术架构

### 4.1 核心包架构（主体）

`claude-cli-connector` 本身是一个纯 Python 库，不包含任何网络服务。调用方（脚本、agent 框架、或 demo）直接 `import` 使用。

```
┌──────────────────────────────────────────────────────────────┐
│            调用方（Python 脚本 / Agent 框架）                 │
│                                                              │
│  from claude_cli_connector import ClaudeSession              │
│  session = ClaudeSession.create(...)                         │
│  response = session.send_and_wait("...")                     │
└───────────────────────────┬──────────────────────────────────┘
                            │  Python 方法调用
┌───────────────────────────▼──────────────────────────────────┐
│              claude-cli-connector（本包）                     │
│                                                              │
│  ClaudeSession                                               │
│    ├─ send() / send_and_wait() / capture() / tail()          │
│    ├─ interrupt() / is_ready() / is_alive() / kill()         │
│    └─ new_output_since_last_capture()                        │
│                                                              │
│  SessionManager                                              │
│    └─ create / attach / get / kill_all / collect_responses   │
│                                                              │
│  TmuxTransport          parser.py          SessionStore      │
│    libtmux 封装           ANSI 清洗           JSON 持久化      │
│    send_keys               ready detection    ~/.local/share  │
│    capture_pane            choice detection                   │
└───────────────────────────┬──────────────────────────────────┘
                            │  tmux send-keys / capture-pane
┌───────────────────────────▼──────────────────────────────────┐
│              tmux session: ccc-<name>                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  pane 0:  claude  (Claude Code CLI)                  │   │
│  │  > █                                                 │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 demo 层（可选，仅用于端到端验证）

`demo/server.py` 是一个独立的 FastAPI 应用，它调用本包的 Python API，并通过 SSE 将 pane 输出推送到浏览器。它**不是本包的一部分**，也不应被当作本包的接口层。

```
┌─────────────────────────────────────┐
│          浏览器（调试用）            │
└──────────────┬──────────────────────┘
               │  HTTP / SSE
┌──────────────▼──────────────────────┐   ← demo/server.py
│  FastAPI Web Server                 │     仅用于开发验证
│  · pane 轮询 → SSE 推送             │     不是本包核心
│  · choice menu 渲染                 │
└──────────────┬──────────────────────┘
               │  调用本包 Python API（与任何其他调用方完全一致）
┌──────────────▼──────────────────────┐
│      claude-cli-connector（本包）    │
└─────────────────────────────────────┘
```

### 4.3 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 终端控制层 | tmux | 稳定、成熟，自带 PTY 管理和 session 持久化，Python 进程退出后 CLI 仍存活 |
| Python tmux 绑定 | libtmux | 类型完善、维护活跃 |
| 会话持久化 | JSON 文件（~/.local/share/ccc/） | 轻量、无依赖、进程重启后可恢复 |
| ready detection 策略 | 三层叠加 | 单一策略不够鲁棒，三层覆盖不同 Claude CLI 版本 |
| pane 宽度 | 220 列 | 避免 CJK 字符因 wrap 导致输出乱序 |

### 4.4 ready detection 三层策略

```
capture-pane()
     │
     ▼
① 检测 spinner / "Thinking..." → 确认 BUSY（高置信）
     │ 未命中
     ▼
② 检测 "> " / "╰─>" prompt pattern → 确认 READY（高置信）
     │ 未命中
     ▼
③ 与上一次 capture 对比 → 内容相同 + elapsed ≥ 0.8s → 确认 READY（中置信）
     │ 未命中
     ▼
继续 polling（sleep 200ms）
```

### 4.5 交互式选项检测

Claude CLI 在弹出选项时，输出格式通常为以下两种之一：

```
# 数字列表（最常见）
1. claude-opus-4-5
2. claude-sonnet-4-5
3. claude-haiku-4-5

# 箭头光标（ink/inquirer 类 TUI 组件）
❯ claude-sonnet-4-5   ← 当前选中
  claude-opus-4-5
  claude-haiku-4-5
```

`parser.py` 需要识别这两种模式，向上层返回结构化的选项列表：
```python
[{"key": "1", "label": "claude-opus-4-5"},
 {"key": "2", "label": "claude-sonnet-4-5", "selected": True},
 {"key": "3", "label": "claude-haiku-4-5"}]
```

---

## 5. Python API 设计（核心包）

```python
# ── 会话生命周期 ──────────────────────────────────────────────
session = ClaudeSession.create(name, cwd, command="claude")
session = ClaudeSession.attach(name)          # 重连已有 session
with ClaudeSession.create(...) as session:    # context manager，退出自动 kill
    ...

# ── 发送与接收 ────────────────────────────────────────────────
session.send(text, enter=True)                # 非阻塞发送
response = session.send_and_wait(text,        # 阻塞直到 ready
                                 timeout=300,
                                 initial_delay=0.8)
pane_text = session.capture()                 # 当前全量 pane 文本
new_text  = session.new_output_since_last_capture()  # 增量输出

# ── 状态查询 ──────────────────────────────────────────────────
session.is_ready()    # 非阻塞：是否等待输入
session.is_alive()    # tmux session 是否存活

# ── 交互式选项 ────────────────────────────────────────────────
# （在 parser.py 检测到 choice menu 时使用）
session.send("2")     # 发送数字键响应选项

# ── 控制 ─────────────────────────────────────────────────────
session.interrupt()   # Ctrl-C，中断当前生成
session.tail(lines=40)
session.kill()        # 终止并从 store 移除

# ── 多会话 ───────────────────────────────────────────────────
mgr = SessionManager()
mgr.create(name, cwd)
mgr.attach(name)
mgr.get(name)
mgr.kill(name) / mgr.kill_all()
mgr.collect_responses(timeout)   # 等待所有 session ready，返回 {name: text}
mgr.prune_dead()                 # 清理已死亡的 session
mgr.list_stored_sessions()       # 从 store 读取，包含离线 session
```

---

## 6. demo 说明（端到端验证工具）

> 本节描述 `demo/` 目录的定位和功能，供开发者在调试本包时参考。

### 6.1 定位

`demo/server.py` 是一个 **独立的 FastAPI 应用**，其唯一目的是提供一个浏览器界面，让开发者可以不写代码地手动验证 `claude-cli-connector` 的行为。它对本包的调用方式与任何普通 Python 脚本完全相同，没有特殊权限，也不是本包的官方接口。

### 6.2 demo 提供的验证能力

| 验证点 | 如何验证 |
|---|---|
| `send_and_wait()` ready detection 准确性 | 发送消息，观察页面何时停止更新 vs Claude 实际完成时间 |
| choice menu 检测 | 触发模型选择，观察选项按钮是否正确渲染 |
| session 持久化与 attach | 重启 server，观察已有 session 是否恢复 |
| interrupt | 点击 ⚡ 按钮，确认 Ctrl-C 生效 |
| 多 session 并发 | 创建多个 session，分别发消息，确认互不干扰 |

### 6.3 demo REST API（内部使用）

这些接口是 demo Web UI 的实现细节，不是本包对外暴露的 API。

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/sessions` | 列出所有会话 |
| `POST` | `/api/sessions` | 创建新会话 `{name, cwd}` |
| `DELETE` | `/api/sessions/{name}` | 关闭并删除会话 |
| `POST` | `/api/sessions/{name}/send` | 发送消息 `{text}` |
| `POST` | `/api/sessions/{name}/interrupt` | 发送 Ctrl-C |
| `GET` | `/api/sessions/{name}/stream` | SSE 流式 pane 输出 |
| `GET` | `/` | Web UI 页面 |

### 6.4 demo UI 草图

```
┌─────────────────────────────────────────────────────────────────┐
│ claude-cli-connector  Web UI  [dev/debug only]                  │
├──────────────┬──────────────────────────────────────────────────┤
│              │  Session: myproject              [⚡ Interrupt]   │
│ Sessions     │ ─────────────────────────────────────────────── │
│ ──────────── │                                                  │
│ ● myproject  │  > Hello! I'm analyzing your codebase...        │
│   backend    │    Found 3 main modules:                         │
│              │    1. auth.py  - authentication                  │
│ [+ New]      │    2. api.py   - REST endpoints                  │
│              │    3. db.py   - database models                  │
│              │                                                  │
│              │  ● Thinking...                     [⠋ spinner]  │
│              │ ─────────────────────────────────────────────── │
│              │  ┌─────────────────────────────────────────┐    │
│              │  │ Ask Claude anything...            [Send] │    │
│              │  └─────────────────────────────────────────┘    │
└──────────────┴──────────────────────────────────────────────────┘

当检测到选项菜单时，自动渲染为按钮：

│  Which model would you like to use?                            │
│  ┌──────────────┐ ┌─────────────────┐ ┌──────────────────┐   │
│  │ claude-opus  │ │ claude-sonnet ✓ │ │  claude-haiku    │   │
│  └──────────────┘ └─────────────────┘ └──────────────────┘   │
```

---

## 7. 依赖与环境

### 7.1 核心包运行时依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| tmux | ≥ 3.0 | Session / PTY 管理 |
| Claude Code CLI | latest | 被控制的 AI 进程 |
| Python | ≥ 3.10 | 包运行时 |
| libtmux | ≥ 0.30 | Python tmux 绑定 |
| pydantic | ≥ 2.0 | 数据模型（SessionRecord） |
| typer | ≥ 0.12 | `ccc` CLI 入口 |
| rich | ≥ 13 | 终端美化 |

### 7.2 demo 额外依赖（仅 demo/）

| 依赖 | 版本 | 用途 |
|---|---|---|
| fastapi | ≥ 0.110 | Web 框架 |
| uvicorn | ≥ 0.29 | ASGI server |
| sse-starlette | ≥ 2.0 | SSE 响应支持 |

---

## 8. 已知限制与风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| `capture-pane` 是屏幕快照，非原始字节流 | prompt pattern 可能随 CLI 版本变化而失效 | 提供 `_PROMPT_PATTERNS` 为可配置项，用户可自定义 |
| `send-keys` 与 `capture-pane` 之间存在 race condition | 偶发性地读到旧输出 | 增加 `initial_delay` + stability 双重保障 |
| 宽字符（CJK）在 pane 里的渲染宽度与 ASCII 不同 | 输出截断或乱码 | 设置 pane width ≥ 220，避免 CJK wrap |
| Claude CLI 版本更新后 prompt 格式变化 | ready detection 失效 | 版本锁定 + CI 中跑集成测试 |
| tmux 版本差异 | `capture-pane` 参数不同 | 标注最低版本 3.0，文档说明 |

---

## 9. 里程碑

| 里程碑 | 内容 | 目标日期 |
|---|---|---|
| M1 | 核心包（transport + parser + session + store）可用 | 已完成 |
| M2 | demo Web UI 可运行（端到端验证通过） | 已完成 |
| M3 | choice menu 结构化检测（`detect_choices()` 方法） | 下次迭代 |
| M4 | 集成测试套件（需要真实 tmux 环境，CI 中运行） | TBD |
| M5 | 发布到内部 PyPI | TBD |
