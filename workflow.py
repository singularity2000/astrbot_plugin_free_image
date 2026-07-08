import asyncio
import base64
import io
import os
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.parse import unquote, urlparse
import aiohttp
from PIL import Image as PILImage
from astrbot import logger
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
try:
    from astrbot.core.utils.quoted_message import extract_quoted_message_images
except Exception:  # pragma: no cover - 兼容旧版 AstrBot
    extract_quoted_message_images = None

class ImageWorkflow:
    def __init__(self, config: AstrBotConfig, proxy_url: str | None = None):
        if proxy_url: logger.info(f"ImageWorkflow 使用代理: {proxy_url}")
        self.conf = config
        self.session = aiohttp.ClientSession()
        self.proxy = proxy_url

    async def _download_image(self, url: str) -> bytes | None:
        download_timeout = self.conf.get("general", {}).get("download_timeout", 30)
        try:
            async with self.session.get(url, proxy=self.proxy, timeout=download_timeout) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: {url}, 错误: {e}")
            return None

    async def _get_avatar(self, user_id: str) -> bytes | None:
        if not user_id.isdigit(): return None
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return await self._download_image(avatar_url)

    def _extract_first_frame_sync(self, raw: bytes) -> bytes:
        img_io = io.BytesIO(raw)
        try:
            with PILImage.open(img_io) as img:
                if getattr(img, "is_animated", False):
                    img.seek(0)
                    first_frame = img.convert("RGBA")
                    out_io = io.BytesIO()
                    first_frame.save(out_io, format="PNG")
                    return out_io.getvalue()
        except Exception:
            pass
        return raw

    @staticmethod
    def _file_uri_to_path(src: str) -> Path | None:
        parsed = urlparse(src)
        if parsed.scheme != "file":
            return None
        raw_path = unquote(parsed.path or "")
        if os.name == "nt" and raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        if not raw_path:
            return None
        return Path(raw_path)

    @staticmethod
    def _dedupe_refs(refs: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            value = str(ref or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    async def _load_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        src = str(src or "").strip()
        if not src:
            return None
        loop = asyncio.get_running_loop()
        file_uri_path = self._file_uri_to_path(src)
        try:
            if file_uri_path and file_uri_path.is_file():
                raw = await loop.run_in_executor(None, file_uri_path.read_bytes)
            elif Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)
            elif src.startswith("http"):
                raw = await self._download_image(src)
            elif src.startswith("base64://"):
                raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            elif src.startswith("data:image") and "," in src:
                raw = await loop.run_in_executor(None, base64.b64decode, src.split(",", 1)[1])
        except (OSError, ValueError) as e:
            logger.debug(f"图片读取失败: {src}, 错误: {e}")
            return None
        if not raw: return None
        return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

    async def _image_component_ref(self, component: Image) -> str:
        try:
            return await component.convert_to_file_path()
        except Exception as e:
            logger.debug(f"图片组件转本地路径失败，回退原始引用: {e}")
        return str(component.url or component.file or component.path or "").strip()

    async def _load_image_components(self, components: Iterable[Image]) -> list[bytes]:
        refs = []
        for component in components:
            ref = await self._image_component_ref(component)
            if ref:
                refs.append(ref)
        return await self._load_image_refs(refs)

    async def _load_image_refs(self, refs: Iterable[str]) -> list[bytes]:
        images: list[bytes] = []
        for ref in self._dedupe_refs(refs):
            if img := await self._load_bytes(ref):
                images.append(img)
        return images

    def _provider_request_image_refs(self, event: AstrMessageEvent) -> list[str]:
        try:
            req: Any = event.get_extra("provider_request")
        except Exception:
            req = None
        refs = getattr(req, "image_urls", None) if req is not None else None
        if not isinstance(refs, list):
            return []
        return self._dedupe_refs(ref for ref in refs if isinstance(ref, str))

    async def _quoted_extractor_image_refs(self, event: AstrMessageEvent) -> list[str]:
        if extract_quoted_message_images is None:
            return []
        refs: list[str] = []
        for seg in event.message_obj.message:
            if not isinstance(seg, Reply):
                continue
            try:
                refs.extend(await extract_quoted_message_images(event, seg))
            except Exception as e:
                logger.debug(f"引用消息图片解析失败: {e}")
        return self._dedupe_refs(refs)

    async def _collect_context_images(
        self,
        event: AstrMessageEvent,
        *,
        include_llm_fallbacks: bool,
        include_direct_images: bool = True,
        include_at_avatars: bool = True,
        append_at_after_explicit: bool = False,
        sender_avatar_fallback: bool = False,
        ignore_bot_at: bool = False,
    ) -> List[bytes]:
        """统一读取事件上下文中的参考图。

        读取优先级保持插件既有设计：引用图 > LLM/框架兜底引用图 > 当前消息图片 > @ 头像 > 发送者头像兜底。
        send_selfie 与 image_generation 通过参数裁剪同一套策略，避免 AstrBot 框架升级后两边行为漂移。
        """
        bot_id = str(event.get_self_id() or "")
        at_user_ids: list[str] = []
        reply_images: list[Image] = []
        direct_images: list[Image] = []

        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                reply_images.extend(s for s in seg.chain if isinstance(s, Image))
            elif isinstance(seg, Image):
                direct_images.append(seg)
            elif isinstance(seg, At):
                uid = str(seg.qq)
                if ignore_bot_at and uid == bot_id:
                    continue
                at_user_ids.append(uid)

        # 1. 引用消息链中的图片最可信，先读。
        quoted_images = await self._load_image_components(reply_images)
        if quoted_images:
            return quoted_images

        # 2. LLM 工具 / AstrBot v4.26+ 可能把引用图放在 provider_request 或异步 extractor 中。
        if include_llm_fallbacks:
            fallback_images = await self._load_image_refs(self._provider_request_image_refs(event))
            if not fallback_images:
                fallback_images = await self._load_image_refs(await self._quoted_extractor_image_refs(event))
            if fallback_images:
                return fallback_images

        # 3. 当前消息直接携带的图片。
        direct_loaded: list[bytes] = []
        if include_direct_images:
            direct_loaded = await self._load_image_components(direct_images)
            if direct_loaded:
                if append_at_after_explicit and include_at_avatars:
                    for uid in at_user_ids:
                        if avatar := await self._get_avatar(uid):
                            direct_loaded.append(avatar)
                return direct_loaded

        # 4. @ 用户头像。
        if include_at_avatars and at_user_ids:
            avatar_images: list[bytes] = []
            for uid in at_user_ids:
                if avatar := await self._get_avatar(uid):
                    avatar_images.append(avatar)
            if avatar_images:
                return avatar_images

        # 5. 图生图命令的历史行为：无显式图片时用发送者头像兜底；自拍不启用。
        if sender_avatar_fallback:
            if avatar := await self._get_avatar(event.get_sender_id()):
                return [avatar]

        return []

    async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
        """图生图参考图读取。保留发送者头像兜底，并兼容 LLM/引用图解析兜底。"""
        return await self._collect_context_images(
            event,
            include_llm_fallbacks=True,
            include_direct_images=True,
            include_at_avatars=True,
            append_at_after_explicit=False,
            sender_avatar_fallback=True,
            ignore_bot_at=False,
        )

    async def has_context_images(self, event: AstrMessageEvent) -> bool:
        """轻量判断事件是否显式携带/引用图片，不把 @ 头像或发送者头像兜底算作图片。"""
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                return True
            if isinstance(seg, Reply) and seg.chain and any(isinstance(s, Image) for s in seg.chain):
                return True
        if self._provider_request_image_refs(event):
            return True
        return bool(await self._quoted_extractor_image_refs(event))

    async def get_selfie_extra_images(self, event: AstrMessageEvent) -> List[bytes]:
        """自拍专用图片读取，不触发发送者头像兜底，@机器人自身被忽略。"""
        return await self._collect_context_images(
            event,
            include_llm_fallbacks=True,
            include_direct_images=True,
            include_at_avatars=True,
            append_at_after_explicit=True,
            sender_avatar_fallback=False,
            ignore_bot_at=True,
        )

    async def terminate(self):
        if self.session and not self.session.closed: await self.session.close()
