import asyncio
import base64
import binascii
import json
import re
from typing import Any, List, Union

from astrbot import logger

from .base import BaseProvider


class OpenAICompatChatProvider(BaseProvider):
    """兼容 OpenAI Chat Completions 及其第三方兼容端点。"""

    IMAGE_URL_RE = re.compile(r"https?://[^\s)'\"]+", re.IGNORECASE)
    MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
    DATA_URI_RE = re.compile(
        r"data:(image|video)/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s_-]+)",
        re.IGNORECASE,
    )
    VIDEO_EXTENSIONS = (
        ".mp4",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
        ".m4v",
        ".gifv",
    )

    def _extract_readable_summary(self, response_text: str) -> str | None:
        summary_parts: list[str] = []

        try:
            payload = json.loads(response_text)
            for key in ("output_text", "text", "response"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    summary_parts.append(value.strip())

            choices = payload.get("choices") or []
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    for container_name in ("message", "delta"):
                        container = choice.get(container_name)
                        if not isinstance(container, dict):
                            continue
                        content = container.get("content")
                        if isinstance(content, str) and content.strip():
                            summary_parts.append(content.strip())
                        elif isinstance(content, list):
                            for part in content:
                                if not isinstance(part, dict):
                                    continue
                                text_value = part.get("text")
                                if isinstance(text_value, str) and text_value.strip():
                                    summary_parts.append(text_value.strip())
        except json.JSONDecodeError:
            pass

        if not summary_parts:
            fallback = response_text.strip()
            if fallback:
                return fallback[:500]
            return None

        merged = " | ".join(part for part in summary_parts if part)
        return merged[:500] if merged else None

    def _is_video_url(self, url: str) -> bool:
        lowered = url.lower().split("?", 1)[0]
        return lowered.endswith(self.VIDEO_EXTENSIONS) or "/video" in lowered

    def _decode_base64_payload(self, payload: str) -> bytes | None:
        cleaned = re.sub(r"\s+", "", payload)
        try:
            return base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError):
            return None

    def _extract_from_text(
        self, text: str
    ) -> tuple[list[bytes], str | dict[str, str] | None]:
        images: list[bytes] = []
        if not text:
            return images, None

        markdown_match = self.MARKDOWN_IMAGE_RE.search(text)
        if markdown_match:
            candidate = markdown_match.group(1).strip()
            if candidate.startswith("data:"):
                return self._extract_from_data_uri(candidate)
            if self._is_video_url(candidate):
                return images, {"type": "video", "url": candidate}
            return images, candidate

        data_match = self.DATA_URI_RE.search(text)
        if data_match:
            media_type = data_match.group(1).lower()
            raw = self._decode_base64_payload(data_match.group(3))
            if raw:
                if media_type == "video":
                    return (
                        images,
                        "接口返回了 base64 视频数据，当前插件暂不支持直接发送内嵌视频数据",
                    )
                images.append(raw)
                return images, None

        for url in self.IMAGE_URL_RE.findall(text):
            cleaned = url.rstrip(").,;\"'")
            if self._is_video_url(cleaned):
                return images, {"type": "video", "url": cleaned}
            return images, cleaned

        return images, None

    def _extract_from_data_uri(
        self, uri: str
    ) -> tuple[list[bytes], str | dict[str, str] | None]:
        images: list[bytes] = []
        match = self.DATA_URI_RE.search(uri)
        if not match:
            return images, None
        media_type = match.group(1).lower()
        raw = self._decode_base64_payload(match.group(3))
        if not raw:
            return images, "接口返回了无效的 base64 数据"
        if media_type == "video":
            return (
                images,
                "接口返回了 base64 视频数据，当前插件暂不支持直接发送内嵌视频数据",
            )
        images.append(raw)
        return images, None

    def _extract_from_message(
        self, message: dict[str, Any]
    ) -> tuple[list[bytes], str | dict[str, str] | None]:
        content = message.get("content", "")

        if isinstance(content, str):
            return self._extract_from_text(content)

        if isinstance(content, list):
            text_parts: list[str] = []
            images: list[bytes] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text_parts.append(str(part.get("text", "")))
                elif part_type == "image_url":
                    image_url = part.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url", "")).strip()
                        if url.startswith("data:"):
                            raws, err = self._extract_from_data_uri(url)
                            if raws:
                                images.extend(raws)
                            if err:
                                return images, err
                        elif url:
                            return (
                                (images, {"type": "video", "url": url})
                                if self._is_video_url(url)
                                else (images, url)
                            )
                elif part_type == "output_text":
                    text_parts.append(str(part.get("text", "")))

            text_images, result = self._extract_from_text(
                "\n".join(p for p in text_parts if p)
            )
            if text_images:
                images.extend(text_images)
            return images, result

        return [], None

    def _extract_from_json_response(
        self, payload: dict[str, Any]
    ) -> tuple[list[bytes], str | dict[str, str] | None]:
        images: list[bytes] = []
        choices = payload.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if isinstance(message, dict):
                raws, result = self._extract_from_message(message)
                if raws:
                    images.extend(raws)
                if result:
                    return images, result

            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                raws, result = self._extract_from_message(delta)
                if raws:
                    images.extend(raws)
                if result:
                    return images, result

        data_items = payload.get("data") or []
        if isinstance(data_items, list):
            for item in data_items:
                if not isinstance(item, dict):
                    continue
                if item.get("b64_json"):
                    raw = self._decode_base64_payload(str(item["b64_json"]))
                    if raw:
                        images.append(raw)
                if item.get("url"):
                    url = str(item["url"]).strip()
                    if self._is_video_url(url):
                        return images, {"type": "video", "url": url}
                    return images, url

        for key in ("output_text", "text", "response"):
            value = payload.get(key)
            if isinstance(value, str):
                raws, result = self._extract_from_text(value)
                if raws:
                    images.extend(raws)
                if result:
                    return images, result

        return images, None

    def _extract_from_sse_response(
        self, response_text: str
    ) -> tuple[list[bytes], str | dict[str, str] | None]:
        images: list[bytes] = []
        text_fragments: list[str] = []
        for line in response_text.strip().splitlines():
            if not line.startswith("data: "):
                continue
            line_data = line[6:].strip()
            if line_data == "[DONE]":
                continue
            try:
                chunk = json.loads(line_data)
            except Exception:
                continue

            raws, result = self._extract_from_json_response(chunk)
            if raws:
                images.extend(raws)
            if result:
                return images, result

            for choice in chunk.get("choices", []):
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {})
                if isinstance(delta, dict):
                    content = delta.get("content", "")
                    if isinstance(content, str):
                        text_fragments.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text_fragments.append(str(part.get("text", "")))

        text_images, result = self._extract_from_text("".join(text_fragments))
        if text_images:
            images.extend(text_images)
        return images, result

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, list[bytes], str, dict[str, str]]:
        api_url = self.node.get("api_url")
        model_name = self.node.get("model")
        if not api_url:
            return f"{self.name}: 配置错误 - 无 API URL"

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in image_bytes_list:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(img).decode('utf-8')}",
                        "detail": "high",
                    },
                }
            )

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content}],
            "stream": self.node.get("stream", True),
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
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    proxy=self.proxy,
                    timeout=self.api_timeout,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        last_err = f"HTTP {resp.status}: {body[:200]}"
                        resource_exhausted = self._is_resource_exhausted(
                            resp.status, body
                        )
                    else:
                        response_text = await resp.text()
                        raw_data_list: list[bytes] = []
                        result: str | dict[str, str] | None = None

                        if "data: " in response_text:
                            raw_data_list, result = self._extract_from_sse_response(
                                response_text
                            )

                        if not raw_data_list and not result:
                            try:
                                response_json = json.loads(response_text)
                                raw_data_list, result = (
                                    self._extract_from_json_response(response_json)
                                )
                            except json.JSONDecodeError:
                                raw_data_list, result = self._extract_from_text(
                                    response_text
                                )

                        if len(raw_data_list) == 1:
                            return raw_data_list[0]
                        if raw_data_list:
                            return raw_data_list

                        if result:
                            if isinstance(result, dict):
                                return result
                            if result.startswith("http://") or result.startswith(
                                "https://"
                            ):
                                downloaded = await self.iwf._download_image(result)
                                if downloaded:
                                    return downloaded
                                last_err = f"下载返回资源失败: {result}"
                            else:
                                last_err = result
                        else:
                            last_err = "未找到可解析的图片或视频结果"

                        summary = self._extract_readable_summary(response_text)
                        if summary:
                            logger.warning(
                                "[OpenAICompatChatProvider] 未解析到图片/视频，文本摘要: %s",
                                summary,
                            )

                        logger.debug(
                            "OpenAICompatChatProvider 未解析到结果，原始响应: %s",
                            response_text,
                        )
            except Exception as e:
                last_err = str(e)

            await self._log_retry_and_sleep(
                attempt_no=attempt_no,
                last_err=last_err,
                resource_exhausted=resource_exhausted,
            )

        return f"OpenAI 兼容 Chat Completions 生成失败: {last_err}"


class Flow2APIProvider(OpenAICompatChatProvider):
    """旧名称兼容别名。"""
