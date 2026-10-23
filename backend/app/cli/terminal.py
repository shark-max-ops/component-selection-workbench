"""使用中文命令与对话操作供电核验任务。"""

import json
from pathlib import Path

from app.agents.human_input import HumanInputError
from app.cli.display import show_evidence, show_pending, show_report, show_reviews, show_status
from app.services.task_service import (
    ROOT,
    create_task,
    get_task_status,
    load_task,
    resume_task,
    review_supply,
    send_message,
    wait_for_input,
)

HELP = """/new [电压]：创建任务，默认 3.3 V；可用 /new 3.3 | PDF路径 指定资料
/tasks：列出任务   /open 任务ID：打开任务
/status：查看状态   /evidence：查看证据   /report：查看程序结果
/review 请求ID | 当前审核版本 | 审核人 | approve或reject | 说明：审核并重新核验
/reviews：查看审核历史；先查看 /status 和 /evidence 并对照 PDF 核对参数及条件
/wait-review：暂停等待审核   /ask 问题：暂停等待澄清
/resume 中断ID | 回答或审核请求ID：恢复当前等待事项
/raw：查看完整原始数据（排查问题时使用）
/help：帮助   /exit：退出
普通中文消息：发送给当前任务并执行。当前仅核验 ADS1115 供电。
接口、分辨率、通道和采样能力尚未核验；新任务采用固定示例需求。"""


def show_event(event, write=print):
    """
    作用：把真实工具和运行事件展示为中文进度。
    输入：event - 事件字典；write - 输出函数。
    输出：无
    """
    # 只展示可观察事件，不生成思维过程或估算百分比。
    labels = {
        "run_started": "运行开始",
        "run_finished": "运行结束",
        "tool_started": "工具开始",
        "tool_finished": "工具结束",
        "tool_failed": "工具失败",
    }
    # 成功工具只显示结束事件，保留失败提示和完整磁盘事件。
    if event["type"] == "tool_started":
        return
    payload = event["payload"]
    write(
        f"[{event['sequence']}] {labels[event['type']]} "
        f"{payload.get('tool', '')} {payload.get('path', '')} "
        f"{payload.get('status', payload.get('reason', ''))}"
    )


def run_terminal(*, root: Path = ROOT, read=input, write=print):
    """
    作用：运行终端命令循环并分发查询、审核与对话。
    输入：root - 任务根目录；read - 输入函数；write - 输出函数。
    输出：无
    """
    # 选中的任务只保存在当前终端，重启后使用 /open 找回。
    current = None
    write("器件核验工作台 · 输入 /tasks 查看任务，/help 查看命令。")
    while True:
        try:
            line = read(f"{current[:13] + '…' if current else '未打开任务'}> ").strip()
            if not line:
                continue
            command, _, argument = line.partition(" ")
            argument = argument.strip()

            # 帮助和退出不读取业务状态，也不调用模型。
            if command == "/exit":
                write("已退出；任务与已保存的等待事项可用 /open 再次打开。")
                return
            if command == "/help":
                write(HELP)
                continue

            # 创建只固定输入；用户发送普通消息后才执行模型。
            if command == "/new":
                voltage, separator, pdf = argument.partition("|")
                meta = create_task(
                    voltage=voltage.strip() or "3.3",
                    root=root,
                    pdf=Path(pdf.strip().strip('"')) if separator else None,
                )
                current = meta["task_id"]
                write(f"已创建 {current}；仅供电核验，未审核。输入“开始核验”执行。")
                continue
            if command == "/tasks":
                tasks = sorted(root.glob("task-*/task.json")) if root.exists() else []
                write("\n".join(path.parent.name for path in tasks) or "暂无任务。")
                continue
            if command == "/open":
                meta = load_task(argument, root=root)
                current = meta["task_id"]
                write(f"已打开 {current}；当前仅供电核验。")
                continue

            # 拼错命令直接提示，避免把命令文本误发给付费模型。
            if command.startswith("/") and command not in {
                "/raw",
                "/status",
                "/evidence",
                "/report",
                "/review",
                "/reviews",
                "/wait-review",
                "/ask",
                "/resume",
            }:
                write("未知命令，请输入 /help。")
                continue
            if current is None:
                write("请先 /new 或 /open 任务ID。")
                continue

            # 查询直接读取保存的记录；报告显示结构化程序结果。
            if command in {"/status", "/evidence", "/report", "/reviews"}:
                status = get_task_status(current, root=root)
                if command == "/status":
                    show_status(status, write)
                elif command == "/evidence":
                    show_evidence(status, write)
                elif command == "/reviews":
                    show_reviews(status.get("reviews", []), write)
                else:
                    show_report(status.get("adc_records", {}).get("constraint_result"), write)
                continue

            if command == "/raw":
                write(json.dumps(get_task_status(current, root=root), ensure_ascii=False, indent=2))
                continue

            if command == "/review":
                fields = [item.strip() for item in argument.split("|", 4)]
                if len(fields) != 5:
                    write("用法：/review 请求ID | 当前审核版本 | 审核人 | approve或reject | 说明")
                    continue
                request_id, revision, reviewer, decision, note = fields
                record = review_supply(
                    current,
                    request_id=request_id,
                    expected_revision=int(revision),
                    reviewer=reviewer,
                    decision=decision,
                    note=note,
                    root=root,
                )
                write(f"已保存审核版本 {record['revision']}；本次未调用模型。")
                show_report(record["report"], write)
                write("下一步：/status 查看是否仍在等待恢复。")
                continue

            if command in {"/wait-review", "/ask"}:
                options = (
                    {"kind": "clarification", "question": argument} if command == "/ask" else {}
                )
                result = wait_for_input(current, root=root, **options)
                show_pending(result["pending"], write)
                write("任务已暂停；可退出后重新 /open，并用 /resume 恢复。")
                continue
            if command == "/resume":
                interrupt_id, separator, answer = argument.partition("|")
                if not separator:
                    write("用法：/resume 中断ID | 回答或审核请求ID")
                    continue
                result = resume_task(current, interrupt_id.strip(), answer.strip(), root=root)
                show_report(result["report"], write)
                write("恢复答复：" + str(result["reply"]))
                continue

            # 对话才进入实际执行，并分别展示模型解释与程序核验。
            result = send_message(
                current, line, root=root, on_event=lambda event: show_event(event, write)
            )
            write("模型答复：" + str(result["reply"]))
            show_report(result["report"], write)
        except (EOFError, KeyboardInterrupt):
            # 执行中断不承诺任意位置恢复；服务已记录失败清单。
            write("终端已退出；执行中断可能留下未完成节点，暂不支持自动恢复。")
            return
        except HumanInputError as exc:
            write(str(exc))
        except Exception as exc:
            # 不展示可能含配置的异常内容，也不把执行失败当作器件不满足。
            write(f"操作失败（{type(exc).__name__}）。请检查任务ID、输入或运行归档。")
