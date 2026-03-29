from astrbot import logger
from astrbot.core import AstrBotConfig


async def migrate_legacy_config(conf: AstrBotConfig) -> None:
    """将旧版单选 API 配置迁移到新版 api_pipeline 管线配置（仅首次升级时执行）。"""
    # 如果已有 pipeline 配置，说明已经迁移过或是新用户
    if conf.get("api_pipeline", []):
        return

    # 检测旧配置键
    old_api_from = conf.get("api_from", "")
    if not old_api_from:
        return

    logger.info(
        f"[迁移] 检测到旧版配置 api_from={old_api_from}，正在自动迁移到 api_pipeline..."
    )

    # 旧的 template_key 映射
    legacy_map = {
        "Gemini": "gemini",
        "OpenAI-responses": "openai_responses",
        "siliconflow": "siliconflow",
        "bigmodel": "bigmodel",
        "Flow2API": "openai_compat_chat",
        "OpenAI-compat-chat": "openai_compat_chat",
        "Vertex_AI_Anonymous": "vertex_ai_anonymous",
    }
    template_key = legacy_map.get(old_api_from, "openai_responses")

    # 构建迁移后的节点配置
    node = {"__template_key": template_key, "enabled": True}

    if template_key == "vertex_ai_anonymous":
        # Vertex AI 不需要 api_keys/api_url
        node["model"] = conf.get("model", "gemini-3-pro-image-preview")
        node["image_size"] = conf.get("vertex_ai_image_size", "智能匹配")
        node["system_prompt"] = conf.get("vertex_ai_system_prompt", "")
        node["impersonate_list"] = conf.get(
            "vertex_ai_impersonate_list",
            [
                "chrome131",
                "chrome124",
                "firefox135",
                "firefox133",
                "safari18_0",
                "safari17_0",
            ],
        )
        node["verbose_logging"] = conf.get("vertex_ai_verbose_logging", True)
        node["recaptcha_base_api"] = conf.get(
            "recaptcha_base_api", "https://www.google.com"
        )
        node["vertex_ai_base_api"] = conf.get(
            "vertex_ai_base_api", "https://cloudconsole-pa.clients6.google.com"
        )
        node["max_retry"] = conf.get("provider_max_retry", 10)
    else:
        node["api_url"] = conf.get("api_url", "")
        node["model"] = conf.get("model", "")
        node["api_keys"] = conf.get("api_keys", [])
        node["max_retry"] = conf.get("provider_max_retry", 3)

    # 迁移旧的全局代理设置
    if conf.get("use_proxy", False) and conf.get("proxy_url", ""):
        node["proxy"] = conf.get("proxy_url", "")

    # 迁移旧的全局超时设置
    old_timeout = conf.get("api_timeout", None)
    if old_timeout is not None:
        node["api_timeout"] = old_timeout

    conf["api_pipeline"] = [node]
    conf.save_config()
    logger.info(f"[迁移] 已将旧配置迁移为 api_pipeline 的首个节点: {template_key}")
