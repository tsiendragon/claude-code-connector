# Product Requirements Document
## claude-cli-connector

**版本**: v0.1
**作者**: Lilong Qian
**更新日期**: 2026-03-05
**状态**: Draft
**关联文档**: [TECH_DESIGN.md](./TECH_DESIGN.md)

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

### 1.2 核心问题

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

**核心目标**: 提供一个稳定、可靠的 Python 低层包，使任何 Python 程序都能以编程方式与一个正在运行的 claude CLI 进程双向交互。

> **关于 demo**: `demo/` 目录下的 FastAPI Web UI **不是本包的核心功能**，仅作为端到端验证工具，帮助开发者直观确认 `claude-cli-connector` 能否正常工作。实际集成场景中，上层应用直接调用本包的 Python API。

### 2.1 成功指标

| 指标 | 目标值 |
|---|---|
| `send_and_wait()` ready detection 误差（过早/过晚返回） | ≤ 500ms |
| Python 进程重启后重连已有 session 的成功率 | ≥ 99% |
| 多 session 并发数 | ≥ 10 |
| `capture()` 调用 P99 延迟 | ≤ 50ms |

### 2.2 非目标（Out of Scope）

- 不实现 token 级流式精确解析（`capture-pane` 是屏幕快照，非原始字节流）。
- 不支持 Windows（tmux 依赖 POSIX）。
- 不重新实现 Anthropic API SDK（本包是 CLI 层封装，不走 API 直连）。
- demo 不做用户鉴权，不面向生产部署。

---

## 3. 用户与使用场景（Use Cases）

### 3.1 用户画像

| 角色 | 描述 |
|---|---|
| **AI 工程师** | 写 Python 脚本，需要把 claude CLI 嵌入自动化流程或 CI/CD |
| **平台开发者** | 在 eagleeye 等 agent 平台里集成 claude CLI 作为工具节点 |
| **包开发者** | 开发/调试本包，需要端到端验证（使用 demo） |

### 3.2 Use Cases

#### UC-1: Python 脚本调用 Claude CLI 完成任务

**角色**: AI 工程师

```
用户故事: 作为 AI 工程师，我希望用 Python 调用 claude CLI 分析一个 git diff，
          并拿到完整的文本结果，以便接入 CI/CD 流程写入 PR comment。
```

**验收标准**:
- `send_and_wait()` 必须在 Claude 完整输出后才返回，不能过早截断。
- 支持自定义超时，超时后抛出 `SessionTimeoutError`。
- session 死亡时抛出 `ConnectorError`，不能静默失败。

---

#### UC-2: 多会话并发管理

**角色**: 平台开发者

```
用户故事: 作为平台开发者，我希望为每个任务动态创建一个 claude CLI 会话，
          并发运行，平台重启后能恢复所有会话继续工作。
```

**验收标准**:
- 支持同时运行 ≥ 10 个 session，互不干扰。
- 会话元数据持久化（重启后可 attach）。
- `prune_dead()` 能自动清理已死亡的 session。

---

#### UC-3: 响应 claude CLI 的交互式提示

**角色**: AI 工程师

```
用户故事: 作为 AI 工程师，我希望 Python 能检测到 claude CLI 弹出的选项菜单
         （如模型选择、命令确认），并以编程方式发送对应的选择键。
```

**验收标准**:
- 能识别数字列表和箭头光标两种菜单格式。
- 返回结构化选项列表，供上层逻辑自动选择。

---

#### UC-4: 端到端验证（demo）

**角色**: 包开发者

```
用户故事: 作为包开发者，我希望有一个浏览器界面，让我不写代码就能手动验证
          send/capture/ready-detection/choice-detection 的实际行为是否符合预期。
```

**验收标准**:
- demo 启动后 `http://localhost:8765` 可访问。
- 能创建/切换/关闭 session。
- 消息发送后 pane 输出实时刷新（≤ 300ms 可见）。
- 检测到 choice menu 时自动渲染选项按钮。

---

## 4. 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 核心包可用（transport + parser + session + store） | ✅ 完成 |
| M2 | demo Web UI 可运行（端到端验证通过） | ✅ 完成 |
| M3 | choice menu 结构化检测（`detect_choices()` 方法） | 🔜 下次迭代 |
| M4 | 集成测试套件（需要真实 tmux 环境） | TBD |
| M5 | 发布到内部 PyPI | TBD |
