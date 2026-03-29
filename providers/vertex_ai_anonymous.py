import asyncio
import base64
import json
import random
import re
import string
from typing import List, Optional, Union
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from astrbot import logger

from .base import BaseProvider

QUERY_SIGNATURE = "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
OPERATION_NAME = "StreamGenerateContentAnonymous"
DEFAULT_RECAPTCHA_BASE = "https://www.google.com"
DEFAULT_VERTEX_BASE = "https://cloudconsole-pa.clients6.google.com"
VERTEX_API_PATH = (
    "/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql"
)
VERTEX_API_KEY = "AIzaSyCI-zsRP85UVOi0DjtiCwWBwQ1djDy741g"

try:
    from curl_cffi.requests import AsyncSession as CurlSession

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


class VertexAIAnonymousProvider(BaseProvider):
    """
    Vertex AI 匿名接口（逆向）。
    无需用户提供 API Key，自带 Recaptcha 验证与浏览器指纹伪装。
    """

    def __init__(self, node_config: dict, workflow, global_config):
        super().__init__(node_config, workflow, global_config)
        self.impersonate_index = 0
        self.request_count = 0
        self._curl_session = None
        # 并发控制：避免 in-flight 请求被 close() 误伤。
        # 设计目标：不串行化整个 generate；只在切换/创建 session 与更新计数时做极短临界区保护。
        self._session_lock = asyncio.Lock()
        self._inflight_sessions: dict[object, int] = {}
        self._retired_sessions: set[object] = set()

    async def _get_or_create_active_session_locked(self):
        """在持有 _session_lock 的前提下获取/创建 active session。"""
        if not CURL_CFFI_AVAILABLE:
            return None

        if self._curl_session is None:
            imp_list = self.node.get(
                "impersonate_list", ["chrome131", "firefox135", "safari18_0"]
            )
            if not imp_list:
                imp_list = ["chrome131"]

            impersonate = imp_list[self.impersonate_index % len(imp_list)]
            self.impersonate_index += 1

            if self.node.get("verbose_logging", True):
                logger.info(
                    f"[VertexAI] 正在创建新会话 | 指纹: {impersonate} | 轮次: {self.impersonate_index}"
                )

            self._curl_session = CurlSession(impersonate=impersonate, proxy=self.proxy)

        return self._curl_session

    async def _acquire_curl_session(self):
        """获取可用 session，并增加 in-flight 引用计数。"""
        if not CURL_CFFI_AVAILABLE:
            return None

        async with self._session_lock:
            session = await self._get_or_create_active_session_locked()
            if session is None:
                return None
            self._inflight_sessions[session] = (
                self._inflight_sessions.get(session, 0) + 1
            )
            return session

    async def _release_curl_session(self, session) -> None:
        """释放 session 的 in-flight 引用；若已退役且无人使用则真正关闭。"""
        if session is None:
            return

        to_close = None
        async with self._session_lock:
            current = self._inflight_sessions.get(session, 0)
            if current <= 1:
                self._inflight_sessions.pop(session, None)
            else:
                self._inflight_sessions[session] = current - 1

            if (
                session in self._retired_sessions
                and self._inflight_sessions.get(session, 0) == 0
            ):
                self._retired_sessions.discard(session)
                to_close = session

        # close() 可能涉及底层资源回收；放到锁外，避免阻塞其他协程进入临界区。
        if to_close is not None:
            try:
                to_close.close()
            except Exception:
                pass

    async def _retire_active_session(self) -> None:
        """退役当前 active session：后续请求会创建新会话；旧会话等待 in-flight 结束再关闭。"""
        to_close = None
        async with self._session_lock:
            session = self._curl_session
            if session is None:
                return

            # 标记退役，并清空 active 指针，让后续请求创建新会话。
            self._retired_sessions.add(session)
            self._curl_session = None

            # 若当前无人使用，可立即关闭；否则延迟到 release 时关闭。
            if self._inflight_sessions.get(session, 0) == 0:
                self._retired_sessions.discard(session)
                to_close = session

        if to_close is not None:
            try:
                to_close.close()
            except Exception:
                pass

    async def _get_curl_session(self):
        """获取或创建持久化的 curl_cffi 会话。

        注意：该方法不做 in-flight 引用计数，保留用于兼容/内部调用。
        新代码应优先使用 _acquire_curl_session/_release_curl_session。
        """
        if not CURL_CFFI_AVAILABLE:
            return None
        async with self._session_lock:
            return await self._get_or_create_active_session_locked()

    async def close(self):
        """关闭 curl_cffi 会话"""
        await self._retire_active_session()

    async def generate(
        self, image_bytes_list: List[bytes], prompt: str
    ) -> Union[bytes, str]:
        if not CURL_CFFI_AVAILABLE:
            return "Vertex AI 生成失败: 环境缺失 curl_cffi，无法规避 Google 指纹风控。请安装 curl_cffi 后重试。"

        # 读取会话老化阈值（默认 5 次）
        aging_threshold = self.node.get("session_aging_threshold", 5)

        # 会话老化机制：达到阈值后主动轮换指纹
        if self.request_count >= aging_threshold:
            if self.node.get("verbose_logging", True):
                logger.info(
                    f"[VertexAI] 会话老化 ({self.request_count}次请求)，主动销毁并轮换指纹"
                )
            await self.close()
            self.request_count = 0

        body = self._build_body(prompt, image_bytes_list)

        recaptcha_base = self.node.get("recaptcha_base_api", DEFAULT_RECAPTCHA_BASE)
        vertex_base = self.node.get("vertex_ai_base_api", DEFAULT_VERTEX_BASE)
        api_timeout = self.api_timeout

        # 解析重试间隔配置
        retry_mode = self.node.get("retry_mode", "指数")
        retry_range = self.node.get("retry_range", "2,16")
        try:
            min_d, max_d = map(float, retry_range.split(","))
        except (ValueError, AttributeError):
            min_d, max_d = 2.0, 16.0

        last_err = "未知错误"
        token = await self._get_recaptcha_token(recaptcha_base)
        if not token:
            logger.warning("首次获取 recaptcha token 失败，将尝试进入重试逻辑自动恢复")

        # 当前 recaptcha token 的本地尝试次数：
        # 经验上在 Failed to verify action 场景下，第二次提交同 token 可能成功
        captcha_try_count = 0
        attempt = 0
        error_status_code = None
        next_delay = 0.0
        while attempt < self.max_retry:
            attempt += 1

            # 随机退避与验证码刷新逻辑 (除第一次尝试外；若初始 token 缺失也需要执行)
            if attempt > 1 or not token:
                # 借鉴 big_banana: 资源耗尽 (8) 或 Token 失效 (3) 时刷新验证码，或者初始 token 缺失
                if error_status_code in [3, 8] or not token:
                    new_token = await self._get_recaptcha_token(recaptcha_base)
                    if new_token:
                        token = new_token
                        captcha_try_count = 0
                    else:
                        last_err = "获取 recaptcha token 失败"
                        await self.close()
                        self.request_count = 0  # 被动轮换后重置计数

                await asyncio.sleep(next_delay)

            error_status_code = None
            # 重置本轮错误状态
            current_iter_err = None

            if not token:
                current_iter_err = "获取 recaptcha token 失败"

            if not current_iter_err:
                body["variables"]["recaptchaToken"] = token
                api_url = self._build_api_url(vertex_base)

                # 执行生图请求 (使用 curl_cffi)
                # 每次请求尝试都计数（触发老化机制），放在开头确保只计一次
                async with self._session_lock:
                    self.request_count += 1
                try:
                    headers = {
                        "referer": "https://console.cloud.google.com/vertex-ai",
                        "Content-Type": "application/json",
                        "Origin": "https://console.cloud.google.com",
                    }
                    session = await self._acquire_curl_session()
                    if not session:
                        raise RuntimeError("无法创建 curl_cffi 会话")

                    try:
                        resp = await session.post(
                            api_url, json=body, headers=headers, timeout=api_timeout
                        )
                    finally:
                        await self._release_curl_session(session)

                    if resp.status_code != 200:
                        error_status_code = 8 if resp.status_code == 429 else None
                        current_iter_err = f"API请求失败 (HTTP {resp.status_code})"
                        if resp.status_code != 429:
                            await self.close()
                            self.request_count = 0  # 被动轮换后重置计数
                    else:
                        result = resp.json()
                        parsed = self._parse_result(result)
                        if parsed:
                            error_status_code = parsed["status_code"]
                            if parsed["image_data"]:
                                return base64.b64decode(parsed["image_data"])
                            if parsed["safety_blocked"]:
                                return parsed["last_err"]
                            current_iter_err = parsed["last_err"]
                except Exception as e:
                    current_iter_err = f"Vertex AI 请求异常: {str(e)}"
                    await self.close()
                    self.request_count = 0  # 被动轮换后重置计数

            # --- 统一错误处理与重试逻辑 ---
            if current_iter_err:
                last_err = current_iter_err

            # 处理 reCAPTCHA 验证失败 (FVA)
            if (
                error_status_code == 3
                and "Failed to verify action" in last_err
                and captcha_try_count < 1
            ):
                # FVA 复用 token 不算新尝试，减回去
                async with self._session_lock:
                    self.request_count -= 1
                captcha_try_count += 1
                next_delay = 2.0
                logger.info(
                    f"[VertexAI] 命中 Failed to verify action，复用同一 token {next_delay:.2f}s 后重试一次"
                )
                attempt -= 1
                error_status_code = None
                continue

            # 处理其他需要退避的重试
            if attempt < self.max_retry:
                if retry_mode == "指数":
                    next_delay = min(min_d * (2 ** (attempt - 1)), max_d)
                else:
                    next_delay = random.uniform(min_d, max_d)

                log_msg = f"Vertex AI API 调用失败，重试 ({attempt}/{self.max_retry}): {last_err}，{next_delay:.2f}s 后重试"
                if error_status_code == 8:
                    logger.warning(f"Vertex AI 429 Limited. {log_msg}")
                else:
                    logger.warning(log_msg)
            else:
                logger.warning(
                    f"Vertex AI API 调用失败，达到最大重试次数 ({attempt}/{self.max_retry}): {last_err}"
                )

        return f"Vertex AI 生成失败: {last_err}"

    def _build_body(self, prompt: str, image_bytes_list: List[bytes]) -> dict:
        model_name = self.node.get("model", "gemini-3-pro-image-preview")

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

        # 清晰度处理
        image_size = self.node.get("image_size", "智能匹配")
        if image_size == "智能匹配":
            sz_match = re.search(r"\b([124]K)\b", prompt, re.IGNORECASE)
            image_size = sz_match.group(1).upper() if sz_match else "1K"

        context = {
            "model": model_name,
            "contents": [{"parts": parts, "role": "user"}],
            "generationConfig": {
                "temperature": 1,
                "topP": 0.95,
                "maxOutputTokens": 32768,
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "imageOutputOptions": {"mimeType": "image/png"},
                    "personGeneration": "ALLOW_ALL",
                    "imageSize": image_size,
                },
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
            ],
            "region": "global",
        }

        system_prompt = self.node.get("system_prompt", "")
        if system_prompt:
            context["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        return {
            "querySignature": QUERY_SIGNATURE,
            "operationName": OPERATION_NAME,
            "variables": context,
        }

    def _build_api_url(self, vertex_base: str) -> str:
        return f"{vertex_base}{VERTEX_API_PATH}?key={VERTEX_API_KEY}&prettyPrint=false"

    def _parse_result(self, result: list) -> Optional[dict]:
        if not isinstance(result, list):
            raise TypeError("Unexpected response type")

        last_err = None
        status_code = None

        for elem in result:
            for item in elem.get("results", []):
                if item.get("errors"):
                    err_item = item["errors"][0]
                    err_msg = err_item.get("message", "")
                    status_code = (
                        err_item.get("extensions", {}).get("status", {}).get("code")
                    )
                    last_err = f"API返回错误: {err_msg}"
                    if "SAFETY" in last_err or "content" in last_err.lower():
                        return {
                            "image_data": None,
                            "status_code": status_code,
                            "last_err": last_err,
                            "safety_blocked": True,
                        }
                    continue

                for candidate in item.get("data", {}).get("candidates", []):
                    if candidate.get("finishReason") == "STOP":
                        for part in candidate.get("content", {}).get("parts", []):
                            if "inlineData" in part and part["inlineData"].get("data"):
                                image_data = part["inlineData"]["data"]
                                return {
                                    "image_data": image_data,
                                    "status_code": None,
                                    "last_err": None,
                                    "safety_blocked": False,
                                }
                    elif candidate.get("finishReason"):
                        last_err = f"生成中断: {candidate.get('finishReason')}"
                        continue

        return {
            "image_data": None,
            "status_code": status_code,
            "last_err": last_err,
            "safety_blocked": False,
        }

    async def _get_recaptcha_token_curl(self, base_url: str) -> Optional[str]:
        """使用 curl_cffi 获取 Recaptcha Token，保持指纹一致性"""
        session = await self._acquire_curl_session()
        if not session:
            return None

        headers = {"Referer": "https://console.cloud.google.com/vertex-ai"}

        try:
            for _ in range(3):
                try:
                    cb = "".join(
                        random.choices(string.ascii_letters + string.digits, k=10)
                    )
                    anchor_url = (
                        f"{base_url}/recaptcha/enterprise/anchor?ar=1"
                        f"&k=6LdCjtspAAAAAMcV4TGdWLJqRTEk1TfpdLqEnKdj"
                        f"&co=aHR0cHM6Ly9jb25zb2xlLmNsb3VkLmdvb2dsZS5jb206NDQz"
                        f"&hl=zh-CN&v=jdMmXeCQEkPbnFDy9T04NbgJ"
                        f"&size=invisible&anchor-ms=20000&execute-ms=15000&cb={cb}"
                    )

                    resp = await session.get(anchor_url, headers=headers)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "html.parser")
                    token_input = soup.find("input", {"id": "recaptcha-token"})
                    if not token_input:
                        continue
                    base_token = token_input.get("value")

                    reload_url = f"{base_url}/recaptcha/enterprise/reload?k=6LdCjtspAAAAAMcV4TGdWLJqRTEk1TfpdLqEnKdj"
                    parsed = urlparse(anchor_url)
                    query_params = parse_qs(parsed.query)
                    post_payload = {
                        "v": query_params["v"][0],
                        "reason": "q",
                        "k": query_params["k"][0],
                        "c": base_token,
                        "co": query_params["co"][0],
                        "hl": query_params["hl"][0],
                        "size": "invisible",
                        "vh": "6581054572",
                        "chr": "",
                        "bg": "",
                    }

                    resp = await session.post(
                        reload_url,
                        data=post_payload,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Origin": "https://console.cloud.google.com",
                            **headers,
                        },
                    )
                    if resp.status_code != 200:
                        continue

                    token_match = re.search(r'rresp","(.*?)"', resp.text)
                    if token_match:
                        return token_match.group(1)
                except Exception as e:
                    logger.warning(f"curl_cffi 获取 recaptcha token 异常: {str(e)}")
                    await self.close()
                    async with self._session_lock:
                        self.request_count = 0  # 被动轮换后重置计数
            return None
        finally:
            await self._release_curl_session(session)

    async def _get_recaptcha_token(self, base_url: str) -> Optional[str]:
        if not CURL_CFFI_AVAILABLE:
            return None
        return await self._get_recaptcha_token_curl(base_url)
