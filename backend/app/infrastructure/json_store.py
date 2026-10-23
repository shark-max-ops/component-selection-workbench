"""JSON 文件归档。"""

import json
from pathlib import Path


def save_json(path: Path, data: object) -> None:
    """
    作用：将对象保存为 UTF-8 JSON 文件。
    输入：path - 目标路径；data - 可序列化对象。
    输出：无
    """
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
