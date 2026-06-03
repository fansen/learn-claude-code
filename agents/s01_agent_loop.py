#!/usr/bin/env python3
# Harness: the loop -- keep feeding real tool results back into the model.
"""
s01_agent_loop.py - Agent Loop（智能体循环）

AI 编程智能体的核心模式，整个秘密就在一个循环里：

    user message
      -> model reply                    # 模型回复
      -> if tool_use: execute tools     # 如果要调用工具就执行
      -> write tool_result to messages  # 把工具结果写回对话历史
      -> continue                       # 继续循环

    +----------+      +-------+      +---------+
    |  用户    | ---> |  LLM  | ---> |  工具   |
    |  输入    |      | 大模型 |      |  执行   |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   工具结果     |
                          +---------------+
                          （循环继续）

核心思路：把工具执行结果喂回模型，直到模型自己决定停下来。
生产级的智能体在这个循环之上再叠加策略、钩子和生命周期控制。

本文件刻意保持循环精简，但把循环状态（LoopState）显式抽出来，
方便后续章节在同一结构上扩展。
"""

import os
import subprocess
from dataclasses import dataclass

# 修复 macOS 终端下中文/UTF-8 字符的退格和输入问题
try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

# Anthropic SDK 跟 Anthropic 公司没有绑定关系，它只是一个懂 Anthropic API 格式的 HTTP 客户端。
# 发给谁取决于 base_url —— 可以是 Anthropic、DeepSeek、或任何兼容该协议的服务。
#
# SDK 的作用就是帮你省去手写 HTTP 请求的麻烦：
#
#   你写的代码                            SDK 帮你做的
#   ─────────                            ──────────
#   client.messages.create(              → 构造 HTTP 请求体（JSON 格式）
#       model="deepseek-chat",           → 设置请求头（Content-Type、API Key...）
#       messages=[...],                  → POST 发送到 base_url + /v1/messages
#       tools=[...]                      → 解析响应 JSON 为 Python 对象
#   )                                    → 处理错误、重试、流式传输等
#
# 不用 SDK，自己用 requests 手写也完全可以，效果一模一样：
#
#   requests.post("https://api.deepseek.com/anthropic/v1/messages",
#       headers={"x-api-key": "sk-xxx", "anthropic-version": "2023-06-01"},
#       json={"model": "deepseek-chat", "messages": [...], "max_tokens": 8000})
#
# 这里用的是 Python SDK（pip install anthropic），而 Claude Code CLI 用的是
# TypeScript SDK（@anthropic-ai/sdk，npm 包）。两者只是不同语言的封装，
# 底层都是调同一个 HTTP API（POST /v1/messages），效果一样。
from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量（override=True 表示 .env 优先于已有环境变量）
load_dotenv(override=True)

# 使用自定义 base_url 时，移除 SDK 自带的认证 token，避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化 API 客户端。base_url 决定请求发往哪个服务（Anthropic / DeepSeek / 其他兼容端点）
# Claude Code CLI 也是同样的机制：设置 ANTHROPIC_BASE_URL 环境变量即可切换后端
#
# 整体流程：你的代码 → Anthropic SDK（按协议打包）→ DeepSeek 服务器（按协议解析并响应）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 实际调用的模型由 MODEL_ID 环境变量决定（如 deepseek-chat、claude-sonnet-4-20250514 等）
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型它是一个编程智能体，使用 bash 工具检查和修改工作区
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to inspect and change the workspace. Act first, then report clearly."
)

# 工具定义：目前只有一个 bash 工具，模型可以通过它执行任意 shell 命令
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command in the current workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


@dataclass
class LoopState:
    """
    最小循环状态：对话历史、轮次计数、以及为什么要继续循环。

    把状态显式抽成一个对象，是为了让后续章节可以在同一结构上扩展
    （比如加权限检查、加 hook 触发、加上下文压缩），而不用改循环本身。
    """
    messages: list            # 完整对话历史
    turn_count: int = 1       # 当前是第几轮工具调用
    transition_reason: str | None = None  # 为什么进入下一轮（如 "tool_result"）


def run_bash(command: str) -> str:
    """执行 shell 命令并返回输出，带有基本的危险命令拦截和超时保护。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    output = (result.stdout + result.stderr).strip()
    # 限制输出长度为 50000 字符，防止超长输出撑爆上下文
    return output[:50000] if output else "(no output)"


def extract_text(content) -> str:
    """从模型回复的 content 列表中提取所有文本块，拼成一个字符串。"""
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def execute_tool_calls(response_content) -> list[dict]:
    """
    遍历模型回复中的所有 tool_use 块，逐个执行，收集结果。

    返回的列表格式符合 Anthropic API 的 tool_result 约定，
    可以直接作为 "user" 消息的 content 喂回模型。
    """
    results = []
    for block in response_content:
        if block.type != "tool_use":
            continue
        command = block.input["command"]
        # 黄色打印正在执行的命令
        print(f"\033[33m$ {command}\033[0m")
        output = run_bash(command)
        # 只预览前 200 字符
        print(output[:200])
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })
    return results


def run_one_turn(state: LoopState) -> bool:
    """
    执行一轮：调用模型 → 执行工具 → 更新状态。

    返回 True 表示还要继续循环（模型调用了工具），
    返回 False 表示循环结束（模型给出了文本回复，或没有工具调用）。
    """
    # 1. 调用模型，传入完整对话历史 + 可用工具列表
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=state.messages,
        tools=TOOLS,
        max_tokens=8000,
    )
    # 2. 把模型的回复追加到对话历史（保持上下文完整）
    state.messages.append({"role": "assistant", "content": response.content})

    # 3. 如果模型没有调用工具，说明它已经给出了最终回答，退出循环
    if response.stop_reason != "tool_use":
        state.transition_reason = None
        return False

    # 4. 执行所有工具调用
    results = execute_tool_calls(response.content)
    if not results:
        state.transition_reason = None
        return False

    # 5. 把工具执行结果作为 "user" 消息追加，喂回模型（这是 Anthropic API 的约定格式）
    state.messages.append({"role": "user", "content": results})
    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


def agent_loop(state: LoopState) -> None:
    """
    核心智能体循环。

    不断调用 run_one_turn（模型 → 工具 → 结果喂回），
    直到模型返回的 stop_reason 不再是 "tool_use"（即模型认为任务完成）。
    """
    while run_one_turn(state):
        pass


# ===== 入口：交互式 REPL =====
if __name__ == "__main__":
    history = []  # 对话历史，在整个会话期间持续累积
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")  # 青色提示符
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 用户输入追加到对话历史
        history.append({"role": "user", "content": query})
        state = LoopState(messages=history)
        # 运行智能体循环（会原地修改 history，追加助手回复和工具调用结果）
        agent_loop(state)

        # 打印模型的最终文本回复
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
