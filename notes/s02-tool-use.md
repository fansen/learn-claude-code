# S02 Tool Use 学习笔记

## 核心总结

s02 的循环没变，只是加了工具分发表。通过 dict 查表把工具名映射到处理函数，加新工具不用改循环。同时加了路径安全和消息规范化来防错。

## 问题层

### 1. s02 相比 s01 多了什么？循环本身变了吗？

循环本身（`while True` + `stop_reason` 退出）没变。新增了：
- 4 个工具（bash/read_file/write_file/edit_file）替代 s01 的单一 bash
- 工具分发表 `TOOL_HANDLERS`
- 路径安全函数 `safe_path()`
- 消息规范化函数 `normalize_messages()`
- 并发安全分类标记

### 2. 为什么需要多种工具，只有 bash 不够吗？

bash 能做一切但不够精确。read/write/edit 给模型更结构化的操作方式——模型不用拼 shell 命令，直接传路径和内容，出错概率更低，harness 也更容易做安全检查。

## 方案层

### 3. 工具分发表 TOOL_HANDLERS 的设计

key 是工具名（字符串），value 是处理函数（lambda）。分发过程：

```python
handler = TOOL_HANDLERS.get("read_file")   # 用工具名查到处理函数
output = handler(**{"path": "main.py"})     # 把模型传的参数展开，调用函数
```

用 dict 而不是 if-else 的好处：加新工具只需加一行，不改分发逻辑。agent_loop 完全不用动。

加一个新工具只需改两处：
1. `TOOL_HANDLERS` 加一行处理函数
2. `TOOLS` 加一个 JSON Schema 定义让模型知道

### 4. safe_path() 路径安全——防的是路径穿越攻击（path traversal）

模型可能生成 `read_file(path="../../etc/passwd")`。safe_path 的做法：

```python
path = (WORKDIR / "../../etc/passwd").resolve()  # 解析为 /etc/passwd
path.is_relative_to(WORKDIR)                      # 不在工作区内 → 抛异常
```

没有这个检查，模型能读写系统任意文件。

### 5. normalize_messages() 的三件事

1. **剥元数据** — 清理 harness 内部用的 `_` 开头字段，API 不认识这些

   **什么是元数据？** 后续 session（权限、hook 等）会往消息的 content block 里塞 harness 自己用的内部字段，比如 `_permission: "approved"`、`_exec_time_ms: 230`。这些是 harness 不同模块之间传递信息用的内部记录。

   **为什么 API 不认识？** Anthropic API 对消息格式有严格 schema，只接受它定义过的字段（`type`、`text`、`tool_use_id`、`content` 等）。多传一个 `_permission`，API 直接报错。

   **流程：** harness 内部处理时往 block 加 `_` 字段做记录 → 发 API 前 normalize_messages() 把 `_` 字段全部剥掉 → 发给 API 的是干净的标准格式消息。

   s02 本身还没有代码产生 `_` 字段，这是预防性设计，后面 s07（权限）、s08（hook）等 session 会用到。
2. **补孤立 tool_result** — 如果有 tool_use 没有对应的 tool_result（比如 harness 中途崩溃），API 会直接 400 报错。补一个 `(cancelled)` 占位 result 防止报错
3. **合并同角色消息** — API 要求 user/assistant 严格交替，连续同角色消息要合并

### 6. 并发安全分类 CONCURRENCY_SAFE / UNSAFE

在 s02 中没有被实际使用，是为后续 session 准备的声明式标记。

- `CONCURRENCY_SAFE = {"read_file"}` — 只读工具可以安全并行
- `CONCURRENCY_UNSAFE = {"write_file", "edit_file"}` — 写入工具必须串行

场景：模型一次调用 read("a.py") + read("b.py") + write("a.py")，read 可以并行，但 read 和 write 同一个文件不能并行。

## 影响层

### 7. 工具定义（TOOLS）和工具处理（TOOL_HANDLERS）的分离

- `TOOLS`（JSON Schema 列表）是给模型看的"菜单"——模型据此决定调用哪个工具、传什么参数
- `TOOL_HANDLERS`（dict）是 harness 内部的执行逻辑——模型看不到

两者通过工具名关联，但职责完全分离。

### 8. 这个分发模式怎么影响后续扩展？

后续 session 加工具（TodoWrite、skill_load、task_create 等）只需往 TOOL_HANDLERS 和 TOOLS 里各加一条，循环逻辑不用改。这就是 s02 的核心设计——**让循环与工具解耦**。
