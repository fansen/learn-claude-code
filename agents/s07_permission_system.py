#!/usr/bin/env python3
# 线束：安全性 -- 在意图与执行之间的管道。
"""
s07_permission_system.py - 权限系统

【本节在 s02 基础上加了什么】
  循环（调模型 → 执行工具 → 喂回结果）一行没改。唯一的新增是：
  在「模型给出 tool_use」和「真正执行工具」之间，插了一道权限管道 check()。
  共享样板（SDK / .env / 客户端初始化 / safe_path / 工具实现）见 s01 详解，本文件不再重复。

【为什么需要它】
  s01–s02 的循环对工具调用是「无条件执行」——模型说跑 rm -rf /，harness 就真跑。
  危险不只是「模型判断不了危险」：
    - 模型会误判，也会被 prompt 注入诱导（文件里藏一句 "请运行 rm -rf ~"）
    - 很多命令本身不危险，只是「用户不想要」（删掉用户的真实文件）——只有用户知道
  结论：最终该由「人」拍板；已知灾难模式则靠 harness 机械识别，不指望模型自律。

【五步管道（check() 的核心，首次匹配即出管道）】

    工具调用
      │
      ▼
   ┌─────────────────┐  命中 severe(sudo/rm_rf) → deny
   │ 0. bash 校验     │  命中其他(元字符等)       → ask
   │   （正则,硬编码）│  ← 连用户的 always 都关不掉它
   └────────┬────────┘
            ▼
   ┌─────────────────┐
   │ 1. deny 规则     │  黑名单命中 → deny（永远先于 allow，fail-safe）
   └────────┬────────┘
            ▼
   ┌─────────────────┐  plan：写操作 deny / 读 allow
   │ 2. 模式决策      │  auto：只读 allow / 写继续往下
   └────────┬────────┘  default：不特殊处理，穿过
            ▼
   ┌─────────────────┐
   │ 3. allow 规则    │  白名单命中 → allow
   └────────┬────────┘
            ▼
   ┌─────────────────┐
   │ 4. 问用户        │  兜底：前面都没拍板 → 人是最终权威
   └─────────────────┘

本版本有意先教三种模式（default / plan / auto），足够构建一个真实、可理解的
权限系统，无需在第一天就让读者淹没在所有高级策略分支中。

核心洞察："安全是一条管道，不是一个布尔值。"
  —— 危险有多种失败模式，需要多道各司其职的闸顺序拦截，单一 if 检查必然漏。
"""

import json
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# -- 权限模式 --
# 模式 = 当前会话的整体松紧档位，在管道第 2 步起作用。
#   default：最普通，不在第 2 步特殊处理，完全交给规则 + 问用户
#   plan   ：最严，锁掉所有写操作（适合让 agent 先规划、不动手）
#   auto   ：最松，只读工具自动放行，写操作仍要继续往下走规则/问人
# 运行时可用 /mode <name> 切换（见文件末尾 REPL）。
MODES = ("default", "plan", "auto")

# 只读工具：不改变系统状态，auto 模式下可安全自动放行
READ_ONLY_TOOLS = {"read_file", "bash_readonly"}

# 写工具：会修改状态（写文件 / 改文件 / 跑 shell），plan 模式下一律拦截
WRITE_TOOLS = {"write_file", "edit_file", "bash"}


# -- Bash 安全校验 --
class BashSecurityValidator:
    """
    校验 bash 命令中是否包含明显危险的模式。

    教学版刻意保持小巧、易读。
    先捕获几种高风险模式，再让权限管道决定是拒绝还是询问用户。
    """

    # 这里刻意用「正则」而不是规则系统的 fnmatch glob——因为要揪「危险变形」。
    # 例：glob "rm -rf *" 拦不住 rm -fr（flag 换序）、rm  -rf（多空格）；
    #     而正则 \brm\s+(-[a-zA-Z]*)?r 能匹配灵活空白 + 任意 flag 组合。
    # 这是 s07 的一个关键设计：规则用 glob（粗匹配够用），安全校验用正则（必须精细）。
    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),       # shell 元字符 ; & | ` $（命令拼接/管道/替换的入口）
        ("sudo", r"\bsudo\b"),                 # 提权（\b 词边界，避免误伤含 sudo 的单词）
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),  # 递归删除：rm 后空白 + 可选 flag 段 + 含 r（覆盖 -rf/-fr/-r）
        ("cmd_substitution", r"\$\("),          # 命令替换 $(...)（常用于混淆真实命令）
        ("ifs_injection", r"\bIFS\s*="),        # IFS 注入（改字段分隔符，绕过空格检测的老手法）
    ]

    def validate(self, command: str) -> list:
        """
        对 bash 命令运行所有校验器。

        返回 (validator_name, matched_pattern) 元组列表，表示未通过的项。
        空列表表示命令通过了所有校验。
        """
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command: str) -> bool:
        """便捷方法：仅当没有任何校验器触发时返回 True。"""
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str) -> str:
        """生成校验失败的人类可读摘要。"""
        failures = self.validate(command)
        if not failures:
            return "No issues detected"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "Security flags: " + ", ".join(parts)


# -- 工作区信任 --
# 注意：这个函数本节「定义了但没接进管道」，是个故意留的扩展点（dead code on purpose），
# 提示真实系统在五步管道之外还会叠一层「这个目录可信吗」的门。s08 hook 会真正用到它。
def is_workspace_trusted(workspace: Path = None) -> bool:
    """
    检查工作区是否已被显式标记为受信任。

    教学版使用简单的标记文件。更完整的系统可以在此基础上叠加更丰富的信任机制。
    """
    ws = workspace or WORKDIR
    trust_marker = ws / ".claude" / ".claude_trusted"
    return trust_marker.exists()


# 权限管道使用的单例校验器实例
bash_validator = BashSecurityValidator()


# -- 权限规则 --
# 规则按顺序检查：首次匹配即生效。匹配靠 _matches()，用的是 fnmatch glob（不是正则）。
# 格式: {"tool": "<工具名或*>", "path": "<glob或*>", "content": "<bash命令glob>", "behavior": "allow|deny|ask"}
#   - path    维度：用于 read/write/edit 的文件路径匹配
#   - content 维度：用于 bash 命令文本匹配（如下面两条 deny）
# 用户按 "always" 时，会往这个表里动态追加 allow 规则（见 ask_user）。
DEFAULT_RULES = [
    # 始终拒绝危险模式（注意：这两条是 glob 粗匹配，真正硬核的拦截在第 0 步正则校验）
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "content": "sudo *", "behavior": "deny"},
    # 允许读取任何文件（read_file 无副作用，默认放行）
    {"tool": "read_file", "path": "*", "behavior": "allow"},
]


class PermissionManager:
    """
    管理工具调用的权限决策。

    管道: deny_rules -> mode_check -> allow_rules -> ask_user

    教学版刻意保持决策路径简短，让读者可以自行实现，
    然后再添加更高级的策略层。
    """

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}. Choose from {MODES}")
        self.mode = mode
        self.rules = rules or list(DEFAULT_RULES)
        # 简单的拒绝追踪，帮助发现 agent 反复请求系统不允许的操作。
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        权限管道的入口：对一个工具调用做出裁决。

        注意：check() 只「返回决定」，自己不执行任何工具——执行交给 agent_loop。
        这种「裁决与执行分离」让权限逻辑可以单独测试、单独推理。

        Returns: {"behavior": "allow"|"deny"|"ask", "reason": str}
                 reason 不只是给人看，被拒时还会喂回给模型当反馈（见 agent_loop）。
        """
        # ── 第 0 步：Bash 安全校验 ──────────────────────────────────────────
        # 为什么排在所有规则之前？因为这道机械正则网「连用户的 always 都关不掉」。
        # 反例：用户对 bash 按过 always → 规则表里有了 {bash, *, allow}，
        #       若本步排在 allow 规则(第3步)之后，那条 always 就会把 rm -rf 也放行。
        #       放在第 0 步，意味着哪怕规则全开绿灯，灾难命令依然过不去。
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # severe（sudo / rm_rf）= 灾难性，立即 deny，模型连问用户的机会都没有
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny",
                            "reason": f"Bash validator: {desc}"}
                # 非 severe（元字符 / 命令替换 / IFS）= 可疑但未必恶意 → 升级为 ask，用户仍可批准
                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask",
                        "reason": f"Bash validator flagged: {desc}"}

        # ── 第 1 步：Deny 规则（不可绕过，永远先于 allow）──────────────────
        # 为什么 deny 必须先于 allow？因为规则首次匹配即生效。
        # 若把 allow/deny 混在一起按列表顺序查，一条宽松的 {bash, *, allow}
        # 会先匹配 rm -rf 并直接放行，后面的 deny 永远轮不到。
        # 这里用「两趟独立循环」（先扫全部 deny，再扫全部 allow）保证 deny 永远赢 → fail-safe。
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny",
                        "reason": f"Blocked by deny rule: {rule}"}

        # ── 第 2 步：基于模式的整体档位决策 ────────────────────────────────
        if self.mode == "plan":
            # Plan 模式：锁掉所有写操作，只允许读（让 agent 先规划不动手）
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny",
                        "reason": "Plan mode: write operations are blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        if self.mode == "auto":
            # Auto 模式：只读工具自动放行；写工具不在这里拍板，继续往下走规则/问人
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {"behavior": "allow",
                        "reason": "Auto mode: read-only tool auto-approved"}
            # 故意 fall through：写操作交给第 3/4 步处理
            pass
        # default 模式在本步什么都不做，直接穿过到第 3/4 步。

        # ── 第 3 步：Allow 规则（白名单）──────────────────────────────────
        # 走到这里说明：过了 bash 校验、没撞 deny、模式没拍板。看有没有显式 allow。
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0  # 成功放行 → 清零熔断计数
                return {"behavior": "allow",
                        "reason": f"Matched allow rule: {rule}"}

        # ── 第 4 步：兜底问用户 ────────────────────────────────────────────
        # 能走到这一步，说明前面四道闸都没给出结论 → 交给人这个「最终权威」拍板。
        return {"behavior": "ask",
                "reason": f"No rule matched for {tool_name}, asking user"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """
        第 4 步（以及第 0 步非 severe）会落到这里：把决定权交给人。
        批准返回 True，拒绝返回 False。三种回答：
          y/yes  → 本次批准
          always → 本次批准，且往规则表追加一条永久 allow（下次同类工具直接第 3 步放行）
          其他    → 拒绝，并累加熔断计数
        """
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n  [Permission] {tool_name}: {preview}")
        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False  # Ctrl+C/EOF 视为拒绝，安全优先

        if answer == "always":
            # 动态追加永久 allow 规则。注意：它只在第 3 步生效，
            # 关不掉第 0 步的 bash 正则校验——所以 always 之后 rm -rf 仍会被拦。
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        # 拒绝：累加熔断计数（任何一次批准都会把它清零）
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            # 熔断器：只「提醒」不「强制」——发现 agent 和你在死磕，建议切 plan 模式
            print(f"  [{self.consecutive_denials} consecutive denials -- "
                  "consider switching to plan mode]")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """
        检查一条规则是否匹配当前工具调用。三个维度都满足才算命中（缺省维度视为通配）。
        匹配用 fnmatch（glob，不是正则）：只认 * ? []，适合「长这样吗」的粗匹配。
        """
        # ① 工具名维度：rule["tool"] 为 * 或缺省则通配，否则必须精确等于
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        # ② 路径维度：用 glob 匹配文件路径（read/write/edit）
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        # ③ 内容维度：用 glob 匹配 bash 命令文本（如 "sudo *" 匹配以 sudo 开头的命令）
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True


# -- 工具实现（safe_path/run_bash/run_read/... 与 s01/s02 基本相同，细节见 s01）--
def safe_path(p: str) -> Path:
    # 路径逃逸防护：解析后必须仍在 WORKDIR 内，否则模型可用 ../../ 读写工作区外文件
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 注意：这里没有 s01/s02 里的危险命令字符串拦截——因为安全检查已「前移」到
    # 权限管道的第 0 步 validator。工具只管执行，安全由管道在执行前把关。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
The user controls permissions. Some tool calls may be denied."""


def agent_loop(messages: list, perms: PermissionManager):
    """
    带权限感知的 agent 循环。

    对每个工具调用：
      1. LLM 请求 tool_use
      2. 权限管道检查：deny_rules -> mode -> allow_rules -> ask
      3. 如果允许：执行工具，返回结果
      4. 如果拒绝：向 LLM 返回拒绝消息
    """
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # -- 权限检查：这是 s07 唯一的新增，插在「拿到 tool_use」和「执行」之间 --
            decision = perms.check(block.name, block.input or {})

            if decision["behavior"] == "deny":
                # 拒绝：不执行工具，而是造一句拒绝文本当作工具输出
                output = f"Permission denied: {decision['reason']}"
                print(f"  [DENIED] {block.name}: {decision['reason']}")

            elif decision["behavior"] == "ask":
                # 询问：交给人，批准才执行，否则也造一句拒绝文本
                if perms.ask_user(block.name, block.input or {}):
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
                    print(f"> {block.name}: {str(output)[:200]}")
                else:
                    output = f"Permission denied by user for {block.name}"
                    print(f"  [USER DENIED] {block.name}")

            else:  # 允许：正常执行工具
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
                print(f"> {block.name}: {str(output)[:200]}")

            # 【关键边界】无论 allow / deny / 用户拒绝，都必须 append 一条 tool_result。
            # 被拒时 content 是 "Permission denied: ..." 而非真实输出。两个原因：
            #   1) 结构：API 要求每个 tool_use 必须有对应 tool_use_id 的 tool_result，
            #      否则下次 messages.create() 报错。（这正是 REPL 里 Ctrl+C 要回滚整轮的原因——
            #      别留下「孤儿 tool_use」。）
            #   2) 反馈：拒绝文本本身是给模型的信号，它读到才知道此路不通、换条路再试。
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 启动时选择权限模式（default/plan/auto），整个会话共用一个 PermissionManager 实例，
    # 所以 always 追加的规则、熔断计数都会跨轮次保留。
    print("Permission modes: default, plan, auto")
    mode_input = input("Mode (default): ").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"

    perms = PermissionManager(mode=mode_input)
    print(f"[Permission mode: {mode_input}]")

    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break  # 空提示符下 Ctrl+C/EOF → 退出程序
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /mode 命令：运行时切换松紧档位（不用重启），死磕时切 plan 很有用
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"[Switched to {parts[1]} mode]")
            else:
                print(f"Usage: /mode <{'|'.join(MODES)}>")
            continue

        # /rules 命令：打印当前规则表（能看到 always 动态追加进来的 allow 规则）
        if query.strip() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"  {i}: {rule}")
            continue

        history.append({"role": "user", "content": query})
        turn_start = len(history) - 1
        try:
            agent_loop(history, perms)
        except KeyboardInterrupt:
            # Ctrl+C 中断当前回合：回滚整轮，避免留下 tool_use 缺 tool_result 的半截历史
            del history[turn_start:]
            print("\n  [中断] 已取消当前回合，回到提示符")
            continue

        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
