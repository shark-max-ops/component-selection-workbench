"""统一创建模型客户端，供 Agent 和后续业务模块使用。"""

from langchain_openai import ChatOpenAI

from app.config import Settings


def create_chat_model(settings: Settings) -> ChatOpenAI:
    """
    作用：创建兼容 OpenAI Chat Completions 接口的客户端。
    输入：经过检查的项目配置。
    输出：ChatOpenAI 实例。
    要点：设置单次请求超时和有限重试；创建对象时不发起请求。
    边界：服务商的扩展字段或特殊推理模式，需要后续单独适配。
    """
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=60.0,
        max_retries=1,
    )
