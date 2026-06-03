# S01 Agent Loop 学习笔记

## 问题层 — 是什么 / 为什么存在

### 1. Agent Loop 到底在解决什么问题？为什么 LLM 不能一次性完成任务？

LLM 本身只能生成文本，不能读文件、跑命令、改代码。要完成实际任务，它需要反复「思考 → 调工具 → 看结果 → 再思考」。Agent Loop 就是这个循环的骨架——让模型可以多轮迭代，每轮拿到新信息后再推进一步，直到任务完成。

### 2. 为什么工具结果要以 "user" 角色喂回模型？

Anthropic API 要求消息严格 user → assistant → user → assistant 交替。模型调用工具后，上一条是 assistant，下一条只能是 user。这不是因为工具结果"属于用户"，而是协议的交替规则决定的。在模型的世界观里只有"我"（assistant）和"不是我"（user），工具执行发生在模型外部，所以归 user 侧。多个 tool_result 打包成一条 user 消息的 content 列表。

## 方案层 — 怎么实现的 / 设计取舍

### 3. 循环的退出条件：stop_reason 机制

每次模型回复带一个 `stop_reason`。如果是 `"tool_use"`，说明模型还想调工具，循环继续。如果是其他值（如 `"end_turn"`），说明模型认为任务完成，给出了最终文本回答，循环退出。决定权在模型手里——harness 不判断任务有没有完成，模型自己决定什么时候停。

### 4. LoopState 为什么要显式抽出来？

把 messages、turn_count、transition_reason 封装成一个 dataclass，而不是用散落的全局变量，是为了后续扩展。后面的 session 会在 LoopState 上加权限状态、hook 触发标记、上下文压缩信息等字段，只改 LoopState 定义，不用改循环逻辑本身。

### 5. messages 列表的增长过程——一轮循环里发生了什么？

以用户问"当前目录有什么文件"为例：

```
[0] user:      "当前目录有什么文件"        ← 用户输入（循环开始前）
[1] assistant: [tool_use: bash("ls")]      ← 模型决定调工具（run_one_turn 第一步）
[2] user:      [tool_result: "a.py b.py"]  ← harness 执行后喂回（run_one_turn 第二步）
[3] assistant: [text: "当前目录有 a.py..."] ← 模型最终回答（run_one_turn 第二次调用）
```

每次 `run_one_turn` append 1~2 条消息：模型回复（assistant）+ 工具结果（user）。最后一轮只 append 一条 assistant（纯文本回答）。

### 6. run_bash 的安全边界：做了什么、没做什么？

做了：
- 字符串匹配拦截危险命令（`rm -rf /`、`sudo`、`shutdown` 等）
- 120 秒超时保护
- 输出截断到 50000 字符，防止撑爆上下文

没做：
- 没有沙箱隔离（命令直接在当前系统执行）
- 字符串匹配很容易绕过（比如 `rm -rf  /` 多个空格就绕了）
- 没有文件路径限制（可以读写系统任意位置）
- 没有网络限制

这是教学级的安全措施，生产级需要容器/沙箱。

## 影响层 — 更大的上下文

### 7. 这个循环和 Claude Code CLI 的关系——简化了什么、省略了什么？

这是 Claude Code CLI 的最小骨架模型。核心循环（调模型 → 执行工具 → 喂回结果）是一样的。省略了：权限系统、hook 系统、记忆/上下文压缩、子 Agent、任务管理、团队协作、MCP 插件、Cron 调度、worktree 隔离等。这些就是后续 s02-s19 逐个叠加的内容。

### 8. 后续 18 个 session 叠加了什么？

在同一个循环结构上逐层叠加：
- s02 多工具分发、s03 TodoWrite 规划、s04 子 Agent、s05 Skill 注入、s06 上下文压缩
- s07 权限系统、s08 Hook、s09 记忆、s10 系统提示词、s11 错误恢复
- s12 任务系统、s13 后台任务、s14 Cron 调度
- s15 Agent 团队、s16 协议 FSM、s17 自治 Agent、s18 Worktree 隔离、s19 MCP 插件

每个 session 加一个机制，循环本身的 `while run_one_turn(state)` 结构始终不变。

## 关键概念

### Harness（线束）

Harness 不干活，它把干活的人（OS、模型）串起来。具体来说：

```
Model: "我要执行 ls"
   ↓
Harness (Python 程序):  收到 tool_use → 调用 subprocess.run("ls")
   ↓
OS (macOS/Windows):  实际执行 ls，返回文件列表
   ↓
Harness:  拿到 OS 的输出 → 包装成 tool_result → 塞进 messages
   ↓
Model:  收到 tool_result，继续思考
```

Harness 是调度者/编排者，不是执行者。这也是为什么这个项目叫 "harness engineering"（线束工程）。
