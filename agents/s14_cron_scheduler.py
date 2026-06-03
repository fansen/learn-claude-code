#!/usr/bin/env python3
# 线束机制：时间 -- Agent 自行调度未来的工作
"""
s14_cron_scheduler.py - Cron 定时任务

Agent 可以使用标准 cron 表达式调度 prompt 在未来执行。当调度时间匹配当前时间时，
会将通知推入主对话循环。

    Cron expression: 5 fields
    +-------+-------+-------+-------+-------+
    | min   | hour  | dom   | month | dow   |
    | 0-59  | 0-23  | 1-31  | 1-12  | 0-6   |
    +-------+-------+-------+-------+-------+
    Examples:
      "*/5 * * * *"   -> every 5 minutes
      "0 9 * * 1"     -> Monday 9:00 AM
      "30 14 * * *"   -> daily 2:30 PM

    两种持久化模式：
    +--------------------+-------------------------------+
    | session-only       | 内存列表，退出即丢失          |
    | durable            | .claude/scheduled_tasks.json  |
    +--------------------+-------------------------------+

    两种触发模式：
    +--------------------+-------------------------------+
    | recurring          | 重复执行直到删除或 7 天自动过期 |
    | one-shot           | 执行一次后自动删除             |
    +--------------------+-------------------------------+

    Jitter（抖动）：recurring 任务可以避开整分钟边界。

    Architecture:
    +-------------------------------+
    |  Background thread            |
    |  (checks every 1 second)      |
    |                               |
    |  for each task:               |
    |    if cron_matches(now):      |
    |      enqueue notification     |
    +-------------------------------+
              |
              v
    [notification_queue]
              |
         (drained at top of agent_loop)
              |
              v
    [injected as user messages before LLM call]

核心思想：调度记住未来的工作，时间到了再交回同一个主循环处理。
"""

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue, Empty

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = WORKDIR / ".claude" / "cron.lock"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]  # recurring 任务避开这些整分钟
JITTER_OFFSET_MAX = 4     # 偏移范围（分钟）
# 教学版本：需要时使用简单的 1-4 分钟偏移。


class CronLock:
    """
    基于 PID 文件的锁，防止多个会话触发同一个 cron 任务。
    """

    def __init__(self, lock_path: Path = None):
        self._lock_path = lock_path or CRON_LOCK_FILE

    def acquire(self) -> bool:
        """
        尝试获取 cron 锁。成功返回 True。

        如果锁文件存在，检查其中的 PID 对应进程是否仍然存活。
        如果进程已死，说明锁已过期，可以接管。
        """
        if self._lock_path.exists():
            try:
                stored_pid = int(self._lock_path.read_text().strip())
                # PID 存活探测：发送信号 0（无操作）检查进程是否存在
                os.kill(stored_pid, 0)
                # 进程存活 -- 锁被另一个会话持有
                return False
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                # 过期锁（进程已死或 PID 无法解析）-- 移除
                pass
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        """如果锁文件属于当前进程则移除。"""
        try:
            if self._lock_path.exists():
                stored_pid = int(self._lock_path.read_text().strip())
                if stored_pid == os.getpid():
                    self._lock_path.unlink()
        except (ValueError, OSError):
            pass


def cron_matches(expr: str, dt: datetime) -> bool:
    """
    检查 5 字段 cron 表达式是否匹配给定的 datetime。

    字段：minute hour day-of-month month day-of-week
    支持：*（任意）、*/N（每 N）、N（精确）、N-M（范围）、N,M（列表）

    无外部依赖 -- 简单手动匹配。
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    # Python weekday：0=周一；cron：0=周日。需要转换。
    cron_dow = (dt.weekday() + 1) % 7
    values[4] = cron_dow
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """匹配单个 cron 字段与值。"""
    if field == "*":
        return True

    for part in field.split(","):
        # 处理步长：*/N 或 N-M/S
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)

        if part == "*":
            # */N -- 检查值是否在步长网格上
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            # 范围：N-M
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            # 精确值
            if int(part) == value:
                return True

    return False


class CronScheduler:
    """
    管理定时任务的后台检查。

    教学版本只保留核心部件：调度记录、分钟检查器、可选持久化和通知队列。
    """

    def __init__(self):
        self.tasks = []        # 任务字典列表
        self.queue = Queue()   # 通知队列
        self._stop_event = threading.Event()
        self._thread = None
        self._last_check_minute = -1  # 避免同一分钟内重复触发

    def start(self):
        """加载持久化任务并启动后台检查线程。"""
        self._load_durable()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        count = len(self.tasks)
        if count:
            print(f"[Cron] Loaded {count} scheduled tasks")

    def stop(self):
        """停止后台线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, cron_expr: str, prompt: str,
               recurring: bool = True, durable: bool = False) -> str:
        """创建新的定时任务。返回任务 ID。"""
        task_id = str(uuid.uuid4())[:8]
        now = time.time()

        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": now,
        }

        # recurring 任务的 jitter：如果 cron 在 :00 或 :30 触发，
        # 记录下来以便稍微偏移检查时间
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)

        self.tasks.append(task)
        if durable:
            self._save_durable()

        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"
        return f"Created task {task_id} ({mode}, {store}): cron={cron_expr}"

    def delete(self, task_id: str) -> str:
        """按 ID 删除定时任务。"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"Deleted task {task_id}"
        return f"Task {task_id} not found"

    def list_tasks(self) -> str:
        """列出所有定时任务。"""
        if not self.tasks:
            return "No scheduled tasks."
        lines = []
        for t in self.tasks:
            mode = "recurring" if t["recurring"] else "one-shot"
            store = "durable" if t["durable"] else "session"
            age_hours = (time.time() - t["createdAt"]) / 3600
            lines.append(
                f"  {t['id']}  {t['cron']}  [{mode}/{store}] "
                f"({age_hours:.1f}h old): {t['prompt'][:60]}"
            )
        return "\n".join(lines)

    def drain_notifications(self) -> list[str]:
        """排空队列中所有待处理的通知。"""
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications

    def _compute_jitter(self, cron_expr: str) -> int:
        """如果 cron 目标为 :00 或 :30，返回一个小偏移量（1-4 分钟）。"""
        fields = cron_expr.strip().split()
        if len(fields) < 1:
            return 0
        minute_field = fields[0]
        try:
            minute_val = int(minute_field)
            if minute_val in JITTER_MINUTES:
                # 基于表达式哈希的确定性 jitter
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0

    def _check_loop(self):
        """后台线程：每秒检查是否有任务到期。"""
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute

            # 每分钟只检查一次，避免重复触发
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime):
        """将所有任务与当前时间比对，触发匹配的任务。"""
        expired = []
        fired_oneshots = []

        for task in self.tasks:
            # 自动过期：超过 7 天的 recurring 任务
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue

            # 匹配检查时应用 jitter 偏移
            check_time = now
            jitter = task.get("jitter_offset", 0)
            if jitter:
                check_time = now - timedelta(minutes=jitter)

            if cron_matches(task["cron"], check_time):
                notification = (
                    f"[Scheduled task {task['id']}]: {task['prompt']}"
                )
                self.queue.put(notification)
                task["last_fired"] = time.time()
                print(f"[Cron] Fired: {task['id']}")

                if not task["recurring"]:
                    fired_oneshots.append(task["id"])

        # 清理过期和 one-shot 任务
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            for tid in expired:
                print(f"[Cron] Auto-expired: {tid} (older than {AUTO_EXPIRY_DAYS} days)")
            for tid in fired_oneshots:
                print(f"[Cron] One-shot completed and removed: {tid}")
            self._save_durable()

    def _load_durable(self):
        """从 .claude/scheduled_tasks.json 加载持久化任务。"""
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text())
            # 只加载持久化任务
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[Cron] Error loading tasks: {e}")

    def detect_missed_tasks(self) -> list[dict]:
        """
        启动时检查每个持久化任务的 last_fired 时间。

        如果某个任务在会话关闭期间应该触发过（即 last_fired 到现在之间
        存在至少一个 cron 匹配），则标记为 missed。调用方可以让用户决定
        是执行还是丢弃每个错过的任务。
        """
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            last_dt = datetime.fromtimestamp(last_fired)
            # 从 last_fired 逐分钟向前遍历到现在（上限 24 小时）
            check = last_dt + timedelta(minutes=1)
            cap = min(now, last_dt + timedelta(hours=24))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append({
                        "id": task["id"],
                        "cron": task["cron"],
                        "prompt": task["prompt"],
                        "missed_at": check.isoformat(),
                    })
                    break  # 一次 miss 就足以标记
                check += timedelta(minutes=1)
        return missed

    def _save_durable(self):
        """将持久化任务保存到磁盘。"""
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(
            json.dumps(durable, indent=2) + "\n"
        )


# 全局调度器
scheduler = CronScheduler()


# -- 工具实现 --
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
    "bash":        lambda **kw: run_bash(kw["command"]),
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "cron_create": lambda **kw: scheduler.create(
        kw["cron"], kw["prompt"], kw.get("recurring", True), kw.get("durable", False)),
    "cron_delete": lambda **kw: scheduler.delete(kw["id"]),
    "cron_list":   lambda **kw: scheduler.list_tasks(),
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
    {"name": "cron_create", "description": "Schedule a recurring or one-shot task with a cron expression.",
     "input_schema": {"type": "object", "properties": {
         "cron": {"type": "string", "description": "5-field cron expression: 'min hour dom month dow'"},
         "prompt": {"type": "string", "description": "The prompt to inject when the task fires"},
         "recurring": {"type": "boolean", "description": "true=repeat, false=fire once then delete. Default true."},
         "durable": {"type": "boolean", "description": "true=persist to disk, false=session-only. Default false."},
     }, "required": ["cron", "prompt"]}},
    {"name": "cron_delete", "description": "Delete a scheduled task by ID.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "Task ID to delete"},
     }, "required": ["id"]}},
    {"name": "cron_list", "description": "List all scheduled tasks.",
     "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.\n\nYou can schedule future work with cron_create. Tasks fire automatically and their prompts are injected into the conversation."


def agent_loop(messages: list):
    """
    感知 cron 的 agent 循环。

    每次 LLM 调用前排空通知队列，将触发的任务 prompt 作为 user 消息注入。
    这就是 agent "醒来"处理定时工作的方式。
    """
    while True:
        # 排空定时任务通知
        notifications = scheduler.drain_notifications()
        for note in notifications:
            print(f"[Cron notification] {note[:100]}")
            messages.append({"role": "user", "content": note})

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
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**(block.input or {})) if handler else f"Unknown: {block.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    scheduler.start()
    print("[Cron scheduler running. Background checks every second.]")
    print("[Commands: /cron to list tasks, /test to fire a test notification]")

    history = []
    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            scheduler.stop()
            break
        if query.strip().lower() in ("q", "exit", ""):
            scheduler.stop()
            break

        if query.strip() == "/cron":
            print(scheduler.list_tasks())
            continue

        if query.strip() == "/test":
            # 手动入队一个测试通知用于演示
            scheduler.queue.put("[Scheduled task test-0000]: This is a test notification.")
            print("[Test notification enqueued. It will be injected on your next message.]")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
