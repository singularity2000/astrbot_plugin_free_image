import asyncio
import base64
import io
from pathlib import Path
from typing import List, Optional
import aiohttp
from PIL import Image as PILImage
from astrbot import logger
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

class ImageWorkflow:
    def __init__(self, config: AstrBotConfig, proxy_url: str | None = None):
        if proxy_url: logger.info(f"ImageWorkflow 使用代理: {proxy_url}")
        self.conf = config
        self.session = aiohttp.ClientSession()
        self.proxy = proxy_url

    async def _download_image(self, url: str) -> bytes | None:
        download_timeout = self.conf.get("download_timeout", 30)
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

    async def _load_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()
        if Path(src).is_file():
            raw = await loop.run_in_executor(None, Path(src).read_bytes)
        elif src.startswith("http"):
            raw = await self._download_image(src)
        elif src.startswith("base64://"):
            raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
        if not raw: return None
        return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

    async def get_images(self, event: AstrMessageEvent) -> List[bytes]:
        img_bytes_list: List[bytes] = []
        at_user_ids: List[str] = []

        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for s_chain in seg.chain:
                    if isinstance(s_chain, Image):
                        if s_chain.url and (img := await self._load_bytes(s_chain.url)):
                            img_bytes_list.append(img)
                        elif s_chain.file and (img := await self._load_bytes(s_chain.file)):
                            img_bytes_list.append(img)

        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                if seg.url and (img := await self._load_bytes(seg.url)):
                    img_bytes_list.append(img)
                elif seg.file and (img := await self._load_bytes(seg.file)):
                    img_bytes_list.append(img)
            elif isinstance(seg, At):
                at_user_ids.append(str(seg.qq))

        if img_bytes_list: return img_bytes_list

        if at_user_ids:
            for user_id in at_user_ids:
                if avatar := await self._get_avatar(user_id):
                    img_bytes_list.append(avatar)
            return img_bytes_list

        if avatar := await self._get_avatar(event.get_sender_id()):
            img_bytes_list.append(avatar)

        return img_bytes_list

    async def terminate(self):
        if self.session and not self.session.closed: await self.session.close()