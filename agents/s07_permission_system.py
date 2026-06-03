#!/usr/bin/env python3
# 线束：安全性 -- 在意图与执行之间的管道。
"""
s07_permission_system.py - 权限系统

每个工具调用在执行前都要经过权限管道。

教学版管道：
  1. deny 规则
  2. 模式检查
  3. allow 规则
  4. 询问用户

本版本有意先教三种模式：
  - default
  - plan
  - auto

这已足够构建一个真实、可理解的权限系统，
无需在第一天就让读者淹没在所有高级策略分支中。

核心洞察："安全是一条管道，不是一个布尔值。"
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
# 教学版先从三种清晰的模式开始。
MODES = ("default", "plan", "auto")

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}

# 会修改状态的工具
WRITE_TOOLS = {"write_file", "edit_file", "bash"}


# -- Bash 安全校验 --
class BashSecurityValidator:
    """
    校验 bash 命令中是否包含明显危险的模式。

    教学版刻意保持小巧、易读。
    先捕获几种高风险模式，再让权限管道决定是拒绝还是询问用户。
    """

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),       # shell 元字符
        ("sudo", r"\bsudo\b"),                 # 提权
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),  # 递归删除
        ("cmd_substitution", r"\$\("),          # 命令替换
        ("ifs_injection", r"\bIFS\s*="),        # IFS 注入
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
# 规则按顺序检查：首次匹配即生效。
# 格式: {"tool": "<tool_name_or_*>", "path": "<glob_or_*>", "behavior": "allow|deny|ask"}
DEFAULT_RULES = [
    # 始终拒绝危险模式
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "content": "sudo *", "behavior": "deny"},
    # 允许读取任何文件
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
        Returns: {"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        # 第 0 步：Bash 安全校验（在 deny 规则之前）
        # 教学版提前检查，便于理解。
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # 严重模式（sudo, rm_rf）立即拒绝
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny",
                            "reason": f"Bash validator: {desc}"}
                # 其他模式升级为询问（用户仍可批准）
                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask",
                        "reason": f"Bash validator flagged: {desc}"}

        # 第 1 步：Deny 规则（不可绕过，始终最先检查）
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny",
                        "reason": f"Blocked by deny rule: {rule}"}

        # 第 2 步：基于模式的决策
        if self.mode == "plan":
            # Plan 模式：拒绝所有写操作，允许读取
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny",
                        "reason": "Plan mode: write operations are blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        if self.mode == "auto":
            # Auto 模式：自动允许只读工具，写操作需询问
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {"behavior": "allow",
                        "reason": "Auto mode: read-only tool auto-approved"}
            # 教学版：继续向下检查 allow 规则，然后询问
            pass

        # 第 3 步：Allow 规则
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow",
                        "reason": f"Matched allow rule: {rule}"}

        # 第 4 步：询问用户（未匹配工具的默认行为）
        return {"behavior": "ask",
                "reason": f"No rule matched for {tool_name}, asking user"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """交互式审批提示。批准返回 True。"""
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n  [Permission] {tool_name}: {preview}")
        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            # 为该工具添加永久 allow 规则
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        # 追踪拒绝次数，用于熔断
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} consecutive denials -- "
                  "consider switching to plan mode]")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """检查规则是否匹配该工具调用。"""
        # 工具名匹配
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        # 路径模式匹配
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        # 内容模式匹配（用于 bash 命令）
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
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

            # -- 权限检查 --
            decision = perms.check(block.name, block.input or {})

            if decision["behavior"] == "deny":
                output = f"Permission denied: {decision['reason']}"
                print(f"  [DENIED] {block.name}: {decision['reason']}")

            elif decision["behavior"] == "ask":
                if perms.ask_user(block.name, block.input or {}):
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
                    print(f"> {block.name}: {str(output)[:200]}")
                else:
                    output = f"Permission denied by user for {block.name}"
                    print(f"  [USER DENIED] {block.name}")

            else:  # 允许
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
                print(f"> {block.name}: {str(output)[:200]}")

            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 启动时选择权限模式
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
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /mode 命令：运行时切换模式
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"[Switched to {parts[1]} mode]")
            else:
                print(f"Usage: /mode <{'|'.join(MODES)}>")
            continue

        # /rules 命令：显示当前规则
        if query.strip() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"  {i}: {rule}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history, perms)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
