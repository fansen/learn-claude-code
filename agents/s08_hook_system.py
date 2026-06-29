#!/usr/bin/env python3
# 线束：可扩展性 -- 在不修改循环的情况下注入行为。
"""
s08_hook_system.py - Hook 系统

【本节在 s02 基础上加了什么】
  循环依旧没变。新增的是「扩展点」：在循环的几个关键时刻，去外部跑用户配置的
  shell 命令（hook），让 hook 有机会阻止 / 注入消息 / 改写输入。
  共享样板（SDK / .env / safe_path / 工具实现）见 s01，本文件不再重复。

【和 s07 权限系统的关系】
  s07 的拦截是「内置」的（管道写死在 check 里）；s08 把拦截权交给用户——
  你写个脚本配进 .hooks.json，就能往循环里插自己的逻辑，不用改 agent 源码。
  你平时用 Claude Code 时被 git-guardrails.sh 拦下 push 到 main，就是这套机制。

【三个教学事件 / 退出码契约】
  事件：SessionStart（会话开始）、PreToolUse（执行工具前）、PostToolUse（执行工具后）
  hook 是个外部命令，靠「退出码」跟 harness 通信：
    退出码 0 -> 继续（stdout 可选结构化 JSON：改输入 / 注入上下文 / 覆盖权限）
    退出码 1 -> 阻止本次工具执行（stderr 作为阻止原因）
    退出码 2 -> 注入一条消息给模型（stderr 作为消息内容）

  调用点示意：
      SessionStart（启动一次）
         │
         ▼
      模型给出 tool_use
         │
         ▼
      PreToolUse hook ──退出码1──> 阻止，不执行
         │ 退出码0/2
         ▼
      执行工具
         │
         ▼
      PostToolUse hook（可追加备注到工具输出）
         │
         ▼
      tool_result 喂回模型

这有意比生产系统简单：先讲清「扩展模式」，再引入事件特定的边界情况。

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

# 教学版只保留三个最清晰的事件。更完整的系统可以后续扩展事件面（PreCompact、SessionEnd 等）。
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")

# hook 是外部命令，必须有超时——否则一个卡住的 hook 能挂起整个 agent
HOOK_TIMEOUT = 30  # 秒
# 真实 CC 的超时配置（教学版统一用 30s）：
#   TOOL_HOOK_EXECUTION_TIMEOUT_MS = 600000（工具 hook 10 分钟）
#   SESSION_END_HOOK_TIMEOUT_MS = 1500（SessionEnd hook 1.5 秒）

# 工作区信任标记文件。Hook 仅在此文件存在（或 SDK 模式）时运行。
# 为什么要这道门？hook 会执行任意 shell 命令——如果克隆了别人的仓库，里面带了恶意
# .hooks.json，没有信任门就会在你打开项目时自动执行。所以「未信任目录不跑 hook」。
# （对比 s07：那里 is_workspace_trusted 定义了却没接入；s08 这里真正用上了。）
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
        # 按事件分桶存放 hook 定义；没有配置文件就保持三个空列表（等于没装 hook）
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        self._sdk_mode = sdk_mode  # SDK 模式下信任视为隐式（见 _check_workspace_trust）
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
        # 聚合多个 hook 的结果：任一 hook 阻止 → blocked；退出码 2 的消息收集到 messages
        result = {"blocked": False, "messages": []}

        # 信任门：未信任工作区直接返回空结果（等于「不跑任何 hook」），防止恶意 .hooks.json
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            # matcher = 工具名过滤器，让某个 hook 只对特定工具触发（如只盯 bash）。
            # "*" 或缺省 = 匹配所有工具；否则要求工具名精确相等。
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            # 把本次工具调用的上下文通过「环境变量」传给 hook 脚本，
            # 这样 hook 用 $HOOK_TOOL_NAME / $HOOK_TOOL_INPUT 就能读到要审查的内容。
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

                # ── 退出码契约：hook 用退出码告诉 harness 该怎么办 ──
                if r.returncode == 0:
                    # 0 = 放行。stdout 可选，作日志打印
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")

                    # 进阶能力：如果 stdout 是结构化 JSON，hook 还能更主动地干预——
                    #   updatedInput      → 改写本次工具的输入参数（如自动给路径加前缀）
                    #   additionalContext → 往对话里注入一段额外上下文给模型
                    #   permissionDecision→ 覆盖权限裁决（hook 充当 s07 之外的另一个决策源）
                    # 解析失败就忽略——简单 hook 本来就不输出 JSON，属正常情况。
                    # 退出码=粗开关（拦/放/提醒），结构化 stdout=细操控（改输入/注上下文/管权限）。
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
                    # 1 = 阻止本次工具执行；stderr 当作给模型/用户看的原因
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] BLOCKED: {reason[:200]}")

                elif r.returncode == 2:
                    # 2 = 注入一条消息（不阻止执行）；stderr 当作消息内容
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

    循环骨架和 s01/s02 完全一样，唯一区别是：在「执行工具」前后各插了一个 hook 调用点。
    教学版只保留最清晰的集成点：SessionStart、PreToolUse、执行工具、PostToolUse。
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

            # ctx 是传给 hook 的上下文。
            # 注意一个教学版的小坑：run_hooks 里的 updatedInput 会把 ctx["tool_input"]
            # 重新赋值成新 dict，但下面执行工具用的是这里的局部变量 tool_input（line ~332），
            # 两者解绑后 updatedInput 改的版本只被 PostToolUse 的 ctx 看到，并不影响实际执行。
            # 真实 CC 里 updatedInput 是会改写执行输入的；教学版为简化没接到执行处。
            tool_input = dict(block.input or {})
            ctx = {"tool_name": block.name, "tool_input": tool_input}

            # ── PreToolUse：执行前的拦截点（可阻止 / 注入 / 改写输入）──
            pre_result = hooks.run_hooks("PreToolUse", ctx)

            # 退出码 2 的 hook 消息：包成 tool_result 注入回对话
            for msg in pre_result.get("messages", []):
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"[Hook message]: {msg}",
                })

            # 退出码 1：hook 否决了这次调用 → 不执行工具，把原因当输出喂回模型
            # （和 s07 一样的边界：被拦也要回 tool_result，保证 tool_use/tool_result 配对）
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

            # ── PostToolUse：执行后的观察点（拿得到工具输出，可追加备注）──
            # 典型用途：跑完 write_file 后自动 lint、记录审计日志、给输出补充说明。
            ctx["tool_output"] = output
            post_result = hooks.run_hooks("PostToolUse", ctx)

            # post-hook 的消息直接拼到工具输出末尾（而不是单独一条 tool_result）
            for msg in post_result.get("messages", []):
                output += f"\n[Hook note]: {msg}"

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # HookManager() 会自动从工作区的 .hooks.json 加载 hook 配置
    hooks = HookManager()

    # SessionStart：会话开始触发一次（典型用途：打印环境信息、做启动检查、注入初始上下文）
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
