# S06 Subagent 学习笔记

> 编号说明：本文按**官网/新版**课程序列（Subagent = s06）。对应仓库旧版脚本是 `agents/s04_subagent.py`。两套轨道编号不一致，机制本身相同。

## 问题层 — 是什么 / 为什么存在

### 1. 子 Agent 解决什么问题？

有些子任务会产生**海量噪音 token**——比如"搞清楚这个大项目的架构"，要读几十个文件、跑一堆 grep。如果主 Agent 自己在当前对话里干这活，几十个文件的内容全堆进它的上下文，**还没开始真正的任务，注意力就被探索垃圾冲散了**。这是 s05（TodoWrite）那个"上下文变长→注意力衰减"问题的**放大版**。

### 2. 核心洞察：`messages=[]` 提供上下文隔离

派一个**全新上下文**的子 Agent 去干这活，它在自己独立的对话里折腾几十轮，干完**只返回一段摘要**给主 Agent。中间那几十个文件内容、grep 输出全部留在子 Agent 自己的上下文里、用完即弃。

这是一种**压缩**：子 Agent 烧了几万 token 的探索，主 Agent 只接收几百 token 的结论。主 Agent 上下文始终干净。

### 3. 隔离 ≠ 沙箱（最关键的边界）

子 Agent 和主 Agent **共享文件系统**（同一个 WORKDIR）。要切清楚两样完全不同的东西：

| | 共享/隔离 | 含义 |
|---|---|---|
| **文件系统（硬盘）** | **共享** ✅ | 子 Agent `write_file` 真的会写到磁盘，主 Agent 之后能读到 |
| **上下文（对话历史 messages）** | **隔离** ❌ | 子 Agent 的 sub_messages 独立、用完丢弃，主 Agent 永远看不到 |

**隔离 ≠ 把子 Agent 关进小黑屋。** 恰恰相反，它能真真切切读写文件、改代码、跑命令，**副作用全是真的**。被隔开的只有"对话上下文"这一样。

## 方案层 — 怎么实现的 / 设计取舍

### 4. 隔离机制简单到不可思议

不需要沙箱、进程 fork。看 `run_subagent` 第一行：

```python
def run_subagent(prompt):
    sub_messages = [{"role": "user", "content": prompt}]   # 全新、独立的消息数组
    for _ in range(30):                                     # 自己跑 mini 循环
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM,
            messages=sub_messages, tools=CHILD_TOOLS, ...)  # 用自己的 messages
        sub_messages.append(...)          # 结果只进 sub_messages，不进父的 messages
        ...
    return "".join(b.text for b in response.content if hasattr(b, "text"))  # 只返回摘要
```

**隔离 = 一个全新的、跟父 Agent 无关的 `sub_messages` 局部变量。** 子 Agent 在里面折腾几十轮，函数一返回，`sub_messages` 随栈销毁，只有最后 `return` 的摘要活着传回。没有 fork、没有沙箱、没有 IPC——就是"另起一个 messages 列表"。

### 5. 进程内隔离 + 同步阻塞

这是**同一个 Python 进程**里的两个 messages 列表，不是 OS 进程 fork。真实 Claude Code 有 5 种后端（in-process、tmux、iTerm2、fork、remote），教学版只用最简单的进程内。

代价：教学版是**同步**的——父 Agent 调 `run_subagent` 时会**阻塞等它跑完**，并非真并行。真正的并行子 Agent 要靠别的后端。

### 6. 30 轮安全上限

`for _ in range(30)` 是兜底：万一子 Agent 陷进"一直调工具、永不给最终答复"的循环，没有上限就永远回不来。有了它，最多 30 轮强制跳出，从最后一次 response 抠出文本返回。

### 7. 工具过滤：防递归 spawn

- `CHILD_TOOLS`：bash/read/write/edit 四个基础工具。
- `PARENT_TOOLS = CHILD_TOOLS + [task]`。

即 **`CHILD_TOOLS` 里没有 `task` 工具**。为什么？给了子 Agent `task`，它就能再派孙 Agent，孙 Agent 再派……**指数级/无限递归**。教学版用最简单的办法防住：直接不给子 Agent 这个工具。（真实 CC 允许嵌套但有深度限制；教学版一刀切。）

### 8. 父 Agent 分发 task —— 对父完全透明

父 Agent 循环里唯一特殊处理的是 `task` 工具：命中就调 `run_subagent(prompt)`，**返回的摘要被当成一条普通 `tool_result` 塞回父的对话**。

从父 Agent 视角，`task` 和 `bash` 没本质区别——都是"调个工具、拿个字符串结果"。它根本不知道那个字符串背后是另一个 Agent 烧了 30 轮换来的。这就是隔离的优雅：**对父完全透明**。

### 9. AgentTemplate：Agent 定义从哪来

顶部 `AgentTemplate` 类解析 `.md` 文件的 frontmatter（name/tools/model 等）。真实 Claude Code 从 `.claude/agents/*.md` 加载子 Agent 定义（项目里的 Explore、code-reviewer 就是这么定义的）。

注意：**这个类在教学版里定义了但主流程没真正用它**——和 s07 的 `is_workspace_trusted` 一样，是个"指路用"的扩展点，说明真实系统的子 Agent 是可配置定义的，而教学版为简单直接硬编码一个 `SUBAGENT_SYSTEM`。

## 影响层 — 更大的上下文

### 10. 这就是真实 Claude Code 的 Task / Agent 工具

Explore、codex-rescue、general-purpose 这些子 Agent，底层都是这个模式：主 Agent 派一个全新上下文的子 Agent 去干独立的活，只收摘要。真实版更强（5 后端、能真并行、工具可细粒度过滤、agent 从 `.md` 定义）。

### 11. 上下文隔离是核心「扩展手段」

它解决**上下文这个稀缺资源**的问题：
- **防污染**：噪音活儿隔离出去，主线保持清醒。
- **并行**：多个子 Agent 同时探索不同模块（真实版才有）。
- **专职化**：每个子 Agent 可有自己的系统提示词/工具集，变成"专家"（只读的 Explore、专门 review 的 agent）。

### 12. 子 Agent 一次性无状态 vs s15 持久团队

子 Agent 是**一次性、无状态**的：派出去 → 干完 → 摘要 → 销毁，不留记忆。s15 的 agent 团队是**持久 teammate**：能跨任务积累领域上下文、互相发消息。一个是"临时工"，一个是"长期同事"。

### 13. 代价：子 Agent 带失忆出生

因为子 Agent 看不到父的对话历史，它只知道父塞给它的那段 `prompt`。所以**派活的质量全压在 prompt 上**：父必须把子 Agent 需要的所有背景一次性写进 prompt，否则子 Agent 会因"不知道上下文"而跑偏。

这就是为什么"派子 Agent"是门手艺——每次派活都要写一大段自包含的任务描述。隔离保护了主线，代价是子 Agent 的"无知"，弥补这个无知的责任落在 prompt 上。

## 关键概念

### 上下文隔离（Context Isolation）

把一段会产生大量噪音的工作，挪到一个用完即弃的独立上下文里做，只把结论带回主线：

```
父 Agent（干净）                子 Agent（全新 messages=[]）
messages=[...]                 messages=[{prompt}]
   │                              │
   │  task(prompt) ──────────────▶│  跑 mini 循环，读几十文件、试错
   │                              │  （噪音全堆在这里）
   │  ◀──────── 只回摘要 ─────────│
   │  (当普通 tool_result)         └─ 上下文销毁
   ▼
父上下文只多了一段干净结论
```

共享文件系统（副作用真实），隔离对话上下文（噪音不回流）。这是 harness 用来保护"上下文"这个稀缺资源的核心招式。
