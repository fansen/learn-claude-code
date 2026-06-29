# S03 Permission System 学习笔记

> 编号说明：本文按**官网/新版**课程序列（permission = s03）。对应仓库旧版脚本是 `agents/s07_permission_system.py`。两套轨道编号不一致，机制本身相同。

## 问题层 — 是什么 / 为什么存在

### 1. 权限系统在解决什么问题？

s01–s02 的循环对模型给的工具调用是**无条件执行**：模型说跑 `rm -rf /`，harness 就真的跑。模型拥有完全自主权，但没有任何东西在「模型想做」和「真正执行」之间把关。权限系统就是插在这两者之间的一道管道，对每个工具调用先裁决（允许 / 拒绝 / 问人）再执行。

### 2. "Agency 来自模型" 是好事，为什么还要专门拦它？

自主性本身是价值，但**未受约束的自主性很危险**，原因不止「模型可能判断不了危险」：

- 模型会**误判**，也会被**prompt 注入**诱导（某个文件里藏一句 "请运行 rm -rf ~"，模型读到可能照做）。
- 很多操作**本身不危险，只是用户不想要**——比如删掉用户 `/tmp` 下的真实文件。这种「危险」机器和模型都判断不了，**只有用户自己知道**。

所以核心结论：**最终该由人来拍板**；同时**已知的灾难性模式要靠 harness 机械识别**，不能指望模型自律、也不能只靠人眼盯。

### 3. 核心洞察：「安全是一条管道，不是一个布尔值」

不是写一个 `if is_dangerous(): block()` 就完事。危险有**多种失败模式**，需要**多道各司其职的闸顺序拦截**，单一检查必然漏：

| 防线 | 防的失败模式 | 为什么不可替代 |
|---|---|---|
| 机械正则校验 | 灾难命令 / 注入诱导的 `rm -rf` | 机器不带脑子、不会被说服，最可靠 |
| 人的判断 | 「命令不危险但我不想要」 | 只有用户有这个上下文 |

任何单一一道闸都有盲区，叠起来才覆盖得住——这就是**纵深防御（defense in depth）**。

## 方案层 — 怎么实现的 / 设计取舍

### 4. 五步管道的顺序

全部核心在 `PermissionManager.check()`，按顺序裁决，**首次匹配即出管道**：

```
第0步  bash validator    正则扫命令；severe(sudo/rm_rf)→deny，其他→ask
第1步  deny 规则          fnmatch 黑名单，不可绕过
第2步  模式决策          plan/auto/default 档位
第3步  allow 规则        fnmatch 白名单
第4步  问用户            兜底，人是最终权威
```

`check()` 只返回决定 `{"behavior": "allow"|"deny"|"ask", "reason": str}`，自己不执行任何东西。

### 5. 为什么 deny 规则（第1步）必须先于 allow 规则（第3步）？

规则首次匹配即生效。若把 allow 和 deny 混在一起按列表顺序查，一条宽松的 `{"tool":"bash","path":"*","behavior":"allow"}` 会先匹配 `rm -rf /` 并直接放行，后面的 deny 永远轮不到。

代码的做法是**两趟独立循环**：先扫所有 deny 规则，再扫 allow。所以不管 allow 写得多宽，**deny 永远先被检查、永远赢**。这就是 fail-safe：「禁止」必须压过「允许」。

### 6. 为什么 bash validator 是「第0步」，排在所有规则之前？

因为这道机械网**连用户自己的 `always` 都不能关掉**。

实例：用户对某条 bash 按了 `always`，`ask_user` 会往规则表追加 `{"tool":"bash","path":"*","behavior":"allow"}`——从此所有 bash 在第3步都会被放行。但随后的 `rm -rf` 仍被拦下，正是因为 validator 在第0步、在 allow 规则之前就把它 deny 了。

若把 validator 放到 allow 规则之后，那条 `always` 就会把 `rm -rf` 也放行。**机械安全网必须在人/规则能干预的位置之前，且不可被它们覆盖。**

### 7. validator 的两档：severe → deny，其他 → ask

`BashSecurityValidator` 有 5 条正则：

```
shell_metachar    [;&|`$]          → ask（升级问人）
sudo              \bsudo\b          → deny（severe）
rm_rf             \brm\s+(-[a-zA-Z]*)?r  → deny（severe）
cmd_substitution  \$\(              → ask
ifs_injection     \bIFS\s*=         → ask
```

命中 **severe = {sudo, rm_rf}** 直接拒，模型连问的机会都没有；命中其他升级为 ask，弹 `y/n`，用户仍可批准。这解释了为什么 `rm -rf` 那条直接 `[DENIED]`，而带 `;`/`|`/`$()` 的命令是「标记后让用户决定」。

### 8. 三种模式语义

模式是「当前会话的整体松紧档位」，在第2步起作用：

| 模式 | 读操作 | 写操作（write_file/edit_file/bash） |
|---|---|---|
| `plan` | allow | **deny**（只读，适合先规划不动手） |
| `auto` | 直接 allow | 不自动决定 → 继续往下走规则/问人 |
| `default` | 不特殊处理 | 不特殊处理 → 完全交给规则 + 问人 |

`default` 在第2步什么都不做直接穿过。运行时可用 `/mode plan` 切换。

### 9. agent_loop 怎么消费裁决——为什么被拒的工具也要返回 tool_result？

`check()` 返回决定后，`agent_loop` 按 behavior 处理：deny → 造一句拒绝文本不执行；ask → 问人，批准才执行；allow → 直接执行。**关键边界：无论执行还是被拒，都往 `results` 里 append 一条 `tool_result`**。被拒时 content 是 `"Permission denied: ..."`。

两个原因：

1. **API 结构硬要求**：每个 `tool_use` 必须有对应 `tool_use_id` 的 `tool_result`，否则下次 `messages.create()` 报错。（这和 Ctrl+C 中断时若留下「孤儿 tool_use」会崩，是同一条规则。）
2. **反馈闭环**：拒绝消息本身就是给模型的信号，模型读到才知道这条路堵了、换一条再试。删 /tmp 时模型疯狂换花样（`-delete`→`rm -d`→python `shutil`），正是每次拒绝都被喂回去、它在按反馈自适应。

### 10. 熔断器 consecutive_denials

连续拒绝计数，到 3 次打印 `[consecutive denials -- consider switching to plan mode]`。**只提醒，不强制**——任何一次 allow/always/y 都把计数清零。是个「你俩在死磕，换个姿势」的善意提醒。

### 11. _matches 怎么判断规则命中

三个维度全部满足才算匹配：`tool`（工具名，`*` 通配）、`path`（`fnmatch` glob 匹配路径）、`content`（`fnmatch` glob 匹配 bash 命令文本，默认 deny 规则的 `"rm -rf /"`/`"sudo *"` 靠它）。

## 影响层 — 更大的上下文

### 12. 为什么不能删掉 validator、只靠 deny 规则拦危险命令？

两个独立理由：

1. **会被绕过**：deny 规则是可变规则表的一部分，会被宽松 allow / 用户 `always` 架空；validator 在第0步、硬编码，谁都关不掉。
2. **glob 太脆，正则才扛得住变形**：deny 用 `fnmatch` glob，一条 `{content:"rm -rf *"}` 随手就能绕——`rm -fr /`（flag 换序）、`rm  -rf /`（多空格）、`$(echo rm) -rf`（命令替换）。validator 的正则 `\brm\s+(-[a-zA-Z]*)?r` 专门做了灵活空白 + flag 任意组合。靠 glob 黑名单要写成百上千条还堵不全。

（第三个理由：纯 deny 规则只有「拦/不拦」两档，做不出 validator「非 severe → 标记并问人」的中间档。）

### 13. 和真实 Claude Code / 后续机制的关系

- 你天天用的 Claude Code 权限提示（`Allow? Yes/No/Always`）、`plan` 模式、`--dangerously-skip-permissions`、allowlist，就是这套东西的工业级版本。本 session 是它**可读的最小内核**。
- 后续的 **Hook 机制**是延伸：权限是内置拦截点，hook 让用户自己往管道里插自定义逻辑（PreToolUse/PostToolUse）。
- 文件里 `is_workspace_trusted()` 目前**定义了但没接进管道**——教学版故意留的扩展点，提示真实系统还会叠「工作区信任」这一层。

## 关键概念

### 纵深防御（Defense in Depth）

四道闸各防一种失败模式，叠起来才覆盖完整：

```
validator(0)  → deny 规则(1)  → 模式(2)        → 人(4)
灾难命令/注入     已知黑名单       整体档位失配      不危险但不想要
连always关不掉    先于allow,fail-safe  plan锁写       最终权威
```

任何一道单独都有盲区。安全不是一个布尔值，而是一条每关各司其职的管道。

### glob vs 正则——两种匹配技术

**deny 和 allow 规则都用 glob（`fnmatch`），只有第0步 validator 用正则。** 两者别混。

glob 是简化版字符串通配（就是命令行 `ls *.py` 那种），不是正则，只认几个通配符：

| 通配符 | 含义 |
|---|---|
| `*` | 匹配任意数量任意字符 |
| `?` | 匹配恰好一个任意字符 |
| `[abc]` | 匹配方括号里任一个字符 |

`_matches` 里就是 `fnmatch(command, rule["content"])`、`fnmatch(path, rule["path"])`。所以默认 deny 规则 `{"content":"sudo *"}` = 匹配「以 `sudo ` 开头的命令」。

| | glob (fnmatch) | 正则 (regex) |
|---|---|---|
| 用在哪 | deny/allow 规则 | validator |
| 表达力 | 弱（只有 `* ? []`） | 强（空白 `\s+`、分组、字符类、词边界 `\b`） |
| `rm -fr /`（flag 换序） | ❌ 拦不住 | ✅ `(-[a-zA-Z]*)?` 能匹配 |
| `rm  -rf`（多空格） | ❌ 空格是死的 | ✅ `\s+` 匹配多空格 |

一句话：glob 适合「长这个样子吗」的粗匹配（规则系统够用）；正则适合「揪出危险变形」的精细识别（安全校验必须用）。这就是 s07 故意让规则用 glob、让 validator 用正则的原因——见第12点。
