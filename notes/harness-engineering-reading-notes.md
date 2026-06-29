# Harness Engineering 读书笔记

> 来源：《Harness Engineering — Claude Code 设计指南》(book1) + 《Claude Code 和 Codex 的 Harness 设计哲学》(book2)
> 作者：@wquguru / agentway.dev
> 阅读日期：2026-06-11 初读，2026-06-22 整理

---

## 一句话总结

**Harness Engineering 关心的是：在模型并不可靠的前提下，系统仍然能表现出工程系统应有的行为。**

书里的核心立场：**Prompt 决定它怎么说话，Harness 决定它怎么做事。**

---

## 九条原则（book1 主线）

每章提炼一条，最后第九章汇总为十条。

| # | 原则 | 来源章节 |
|---|------|--------|
| 1 | 代理系统的关键能力是**约束执行** | 第1章 为什么需要 Harness Engineering |
| 2 | Prompt 的价值在于是否被纳入一套清楚的**控制结构** | 第2章 Prompt 不是人格 |
| 3 | 代理系统的核心能力是维持**可恢复的执行循环** | 第3章 Query Loop |
| 4 | 工具是**受管执行接口**；权限是代理系统的**基本器官** | 第4章 工具、权限与中断 |
| 5 | 上下文是**工作内存**，治理目标是支持系统继续工作 | 第5章 上下文治理 |
| 6 | 可靠性体现在错误后仍能维持**可解释、可限界、可继续**的执行秩序 | 第6章 错误与恢复 |
| 7 | 多代理依赖清晰分工：研究、实现、验证、综合各自处在不同约束容器里 | 第7章 多代理与验证 |
| 8 | 团队落地关键是先把**可接受边界、验证标准和高频工作流**固定下来 | 第8章 团队落地 |
| 9 | 十条原则汇总（见下方） | 第9章 |

---

## Harness 的五层结构（第1章）

Claude Code 的 harness 不是单层，而是逐层叠加：

1. **受约束的会话系统** — system prompt 分层拼装，不是一段"万能提示词"
2. **代理依赖持续循环** — query loop 管理跨轮状态，不是单次问答
3. **工具调用必须服从调度** — 先分批再执行，并发不能破坏因果秩序
4. **最危险的工具配最细的规矩** — Bash 是风险放大器，需要特殊高压治理
5. **错误属于主路径** — 不是用 catch 兜底，而是按结构性条件处理

**控制平面三条硬约束（invariants）：**
- prompt 必须分层（身份 ≠ 运行时约束）
- 工具受调度纪律约束
- 可恢复错误进入主路径（recover 或 terminate_clean）

---

## Prompt 是控制平面，不是人格（第2章）

**核心洞察**：很多人把 prompt 当人设文案，Claude Code 把它当运行时协议。

- prompt 是**分层**的（default / append / agent / CLAUDE.md / memory）
- 有明确**优先级链**：override > coordinator > agent > custom > default
- 与 memory、CLAUDE.md、agent instructions、MCP instructions 组成完整控制平面
- 有缓存和动态 section 机制（systemPromptSection vs DANGEROUS_uncachedSystemPromptSection）
- 用户可以覆盖 prompt，但不能跳过这套结构

**判断标准**：删掉某段 prompt 后，系统行为会不会出现结构性变化？如果会，说明它真是控制面；如果不会，可能只是装饰。

---

## Query Loop：代理系统的心跳（第3章）

**query() 只是壳，真正重要的是 queryLoop()。**

queryLoop 的跨轮状态：messages、toolUseContext、autoCompactTracking、maxOutputTokensRecoveryCount、hasAttemptedReactiveCompact、pendingToolUseSummary、stopHookActive、turnCount、transition。

**循环每轮做的事**（调用模型之前）：
1. memory 预取
2. skill discovery
3. 截取 compact boundary 之后的有效消息
4. 应用 tool result budget
5. history snip
6. microcompact
7. context collapse
8. autocompact

**关键设计**：把"上下文治理"放在"模型推理"之前。先整理现场再开工。

**停止条件矩阵**：区分 stream 正常结束+tool_use、无 tool_use 进入 stop hooks、用户中断、prompt-too-long 恢复、max-output-tokens 恢复、stop hook 阻塞导致重进循环、API 错误直接返回。

---

## 工具、权限与中断（第4章）

**一旦模型开始调用工具，问题的性质就变了**：从"回答得不够好"变成"执行造成实际破坏"。

### 工具调度
- 先按并发安全性分批（partitionToolCalls + isConcurrencySafe）
- 并发路径里用 contextModifier 缓存，再按原始 block 顺序回放
- 即便执行是并发的，语义上的上下文演化仍然保持确定顺序

### 权限三态
- **allow**：真正放行
- **deny**：直接拒绝
- **ask**：进入协调器/classifier/交互式审批
- 不变式：ask 永远不得自动升级为 allow；deny 对同一 tool_use_id 不得重试为 allow

### 中断是一等语义
StreamingToolExecutor 把中断当成和执行本身同样重要的语义。系统不仅要知道工具能不能开始，还要知道它被打断时如何收场、如何补齐结果、是否允许新消息插入。

### Bash 为什么永远比别的工具更可疑
Bash 几乎不受领域边界约束，所以 Claude Code 对它特殊高压治理：prompt 层面大量明确规则 + 权限层面 classifier 与规则匹配 + subcommand 数量上限。

---

## 上下文治理：Memory、CLAUDE.md 与 Compact（第5章）

**"信息越多，系统越聪明"是一个危险的神话。** 上下文不是图书馆，是预算。

### CLAUDE.md 体系
- managed memory（/etc/claude-code/CLAUDE.md）
- user memory（~/.claude/CLAUDE.md）
- project memory（项目根目录 CLAUDE.md、.claude/CLAUDE.md、.claude/rules/*.md）
- local memory（CLAUDE.local.md）
- 按优先级和目录距离加载

### MEMORY.md 是索引，不是日记本
- 入口文件必须短（MAX_ENTRYPOINT_LINES = 200, MAX_ENTRYPOINT_BYTES = 25,000）
- 正文写进独立文件，MEMORY.md 只放一行指针
- 长期记忆必须分成"入口"和"正文"

### Session Memory
模板栏目：Current State / Task specification / Files and Functions / Workflow / Errors & Corrections / Learnings / Key results / Worklog。目标是压缩出未来继续干活所需的骨架，不是完整复刻对话。

### Compact
- 预留输出预算（MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20,000）
- 缓冲区（AUTOCOMPACT_BUFFER_TOKENS = 13,000）
- 连续失败熔断（MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3）
- compact 之后恢复工作语义：清空 readFileState、重新生成 file attachments、补回 plan/skills/deferred tools 的 delta attachment
- **compact 是受控重启，不是聊天总结**

---

## 错误与恢复（第6章）

**工程世界最不值得相信的话，就是"正常情况下"。**

### prompt too long 恢复路径（分层）
1. 先走 collapse drain（便宜）— recoverFromOverflow()
2. 再走 reactive compact（一次性）— tryReactiveCompact()
3. 都试过了还不行 → 跳过 stop hooks，直接 surface error

### max_output_tokens 恢复
1. 先提 cap（maxOutputTokensOverride），不插入 meta message，让模型继续
2. cap 到最大 → 追加 meta user message，要求"直接继续，不要道歉，不要 recap"

### 防死循环
- hasAttemptedReactiveCompact：reactive compact 试过就不再试
- stop hooks 防死循环：prompt too long 恢复后如果 stop hooks 再触发 → 跳过 hooks，surface error
- autocompact consecutive failure ≥ 3 → 熔断

### 中断也是恢复的一部分
中断不只是"用户不想看了"，是需要正确收尾的状态转移。流式打断会消费 StreamingToolExecutor 剩余结果，为悬空 tool_use 生成 synthetic tool_result。

**恢复分层原则**：不用一把重锤；恢复逻辑必须防自回环；自动恢复要可计数可限次可熔断。

---

## 多代理与验证（第7章）

### forked agent 的第一原则是 cache-safe
子代理必须和父代理共享 CacheSafeParams（systemPrompt、userContext、systemContext、toolUseContext、forkContextMessages），否则 prompt cache 失效，成本和延迟立刻变坏。

### 状态隔离
默认隔离所有 mutable state。readFileState 先 clone，abortController 生成 child controller，setAppState 默认 no-op。只有明确 opt-in 才共享。

### 协调者模式
coordinator 的要求：Always synthesize — 当 worker 回报研究结果后，协调者必须先读懂，再写出具体 prompt；不要说"based on your findings"，不要把理解继续外包给 worker。

### 验证必须独立
- 验证与实现必须角色分离
- verification 的目标是证明代码有效，而不只是确认代码存在
- prompt 里明确分层为"implementation 自证 + verification 作为独立 QA"

### agent 生命周期
- SubagentStart / SubagentStop 两类 hook
- 父 abort → 传播到子 abort
- 任务结束后 evict output、更新状态、解除 cleanup 注册

---

## 团队落地（第8章）

**个人顺手，不代表团队就能稳定复用。**

### 团队起步四件事
1. 哪些任务允许 agent 直接参与
2. 哪些改动必须经过人工 review
3. 改完至少要跑什么验证
4. 哪些资源一律不能碰

### 团队分阶段落地清单
- 第1周：分层 CLAUDE.md 生效；统一验证定义；禁区写入仓库级硬约束；按后果划 allow/deny/ask
- 第2周：首批 ≤3 个 skill 上线；定义"完成但带已知问题"的允许条件
- 第3周：stop/post-tool-use hook 落地；stale memory 月度维护流程；基线复盘（Git diff/PR/CI）无缺口
- Gate：新人首次使用无需高手旁站 = 制度成熟

### CLAUDE.md 的价值
适合承载**稳定规则**：代码库级硬约束、统一验证口径、协作纪律、输出风格。不适合堆进去的：频繁波动的临时流程、操作细节、脚本步骤。

### approval 按风险分层
- 读文件、列目录、纯分析 → 低风险
- 改工作区、改配置、执行写操作 → 中风险
- 推 Git、打外网、访问敏感环境 → 高风险

### hook 是高级能力
对多数普通团队来说，第一步是仓库说明文件、code review 规则、CI 和测试要求。hook 放在基础治理稳住之后再引入。

---

## Harness Engineering 十条原则（第9章）

1. **把模型当不稳定部件，不要当同事**
2. **Prompt 是控制面的一部分**（和 runtime/tool schema/memory/hook 一起）
3. **Query loop 才是代理系统的心跳**（输入治理/流式消费/工具调度/恢复分支/停止条件）
4. **工具是受管执行接口**（必须被调度/被授权/被中断/被补账）
5. **上下文是工作内存**（该分层治理，compact 保住继续工作的语义底座）
6. **错误路径就是主路径**（恢复/熔断/限次/防死循环，设计时就要有）
7. **恢复的目标是继续工作**（截断后续写，不是 recap 不是道歉）
8. **多代理的意义是把不确定性分区**（隔离状态/分离角色/coordinator 收束理解）
9. **验证必须独立，不能让系统自己给自己打分**
10. **团队制度比个人技巧重要**（分层 CLAUDE.md / 明确 approval / 可执行 skill / 生命周期 hook / 可追踪 transcript / 统一验证定义）

---

## Book2：Claude Code vs Codex 的 Harness 设计哲学

### 一句话总结
- book1 解释的是：为什么一个可控的 agent 必须采用这种结构
- book2 解释的是：当两套系统都认真做 harness 时，它们为什么会长得不一样

### 共同点
都承认模型不可信，都很会调用工具，都不肯把模型当作一个可以放任自流的部件。

### 核心差异：秩序安放的位置不同

| 维度 | Claude Code | Codex |
|------|-------------|-------|
| **控制面** | 动态 prompt 装配线（每轮重算） | 带编号的公文系统（结构化 fragment） |
| **连续性** | 压进主循环（query loop 心跳纪律） | 拆进 thread/rollout/state bridge |
| **工具与权限** | 运行时编排和危险动作约束 | 工具 schema、审批参数和策略引擎 |
| **本地治理** | 收编成现场记忆（CLAUDE.md/memory/skills） | 结构化注入和事件系统（instructions/hooks） |
| **多代理与验证** | 运行时职责分区，验证独立于实现阶段 | 显式委派、持久状态和工具化协作 |

### Claude Code 的气质
更像从**运行时事故经验**里塑出来的系统。优先解决连续性、恢复和现场治理。关键词：query loop / compact / tool orchestration / interrupt / permission ask/allow/deny / forked agent lifecycle。

### Codex 的气质
更像从**显式结构设计**里塑出来的系统。优先解决控制层命名、策略表达、边界清晰和可组合性。关键词：instruction fragment / thread / approval policy / tool schema / exec policy / sandboxing / state bridge。

### 后来者的选择
- 如果你的问题是**长会话容易失控、恢复路径很脆、验证总被跳过** → 先学 Claude Code 的运行时纪律
- 如果你的问题是**规则来源太散、权限边界不清、工具契约不稳定、团队很难复制同一套行为** → 先学 Codex 的显式控制层
- **不该照抄产品，而该识别自己的主要不确定性**

---

## 附录：检查清单速查

### Agent Runtime 设计清单
- 是否存在明确的 query loop？
- 是否有跨轮状态对象？
- 是否把模型输出当事件流处理？
- 是否能在中断时补齐未完成的 tool result？
- 是否区分完成/失败/恢复/继续？
- 是否为长会话设计了 context budget？

### Prompt 设计清单
- 是否把身份描述、行为规则、工具约束、输出纪律分开组织？
- 是否明确 prompt 的优先级来源？
- 是否把危险动作、越权动作、验证纪律写成明确规则？
- 是否允许团队稳定维护，而非每次修 bug 都往 prompt 里再塞一段话？

### Error Recovery 设计清单
- 可恢复错误是否先进入恢复分支？
- 恢复路径是否分层（先低破坏性，再高破坏性）？
- 是否有防止 reactive compact / stop hooks / retry 相互咬住的保护？
- max_output_tokens 后是否优先续写而非 recap？
- 自动恢复是否有计数、限次和熔断？

### Multi-Agent 设计清单
- fork 时是否考虑 prompt cache 共享和 cache-safe 参数一致性？
- 子代理默认是否隔离 mutable state？
- 是否区分 research / implementation / verification / synthesis 角色？
- coordinator 是否真正承担综合理解？
- verification 是否独立于 implementation？
- agent 生命周期是否可观测、可中止、可清理？
- 父 abort 是否能传播到子代理？

---

## 个人感想

这两本书最大的价值不在于告诉你 Claude Code 源码里有什么函数，而在于从源码结构中提炼出**为什么系统必须是这个形状**。核心洞察就一条：模型不可靠，所以真正需要设计的不是更聪明的 prompt，而是更可靠的约束结构。

最触动的三句话：
- "Harness 比激情重要，制度比聪明重要，验证比自信重要"
- "一个会道歉的系统，不一定成熟。一个知道何时不该开始、何时该重试、何时该中止、何时该准确汇报失败的系统，才更接近成熟"
- "你的主要不确定性在哪，你准备把秩序安放在哪"
