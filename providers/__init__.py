from astrbot import logger
from astrbot.core import AstrBotConfig

from ..workflow import ImageWorkflow
from .base import BaseProvider
from .gemini import GeminiProvider
from .generic import GenericImageProvider
from .openai_compat_chat import Flow2APIProvider, OpenAICompatChatProvider
from .openai_responses import OpenAIResponsesProvider
from .vertex_ai import VertexAIProvider
from .vertex_ai_anonymous import VertexAIAnonymousProvider


PROVIDER_MAP: dict[str, type[BaseProvider]] = {
    "gemini": GeminiProvider,
    "openai_responses": OpenAIResponsesProvider,
    "openai_compat_chat": OpenAICompatChatProvider,
    "siliconflow": GenericImageProvider,
    "bigmodel": GenericImageProvider,
    "flow2api": Flow2APIProvider,
    "vertex_ai": VertexAIProvider,
    "vertex_ai_anonymous": VertexAIAnonymousProvider,
}


def create_provider(
    node_config: dict[str, object],
    workflow: ImageWorkflow,
    global_config: AstrBotConfig,
) -> BaseProvider | None:
    """根据 template_key 创建对应的 Provider 实例。"""
    template_key = str(node_config.get("__template_key", ""))
    cls = PROVIDER_MAP.get(template_key)
    if not cls:
        logger.warning(f"未知的 API 提供商模板: {template_key}，跳过")
        return None
    return cls(node_config, workflow, global_config)
