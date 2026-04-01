import base64
import json
import re
from typing import Any, Dict, List, Optional, Union

from astrbot import logger

from .base import BaseProvider


class GeminiProvider(BaseProvider):
    """Gemini 原生 API（/v1beta/models/{model}:generateContent）。"""

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, list[bytes], str]:
        api_url = self.node.get("api_url")
        model_name = self.node.get("model", "gemini-3.1-flash-image-preview")
        if not api_url:
            return f"{self.name}: 配置错误 - 未设置 API URL"

        url = f"{api_url}/{model_name}:generateContent"
        context = self._build_context(model_name, image_bytes_list, prompt)

        last_err = "未知错误"
        for attempt in range(self.max_retry):
            attempt_no = attempt + 1
            api_key = await self._get_api_key()
            if not api_key:
                return f"{self.name}: 配置错误 - 无 API Key"
            resource_exhausted = False

            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }

            try:
                async with self.iwf.session.post(
                    url,
                    json=context,
                    headers=headers,
                    proxy=self.proxy,
                    timeout=self.api_timeout,
                ) as resp:
                    data = await self._read_response_payload(resp)

                    if resp.status != 200:
                        err_msg = (
                            data.get("error", {}).get("message", "未知原因")
                            if isinstance(data, dict)
                            else str(data)[:200]
                        )
                        last_err = f"API请求失败 (HTTP {resp.status}): {err_msg}"
                        resource_exhausted = self._is_resource_exhausted(
                            resp.status, err_msg
                        )

                    parse_result = self._extract_image_or_error(data)
                    if isinstance(parse_result, bytes):
                        return parse_result
                    if isinstance(parse_result, str):
                        return parse_result
                    last_err = "响应中未包含图片数据"

            except Exception as e:
                last_err = f"请求异常: {e}"

            await self._log_retry_and_sleep(
                attempt_no=attempt_no,
                last_err=last_err,
                resource_exhausted=resource_exhausted,
            )

        return f"Gemini 生成失败: {last_err}"

    async def _read_response_payload(self, resp) -> Dict[str, Any]:
        """兼容反代场景：边读边解析，拿到完整 JSON 立即返回，不等待连接关闭。"""
        buffer = bytearray()

        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            buffer.extend(chunk)

            parsed = self._try_parse_payload(buffer)
            if parsed is not None:
                return parsed

        parsed = self._try_parse_payload(buffer)
        if parsed is not None:
            return parsed

        raw = bytes(buffer).decode("utf-8", errors="ignore")
        raise ValueError(f"无法解析 Gemini 响应: {raw[:300]}")

    def _try_parse_payload(
        self, payload_bytes: bytes | bytearray
    ) -> Optional[Dict[str, Any]]:
        text = bytes(payload_bytes).decode("utf-8", errors="ignore").strip()
        if not text:
            return None

        # 场景1：标准 JSON 响应
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # 场景2：SSE 响应（data: {...}）
        latest_obj: Optional[Dict[str, Any]] = None
        has_sse_line = False
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            has_sse_line = True
            item = line[5:].strip()
            if not item or item == "[DONE]":
                continue
            try:
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    latest_obj = parsed
            except json.JSONDecodeError:
                continue

        if latest_obj is not None:
            return latest_obj
        if has_sse_line:
            return None

        # 场景3：反代在 JSON 前后附加噪声，尝试裁剪首尾大括号
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                return None

        return None

    def _extract_image_or_error(
        self, data: Dict[str, Any]
    ) -> Optional[Union[bytes, list[bytes], str]]:
        """返回 bytes/list[bytes]=成功；str=终止错误；None=未取到图片。"""
        prompt_feedback = data.get("promptFeedback", {})
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            reason = prompt_feedback.get("blockReason", "未知")
            return f"请求被内容安全系统拦截: {reason}"

        image_results: list[bytes] = []

        for candidate in data.get("candidates", []):
            finish_reason = candidate.get("finishReason", "")

            if finish_reason != "STOP":
                if "SAFETY" in finish_reason or "BLOCK" in finish_reason:
                    return f"生成被安全策略拦截: {finish_reason}"
                continue

            for part in candidate.get("content", {}).get("parts", []):
                inline_data = part.get("inlineData")
                if inline_data and inline_data.get("data"):
                    img_b64 = inline_data["data"]
                    logger.info(f"[Gemini] 成功提取图片数据 ({len(img_b64)} chars)")
                    image_results.append(base64.b64decode(img_b64))

        if len(image_results) == 1:
            return image_results[0]
        if image_results:
            return image_results

        return None

    def _build_context(
        self,
        model: str,
        image_bytes_list: List[bytes],
        prompt: str,
    ) -> dict:
        """构建 Gemini API 请求体。"""
        parts = [{"text": prompt}]
        for img in image_bytes_list:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(img).decode("utf-8"),
                    }
                }
            )

        # 清晰度处理（与 VertexAIAnonymousProvider 一致）
        image_size = self.node.get("image_size", "智能匹配")
        if image_size == "智能匹配":
            sz_match = re.search(r"\b([124]K)\b", prompt, re.IGNORECASE)
            image_size = sz_match.group(1).upper() if sz_match else "1K"

        context: dict = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
            ],
        }

        # imageConfig：仅 gemini-3 系模型支持 imageSize 和 google_search
        if "gemini-3" in model.lower():
            context["generationConfig"]["imageConfig"] = {"imageSize": image_size}

            if self.node.get("google_search", False):
                context["tools"] = [{"google_search": {}}]

        # 系统提示词
        system_prompt = self.node.get("system_prompt", "")
        if system_prompt:
            context["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        return context
