#!/usr/bin/env python3
# Harness: planning -- keep the current session plan outside the model's head.
"""
s03_todo_write.py - 会话规划（TodoWrite）

【本节在 s02 基础上加了什么】
  循环没变，只多注册了一个 todo 工具（TOOL_HANDLERS 加一行），外加一个提醒机制。
  共享样板（SDK / .env / safe_path / 工具实现）见 s01，本文件不再重复。

【解决什么问题】
  模型是无状态的：每轮它靠"重读整段对话"来重建"我做到哪了"。上下文越长，注意力越
  被冲散，越容易漂移、忘步骤、跑偏最初目标。TodoWrite 把计划外化成结构化数据，
  从「靠模型记」变成「代码存 + 代码管」——核心洞察是：
      把当前会话计划放在模型脑子外面（keep the plan outside the model's head）。

【两个关键设计】
  1. 全量重写：模型每次发完整计划，harness 无脑覆盖旧的（不是增量改某一项）。
     好处是模型每轮声明"完整真相"，harness 存的状态和模型以为的状态永不失同步。
  2. 提醒机制（nudge）：模型可能开了计划后闷头干活、忘了更新。harness 数着"连续
     几轮没碰 todo"，超过阈值就往对话里夹一句 <reminder> 轻推它刷新（不硬控）。

【边界】
  这是「会话内、临时、内存」的轻量计划；跨会话、落盘、带依赖的持久任务图是 s12 的事。
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# 连续多少轮没更新计划就触发提醒（提醒机制的阈值）
PLAN_REMINDER_INTERVAL = 3

# 系统提示词里明确要求模型：多步任务用 todo、同时只保持一个 in_progress、随进度刷新。
# 但"要求"只是软约束——真正的硬约束在 TodoManager.update 里用代码强制（见下）。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool for multi-step work.
Keep exactly one step in_progress when a task has multiple steps.
Refresh the plan as work advances. Prefer tools over prose."""


@dataclass
class PlanItem:
    """计划中的单个步骤。"""
    content: str                    # 步骤描述
    status: str = "pending"         # pending / in_progress / completed
    active_form: str = ""           # 进行中时的现在进行时标签（可选，仅为渲染可读性）


@dataclass
class PlanningState:
    """计划的整体状态（就是被"外部化"存起来、独立于模型记忆的那份状态）。"""
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0    # 距上次更新计划过了几轮——提醒机制的计数器


class TodoManager:
    """管理会话计划的增删改查和提醒逻辑。"""

    def __init__(self):
        self.state = PlanningState()

    def update(self, items: list) -> str:
        """用新的计划项列表完整替换当前计划（全量覆盖，不是增量）。

        为什么全量覆盖？模型无状态。增量式（"标记第2项完成"）要求模型记的状态和
        这里存的状态时刻同步，一旦数错/顺序不一致就错标。全量覆盖下模型每次声明
        "完整真相"，这里无脑替换，两边永不失同步。
        下面三个 raise 就是把"要短/content必填/status合法/只能一个in_progress"
        这些约束用代码强制——模型守不住的规则，交给外部代码守。
        """
        if len(items) > 12:
            raise ValueError("Keep the session plan short (max 12 items)")

        normalized = []
        in_progress_count = 0
        for index, raw_item in enumerate(items):
            content = str(raw_item.get("content", "")).strip()
            status = str(raw_item.get("status", "pending")).lower()
            active_form = str(raw_item.get("activeForm", "")).strip()

            if not content:
                raise ValueError(f"Item {index}: content required")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Item {index}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1

            normalized.append(PlanItem(
                content=content,
                status=status,
                active_form=active_form,
            ))

        # 不变量：同一时间只能有一个步骤 in_progress。
        # 为什么？① 给模型专注纪律（同时干多件事会分散注意力）；② 给人可读性
        #（任何时刻看列表都能一眼知道"当前在做哪一步"）。
        if in_progress_count > 1:
            raise ValueError("Only one plan item can be in_progress")

        self.state.items = normalized       # 全量替换
        self.state.rounds_since_update = 0  # 刚更新过，计数清零
        return self.render()                # 返回渲染文本，作为 tool_result 让模型看到现状

    def note_round_without_update(self) -> None:
        """记录一轮没有更新计划。"""
        self.state.rounds_since_update += 1

    def reminder(self) -> str | None:
        """如果连续多轮没刷新计划，返回提醒文本；否则返回 None。

        触发要同时满足两个条件：
          ① 计划非空——空计划=任务简单到没建列表，这时催"刷新"既没意义又很吵。
          ② rounds_since_update >= 阈值(3)——连续 3 轮没碰 todo 才算"该催了"。
        """
        if not self.state.items:
            return None
        if self.state.rounds_since_update < PLAN_REMINDER_INTERVAL:
            return None
        return "<reminder>Refresh your current plan before continuing.</reminder>"

    def render(self) -> str:
        """把当前计划渲染成可读文本。"""
        if not self.state.items:
            return "No session plan yet."

        lines = []
        for item in self.state.items:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[item.status]
            line = f"{marker} {item.content}"
            if item.status == "in_progress" and item.active_form:
                line += f" ({item.active_form})"
            lines.append(line)

        completed = sum(1 for item in self.state.items if item.status == "completed")
        lines.append(f"\n({completed}/{len(self.state.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


def safe_path(path_str: str) -> Path:
    """路径安全校验：确保路径不会逃逸出工作区。"""
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path


def run_bash(command: str) -> str:
    """执行 shell 命令，带危险命令拦截和超时保护。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选限制行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as exc:
        return f"Error: {exc}"


def run_write(path: str, content: str) -> str:
    """写入文件，自动创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确替换文件中的文本（只替换第一个匹配）。"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


# 工具分发表。todo 的接入方式和 s02 加任何工具完全一样——加一行，循环不用改。
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # todo 工具：把模型传来的完整计划交给 TodoManager 全量覆盖，返回渲染文本
    "todo": lambda **kw: TODO.update(kw["items"]),
}

# 工具定义列表（JSON Schema 格式）
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "todo",
        "description": "Rewrite the current session plan for multi-step work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Optional present-continuous label.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
]


def extract_text(content) -> str:
    """从模型回复的 content 列表中提取所有文本块。"""
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def agent_loop(messages: list) -> None:
    """核心循环：调模型 → 执行工具 → 喂回结果，直到模型不再调用工具。"""
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})

        if response.stop_reason != "tool_use":
            return

        results = []
        used_todo = False  # 标记这一轮模型有没有调用 todo 工具（提醒机制要用）
        for block in response.content:
            if block.type != "tool_use":
                continue

            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            except Exception as exc:
                output = f"Error: {exc}"

            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })
            if block.name == "todo":
                used_todo = True

        # 计划提醒逻辑（nudge）：
        #   用了 todo → 计数清零；没用 → 计数 +1，再问 reminder() 该不该催。
        if used_todo:
            TODO.state.rounds_since_update = 0
        else:
            TODO.note_round_without_update()
            reminder = TODO.reminder()
            if reminder:
                # 插到 results 最前面：content 块按顺序读，插最前模型先撞见提醒，
                # 不会被后面一大坨 tool_result 淹没。注意这是一个 {"type":"text"} 块，
                # 和其它 {"type":"tool_result"} 块混在同一条 user 消息里——API 允许混装。
                results.insert(0, {"type": "text", "text": reminder})

        messages.append({"role": "user", "content": results})


# ===== 入口：交互式 REPL =====
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
