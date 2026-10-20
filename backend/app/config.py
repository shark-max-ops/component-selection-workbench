"""读取后端配置，并检查模型调用必需的环境变量。"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import SecretStr

# 根据本文件位置定位目录，不依赖终端当前路径。
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
RUNTIME_DIR = PROJECT_DIR / "runtime"


@dataclass(frozen=True)
class Settings:
    """保存配置。

    输入：经过检查的密钥、接口地址和模型名称。
    输出：不可直接修改的配置对象。
    要点：密钥使用 SecretStr，避免打印配置时直接显示密钥。
    """

    llm_api_key: SecretStr
    llm_base_url: str
    llm_model: str


def _read_required(name: str) -> str:
    """读取一个必填环境变量。

    输入：环境变量名称。
    输出：去除首尾空白的字符串。
    边界：变量不存在或为空时，只报告变量名，不输出变量内容。
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少配置：{name}，请检查 backend/.env。")
    return value


def load_settings() -> Settings:
    """读取模型配置并进行基础检查。

    输入：backend/.env 和当前进程环境变量。
    输出：Settings 对象。
    要点：已有进程环境变量优先；这里只检查格式，不验证远程服务。
    """
    load_dotenv(BACKEND_DIR / ".env", override=False)

    api_key = _read_required("LLM_API_KEY")
    base_url = _read_required("LLM_BASE_URL")
    model = _read_required("LLM_MODEL")

    try:
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_url = False

    if not valid_url:
        raise ValueError(
            "LLM_BASE_URL 必须是有效的 HTTP/HTTPS 接口地址，不能包含账号密码、查询参数或片段。"
        )

    return Settings(
        llm_api_key=SecretStr(api_key),
        llm_base_url=base_url.rstrip("/"),
        llm_model=model,
    )
