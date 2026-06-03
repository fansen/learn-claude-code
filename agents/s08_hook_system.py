#!/usr/bin/env python3
# 线束：可扩展性 -- 在不修改循环的情况下注入行为。
"""
s08_hook_system.py - Hook 系统

Hook 是主循环周围的扩展点。
它们让读者可以在不重写循环本身的情况下添加行为。

教学版事件：
  - SessionStart
  - PreToolUse
  - PostToolUse

教学版退出码契约：
  - 0 -> 继续
  - 1 -> 阻止
  - 2 -> 注入消息

这有意比生产系统更简单。目标是在引入事件特定的边界情况之前，
先清晰地教会扩展模式。

核心洞察："不修改循环，扩展 agent。"
"""

import json
import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 教学版只保留三个最清晰的事件。更完整的系统可以后续扩展事件面。

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30  # 秒
# 真实 CC 的超时配置：
#   TOOL_HOOK_EXECUTION_TIMEOUT_MS = 600000（工具 hook 10 分钟）
#   SESSION_END_HOOK_TIMEOUT_MS = 1500（SessionEnd hook 1.5 秒）

# 工作区信任标记文件。Hook 仅在此文件存在（或 SDK 模式下）时运行。
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"


class HookManager:
    """
    从 .hooks.json 配置加载并执行 hook。

    Hook 管理器做三件简单的事：
    - 加载 hook 定义
    - 为事件运行匹配的命令
    - 聚合阻止/消息结果返回给调用方
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[Hooks loaded from {config_path}]")
            except Exception as e:
                print(f"[Hook config error: {e}]")

    def _check_workspace_trust(self) -> bool:
        """
        检查当前工作区是否受信任。

        教学版使用简单的信任标记文件。
        在 SDK 模式下，信任被视为隐式的。
        """
        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()

    def run_hooks(self, event: str, context: dict = None) -> dict:
        """
        执行某个事件的所有 hook。

        返回: {"blocked": bool, "messages": list[str]}
          - blocked: 如果任何 hook 返回退出码 1 则为 True
          - messages: 退出码 2 的 hook 的 stderr 内容（用于注入）
        """
        result = {"blocked": False, "messages": []}

        # 信任门：拒绝在不受信任的工作区中运行 hook
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            # 检查 matcher（PreToolUse/PostToolUse 的工具名过滤器）
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            # 构建带 hook 上下文的环境变量
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False)[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"])[:10000]

            try:
                r = subprocess.run(
                    command, shell=True, cwd=WORKDIR, env=env,
                    capture_output=True, text=True, timeout=HOOK_TIMEOUT,
                )

                if r.returncode == 0:
                    # 静默继续
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")

                    # 可选的结构化 stdout：一个小扩展点，
                    # 在保持教学契约简单的同时提供额外能力。
                    try:
                        hook_output = json.loads(r.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (
                                hook_output["permissionDecision"])
                    except (json.JSONDecodeError, TypeError):
                        pass  # stdout 不是 JSON —— 简单 hook 的正常情况

                elif r.returncode == 1:
                    # 阻止执行
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")

                elif r.returncode == 2:
                    # 注入消息
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"  [hook:{event}] INJECT: {msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")

        return result


# -- 工具实现（与 s02 相同）--
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."


def agent_loop(messages: list, hooks: HookManager):
    """
    带 hook 感知的 agent 循环。

    教学版只保留最清晰的集成点：
    SessionStart、PreToolUse、执行工具、PostToolUse。
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

            tool_input = dict(block.input or {})
            ctx = {"tool_name": block.name, "tool_input": tool_input}

            # -- PreToolUse hook --
            pre_result = hooks.run_hooks("PreToolUse", ctx)

            # 将 hook 消息注入到结果中
            for msg in pre_result.get("messages", []):
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"[Hook message]: {msg}",
                })

            if pre_result.get("blocked"):
                reason = pre_result.get("block_reason", "Blocked by hook")
                output = f"Tool blocked by PreToolUse hook: {reason}"
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": output,
                })
                continue

            # -- 执行工具 --
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**tool_input) if handler else f"Unknown: {block.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {block.name}: {str(output)[:200]}")

            # -- PostToolUse hook --
            ctx["tool_output"] = output
            post_result = hooks.run_hooks("PostToolUse", ctx)

            # 注入 post-hook 消息
            for msg in post_result.get("messages", []):
                output += f"\n[Hook note]: {msg}"

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    hooks = HookManager()

    # 触发 SessionStart hook
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})

    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
