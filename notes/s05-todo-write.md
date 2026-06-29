# S03 TodoWrite 学习笔记

## 核心总结

添加了一个会话计划工具（TodoWrite），让模型拆解任务、聚焦当前步骤、层层推进完成任务。harness 通过往消息流里动态塞提醒来引导模型刷新计划——这是后续 session 大量使用的核心手法。

## 问题层

### 1. TodoWrite 解决什么问题？

没有计划时，步骤多了、运行时间长了，模型会分不清当前到哪一步、什么是重点。TodoWrite 让模型把计划外化成结构化数据，而不是全靠上下文记忆。

### 2. "会话计划"和"持久化任务"的区别

- 会话计划（TodoWrite）：只存在于当前会话的内存中，会话结束就没了
- 持久化任务（后面的 TaskManager）：保存到本地文件（`.tasks/task_N.json`），跨会话存在

## 方案层

### 3. 全量覆盖 vs 增量更新

每次调用 todo 工具都是用新列表**完整替换**旧列表（`self.state.items = normalized`），不是增量操作。

原因：模型擅长一次性输出完整结构，不擅长精确操作索引和做增量 diff。全量覆盖让模型用最自然的方式工作——"我重新想一遍当前计划是什么"。

降低"忘步骤"风险的机制：
- `render()` 返回完整计划文本作为 tool_result，模型每次都能看到完整现状再重写
- `max 12` 保持计划短，进一步降低遗忘概率

### 4. 同一时间只能有一个 in_progress

强制模型聚焦——同时干多件事会分散注意力、降低完成质量。

### 5. 计划提醒机制

- 用 `used_todo` 标记当前轮是否调用了 todo 工具
- 调了就重置 `rounds_since_update = 0`，没调就 `+1`
- 超过 `PLAN_REMINDER_INTERVAL`（3 轮）就在 tool_result 前面插入提醒文本
- `results.insert(0, ...)` 把提醒放在最前面，让模型先看到

### 6. todo 工具的接入方式

和 s02 完全一样——TOOL_HANDLERS 加一行 `"todo": lambda **kw: TODO.update(kw["items"])`，TOOLS 加 JSON Schema 定义。循环不用改。

## 影响层

### 7. harness 通过操控消息流动态干预模型行为

提醒机制的本质：harness 假装用户说了 `<reminder>Refresh your current plan...</reminder>`，塞进 user 消息里。模型不知道这是 harness 塞的，会认真对待。

和 system prompt 的区别：system prompt 是静态的（开头定好不变），塞话是动态的（根据运行时状态决定塞不塞、塞什么）。

这是 harness engineering 的核心手法：**通过运行时操控消息流，动态引导模型行为。** 后续的权限提醒、hook 注入、记忆注入都用这个模式。
