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
- 为团队成员提供一个非终端的图形界面，降低使用门槛。
- 在 Agent 框架（如 eagleeye-task-platform）里，把 claude CLI 作为一个可调用的工具节点。
- 管理多个并发的 claude CLI 会话，比如每个项目/仓库开一个独立 session。

目前没有官方的 Python SDK 能直接控制 claude CLI 进程（与 Anthropic Python SDK 不同，后者是直接调 API 而非 CLI）。

### 1.2 问题

| 需要解决的问题 | 现状 |
|---|---|
| Python 代码如何给 claude CLI 发送输入 | 无标准方案，只能终端手敲 |
| Python 代码如何读取 claude CLI 的输出 | 无标准方案 |
| 如何检测 claude CLI 是否在"思考中"还是"等待输入" | 无 |
| Python 进程重启后如何恢复已有的 claude CLI 会话 | 无 |
| 如何管理多个并发的 claude CLI 会话 | 无 |
| 非技术用户如何通过 Web 界面使用 claude CLI | 无 |

---

## 2. 目标（Goals）

**Primary Goal**: 提供一个稳定、可靠的 Python 低层包，使任何 Python 程序都能以编程方式与一个正在运行的 claude CLI 进程双向交互。

**Secondary Goal**: 在此基础上提供 Web UI demo，验证该包的可用性，并为团队提供一个开箱即用的 claude CLI Web 界面。

### 2.1 核心指标

| 指标 | 目标值 |
|---|---|
| 发送一条消息到得到完整响应的平均延迟（ready detection 误差） | ≤ 500ms |
| Python 进程重启后重连已有 session 的成功率 | ≥ 99% |
| 多 session 并发数 | ≥ 10 |
| Web UI 消息端到端延迟（用户发送 → 页面显示 Claude 回复） | ≤ 1s（首字节） |

### 2.2 非目标（Out of Scope）

- 不实现 token 级流式精确解析（`capture-pane` 是屏幕快照，非原始字节流）。
- 不支持 Windows（tmux 依赖 POSIX）。
- 不重新实现 Anthropic API SDK（本包是 CLI 层封装，不走 API 直连）。
- 不做用户鉴权（demo 仅供内部使用）。

---

## 3. 用户与使用场景（Use Cases）

### 3.1 用户画像

| 角色 | 描述 |
|---|---|
| **AI 工程师** | 写 Python 脚本，需要把 claude CLI 作为 AI 后端嵌入自动化流程 |
| **平台开发者** | 在 eagleeye 等 agent 平台里集成 claude CLI 节点 |
| **研究员 / 产品同学** | 不熟悉终端，希望用 Web 界面与 claude CLI 交互，快速试验 prompting |
| **团队管理者** | 希望为团队开一个共享的 claude CLI Web 服务，多人同时使用不同会话 |

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

---

#### UC-2: 通过 Web UI 与 claude CLI 交互

**角色**: 研究员 / 产品同学
**场景**: 用户打开浏览器，在网页上看到一个终端样式的聊天界面，可以直接与 claude CLI 对话，就像在终端里一样，但更友好。

**典型流程**:
```
1. 用户访问 http://localhost:8765
2. 页面显示当前活跃的 claude CLI 会话列表（或自动创建一个）
3. 用户在输入框输入问题，按 Enter 发送
4. 页面实时流式显示 claude CLI 的输出（streaming display）
5. 当 claude CLI 弹出选项（如"选择模型"），页面显示选项按钮
6. 用户点击按钮，等价于在终端里按对应数字键
7. 对话历史保留在页面上
```

**关键需求**:
- **实时输出**: claude CLI 在"思考"过程中的流式输出要尽快显示（polling 间隔 ≤ 300ms），不能等到完整响应才刷新。
- **选项识别**: 检测 claude CLI 弹出的选项菜单（如模型选择、确认对话框），在 Web UI 上渲染为可点击按钮。
- **状态指示**: UI 要明确显示 claude CLI 当前是"思考中"还是"等待输入"。
- **Interrupt**: 用户可以点击"停止"按钮，发送 Ctrl-C 中断当前生成。

---

#### UC-3: 多会话管理

**角色**: 平台开发者
**场景**: eagleeye-task-platform 里，每个任务对应一个 claude CLI 会话。任务调度器需要动态创建/销毁会话，并能在平台重启后恢复。

**典型流程**:
```python
mgr = SessionManager()
# 任务派发
for task in tasks:
    session = mgr.create(name=task.id, cwd=task.repo_path)
    session.send(task.prompt)

# 等待全部完成
results = mgr.collect_responses(timeout=300)

# 平台重启后恢复
mgr2 = SessionManager()
for name in mgr2.list_stored_sessions():
    s = mgr2.attach(name)
    print(s.tail())
```

---

#### UC-4: 响应 claude CLI 的交互式提示

**角色**: AI 工程师 / Web 用户
**场景**: claude CLI 在某些操作前会弹出交互式确认框或选择菜单，例如：

```
Which model do you want to use?
  1. claude-opus-4-5
  2. claude-sonnet-4-5   ← default
  3. claude-haiku-4-5
```

或：

```
Do you want to run this command? (y/n)
> git reset --hard HEAD~1
```

Python 侧或 Web UI 需要识别这类提示，并以编程方式或点击按钮的方式响应。

**关键需求**:
- `parser.py` 要能识别"选项菜单"类型的输出 pattern。
- Web UI 要把这些选项渲染为按钮，用户点击后调用 `session.send(key)` 发送对应按键。

---

## 4. 技术架构

### 4.1 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    Web Browser / Python Script              │
└───────────────────────────┬─────────────────────────────────┘
                            │  HTTP / SSE / WebSocket
┌───────────────────────────▼─────────────────────────────────┐
│            FastAPI Web Server (demo/server.py)              │
│                                                             │
│  POST /sessions/{name}/send   → session.send()             │
│  GET  /sessions/{name}/stream → SSE (pane polling)         │
│  GET  /sessions               → list sessions              │
│  POST /sessions               → create session             │
│  DELETE /sessions/{name}      → kill session               │
└───────────────────────────┬─────────────────────────────────┘
                            │  Python API
┌───────────────────────────▼─────────────────────────────────┐
│          claude-cli-connector (this package)                │
│                                                             │
│  ClaudeSession   →  TmuxTransport  →  libtmux              │
│  SessionManager  →  SessionStore   →  JSON file            │
│  parser.py       →  ANSI strip + ready detection           │
└───────────────────────────┬─────────────────────────────────┘
                            │  tmux send-keys / capture-pane
┌───────────────────────────▼─────────────────────────────────┐
│              tmux session: ccc-<name>                       │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  pane 0:  claude  (Claude Code CLI)                 │   │
│  │  > █                                                │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 终端控制层 | tmux | 稳定、成熟，自带 PTY 管理和 session 持久化 |
| Python tmux 绑定 | libtmux | 类型完善、维护活跃 |
| 输出传输协议 | SSE（Server-Sent Events） | 单向流式推送，简单、浏览器原生支持，比 WebSocket 轻量 |
| 输出 polling 间隔 | 200ms | 在响应性和 CPU 消耗之间取得平衡 |
| ready detection 策略 | 三层叠加（spinner → prompt pattern → stability） | 单一策略不够鲁棒，三层覆盖不同 Claude CLI 版本 |
| Web UI 实现 | 内嵌 HTML/JS（单文件） | Demo 阶段追求零依赖，无需 Node.js 构建 |

### 4.3 ready detection 三层策略

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

### 4.4 交互式选项检测

Claude CLI 在弹出选项时，输出格式通常为：

```
❯ option text          ← 当前选中项（带箭头或高亮）
  option text
  option text
```

或数字列表：

```
1. claude-opus-4-5
2. claude-sonnet-4-5
3. claude-haiku-4-5
```

`parser.py` 需要识别这两种模式，返回结构化的 `ChoiceMenu` 对象供上层使用。

---

## 5. API 设计

### 5.1 Python API（核心包）

```python
# 创建 / 恢复会话
session = ClaudeSession.create(name, cwd, command="claude")
session = ClaudeSession.attach(name)

# 交互
session.send(text, enter=True)                    # 非阻塞发送
response = session.send_and_wait(text, timeout)   # 阻塞直到 ready
pane_text = session.capture()                     # 获取当前全量输出
new_text = session.new_output_since_last_capture() # 增量输出

# 控制
session.interrupt()   # Ctrl-C
session.is_ready()    # 非阻塞检查是否等待输入
session.is_alive()    # tmux session 是否存活
session.kill()        # 终止

# 多会话
mgr = SessionManager()
mgr.create / attach / get / kill / kill_all
mgr.collect_responses(timeout)
```

### 5.2 REST API（FastAPI demo）

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/sessions` | 列出所有会话（含存活状态） |
| `POST` | `/api/sessions` | 创建新会话 `{name, cwd}` |
| `DELETE` | `/api/sessions/{name}` | 关闭并删除会话 |
| `POST` | `/api/sessions/{name}/send` | 发送消息 `{text}` |
| `POST` | `/api/sessions/{name}/interrupt` | 发送 Ctrl-C |
| `GET` | `/api/sessions/{name}/stream` | SSE 流式输出（polling pane） |
| `GET` | `/` | Web UI 页面 |

---

## 6. Web UI 功能需求

### 6.1 功能列表

| 功能 | 优先级 | 说明 |
|---|---|---|
| 会话列表 + 创建 | P0 | 左侧栏显示所有会话，可以新建或切换 |
| 消息输入框 | P0 | 底部输入框，Enter 发送，Shift+Enter 换行 |
| 实时输出显示 | P0 | SSE 驱动，pane 内容以终端风格展示（等宽字体，深色背景） |
| 状态指示 | P0 | 显示 Claude 是"思考中"还是"等待输入"（spinner 动画） |
| Interrupt 按钮 | P0 | 点击发送 Ctrl-C |
| 交互式选项按钮 | P1 | 检测到选项菜单时，渲染为可点击按钮 |
| 模型切换快捷指令 | P1 | 输入框提供 `/model opus\|sonnet\|haiku` 快捷命令 |
| 消息历史 | P1 | 会话级别的消息历史记录（内存存储） |
| 终端原始视图 | P2 | 切换显示原始 pane capture（含 ANSI 渲染） |

### 6.2 UI 草图

```
┌─────────────────────────────────────────────────────────────────┐
│ claude-cli-connector  Web UI                          [docs] [?] │
├──────────────┬──────────────────────────────────────────────────┤
│              │  Session: myproject              [⚡ Interrupt]   │
│ Sessions     │ ─────────────────────────────────────────────── │
│ ──────────── │                                                  │
│ ● myproject  │  > Hello! I'm analyzing your codebase...        │
│   backend    │    Found 3 main modules:                         │
│   frontend   │    1. auth.py  - authentication                  │
│              │    2. api.py   - REST endpoints                  │
│ [+ New]      │    3. db.py   - database models                  │
│              │                                                  │
│              │  ● Thinking...                     [⠋ spinner]  │
│              │ ─────────────────────────────────────────────── │
│              │  ┌─────────────────────────────────────────┐    │
│              │  │ Ask Claude anything...            [Send] │    │
│              │  └─────────────────────────────────────────┘    │
└──────────────┴──────────────────────────────────────────────────┘

当检测到选项菜单时：

│  Which model would you like to use?                            │
│  ┌──────────────┐ ┌─────────────────┐ ┌──────────────────┐   │
│  │ claude-opus  │ │ claude-sonnet ✓ │ │  claude-haiku    │   │
│  └──────────────┘ └─────────────────┘ └──────────────────┘   │
```

---

## 7. 依赖与环境

### 7.1 运行时依赖

| 依赖 | 版本 | 用途 |
|---|---|---|
| tmux | ≥ 3.0 | Session / PTY 管理 |
| Claude Code CLI | latest | 被控制的 AI 进程 |
| Python | ≥ 3.10 | 包运行时 |
| libtmux | ≥ 0.30 | Python tmux 绑定 |
| pydantic | ≥ 2.0 | 数据模型 |
| typer | ≥ 0.12 | CLI 入口 |
| rich | ≥ 13 | 终端美化 |

### 7.2 Demo 额外依赖

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
| M1 | 核心包（transport + parser + session）可用 | 已完成 |
| M2 | FastAPI Web UI demo 可运行 | 本次 |
| M3 | 交互式选项检测（choice menu） | 下次迭代 |
| M4 | 多用户会话隔离 + 简单鉴权 | TBD |
| M5 | 发布到内部 PyPI | TBD |
