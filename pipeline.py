import asyncio
from datetime import datetime
from typing import List, Optional, Union

from astrbot import logger
from astrbot.core import AstrBotConfig

from .providers import BaseProvider, create_provider
from .providers.base import node_display_name
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
        # 序号取自原始配置下标：未知模板会被 create_provider 跳过，
        # 直接对 self.providers 重新编号会和 `画图模型` 的序号错位。
        for index, node in enumerate(pipeline_config, start=1):
            provider = create_provider(node, self.iwf, self.conf)
            if provider:
                provider.pipeline_index = index
                self.providers.append(provider)
        enabled_names = [p.log_label for p in self.providers if p.enabled]
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
        generation_mode: Optional[str] = None,
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
                return (
                    f"模型 {model_index}🔴{node_display_name(provider.node)} 已关闭，请选择其他模型。",
                    None,
                )
            if not provider.supports_capability(generation_mode):
                return (
                    f"模型 {model_index} 不支持{self._capability_label(generation_mode)}，请指定其他模型。",
                    None,
                )
            logger.info(f"[Pipeline] 指定模型: {provider.log_label}")
            result = await provider.generate(image_bytes_list, prompt)
            if isinstance(result, (bytes, list, dict)):
                return result, node_display_name(provider.node)
            logger.warning(f"[Pipeline] 指定模型 {provider.log_label} 失败: {result}")
            return f"指定模型 {provider.label} 失败: {result}", None

        errors: List[str] = []
        for provider in self.providers:
            if not provider.enabled or not provider.supports_capability(generation_mode):
                continue
            logger.info(f"[Pipeline] 尝试: {provider.log_label}")
            result = await provider.generate(image_bytes_list, prompt)
            if isinstance(result, (bytes, list, dict)):
                logger.info(f"[Pipeline] 成功: {provider.log_label}")
                return result, node_display_name(provider.node)
            logger.warning(f"[Pipeline] {provider.log_label} 失败: {result}")
            # 对外文案只给序号和模型名，不带 API 主机名。
            errors.append(f"{provider.label}: {result}")

        if not errors:
            return (
                "API 管线为空或无已启用的提供商，请在配置页面添加至少一个 API 节点。",
                None,
            )
        return "所有 API 均失败:\n" + "\n".join(errors), None

    @staticmethod
    def _capability_label(capability: Optional[str]) -> str:
        return {"text2image": "文生图", "image2image": "图生图", "text2video": "文生视频", "image2video": "图生视频"}.get(capability or "", "当前模态")

    async def close(self):
        """关闭所有 Provider 的资源。"""
        for p in self.providers:
            await p.close()
