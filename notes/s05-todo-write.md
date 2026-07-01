# S05 TodoWrite 学习笔记

> 编号说明：本文按**官网/新版**课程序列（TodoWrite = s05）。对应仓库旧版脚本是 `agents/s03_todo_write.py`。两套轨道编号不一致，机制本身相同。

## 问题层 — 是什么 / 为什么存在

### 1. TodoWrite 解决什么问题？

一个 agent 做多步任务（如"重构模块"要改 5 个文件），循环一轮一轮跑，每轮模型都要决定"下一步干嘛"。问题在于：**模型是无状态的**，每轮它只看到一整段越来越长的对话，要靠"重读历史"来重建"我做到哪了"。上下文越长，注意力越被冲散，越容易漂移、忘步骤、甚至跑偏最初的目标。

TodoWrite 让模型把计划**外化成结构化数据**，而不是全靠上下文记忆。

### 2. 核心洞察：「把计划放在模型脑子外面」

文件开头点破：*keep the current session plan outside the model's head*。外部化的两层好处：

1. **稳定可靠**：写下来的计划是一份明确的书面清单，每次出现在对话里都是准的；"记在脑子里"是模糊记忆，随上下文变长而退化。写下来 = 强制承诺 + 稳定参照。
2. **可被 harness 管控**：计划存在 `TodoManager` 外部对象里，harness 能对它**强制规则**（最多一个 in_progress、最多 12 项）。这些规则模型自己守不住，交给外部代码守。

一句话：外部化 = 从「靠模型记」变成「代码存 + 代码管」。

### 3. 会话计划 vs 持久任务图

- **会话计划（TodoWrite）**：只存在于当前会话的内存，会话结束就没了。管"这次专注"。
- **持久任务图（后面的 TaskManager，s12）**：保存到本地文件（`.tasks/task_N.json`），跨会话存在、带依赖关系。管"长期编排"。

别混。本节是轻量的会话计划。

## 方案层 — 怎么实现的 / 设计取舍

### 4. 全量重写 vs 增量更新

todo 工具处理函数就一行：`TODO.update(kw["items"])`。模型传的是**整个计划的完整列表**，`update()` 验证后**完整替换**旧计划（`self.state.items = normalized`），不是"改第2项状态"这种增量操作。

为什么全量重写更好？
- **简单**：模型擅长一次性输出完整结构，不擅长精确操作索引、做增量 diff。
- **不失同步**（关键）：模型无状态。增量式要求"模型以为的状态"和"harness 存的状态"时刻同步——模型说"标记第2项完成"，万一数错/顺序不一致就错标了。全量重写下，模型每次直接声明"完整的当前真相"，harness 无脑覆盖，**两边永远不可能不同步**。

配套降低"忘步骤"风险：`render()` 把完整计划作为 tool_result 返回，模型每次都能看到完整现状再重写；`max 12` 保持计划短。

### 5. 三个不变量（update 强制）

- `len(items) <= 12`：计划要短。
- 每项 `content` 必填、`status` 必须是 pending/in_progress/completed 之一。
- **同一时间最多一个 in_progress**（`in_progress_count > 1` 直接报错）。

为什么强制"一个 in_progress"？① 给模型**专注纪律**——同时干多件事会分散注意力、降低质量；② 给人**可读性**——任何时刻看列表都能一眼知道"当前正在做哪一步"。

### 6. 三个类的分工

- `PlanItem`：单个步骤（content / status / active_form）。
- `PlanningState`：整体状态（items + rounds_since_update）。
- `TodoManager`：操作逻辑（update 全量替换 / render 渲染 / reminder 判断该不该提醒）。

`render()` 画成人读的样子（`[ ]` pending、`[>]` in_progress、`[x]` completed，末尾 `(x/n completed)`）。`active_form` 是进行中步骤的"现在进行时"标签，只在 in_progress 时显示，纯为可读性。

### 7. 计划提醒机制（harness 的 nudge）

场景：模型开了计划后闷头干活，好几轮忘了回来更新，计划"过期"。harness 怎么发现并纠正？

在 `agent_loop` 里三段配合：
- 用 `used_todo` 标记这轮是否调用了 todo 工具。
- 调了 → `rounds_since_update = 0`（清零）；没调 → `+1`（累加）。
- `reminder()` 判断：**计划非空 且 rounds_since_update >= PLAN_REMINDER_INTERVAL(3)** 才返回提醒文本，否则 None。
- 触发时 `results.insert(0, {"type": "text", "text": reminder})`——插到 results **最前面**。

两个细节：
- **为什么空计划不提醒**：计划为空 = 任务简单到模型没建列表，这时催"刷新计划"既没意义又很吵。
- **为什么插到最前**：content 块按顺序读，插最前模型先撞见提醒，不会被一大坨工具输出淹没。位置=优先级。

### 8. 一条 user 消息能混装 text 块和 tool_result 块

`results` 列表里其他都是 `{"type":"tool_result",...}`，提醒却是 `{"type":"text",...}`——一条 user 消息的 content 是个**块列表**，可以混装不同类型的块。这正是 harness 能"夹带"提醒的原因：往这个列表里多塞一个 text 块。

### 9. todo 工具的接入方式

和 s02 完全一样：`TOOL_HANDLERS` 加一行 `"todo": lambda **kw: TODO.update(kw["items"])`，`TOOLS` 加 JSON Schema 定义。**循环本身不用改。**

## 影响层 — 更大的上下文

### 10. 这就是真实 Claude Code 的 TodoWrite

你干多步活时滚动的那个待办列表，底层就是这套。本质是**上下文工程（context engineering）**：把"我做到哪了"这种易失状态从模型脑子里挪到外部结构，再可靠地喂回上下文，对抗长对话的注意力衰减。

### 11. harness 通过操控消息流动态干预模型行为

提醒机制的本质：harness 往 user 消息里塞了一句 `<reminder>Refresh your current plan...</reminder>`，模型不知道这是 harness 塞的，会认真对待。

和 system prompt 的区别：system prompt 是**静态**的（开头定好不变）；塞话是**动态**的（根据运行时状态决定塞不塞、塞什么）。这是 harness engineering 的核心手法：**通过运行时操控消息流，动态引导模型行为。** 后续的权限提醒、hook 注入、记忆注入都用这个模式。

### 12. nudge 模式：不硬控，只轻推

注意 harness 没有**强制**模型更新计划（做不到也不该），而是**轻推**——连续几轮没更新就夹一句提醒。这种"不硬控、只提醒"的模式在 s07 也见过（连续拒绝 3 次提醒切 plan 模式）。harness 常用软引导让模型自己回到正轨。

## 关键概念

### 外部化状态（Externalized State）

把易失、易漂移的状态从"模型的记忆"搬到"harness 的数据结构"：

```
模型脑子里（不可靠）            TodoManager（可靠）
──────────────              ──────────────
随上下文变长而退化      →      稳定书面清单，每次都准
规则守不住              →      代码强制（一个 in_progress / ≤12 项）
靠重读历史重建          →      全量重写，声明完整真相
```

这是 harness engineering 的通用招式：把易变、关键的状态挤到模型外面，让核心保持可控。
