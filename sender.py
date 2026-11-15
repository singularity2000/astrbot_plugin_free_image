import asyncio
from typing import Literal

from astrbot import logger
from astrbot.core.message.components import Image, Node, Nodes, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .nsfw import ImageDelivery, ImageDeliveryError, create_apng


class ImageResultSender:
    """Shared result formatting and image sending policy."""

    def __init__(self, conf, persistence):
        self.conf = conf
        self.persistence = persistence

    @staticmethod
    def normalize_image_results(res) -> list[bytes]:
        if isinstance(res, bytes):
            return [res]
        if isinstance(res, list):
            return [item for item in res if isinstance(item, bytes)]
        return []

    def build_success_caption(
        self,
        *,
        elapsed: float,
        is_i2i: bool,
        display_name: str,
        is_master: bool,
        sender_id: str,
        group_id: str,
        model_name: str | None = None,
    ) -> str:
        caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)"]
        if is_i2i:
            caption_parts.append(f"预设: {display_name}")

        if is_master:
            caption_parts.append("管理员剩余次数: ∞")
        else:
            quota = self.conf.get("quota", {})
            if quota.get("enable_user_limit", True):
                caption_parts.append(
                    f"个人剩余次数: {self.persistence.get_user_count(sender_id)}"
                )
            if quota.get("enable_group_limit", False) and group_id:
                caption_parts.append(
                    f"本群剩余次数: {self.persistence.get_group_count(group_id)}"
                )

        if model_name:
            caption_parts.append(f"模型: {model_name}")

        return " | ".join(caption_parts)

    def _should_quote_reply(
        self,
        *,
        request_source: Literal["command", "llm_tool"],
    ) -> bool:
        mode = self.conf.get("general", {}).get("quote_reply_mode", "始终引用回复")
        if mode == "始终单独发送":
            return False
        if mode == "命令引用回复，函数调用单独发送":
            return request_source == "command"
        if mode == "命令单独发送，函数调用引用回复":
            return request_source == "llm_tool"
        return True

    def _should_split_multi_images(self, *, event: AstrMessageEvent) -> bool:
        mode = self.conf.get("general", {}).get("multi_image_send_mode", "始终不分条")
        is_group = bool(event.get_group_id())
        if mode == "始终分条":
            return True
        if mode == "群聊不分条，私聊分条":
            return not is_group
        if mode == "群聊分条，私聊不分条":
            return is_group
        return False

    async def yield_success_images(
        self,
        *,
        event: AstrMessageEvent,
        images: list[bytes],
        caption_text: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
        delivery: ImageDelivery | None = None,
        elapsed: float = 0.0,
        model_name: str | None = None,
        batch_index: int = 1,
        batch_count: int = 1,
    ):
        if delivery and delivery.mode != "normal":
            await self._send_nsfw_images(
                event=event, images=images, caption_text=caption_text,
                concise_mode=concise_mode, request_source=request_source,
                delivery=delivery, elapsed=elapsed, model_name=model_name,
                batch_index=batch_index, batch_count=batch_count,
            )
            return

        quote_reply = self._should_quote_reply(request_source=request_source)
        split_images = len(images) > 1 and self._should_split_multi_images(event=event)
        reply_component = Reply(id=event.message_obj.message_id)

        if concise_mode:
            if split_images:
                for img in images:
                    chain = [Image.fromBytes(img)]
                    if quote_reply:
                        chain.insert(0, reply_component)
                        yield event.chain_result(chain)
                    else:
                        await event.send(MessageChain(chain=chain))
                return

            chain = [Image.fromBytes(img) for img in images]
            if quote_reply:
                chain.insert(0, reply_component)
                yield event.chain_result(chain)
            else:
                await event.send(MessageChain(chain=chain))
            return

        if split_images:
            yield event.chain_result([reply_component, Plain(caption_text)])
            for img in images:
                chain = [Image.fromBytes(img)]
                if quote_reply:
                    chain.insert(0, reply_component)
                    yield event.chain_result(chain)
                else:
                    await event.send(MessageChain(chain=chain))
            return

        if quote_reply:
            chain = [Image.fromBytes(img) for img in images]
            chain.append(Plain(caption_text))
            chain.insert(0, reply_component)
            yield event.chain_result(chain)
            return

        yield event.chain_result([reply_component, Plain(caption_text)])
        chain = [Image.fromBytes(img) for img in images]
        await event.send(MessageChain(chain=chain))

    async def _send_nsfw_images(
        self,
        *,
        event: AstrMessageEvent,
        images: list[bytes],
        caption_text: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
        delivery: ImageDelivery,
        elapsed: float,
        model_name: str | None,
        batch_index: int,
        batch_count: int,
    ) -> None:
        """Send explicitly so transport errors cannot bypass the fail-closed path."""
        try:
            if delivery.mode == "forward":
                if event.get_platform_name() != "aiocqhttp":
                    raise ImageDeliveryError("当前平台不支持合并转发。原图未直接发送。")
                groups = [[img] for img in images] if self._should_split_multi_images(event=event) else [images]
                sender_id = str(event.get_sender_id())
                sender_name = event.get_sender_name() or sender_id
                nodes = [
                    Node(uin=sender_id, name=sender_name, content=[Image.fromBytes(img) for img in group])
                    for group in groups
                ]
                if not concise_mode:
                    nodes.append(Node(
                        uin=str(event.get_self_id()), name="图片生成助手",
                        content=[Plain(caption_text)],
                    ))
                # Even one image is a forward card. Do not mix Reply/Plain outside it.
                await event.send(MessageChain(chain=[Nodes(nodes)]))
                return

            if delivery.mode != "apng":
                raise ImageDeliveryError("NSFW 发送方式无效。原图未直接发送。")

            wrapped: list[bytes] = []
            try:
                # Prepare the entire result before sending any image. Keep the originals
                # untouched: history/cache have already received the provider bytes.
                for index, img in enumerate(images, start=1):
                    wrapped.append(await asyncio.to_thread(
                        create_apng, img, elapsed=elapsed, model_name=model_name,
                        font_path=delivery.font_path, image_index=index,
                        image_count=len(images), batch_index=batch_index, batch_count=batch_count,
                    ))
            except Exception as exc:
                logger.warning(f"[FreeImage NSFW] 封面包装失败（{type(exc).__name__}）")
                raise ImageDeliveryError(
                    "图片已生成，但 NSFW 封面包装失败。请检查图片格式、尺寸和中文字体；原图未直接发送。"
                ) from exc

            # Reuse existing concise/split/quote behavior only with the wrapped bytes.
            async for message in self.yield_success_images(
                event=event, images=wrapped, caption_text=caption_text,
                concise_mode=concise_mode, request_source=request_source,
            ):
                await event.send(message)
        except ImageDeliveryError:
            raise
        except Exception as exc:
            logger.warning(f"[FreeImage NSFW] 发送失败（{type(exc).__name__}）")
            raise ImageDeliveryError(
                "图片已生成，但 NSFW 消息发送失败，部分结果可能未送达。原图未改为直接发送。"
            ) from exc
