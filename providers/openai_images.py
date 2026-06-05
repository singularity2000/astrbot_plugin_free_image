import base64
import math
from io import BytesIO
from typing import Any, List, Union

from aiohttp import FormData
from PIL import Image as PILImage

from astrbot import logger

from .base import BaseProvider


class OpenAIImagesProvider(BaseProvider):
    """OpenAI official Images API provider."""

    def _determine_size(self, prompt: str, image_bytes_list: list[bytes]) -> str:
        """根据节点配置、提示词关键词或参考图比例推断 size 参数。"""
        configured = self.node.get("size", "").strip()
        if configured:
            return configured
        if "横屏" in prompt:
            return "1536x1024"
        if "竖屏" in prompt or "手机" in prompt:
            return "1024x1536"
        if image_bytes_list:
            try:
                with PILImage.open(BytesIO(image_bytes_list[0])) as img:
                    w, h = img.size
                # 限制极端比例
                if w > 3 * h:
                    w = 3 * h
                elif h > 3 * w:
                    h = 3 * w
                max_area, min_area, max_edge = 8294400, 655360, 3840
                scale = 1.0
                if w * h > max_area:
                    scale = math.sqrt(max_area / (w * h))
                elif w * h < min_area:
                    scale = math.sqrt(min_area / (w * h))
                w, h = int(w * scale), int(h * scale)
                for dim, other in ((w, h), (h, w)):
                    if dim > max_edge:
                        scale = max_edge / dim
                        w = int(w * scale)
                        h = int(h * scale)
                w = max(16, round(w / 16) * 16)
                h = max(16, round(h / 16) * 16)
                # 再次修正极端比例
                if w > 3 * h:
                    w = max(16, round(3 * h / 16) * 16)
                elif h > 3 * w:
                    h = max(16, round(3 * w / 16) * 16)
                # 边界收敛
                while w * h > max_area or max(w, h) > max_edge:
                    if w >= h:
                        w -= 16
                    else:
                        h -= 16
                while w * h < min_area:
                    if w <= h:
                        w += 16
                    else:
                        h += 16
                return f"{w}x{h}"
            except Exception as e:
                logger.warning(f"[OpenAIImages] 推断尺寸失败，回退到 auto: {e}")
        return "auto"

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, list[bytes], str]:
        api_url = self.node.get("api_url")
        model_name = self.node.get("model")
        if not api_url:
            return f"{self.name}: 配置错误 - 未设置 API URL"
        if not model_name:
            return f"{self.name}: 配置错误 - 未设置模型名称"

        n = int(self.node.get("n", 1))
        size = self._determine_size(prompt, image_bytes_list)

        last_err = "未知错误"
        for i in range(self.max_retry):
            attempt_no = i + 1
            api_key = await self._get_api_key()
            if not api_key:
                return f"{self.name}: 配置错误 - 无 API Key"

            resource_exhausted = False
            headers = {"Authorization": f"Bearer {api_key}"}

            try:
                if image_bytes_list:
                    data = self._build_edits_form(model_name, prompt, image_bytes_list, n, size)
                    endpoint = self._build_api_url(str(api_url), "edits")
                    async with self.iwf.session.post(
                        endpoint,
                        data=data,
                        headers=headers,
                        proxy=self.proxy,
                        timeout=self.api_timeout,
                    ) as resp:
                        result = await resp.json(content_type=None)
                        parsed = await self._parse_response(resp.status, result)
                else:
                    endpoint = self._build_api_url(str(api_url), "generations")
                    payload = {"model": model_name, "prompt": prompt, "n": n, "size": size}
                    async with self.iwf.session.post(
                        endpoint,
                        json=payload,
                        headers={**headers, "Content-Type": "application/json"},
                        proxy=self.proxy,
                        timeout=self.api_timeout,
                    ) as resp:
                        result = await resp.json(content_type=None)
                        parsed = await self._parse_response(resp.status, result)

                if isinstance(parsed, (bytes, list)):
                    return parsed
                status_code, last_err = parsed
                resource_exhausted = self._is_resource_exhausted(status_code, last_err)
            except Exception as e:
                last_err = f"请求异常: {e}"

            await self._log_retry_and_sleep(
                attempt_no=attempt_no,
                last_err=last_err,
                resource_exhausted=resource_exhausted,
            )

        return f"OpenAI Images 生成失败: {last_err}"

    @staticmethod
    def _build_api_url(api_url: str, endpoint: str) -> str:
        return f"{api_url.rstrip('/')}/{endpoint}"

    def _build_edits_form(
        self, model_name: str, prompt: str, image_bytes_list: list[bytes],
        n: int = 1, size: str = "auto",
    ) -> FormData:
        logger.info(
            f"[OpenAIImages] 正在请求 /images/edits，上传参考图 {len(image_bytes_list)} 张"
        )
        form = FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", prompt)
        form.add_field("n", str(n))
        form.add_field("size", size)
        for index, raw in enumerate(image_bytes_list, start=1):
            filename, image_bytes, content_type = self._normalize_image_payload(raw, index)
            form.add_field(
                "image",
                image_bytes,
                filename=filename,
                content_type=content_type,
            )
        return form

    @staticmethod
    def _normalize_image_payload(raw_bytes: bytes, index: int) -> tuple[str, bytes, str]:
        try:
            with PILImage.open(BytesIO(raw_bytes)) as img:
                if getattr(img, "is_animated", False):
                    img.seek(0)
                img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=100)
                return f"image_{index}.jpg", buf.getvalue(), "image/jpeg"
        except Exception as e:
            logger.warning(
                f"[OpenAIImages] 输入图片归一化失败，将尝试使用原始字节: {e}"
            )
            return f"image_{index}.png", raw_bytes, "image/png"

    async def _parse_response(
        self, status_code: int, result: Any
    ) -> bytes | list[bytes] | tuple[int, str]:
        if status_code != 200:
            err = self._extract_error_message(result) or f"API请求失败 (HTTP {status_code})"
            logger.error(f"[OpenAIImages] 图片生成失败: {err}")
            return status_code, err

        image_results: list[bytes] = []
        if isinstance(result, dict):
            for item in result.get("data", []):
                if not isinstance(item, dict):
                    continue
                b64_data = item.get("b64_json")
                if b64_data:
                    if "base64," in b64_data:
                        b64_data = b64_data.split("base64,", 1)[1]
                    image_results.append(base64.b64decode(b64_data))
                    continue
                url = item.get("url")
                if isinstance(url, str) and url:
                    downloaded = await self.iwf._download_image(url)
                    if downloaded:
                        image_results.append(downloaded)

        if len(image_results) == 1:
            return image_results[0]
        if image_results:
            return image_results

        err = self._extract_error_message(result) or "响应中未包含图片数据"
        logger.warning(f"[OpenAIImages] 请求成功，但未返回图片数据: {str(result)[:300]}")
        return status_code, err

    @staticmethod
    def _extract_error_message(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        error = result.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:300]
        return None
