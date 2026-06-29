import asyncio
from datetime import datetime
from typing import List, Optional, Union

from astrbot import logger
from astrbot.core import AstrBotConfig

from .providers import BaseProvider, create_provider
from .workflow import ImageWorkflow


class ImageGenPipeline:
    """
    管线调度器：按顺序依次调用 enabled 的 Provider，第一个成功即返回，
    失败则自动回退到下一个。
    """

    def __init__(self, global_config: AstrBotConfig, workflow: ImageWorkflow):
        self.conf = global_config
        self.iwf = workflow
        self.providers: List[BaseProvider] = []
        self.api_call_lock = asyncio.Lock()
        self.last_api_call_time: Optional[datetime] = None

    def build(self, pipeline_config: list):
        """从配置列表构建 Provider 链。"""
        self.providers.clear()
        for node in pipeline_config:
            provider = create_provider(node, self.iwf, self.conf)
            if provider:
                self.providers.append(provider)
        enabled_names = [p.name for p in self.providers if p.enabled]
        logger.info(
            f"API 管线构建完成: {enabled_names} "
            f"({len(self.providers)} 个节点, {len(enabled_names)} 个已启用)"
        )

    async def check_rate_limit(self) -> Optional[str]:
        rate_limit = self.conf.get("quota", {}).get("rate_limit_seconds", 120)
        if rate_limit <= 0:
            return None
        async with self.api_call_lock:
            now = datetime.now()
            if self.last_api_call_time:
                elapsed = (now - self.last_api_call_time).total_seconds()
                if elapsed < rate_limit:
                    return f"⏳ 操作太频繁，请在 {int(rate_limit - elapsed)} 秒后再试。"
            self.last_api_call_time = now
        return None

    async def execute(
        self,
        image_bytes_list: List[bytes],
        prompt: str,
        model_index: Optional[int] = None,
    ) -> tuple[Union[bytes, list[bytes], str, dict[str, str]], Optional[str]]:
        """
        依次调用管线中已启用的 Provider。
        返回 (result, model_name)。
        其中 result 为 bytes / list[bytes] / dict 表示成功媒体结果，str 表示全部失败（汇总错误信息）。

        当 model_index 不为 None 时，只调用指定序号（1-based）的 Provider，不回退。
        调用方应已在调用前校验过序号范围和 enabled 状态；此处再做二次防御。
        """
        if model_index is not None:
            if model_index < 1 or model_index > len(self.providers):
                return (
                    f"模型序号 {model_index} 超出范围（1-{len(self.providers)}）。",
                    None,
                )
            provider = self.providers[model_index - 1]
            if not provider.enabled:
                model_name = provider.node.get("model") or provider.name
                return (
                    f"模型 {model_index}🔴{model_name} 已关闭，请选择其他模型。",
                    None,
                )
            logger.info(f"[Pipeline] 指定模型: {provider.name}")
            result = await provider.generate(image_bytes_list, prompt)
            if isinstance(result, (bytes, list, dict)):
                model_name = provider.node.get("model")
                return result, str(model_name) if model_name else None
            logger.warning(f"[Pipeline] 指定模型 {provider.name} 失败: {result}")
            return f"指定模型 {provider.name} 失败: {result}", None

        errors: List[str] = []
        for provider in self.providers:
            if not provider.enabled:
                continue
            logger.info(f"[Pipeline] 尝试: {provider.name}")
            result = await provider.generate(image_bytes_list, prompt)
            if isinstance(result, (bytes, list, dict)):
                model_name = provider.node.get("model")
                return result, str(model_name) if model_name else None
            logger.warning(f"[Pipeline] {provider.name} 失败: {result}")
            errors.append(f"{provider.name}: {result}")

        if not errors:
            return (
                "API 管线为空或无已启用的提供商，请在配置页面添加至少一个 API 节点。",
                None,
            )
        return "所有 API 均失败:\n" + "\n".join(errors), None

    async def close(self):
        """关闭所有 Provider 的资源。"""
        for p in self.providers:
            await p.close()
