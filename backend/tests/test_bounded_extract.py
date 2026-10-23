"""验证供电提取的超时、有限重试与取消边界。"""

import json
import subprocess
from types import SimpleNamespace

from test_adc_tools import make_case

import app.adc.bounded_extract as bounded
from app.adc.extractors import extract_ads1115_supply


def test_timeout_retries_once_then_succeeds(monkeypatch):
    """
    作用：验证一次超时后成功，只接受最终一次提取结果。
    输入：monkeypatch - pytest 依赖替换工具。
    输出：无
    """
    _, document, _, _ = make_case()
    evidence, fact = extract_ads1115_supply(document)
    calls = []

    def run(*args, **kwargs):
        """
        作用：先模拟子进程超时，再提供有效结果。
        输入：args - 子进程参数；kwargs - 运行限制。
        输出：模拟的子进程结果。
        """
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("extract", kwargs["timeout"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "evidence": evidence.model_dump(mode="json"),
                    "fact": fact.model_dump(mode="json"),
                }
            ),
        )

    monkeypatch.setattr(bounded.subprocess, "run", run)
    actual_evidence, actual_fact, attempts, error = bounded.extract_with_bounds(document)
    assert (actual_evidence, actual_fact) == (evidence, fact)
    assert attempts == 2 and error is None
    assert calls == [bounded.TIMEOUT_SECONDS] * 2


def test_cancellation_and_permanent_error_do_not_retry(monkeypatch):
    """
    作用：验证取消和原文格式错误不会重复调用提取子进程。
    输入：monkeypatch - pytest 依赖替换工具。
    输出：无
    """
    _, document, _, _ = make_case()
    calls = []

    def run(*args, **kwargs):
        """
        作用：返回不可重试的原文错误。
        输入：args - 子进程参数；kwargs - 运行限制。
        输出：错误结果。
        """
        calls.append(1)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": "表格格式不匹配",
                    "retryable": False,
                }
            ),
        )

    monkeypatch.setattr(bounded.subprocess, "run", run)
    assert bounded.extract_with_bounds(document, cancelled=lambda: True)[-1] == "操作已取消。"
    assert calls == []
    assert bounded.extract_with_bounds(document)[-2:] == (1, "表格格式不匹配")
    assert calls == [1]


def test_transient_failure_stops_at_budget(monkeypatch):
    """
    作用：验证连续临时失败达到预算后停止。
    输入：monkeypatch - pytest 依赖替换工具。
    输出：无
    """
    _, document, _, _ = make_case()
    calls = []

    def fail(*args, **kwargs):
        """
        作用：模拟提取进程连续超时。
        输入：args - 子进程参数；kwargs - 运行限制。
        输出：无
        """
        calls.append(1)
        raise subprocess.TimeoutExpired("extract", kwargs["timeout"])

    monkeypatch.setattr(bounded.subprocess, "run", fail)
    _, _, attempts, error = bounded.extract_with_bounds(document)
    assert attempts == bounded.MAX_ATTEMPTS == len(calls)
    assert "上限" in error
