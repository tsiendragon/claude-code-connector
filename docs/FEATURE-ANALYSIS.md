# ccc 功能分析：核心 vs 附属

---

## 一、核心功能定义

> **核心 = 如果这个坏了，agent 完全不可用**
>
> 主循环：`创建会话 → 发送 prompt → 等待完成 → 提取回复 → 心跳检测 → 清理`

---

## 二、各 backend 核心机制差异

| 机制 | `claude` / `cursor` | `codex` | `opencode` |
|------|---------------------|---------|------------|
| **发送输入** | `send-keys -l <text>` + `Enter` | `send-keys -l <text>` + `Enter` + 150ms + `Enter`（两次） | `send-keys -l <text>` + `C-m` |
| **就绪检测** | 末尾出现 `❯`，且内容稳定 ≥0.8s | 最后两个 `›` 之间有非 spinner 内容 | `▣ Build · model · Xs` 出现时长后缀 |
| **提取回复** | 末尾 `❯` 向上找 `❯ <用户输入>`，取中间内容 | 最后两个 `›` 之间，去掉 `•` 前缀 | 用 `captureFull`（滚动回）+ 去掉 `█` 侧边栏，找最后完成的 `▣` 向上提取 |
| **权限拦截** | Format A（Allow once/always/Deny）+ Format B（编号列表） | ❌ 无 | ❌ 无 |
| **截图范围** | `capturePane`（可见区域）| `capturePane` | **必须用 `captureFull`**（内容会滚出屏幕）|

---

## 三、核心功能清单

### 基础生命周期

| 功能 | API / 命令 | 说明 |
|------|-----------|------|
| 创建会话 | `ensureSession` / `runSession` / `ccc run` | 启动 tmux session + agent 进程 |
| 心跳检测 | `isAlive` / `ccc attach` | tmux session 是否存活 |
| 销毁会话 | `killSession` / `ccc kill` | 终止进程 + 清除 store |

### 核心交互循环

| 功能 | API / 命令 | 说明 |
|------|-----------|------|
| 发送 prompt + 等待回复 | `send` / `ccc send` | 含等待完成、提取回复的完整流程 |
| 就绪检测 | `detectReady`（内部） | **各 backend 实现不同**，是差异化核心 |
| 提取回复 | `extractLastResponse`（内部） | **各 backend 实现不同**，是差异化核心 |
| 读取当前状态 | `readState` / `ccc read` | 返回 `ready / thinking / approval / choosing / dead` |
| 会话状态总览 | `ccc status` | `readState` 的格式化输出，含 permission / choices 详情 |
| 提取最后回复 | `extractLastResponse` / `ccc last` | 从 pane 内容中提取上一次完整回复，**各 backend 实现不同** |

### 权限处理（claude / cursor 专属核心）

| 功能 | API / 命令 | 说明 |
|------|-----------|------|
| 检测权限弹窗 | `detectPermission`（内部） | Format A + B 两种格式 |
| 响应权限 | `approvePermission` / `ccc approve` | yes / always / no |
| 自动批准 | `ccc send --auto-approve` | 无人值守场景的核心能力 |

> **注**：对使用 `--dangerously-skip-permissions` 的场景（如 NanoClaw），权限处理**降为附属**。
> 对普通交互用户，权限处理是**核心**。

---

## 四、附属功能清单

> 附属 = 核心功能的组合封装，或调试 / 运维便利工具。核心全部通过后测试。

| 功能 | API / 命令 | 属于什么的封装 |
|------|-----------|--------------|
| 等待特定状态 | `waitState` / `ccc wait <state>` | `readState` 的轮询循环 |
| 低层文本输入 | `ccc input` | `sendText` 的直接暴露 |
| 低层按键 | `ccc key` | `sendKey` 的直接暴露 |
| 打断 | `ccc interrupt` | `sendKey("C-c")` |
| 打印末尾行 | `ccc tail` | `capturePane` + 切片 |
| 列出会话 | `ccc ps` | `storeList` + `isAlive` |
| 清理死会话 | `ccc clean` | `storeList` + `isAlive` + `storeDelete` |
| 模型切换 | `ccc model` | `sendText("/model")` + `detectModelPicker` |
| 查看历史 | `ccc history` | 读取 JSONL 日志文件 |
| 单次流式查询 | `ccc stream` | 独立的 `claude -p --output-format stream-json`，无需 tmux |
| 多 agent 协作 | `ccc relay` | 多次 `claudeOneShot` 的编排 |

---

## 五、测试优先级策略

### 核心功能测试（必须充分覆盖）

每个 backend 独立测试，共 **4 backend × 7 核心操作 = 28 个基础用例**，另加边界场景。

#### claude

```
✅ createSession / ensureSession → isAlive = true
✅ send("hello") → 等待 ❯ 稳定 → 返回非空回复
✅ status → state = "ready"，lastResponse 与 send 返回值一致
✅ last → 独立调用，提取内容与 send 返回值一致
✅ send 时出现 Permission 弹窗 → approve(yes) → 继续完成并返回回复
✅ send --auto-approve → 自动通过所有权限弹窗 → 返回回复
✅ killSession → isAlive = false
```

#### cursor

```
✅ createSession → isAlive = true
✅ send("hello") → 等待 ❯ 稳定 → 返回非空回复
✅ status → state = "ready"
✅ last → 内容与 send 返回值一致
✅ killSession → isAlive = false
```

#### codex

```
✅ createSession → isAlive = true
✅ send("hello") → 两次 Enter → 等待 › 计数 +2 → 返回回复（去 • 前缀）
✅ status → state = "ready"
✅ last → 内容与 send 返回值一致
✅ killSession → isAlive = false
```

#### opencode

```
✅ createSession → isAlive = true
✅ send("hello") → C-m → 等待 ▣ + 时长后缀 → captureFull → 返回回复（去 █ 侧边栏）
✅ status → state = "ready"
✅ last --full → 内容与 send 返回值一致（须用 --full 因为内容可能已滚出屏幕）
✅ killSession → isAlive = false
```

### 边界场景（核心测试中必须包含）

| 场景 | 原因 | 涉及 backend |
|------|------|------------|
| 回复内容超过 50 行（超出可见区域） | opencode 必须 `captureFull`；其他 backend 可能截断 | 全部 |
| 连续快速发送两条消息 | 防止第二次 `send` 在第一次未完成时误触发 | 全部 |
| session 中途意外死亡 | `waitReady` 应抛出明确错误而非超时挂起 | 全部 |
| 稳定性阈值边界（0.8s 附近） | `detectReady` 依赖时间窗口，容易出现假阳性 | claude / cursor |
| Permission 弹窗出现在 send 中途 | 需要正确识别并阻塞，而非误判为 ready | claude / cursor |
| 长时间无响应（接近 timeout） | 超时后应返回明确错误，不能静默失败 | 全部 |

### 附属功能测试（基本验证）

```
wait <state>   → 能正确等到 ready / approval / dead
approve        → 能找到权限弹窗并正确按键导航
model list     → /model 打开后能解析 ≥2 个选项
model select   → 能导航到目标并确认
history        → send 后 JSONL 文件有对应记录
tail           → 输出非空，内容与 pane 一致
ps / clean     → 存活判断与 isAlive 一致
relay debate   → 完成一轮，transcript 有双方记录
stream         → claude -p 返回非空文本结果
```

---

## 六、总结

```
核心功能（28 个用例 + 边界场景）：
  createSession / isAlive / send / readState / status / last / killSession

  send + status + last 的差异化实现是测试重点：
    ├─ 输入方式：Enter / Enter+Enter / C-m
    ├─ 就绪检测：❯ 稳定 / › 计数 / ▣ 时长
    ├─ 回复提取：边界标记 / 滚动回 / 去侧边栏
    └─ 权限处理：Format A + B（仅 claude / cursor）

附属功能：核心通过后再测
  （绝大多数是核心函数的薄封装，逻辑无独立风险）
```
