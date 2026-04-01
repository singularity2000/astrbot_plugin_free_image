import asyncio
import base64
from typing import Any, Dict, List, Union

from .base import BaseProvider


class GenericImageProvider(BaseProvider):
    """
    通用的图像生成 API（兼容 siliconflow / bigmodel 等 /images/generations 端点）。
    根据 __template_key 自动决定响应解析字段。
    """

    # 不同提供商的响应数据字段映射
    DATA_FORM = {"siliconflow": "images", "bigmodel": "data"}

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, list[bytes], str]:
        api_url = self.node.get("api_url")
        if not api_url:
            return f"{self.name}: 配置错误 - 未设置 API URL"

        model_name = self.node.get("model")
        payload: Dict[str, Any] = {"model": model_name, "prompt": prompt}

        if image_bytes_list:
            for i, img in enumerate(image_bytes_list[:3]):
                key = "image" if i == 0 else f"image{i + 1}"
                payload[key] = (
                    f"data:image/png;base64,{base64.b64encode(img).decode('utf-8')}"
                )

        template_key = self.node.get("__template_key", "")
        data_form = self.DATA_FORM.get(template_key, "images")

        last_err = "未知错误"
        for i in range(self.max_retry):
            attempt_no = i + 1
            api_key = await self._get_api_key()
            if not api_key:
                return f"{self.name}: 配置错误 - 无 API Key"
            resource_exhausted = False
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }

            try:
                async with self.iwf.session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    proxy=self.proxy,
                    timeout=self.api_timeout,
                ) as resp:
                    if resp.status != 200:
                        last_err = f"API请求失败 (HTTP {resp.status})"
                        resource_exhausted = self._is_resource_exhausted(resp.status)
                    else:
                        data = await resp.json()
                        try:
                            image_results: list[bytes] = []
                            for item in data[data_form]:
                                url = item["url"]
                                if url.startswith("data:image/"):
                                    image_results.append(
                                        base64.b64decode(url.split(",", 1)[1])
                                    )
                                    continue
                                downloaded = await self.iwf._download_image(url)
                                if downloaded:
                                    image_results.append(downloaded)

                            if len(image_results) == 1:
                                return image_results[0]
                            if image_results:
                                return image_results
                            last_err = "下载失败"
                        except Exception:
                            last_err = f"解析响应失败: {str(data)[:200]}"
            except Exception as e:
                last_err = f"错误: {e}"

            await self._log_retry_and_sleep(
                attempt_no=attempt_no,
                last_err=last_err,
                resource_exhausted=resource_exhausted,
            )

        return f"{self.name} 生成失败: {last_err}"
