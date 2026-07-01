#!/usr/bin/env python3
# 线束机制：上下文隔离 -- 保护模型的思维清晰度。
"""
s04_subagent.py - 子 Agent

创建一个 messages=[] 的子 Agent。子 Agent 在独立上下文中工作，
与父 Agent 共享文件系统，完成后只返回摘要给父 Agent。

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    父 Agent 上下文保持干净。
    子 Agent 上下文被丢弃。

核心洞察："messages=[] 提供上下文隔离，父 Agent 保持干净。"

注意：真实的 Claude Code 也使用进程内隔离（而非操作系统级进程 fork）。
子 Agent 在同一进程中以全新的消息数组和隔离的工具上下文运行 -- 与本教学实现相同的模式。

    与真实 Claude Code 的对比：
    +-------------------+------------------+----------------------------------+
    | Aspect            | This demo        | Real Claude Code                 |
    +-------------------+------------------+----------------------------------+
    | Backend           | in-process only  | 5 backends: in-process, tmux,    |
    |                   |                  | iTerm2, fork, remote             |
    | Context isolation | fresh messages=[]| createSubagentContext() isolates  |
    |                   |                  | ~20 fields (tools, permissions,  |
    |                   |                  | cwd, env, hooks, etc.)           |
    | Tool filtering    | manually curated | resolveAgentTools() filters from |
    |                   |                  | parent pool; allowedTools         |
    |                   |                  | replaces all allow rules         |
    | Agent definition  | hardcoded system | .claude/agents/*.md with YAML    |
    |                   | prompt           | frontmatter (AgentTemplate)      |
    +-------------------+------------------+----------------------------------+
"""

import os
import re
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

# 父 Agent 的系统提示词：鼓励它用 task 工具把探索/子任务派出去（保护自己的上下文）
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
# 子 Agent 的系统提示词（写死）：干完活后要"总结发现"——这句是让它只回摘要的关键。
# 真实 CC 里子 Agent 的系统提示词来自 .claude/agents/*.md 定义（见下方 AgentTemplate）。
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


class AgentTemplate:
    """
    从 Markdown frontmatter 解析 Agent 定义。

    注意：这个类在教学版里定义了、但主流程没真正用它——是个"指路用"的扩展点
    （和 s07 的 is_workspace_trusted 一样）。它告诉你：真实系统的子 Agent 是
    「可配置定义」的，而教学版为简单直接硬编码了一个 SUBAGENT_SYSTEM。

    真实的 Claude Code 从 .claude/agents/*.md 加载 Agent 定义。
    frontmatter 字段：name, tools, disallowedTools, skills, hooks,
    model, effort, permissionMode, maxTurns, memory, isolation, color,
    background, initialPrompt, mcpServers。
    3 个来源：内置、自定义（.claude/agents/）、插件提供。
    """
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.config = {}
        self.system_prompt = ""
        self._parse()

    def _parse(self):
        text = self.path.read_text()
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                self.config[k.strip()] = v.strip()
        self.system_prompt = match.group(2).strip()
        self.name = self.config.get("name", self.name)


# -- 父子 Agent 共享的工具实现 --
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
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

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

# 子 Agent 的工具集：只有 bash/read/write/edit 四个基础工具，故意「不含 task 工具」。
# 为什么？给了 task，子 Agent 就能再派孙 Agent，孙 Agent 再派…… 指数级/无限递归。
# 教学版用最简单的办法防住：直接不给。（真实 CC 允许嵌套但有深度限制。）
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- 子 Agent：全新上下文、过滤后的工具、仅返回摘要 --
def run_subagent(prompt: str) -> str:
    # 【隔离机制就是这一行】：一个全新的、跟父 Agent 完全无关的局部 messages 列表。
    # 子 Agent 在里面折腾几十轮，函数返回时这个变量随栈销毁——没有 fork/沙箱/IPC，
    # "上下文隔离"字面上就是"另起一个 messages 列表"。父 Agent 的 messages 毫发无损。
    # 注意：这是同一个 Python 进程里的另一个列表（进程内隔离），且同步阻塞（不是真并行）。
    sub_messages = [{"role": "user", "content": prompt}]  # 全新上下文
    for _ in range(30):  # 安全上限：防子 Agent 陷入"一直调工具、永不给最终答复"而回不来
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,   # 用子 Agent 自己的 messages 和工具集
        )
        # 所有中间过程都只进 sub_messages，绝不进父的 messages
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 工具用的是和父 Agent 共享的 TOOL_HANDLERS（同一个 WORKDIR）——
                # 所以子 Agent 读写文件的副作用是真实的、父 Agent 之后能看到。共享文件系统。
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    # 【压缩】只把最终文本（摘要）返回给父 Agent——子 Agent 那几十轮的噪音上下文全丢弃。
    # 几万 token 的探索 → 几百 token 的结论。
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# -- 父 Agent 工具：基础工具 + task 分发器 --
# 父比子多一个 task 工具（子没有，防递归）。这就是父子唯一的工具差别。
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}},
]


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    # 派子 Agent：同步跑 run_subagent，拿回一段摘要字符串
                    desc = block.input.get("description", "subtask")
                    prompt = block.input.get("prompt", "")
                    print(f"> task ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 【对父透明】不管是 task 还是 bash，结果都当成普通 tool_result 塞回父的对话。
                # 父 Agent 分不清这个字符串背后是"跑了个命令"还是"另一个 Agent 烧了 30 轮"。
                print(f"  {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)

        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
