"""将任务记录展示为面向人工核对的中文摘要。"""

from datetime import datetime, timedelta, timezone

RESULTS = {
    "satisfied": "满足",
    "not_satisfied": "不满足",
    "unknown": "待确认",
    "conflict": "证据冲突",
}
STATES = {
    "created": "尚未执行",
    "completed": "已完成",
    "waiting_input": "等待人工输入",
    "running": "运行中",
    "failed": "执行失败",
    "unfinished": "存在未完成步骤",
}


def show_report(report, write=print):
    """
    作用：突出程序结论、依据和待解决项。
    输入：report - 报告或单项判定；write - 输出函数。
    输出：无
    """
    result = (report or {}).get("constraint_result", report or {})
    write("\n── 程序核验 · 供电 ──")
    if not result:
        write("尚无核验结果。")
        return
    write("结论：" + RESULTS.get(result.get("status"), "尚无判定"))
    if result.get("reason"):
        write("依据：" + result["reason"])
    for item in result.get("unresolved", []):
        write("待办：" + item)
    write("范围：仅供电；接口、通道和采样率等尚未核验。")


def show_pending(pending, write=print, reviews=None):
    """
    作用：展示等待问题与可复制的恢复命令。
    输入：pending - 中断列表；write - 输出函数；reviews - 审核历史。
    输出：无
    """
    for item in pending:
        value = item.get("value", {})
        write("\n── 等待处理 ──")
        write(value.get("question", "请处理当前等待事项。"))
        answer = "你的回答"
        if value.get("kind") == "review":
            latest = reviews[-1] if reviews else {}
            if latest.get("revision", 0) > value.get("review_revision", 0):
                answer = latest["request_id"]
            else:
                answer = "本次审核请求ID"
                write("下一步：/evidence 核对原文 → /review 提交审核 → /resume 恢复。")
        write(f"恢复命令：/resume {item['id']} | {answer}")


def show_status(status, write=print):
    """
    作用：将当前任务压缩为状态、参数和下一步操作。
    输入：status - 任务状态；write - 输出函数。
    输出：无
    """
    records = status.get("adc_records", {})
    fact = records.get("parameter_fact") or {}
    value = fact.get("value", {})
    write("\n── 当前任务 ──")
    write("执行：" + STATES.get(status.get("execution_status"), "尚无状态"))
    write(f"需求：{status.get('requirements', {}).get('supply_voltage_v', '未记录')} V")
    if value:
        write(f"推荐供电：{value.get('lower', '未知')}～{value.get('upper', '未知')} V")
    write(
        f"人工审核：{'已确认' if fact.get('reviewed') else '待核对'}"
        f"  ·  版本 {status.get('review_revision', 0)}"
    )
    result = records.get("constraint_result") or {}
    write("供电判定：" + RESULTS.get(result.get("status"), "尚未核验"))
    if status.get("pending"):
        show_pending(status["pending"], write, status.get("reviews"))
    else:
        write("查看：/evidence 核对依据  ·  /report 查看判定  ·  /reviews 查看审核历史")


def show_evidence(status, write=print):
    """
    作用：展示人工审核需要的参数、适用条件及原文定位。
    输入：status - 任务状态；write - 输出函数。
    输出：无
    """
    records = status.get("adc_records", {})
    evidence = records.get("evidence") or {}
    fact = records.get("parameter_fact") or {}
    write("\n── 人工核对清单 ──")
    if not evidence:
        write("尚无证据，请先完成核验。")
        return
    value = fact.get("value", {})
    write(f"型号：{fact.get('part_number', '未记录')}  ·  参数：推荐供电")
    write(f"范围：{value.get('lower', '未知')}～{value.get('upper', '未知')} V")
    write("适用条件：" + fact.get("conditions", "未记录"))
    write(
        f"资料：{evidence.get('document_version', '未知版本')}"
        f"  ·  PDF 第 {evidence.get('pdf_page', '?')} 页"
    )
    write("章节：" + (evidence.get("section") or "未记录"))
    # 只收紧 PDF 提取留下的空白，完整字符内容仍保留。
    quote = "\n".join(
        " ".join(line.split())
        for line in evidence.get("quote", "未记录").splitlines()
        if line.strip()
    )
    write("原文（已收紧空白）：\n" + quote)
    write("请对照任务绑定的 PDF 核对型号、范围、单位和条件，再提交审核。")
    write(
        f"命令模板：/review 新请求ID | {status.get('review_revision', 0)}"
        " | 你的姓名 | approve | 核对说明"
    )


def show_reviews(reviews, write=print):
    """
    作用：逐条展示审核摘要，省略重复快照和内部引用。
    输入：reviews - 审核列表；write - 输出函数。
    输出：无
    """
    write("\n── 审核历史 ──")
    if not reviews:
        write("尚无审核记录。")
    for item in reviews:
        old = (item.get("previous_records") or {}).get("constraint_result") or {}
        new = (item.get("records") or {}).get("constraint_result") or {}
        decision = "确认" if item.get("decision") == "approve" else "撤回"
        # 展示北京时间，原始时间戳仍可通过 /raw 查询。
        stamp = item.get("created_at", "未记录")
        try:
            stamp = (
                datetime.fromisoformat(stamp)
                .astimezone(timezone(timedelta(hours=8)))
                .strftime("%Y-%m-%d %H:%M 北京时间")
            )
        except (ValueError, TypeError):
            pass
        write(
            f"v{item['revision']} · {decision} · {item.get('reviewer', '未记录')}"
            f" · {RESULTS.get(old.get('status'), '无')} → {RESULTS.get(new.get('status'), '无')}"
        )
        write(f"  请求：{item.get('request_id', '未记录')}  时间：{stamp}")
        write("  说明：" + item.get("note", "未记录"))
