#!/usr/bin/env python3
# 线束机制：集成 -- 工具不只存在于你的代码中
"""
s19_mcp_plugin.py - MCP 与插件系统

本教学章节聚焦最小有用思想：
外部进程可以暴露工具，你的 agent 经过少量标准化后可以像原生工具一样使用它们。

最小路径：
  1. 启动 MCP server 进程
  2. 询问它有哪些工具
  3. 添加前缀并注册这些工具
  4. 将匹配的调用路由到该 server

插件多加一层：发现。一个小清单告诉 agent 启动哪个外部 server。

核心洞察："外部工具应进入同一个工具管道，而不是形成完全独立的世界。"
实践中意味着共享权限检查和标准化的 tool_result 载荷。

建议按此顺序阅读：
1. CapabilityPermissionGate：外部工具仍然经过同一个控制门。
2. MCPClient：一个 server 连接如何暴露工具规格和工具调用。
3. PluginLoader：清单如何声明外部 server。
4. MCPToolRouter / build_tool_pool：原生工具和外部工具如何合并为一个池。

最常见的困惑：
- 插件清单不是 MCP server
- MCP server 不是单个 MCP 工具
- 外部能力不会绕过原生权限路径

教学边界：
本文件教最小有用的 stdio MCP 路径。
市场细节、认证流程、重连逻辑和非工具能力层故意留给桥接文档和后续扩展。
"""

import json
import os
import subprocess
import threading
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PERMISSION_MODES = ("default", "auto")


class CapabilityPermissionGate:
    """
    原生工具与外部能力共享的权限门。

    教学目标很简单：MCP 不绕过控制面。
    原生工具和 MCP 工具都先标准化为能力意图，
    然后通过同一个 allow / ask 策略。
    """

    READ_PREFIXES = ("read", "list", "get", "show", "search", "query", "inspect")
    HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown")

    def __init__(self, mode: str = "default"):
        self.mode = mode if mode in PERMISSION_MODES else "default"

    def normalize(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name.startswith("mcp__"):
            _, server_name, actual_tool = tool_name.split("__", 2)
            source = "mcp"
        else:
            server_name = None
            actual_tool = tool_name
            source = "native"

        lowered = actual_tool.lower()
        if actual_tool == "read_file" or lowered.startswith(self.READ_PREFIXES):
            risk = "read"
        elif actual_tool == "bash":
            command = tool_input.get("command", "")
            risk = "high" if any(
                token in command for token in ("rm -rf", "sudo", "shutdown", "reboot")
            ) else "write"
        elif lowered.startswith(self.HIGH_RISK_PREFIXES):
            risk = "high"
        else:
            risk = "write"

        return {
            "source": source,
            "server": server_name,
            "tool": actual_tool,
            "risk": risk,
        }

    def check(self, tool_name: str, tool_input: dict) -> dict:
        intent = self.normalize(tool_name, tool_input)

        if intent["risk"] == "read":
            return {"behavior": "allow", "reason": "Read capability", "intent": intent}

        if self.mode == "auto" and intent["risk"] != "high":
            return {
                "behavior": "allow",
                "reason": "Auto mode for non-high-risk capability",
                "intent": intent,
            }

        if intent["risk"] == "high":
            return {
                "behavior": "ask",
                "reason": "High-risk capability requires confirmation",
                "intent": intent,
            }

        return {
            "behavior": "ask",
            "reason": "State-changing capability requires confirmation",
            "intent": intent,
        }

    def ask_user(self, intent: dict, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        source = (
            f"{intent['source']}:{intent['server']}/{intent['tool']}"
            if intent.get("server")
            else f"{intent['source']}:{intent['tool']}"
        )
        print(f"\n  [Permission] {source} risk={intent['risk']}: {preview}")
        try:
            answer = input("  Allow? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")


permission_gate = CapabilityPermissionGate()


class MCPClient:
    """
    基于 stdio 的最小 MCP 客户端。

    足以教授核心架构，无需一开始就拖着读者走遍每种传输、认证流程或市场细节。
    """

    def __init__(self, server_name: str, command: str, args: list = None, env: dict = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.process = None
        self._request_id = 0
        self._tools = []  # 缓存的工具列表

    def connect(self):
        """启动 MCP server 进程。"""
        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
            )
            # 发送初始化请求
            self._send({"method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "teaching-agent", "version": "1.0"},
            }})
            response = self._recv()
            if response and "result" in response:
                # 发送已初始化通知
                self._send({"method": "notifications/initialized"})
                return True
        except FileNotFoundError:
            print(f"[MCP] Server command not found: {self.command}")
        except Exception as e:
            print(f"[MCP] Connection failed: {e}")
        return False

    def list_tools(self) -> list:
        """从 server 获取可用工具列表。"""
        self._send({"method": "tools/list", "params": {}})
        response = self._recv()
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """在 server 上执行工具。"""
        self._send({"method": "tools/call", "params": {
            "name": tool_name,
            "arguments": arguments,
        }})
        response = self._recv()
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(c.get("text", str(c)) for c in content)
        if response and "error" in response:
            return f"MCP Error: {response['error'].get('message', 'unknown')}"
        return "MCP Error: no response"

    def get_agent_tools(self) -> list:
        """
        将 MCP 工具转换为 agent 工具格式。

        教学版本使用同样的简单前缀方案：
        mcp__{server_name}__{tool_name}
        """
        agent_tools = []
        for tool in self._tools:
            prefixed_name = f"mcp__{self.server_name}__{tool['name']}"
            agent_tools.append({
                "name": prefixed_name,
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                "_mcp_server": self.server_name,
                "_mcp_tool": tool["name"],
            })
        return agent_tools

    def disconnect(self):
        """关闭 server 进程。"""
        if self.process:
            try:
                self._send({"method": "shutdown"})
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None

    def _send(self, message: dict):
        if not self.process or self.process.poll() is not None:
            return
        self._request_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._request_id, **message}
        line = json.dumps(envelope) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _recv(self) -> dict | None:
        if not self.process or self.process.poll() is not None:
            return None
        try:
            line = self.process.stdout.readline()
            if line:
                return json.loads(line)
        except (json.JSONDecodeError, OSError):
            pass
        return None


class PluginLoader:
    """
    从 .claude-plugin/ 目录加载插件。

    教学版本实现最小有用的插件流程：
    读取清单、发现 MCP server 配置并注册。
    """

    def __init__(self, search_dirs: list = None):
        self.search_dirs = search_dirs or [WORKDIR]
        self.plugins = {}  # name -> manifest

    def scan(self) -> list:
        """扫描目录查找 .claude-plugin/plugin.json 清单。"""
        found = []
        for search_dir in self.search_dirs:
            plugin_dir = Path(search_dir) / ".claude-plugin"
            manifest_path = plugin_dir / "plugin.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    name = manifest.get("name", plugin_dir.parent.name)
                    self.plugins[name] = manifest
                    found.append(name)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Plugin] Failed to load {manifest_path}: {e}")
        return found

    def get_mcp_servers(self) -> dict:
        """
        从已加载的插件中提取 MCP server 配置。
        返回 {server_name: {command, args, env}}。
        """
        servers = {}
        for plugin_name, manifest in self.plugins.items():
            for server_name, config in manifest.get("mcpServers", {}).items():
                servers[f"{plugin_name}__{server_name}"] = config
        return servers


class MCPToolRouter:
    """
    将工具调用路由到正确的 MCP server。

    MCP 工具以 mcp__{server}__{tool} 为前缀，与原生工具共存于同一个工具池。
    路由器剥离前缀并分发到正确的 MCPClient。
    """

    def __init__(self):
        self.clients = {}  # server_name -> MCPClient 映射

    def register_client(self, client: MCPClient):
        self.clients[client.server_name] = client

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call(self, tool_name: str, arguments: dict) -> str:
        """将 MCP 工具调用路由到正确的 server。"""
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: Invalid MCP tool name: {tool_name}"
        _, server_name, actual_tool = parts
        client = self.clients.get(server_name)
        if not client:
            return f"Error: MCP server not found: {server_name}"
        return client.call_tool(actual_tool, arguments)

    def get_all_tools(self) -> list:
        """从所有已连接的 MCP server 收集工具。"""
        tools = []
        for client in self.clients.values():
            tools.extend(client.get_agent_tools())
        return tools


# -- 原生工具实现（与 s02 相同） --
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

def run_read(path: str) -> str:
    try:
        return safe_path(path).read_text()[:50000]
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


NATIVE_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

NATIVE_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- MCP 工具路由器（全局） --
mcp_router = MCPToolRouter()
plugin_loader = PluginLoader()


def build_tool_pool() -> list:
    """
    组装完整工具池：原生 + MCP 工具。

    名称冲突时原生工具优先，确保添加外部工具后本地核心仍然可预测。
    """
    all_tools = list(NATIVE_TOOLS)
    mcp_tools = mcp_router.get_all_tools()

    native_names = {t["name"] for t in all_tools}
    for tool in mcp_tools:
        if tool["name"] not in native_names:
            all_tools.append(tool)

    return all_tools


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """分发到原生处理器或 MCP 路由器。"""
    if mcp_router.is_mcp_tool(tool_name):
        return mcp_router.call(tool_name, tool_input)
    handler = NATIVE_HANDLERS.get(tool_name)
    if handler:
        return handler(**tool_input)
    return f"Unknown tool: {tool_name}"


def normalize_tool_result(tool_name: str, output: str, intent: dict | None = None) -> str:
    intent = intent or permission_gate.normalize(tool_name, {})
    status = "error" if "Error:" in output or "MCP Error:" in output else "ok"
    payload = {
        "source": intent["source"],
        "server": intent.get("server"),
        "tool": intent["tool"],
        "risk": intent["risk"],
        "status": status,
        "preview": output[:500],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def agent_loop(messages: list):
    """统一原生 + MCP 工具池的 agent 循环。"""
    tools = build_tool_pool()

    while True:
        system = (
            f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.\n"
            "You have both native tools and MCP tools available.\n"
            "MCP tools are prefixed with mcp__{server}__{tool}.\n"
            "All capabilities pass through the same permission gate before execution."
        )
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=tools, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            decision = permission_gate.check(block.name, block.input or {})
            try:
                if decision["behavior"] == "deny":
                    output = f"Permission denied: {decision['reason']}"
                elif decision["behavior"] == "ask" and not permission_gate.ask_user(
                    decision["intent"], block.input or {}
                ):
                    output = f"Permission denied by user: {decision['reason']}"
                else:
                    output = handle_tool_call(block.name, block.input or {})
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": normalize_tool_result(
                    block.name,
                    str(output),
                    decision.get("intent"),
                ),
            })

        messages.append({"role": "user", "content": results})


# 后续可添加的升级：
# - 更多传输方式
# - 认证 / 审批流程
# - server 重连和生命周期管理
# - 外部工具到达模型前的过滤
# - 更丰富的插件安装和更新处理


if __name__ == "__main__":
    # 扫描插件
    found = plugin_loader.scan()
    if found:
        print(f"[Plugins loaded: {', '.join(found)}]")
        for server_name, config in plugin_loader.get_mcp_servers().items():
            mcp_client = MCPClient(server_name, config.get("command", ""), config.get("args", []))
            if mcp_client.connect():
                mcp_client.list_tools()
                mcp_router.register_client(mcp_client)
                print(f"[MCP] Connected to {server_name}")

    tool_count = len(build_tool_pool())
    mcp_count = len(mcp_router.get_all_tools())
    print(f"[Tool pool: {tool_count} tools ({mcp_count} from MCP)]")

    history = []
    while True:
        try:
            query = input("\033[36ms19 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.strip() == "/tools":
            for tool in build_tool_pool():
                prefix = "[MCP] " if tool["name"].startswith("mcp__") else "       "
                print(f"  {prefix}{tool['name']}: {tool.get('description', '')[:60]}")
            continue

        if query.strip() == "/mcp":
            if mcp_router.clients:
                for name, c in mcp_router.clients.items():
                    tools = c.get_agent_tools()
                    print(f"  {name}: {len(tools)} tools")
            else:
                print("  (no MCP servers connected)")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()

    # 清理 MCP 连接
    for c in mcp_router.clients.values():
        c.disconnect()
