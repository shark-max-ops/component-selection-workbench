"""对纯提取步骤施加超时和有限重试。"""

import json
import os
import subprocess
import sys

from app.contracts import Evidence, ParameterFact
from app.infrastructure.documents import RegisteredDocument

MAX_ATTEMPTS = 2
TIMEOUT_SECONDS = 15


def extract_with_bounds(document: RegisteredDocument, *, cancelled=lambda: False):
    """
    作用：在独立进程中执行提取，超时可终止并有限重试。
    输入：document - 已绑定文档；cancelled - 取消状态读取函数。
    输出：证据、参数、尝试次数及可选失败说明。
    """
    # 每次只传不可变文档快照；子进程不接触宿主业务库。
    payload = json.dumps(
        {
            "sha256": document.sha256,
            "version": document.version,
            "source_url": document.source_url,
            "directory": str(document.directory),
            "pages": document.pages,
        },
        ensure_ascii=False,
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancelled():
            return None, None, attempt - 1, "操作已取消。"
        try:
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "app.adc.extract_worker"],
                input=payload,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode == 0:
                result = json.loads(completed.stdout)
                if result["ok"]:
                    return (
                        Evidence.model_validate(result["evidence"]),
                        ParameterFact.model_validate(result["fact"]),
                        attempt,
                        None,
                    )
                if not result.get("retryable"):
                    return None, None, attempt, result["error"]
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
        # 超时或临时子进程错误才重试；原文格式错误不会被重试掩盖。
    return None, None, MAX_ATTEMPTS, "证据提取暂时失败；已达到两次尝试上限。"
