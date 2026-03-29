import asyncio
import base64
from typing import List, Union

from .base import BaseProvider


class OpenAIResponsesProvider(BaseProvider):
    """兼容 OpenAI /v1/responses 端点。"""

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, str]:
        api_url = self.node.get("api_url")
        model_name = self.node.get("model")

        content_items = [{"type": "input_text", "text": prompt}]
        for img in image_bytes_list:
            content_items.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{base64.b64encode(img).decode('utf-8')}",
                }
            )

        payload = {
            "model": model_name,
            "input": [{"role": "user", "content": content_items}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
        }

        last_err = "未知错误"
        for i in range(self.max_retry):
            attempt_no = i + 1
            api_key = await self._get_api_key()
            if not api_key:
                return f"{self.name}: 配置错误 - 无 API Key"
            resource_exhausted = False

            try:
                async with self.iwf.session.post(
                    api_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    proxy=self.proxy,
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        last_err = f"API错误: {data}"
                        resource_exhausted = self._is_resource_exhausted(
                            resp.status, str(data)
                        )
                    else:
                        b64 = None
                        if data.get("object") == "response":
                            for item in data.get("output", []):
                                if item.get("type") == "image_generation_call":
                                    b64 = item.get("result")
                                    break
                        if not b64:
                            for item in data.get("data", []):
                                if item.get("type") == "image_result":
                                    b64 = item.get("b64_json")
                                    break

                        if b64:
                            if "base64," in b64:
                                b64 = b64.split("base64,", 1)[1]
                            return base64.b64decode(b64)
                        last_err = "未找到图像数据"
            except Exception as e:
                last_err = str(e)

            await self._log_retry_and_sleep(
                attempt_no=attempt_no,
                last_err=last_err,
                resource_exhausted=resource_exhausted,
            )

        return f"OpenAI Responses 生成失败: {last_err}"
