"""记录真实工具事件，供终端展示和运行复查。"""

import json
from datetime import UTC, datetime
from threading import Lock

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class RunEvents(AgentMiddleware):
    """
    作用：将真实工具调用记录为有序运行事件。
    输入：task_id - 任务 ID；run_id - 运行 ID；path - 事件文件；on_event - 可选回调。
    输出：RunEvents 事件中间件对象。
    """

    def __init__(self, task_id, run_id, path, on_event=None):
        """
        作用：绑定当前运行的事件出口并初始化序号与锁。
        输入：task_id - 任务 ID；run_id - 运行 ID；path - 事件文件；on_event - 可选回调。
        输出：无
        """
        # 每次运行从 1 编号；锁保证并行工具写入时不混行。
        self.task_id, self.run_id = task_id, run_id
        self.path, self.on_event = path, on_event
        self.sequence = 0
        self.lock = Lock()

    def emit(self, event_type, **payload):
        """
        作用：追加事件记录并通知展示回调。
        输入：event_type - 事件类型；payload - 事件载荷。
        输出：无
        """
        # 先落盘再通知界面，界面故障不能破坏业务执行。
        with self.lock:
            self.sequence += 1
            event = dict(
                task_id=self.task_id,
                run_id=self.run_id,
                sequence=self.sequence,
                time=datetime.now(UTC).isoformat(),
                type=event_type,
                payload=payload,
            )
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            if self.on_event:
                try:
                    self.on_event(event)
                except Exception:
                    # 展示失败不改变工具结果；磁盘事件仍可复查。
                    pass

    def wrap_tool_call(self, request, handler):
        """
        作用：记录工具执行的开始、结束或失败事件。
        输入：request - 工具请求；handler - 实际执行器。
        输出：工具原始响应。
        """
        # 只展示名称、调用标识和文件位置，不记录完整工具参数。
        call = request.tool_call
        detail = dict(tool=call["name"], call_id=call["id"])
        path = call["args"].get("file_path")
        if isinstance(path, str):
            detail["path"] = path
        self.emit("tool_started", **detail)
        try:
            response = handler(request)
        except (Exception, KeyboardInterrupt) as exc:
            self.emit("tool_failed", **detail, reason=type(exc).__name__)
            raise

        # 部分业务中间件返回 Command，从中取消息判断执行状态。
        messages = (
            response.update.get("messages", [])
            if isinstance(response, Command) and isinstance(response.update, dict)
            else [response]
        )
        failed = any(isinstance(item, ToolMessage) and item.status == "error" for item in messages)
        self.emit(
            "tool_failed" if failed else "tool_finished",
            **detail,
            reason="工具返回错误，详见 messages.json" if failed else "执行结束",
        )
        return response
