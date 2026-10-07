import asyncio
import base64
import math
from io import BytesIO
from typing import Any, List, Union

from aiohttp import FormData
from PIL import Image as PILImage

from astrbot import logger

from .base import BaseProvider, ReferenceLogContext


class ResultDownloadFailure(str):
    """已得到生成结果，结束本节点尝试；由管线按原有规则回退。"""


class OpenAIImagesProvider(BaseProvider):
    """OpenAI official Images API provider."""

    def _optional_params(self) -> dict:
        params = {}
        for key in ("quality", "output_format", "background"):
            value = self.node.get(key, "")
            if value and value != "不发送":
                params[key] = value
        compression = self.node.get("output_compression", -1)
        if compression not in (None, "", -1, "-1"):
            compression = int(compression)
            if not 0 <= compression <= 100:
                raise ValueError("输出压缩必须为 -1（不发送）或 0～100")
            if params.get("output_format") in ("jpeg", "webp"):
                params["output_compression"] = compression
        if params.get("background") == "transparent" and params.get("output_format") == "jpeg":
            raise ValueError("透明背景不能搭配 JPEG，请选择 PNG 或 WebP")
        return params

    def _preset_size(self) -> str:
        # 预设为明确的像素尺寸；不静默缩小超出 GPT Image 2 限制的组合。
        sizes = {
            "1K": {"1:1": "1024x1024", "16:9": "1536x864", "4:3": "1152x864", "3:2": "1248x832"},
            "2K": {"1:1": "2048x2048", "16:9": "2048x1152", "4:3": "2048x1536", "3:2": "2016x1344"},
            "4K": {"16:9": "3840x2160"},
        }
        tier = self.node.get("size_resolution", "1K")
        ratio = self.node.get("size_ratio", "1:1")
        reverse = ratio in ("9:16", "3:4", "2:3")
        lookup = ":".join(reversed(ratio.split(":"))) if reverse else ratio
        size = sizes.get(tier, {}).get(lookup)
        if not size:
            raise ValueError("此分辨率与比例组合超出预设限制；请选择其他组合，或按中转站文档使用自定义尺寸")
        return "x".join(reversed(size.split("x"))) if reverse else size

    def _determine_size(self, prompt: str, image_bytes_list: list[bytes]) -> str:
        """根据节点配置、提示词关键词或参考图比例推断 size 参数。"""
        mode = self.node.get("size_mode", "兼容旧配置")
        if mode == "模型自动":
            return "auto"
        if mode == "预设尺寸":
            return self._preset_size()
        configured = str(self.node.get("size", "") or "").strip()
        if mode == "自定义":
            parts = configured.lower().split("x")
            if len(parts) != 2 or not all(v.isdigit() and int(v) > 0 for v in parts):
                raise ValueError("自定义尺寸请填写正整数宽x高，例如 2048x1152")
            return configured.lower()
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
        self, image_bytes_list: List[bytes], prompt: str,
        *, request_log: ReferenceLogContext | None = None,
    ) -> Union[bytes, list[bytes], str]:
        request_log = request_log or ReferenceLogContext(original_count=len(image_bytes_list))
        api_url = self.node.get("api_url")
        model_name = self.node.get("model")
        if not api_url:
            return "配置错误 - 未设置 API URL"
        if not model_name:
            return "配置错误 - 未设置模型名称"

        try:
            n = int(self.node.get("n", 1))
            if n < 1 or int(self.max_retry) < 1:
                raise ValueError("生成数量和最大尝试次数必须至少为 1")
            size = self._determine_size(prompt, image_bytes_list)
            optional = self._optional_params()
        except (ValueError, TypeError) as exc:
            return f"配置错误 - {exc}"

        last_err = "未知错误"
        for i in range(self.max_retry):
            attempt_no = i + 1
            api_key = await self._get_api_key()
            if not api_key:
                return "配置错误 - 无 API Key"

            resource_exhausted = False
            headers = {"Authorization": f"Bearer {api_key}"}

            try:
                if image_bytes_list:
                    data = self._build_edits_form(model_name, prompt, image_bytes_list, n, size)
                    endpoint = self._build_api_url(str(api_url), "edits")
                    self._log_image_request(
                        request_log, received_count=len(image_bytes_list),
                        sent_count=len(image_bytes_list), attempt_no=attempt_no,
                    )
                    async with self.iwf.session.post(
                        endpoint,
                        data=data,
                        headers=headers,
                        proxy=self.proxy,
                        timeout=self.api_timeout,
                    ) as resp:
                        result = await resp.json(content_type=None)
                        status = resp.status
                else:
                    endpoint = self._build_api_url(str(api_url), "generations")
                    payload = {"model": model_name, "prompt": prompt, "n": n, "size": size, **optional}
                    self._log_image_request(
                        request_log, received_count=len(image_bytes_list),
                        sent_count=0, attempt_no=attempt_no,
                    )
                    async with self.iwf.session.post(
                        endpoint,
                        json=payload,
                        headers={**headers, "Content-Type": "application/json"},
                        proxy=self.proxy,
                        timeout=self.api_timeout,
                    ) as resp:
                        result = await resp.json(content_type=None)
                        status = resp.status

                parsed = await self._parse_response(status, result)
                if isinstance(parsed, ResultDownloadFailure):
                    return parsed
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

        return f"生成失败: {last_err}"

    @staticmethod
    def _build_api_url(api_url: str, endpoint: str) -> str:
        return f"{api_url.rstrip('/')}/{endpoint}"

    def _build_edits_form(
        self, model_name: str, prompt: str, image_bytes_list: list[bytes],
        n: int = 1, size: str = "auto",
    ) -> FormData:
        form = FormData()
        form.add_field("model", model_name)
        form.add_field("prompt", prompt)
        form.add_field("n", str(n))
        form.add_field("size", size)
        for key, value in self._optional_params().items():
            form.add_field(key, str(value))
        field_name = self.node.get("image_field_name", "image")
        if field_name not in ("image", "image[]"):
            raise ValueError("图片上传字段必须是 image 或 image[]")
        for index, raw in enumerate(image_bytes_list, start=1):
            filename, image_bytes, content_type = self._normalize_image_payload(raw, index)
            form.add_field(
                field_name,
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

    @staticmethod
    def _valid_image(raw: bytes) -> bool:
        try:
            with PILImage.open(BytesIO(raw)) as image:
                image.verify()
            return True
        except Exception:
            return False

    async def _download_result(self, url: str) -> tuple[bytes | None, str]:
        timeout = self.iwf.conf.get("general", {}).get("download_timeout", 30)
        last_error = "下载结果为空"
        for attempt in range(1, self.max_retry + 1):
            try:
                # 不传 API Authorization，也不改写签名 URL；仅继承当前节点代理。
                async with self.iwf.session.get(url, proxy=self.proxy, timeout=timeout) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
                if raw and await asyncio.to_thread(self._valid_image, raw):
                    return raw, ""
                last_error = "下载内容为空或不是有效图片"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retry:
                logger.warning(f"[{self.log_label}] 图片下载失败，重试下载 ({attempt}/{self.max_retry}): {last_error}")
                await asyncio.sleep(self._normal_retry_delay())
        return None, last_error

    async def _parse_response(
        self, status_code: int, result: Any
    ) -> bytes | list[bytes] | tuple[int, str] | ResultDownloadFailure:
        if status_code != 200:
            return status_code, self._extract_error_message(result) or f"API请求失败 (HTTP {status_code})"
        items = result.get("data", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            return status_code, "响应 data 字段不是列表"
        image_results = []
        failures = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                failures.append(f"第{index}项不是图片对象")
                continue
            b64_data = item.get("b64_json")
            if isinstance(b64_data, str) and b64_data:
                try:
                    raw = base64.b64decode("".join(b64_data.split("base64,", 1)[-1].split()), validate=True)
                    if await asyncio.to_thread(self._valid_image, raw):
                        image_results.append(raw)
                        continue
                except (ValueError, TypeError):
                    pass
            url = item.get("url")
            if isinstance(url, str) and url:
                raw, error = await self._download_result(url)
                if raw:
                    image_results.append(raw)
                    continue
                failures.append(f"第{index}张下载失败: {error}")
            else:
                failures.append(f"第{index}张缺少有效图片数据")
        if image_results:
            if failures:
                logger.warning(f"[{self.log_label}] 仅取得 {len(image_results)}/{len(items)} 张图片；" + "; ".join(failures))
            return image_results[0] if len(image_results) == 1 else image_results
        if any(isinstance(item, dict) and item.get("url") for item in items):
            return ResultDownloadFailure("已返回生成结果，但取图失败；已结束本节点尝试，不在本节点重新生图。" + "; ".join(failures))
        return status_code, self._extract_error_message(result) or "响应中未包含图片数据"

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
