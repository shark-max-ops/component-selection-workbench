"""在可终止的独立进程中提取指定手册供电参数。"""

import json
import sys
from pathlib import Path

from app.adc.extractors import extract_ads1115_supply
from app.infrastructure.documents import RegisteredDocument


def main() -> None:
    """
    作用：读取单次文档快照并输出供电提取结果。
    输入：标准输入中的文档 JSON。
    输出：标准输出中的证据与参数 JSON。
    """
    # 子进程只接收已绑定的文档快照，不重新下载或写入业务记录。
    payload = json.load(sys.stdin)
    document = RegisteredDocument(
        sha256=payload["sha256"],
        version=payload["version"],
        source_url=payload["source_url"],
        directory=Path(payload["directory"]),
        pages=tuple(payload["pages"]),
    )
    try:
        evidence, fact = extract_ads1115_supply(document)
        result = {
            "ok": True,
            "evidence": evidence.model_dump(mode="json"),
            "fact": fact.model_dump(mode="json"),
        }
    except ValueError as exc:
        result = {"ok": False, "error": str(exc), "retryable": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
