import asyncio
from datetime import datetime
from typing import Dict, Literal, Optional

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image, Plain, Reply, Video
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .commands import CommandHandlers
from .pipeline import ImageGenPipeline
from .quota import PersistenceManager, UsageGuard
from .selfie import (
    build_selfie_prompt,
    combine_images,
    load_persona_images,
    resolve_persona,
    resolve_style,
)
from .sender import ImageResultSender
from .workflow import ImageWorkflow


@register(
    "astrbot_plugin_free_image",
    "Singularity2000",
    "文生图、图生图，可自定义提示词模板，兼容多种端点",
    "3.0.0",
    "https://github.com/singularity2000/astrbot_plugin_free_image",
)
class ImageGenerationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.persistence = PersistenceManager(config, StarTools.get_data_dir())
        self.pipeline: Optional[ImageGenPipeline] = None
        self.iwf: Optional[ImageWorkflow] = None
        self.sender: Optional[ImageResultSender] = None
        self.usage_guard: Optional[UsageGuard] = None
        self.commands = CommandHandlers(self)
        self.prompt_map: Dict[str, str] = {}

    async def initialize(self):
        self.iwf = ImageWorkflow(self.conf)

        # --- 构建 Pipeline ---
        self.pipeline = ImageGenPipeline(self.conf, self.iwf)
        pipeline_config = self.conf.get("api_pipeline", [])
        self.pipeline.build(pipeline_config)
        self.sender = ImageResultSender(self.conf, self.persistence)
        self.usage_guard = UsageGuard(self.conf, self.persistence, self.pipeline)

        await self.persistence.load_all()
        await self.commands.load_prompt_map()

        # 获取自动注册的工具实例并动态更新描述（保留自定义描述功能）
        tool = self.context.get_llm_tool_manager().get_func("image_generation")
        if tool:
            tool.description = self.conf.get(
                "llm_tool_description",
                "专业的文生图、图生图工具。理解用户语义，仅当用户需要你生图，或修改图片内容时才调用此工具。",
            )

            custom_prompt_desc = self.conf.get(
                "llm_prompt_description",
                "Change the user's input into a professional image generation prompt while strictly preserving the original intent.",
            )
            if (
                "properties" in tool.parameters
                and "prompt" in tool.parameters["properties"]
            ):
                tool.parameters["properties"]["prompt"]["description"] = (
                    custom_prompt_desc
                )

        logger.info("astrbot_plugin_free_image 插件已加载")

        # send_selfie 工具动态描述注入
        selfie_tool = self.context.get_llm_tool_manager().get_func("send_selfie")
        if selfie_tool:
            selfie_tool.description = self.conf.get(
                "selfie_tool_description",
                "以你的形象生成图片。理解用户语义，在用户要求生成“有你出镜”的图片时（如自拍、合影、展示形象等）须调用此工具，区别于常规生图。此工具自带你的形象参考图。",
            )
            guidance = self.conf.get(
                "selfie_prompt_guidance",
                "按照用户要求，合理补充照片的细节，可选：服装、姿势、场景、神态。可用第一人称的“我”称呼自己。避免描述人物的身份、头部外貌，以防止和参考图矛盾。",
            )
            if (
                "properties" in selfie_tool.parameters
                and "action" in selfie_tool.parameters["properties"]
            ):
                selfie_tool.parameters["properties"]["action"]["description"] = guidance
            if "properties" in selfie_tool.parameters and "style_id" in selfie_tool.parameters["properties"]:
                from .selfie import _all_styles
                styles_info = [f"{s['id']}（{s['name']}）" for s in _all_styles(self.conf) if s.get("id") and s.get("name")]
                style_hint = f"可选。指定风格ID，留空由插件自动选择。当前已配置：{', '.join(styles_info)}" if styles_info else "可选。指定风格ID，留空由插件自动选择"
                selfie_tool.parameters["properties"]["style_id"]["description"] = style_hint

    def _strip_wake_prefix(self, text: str) -> str:
        wake_prefixes = self.context.get_config().get("wake_prefix", [])
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        for prefix in sorted((p for p in wake_prefixes if p), key=len, reverse=True):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    def _get_plain_message_text(
        self, event: AstrMessageEvent, *, strip_wake_prefix: bool = False
    ) -> str:
        text = "".join(
            seg.text for seg in event.message_obj.message if isinstance(seg, Plain)
        ).strip()
        if strip_wake_prefix:
            return self._strip_wake_prefix(text)
        return text

    def _strip_command_prefix(self, text: str, command: str) -> str:
        text = text.strip()
        if text.startswith(command):
            return text.removeprefix(command).strip()
        return text

    @filter.llm_tool(name="image_generation")
    async def image_generation(self, event: AstrMessageEvent, prompt: str, count: int = 1):
        """专业的文生图、图生图工具。理解用户语义，仅当用户需要你生图，或修改图片内容时才调用此工具。

        Args:
            prompt(string): Change the user's input into a professional image generation prompt while strictly preserving the original intent.
            count(int): 生图数量（1~3），若不指定，默认为1。除非用户明确要求，否则跳过此参数。
        """
        # 检测是否包含图片组件（直接发送或引用）
        has_direct_image = False
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                has_direct_image = True
                break
            if isinstance(seg, Reply) and seg.chain:
                if any(isinstance(s, Image) for s in seg.chain):
                    has_direct_image = True
                    break

        # 智能决策：有图则图生图，无图则文生图
        is_i2i = has_direct_image

        # clamp count 到 1~3
        try:
            count = max(1, min(3, int(count)))
        except (TypeError, ValueError):
            count = 1

        # 异步启动后台任务，避免阻塞 LLM 导致超时
        async def _run_background_gen():
            try:
                async for result in self.handle_image_gen_logic(
                    event, prompt, is_i2i=is_i2i, request_source="llm_tool", count=count
                ):
                    await self._send_with_auto_quote(event, result, request_source="llm_tool")
            except Exception as e:
                logger.error(f"Background image generation failed: {e}")

        asyncio.create_task(_run_background_gen())

        # 停止事件传播，阻止 LLM 继续生成回复
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_image_gen_request(self, event: AstrMessageEvent):
        async for res in self.commands.on_image_gen_request(event):
            yield res

    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        async for res in self.commands.on_text_to_image_request(event):
            yield res

    @filter.command("图生图", prefix_optional=True)
    async def on_image_to_image_request(self, event: AstrMessageEvent):
        async for res in self.commands.on_image_to_image_request(event):
            yield res

    def _should_auto_quote(self, event: AstrMessageEvent) -> bool:
        """已废弃：保留方法体以兼容潜在的外部调用。
        文案引用现在由 _send_plain_direct / _send_with_auto_quote 无条件处理，
        不再跟随主框架 reply_with_quote 全局开关。图片引用由 quote_reply_mode 控制。
        """
        return True

    def _quoted_plain_result(self, event: AstrMessageEvent, text: str):
        """生成带 Reply 的纯文本结果，绕过主框架 reply_with_quote 装饰。
        用于 handle_image_gen_logic 里的状态/失败/配额提示，保证无论全局开关如何都引用原消息。
        """
        return event.chain_result(
            [Reply(id=event.message_obj.message_id), Plain(text)]
        )

    async def _send_plain_direct(
        self, event: AstrMessageEvent, text: str, with_reply: bool = True
    ) -> None:
        chain: list = [Plain(text)]
        if with_reply:
            chain.insert(0, Reply(id=event.message_obj.message_id))
        await event.send(MessageChain(chain=chain))

    async def _send_with_auto_quote(
        self,
        event: AstrMessageEvent,
        message,
        request_source: Literal["command", "llm_tool"] = "llm_tool",
    ) -> None:
        """对 event.send 发送的文案消息模拟 yield 管道的引用回复行为。
        仅处理纯 Plain 文案（开始/失败/配额提示等），无条件插入 Reply，
        不受 quote_reply_mode 和主框架 reply_with_quote 影响——quote_reply_mode 只作用于图片本身，
        由 sender.yield_success_images 负责。含 Image 的消息和已有 Reply 的消息保持原样。
        """
        chain = getattr(message, "chain", None)
        if chain:
            has_reply = any(isinstance(seg, Reply) for seg in chain)
            is_plain_only = all(isinstance(item, Plain) for item in chain)
            if not has_reply and is_plain_only:
                chain.insert(0, Reply(id=event.message_obj.message_id))
        await event.send(message)

    def _compact_selfie_failure_reason(self, failure_msg: str) -> str:
        reason = str(failure_msg or "").strip()
        prefix = "自拍失败，原因："
        if reason.startswith(prefix):
            reason = reason[len(prefix):].strip()
        return reason or "未知原因"

    def _fallback_selfie_failure_message(self, failure_msg: str) -> str:
        reason = self._compact_selfie_failure_reason(failure_msg)
        if reason.startswith("所有 API 均失败"):
            return "这次自拍没拍出来，图片服务连续尝试后都没有成功。可以稍后再让我试一次。"
        if "次数" in reason or "冷却" in reason or "频繁" in reason:
            return reason
        if "人设" in reason or "参考图" in reason:
            return reason
        return f"这次自拍没拍出来，原因是：{reason}"

    async def _explain_selfie_failure_with_llm(
        self, event: AstrMessageEvent, failure_msg: str
    ) -> None:
        reason = self._compact_selfie_failure_reason(failure_msg)
        prompt = (
            "你刚刚尝试给用户生成一张机器人自拍，但图片生成失败了。\n"
            f"真实失败原因：{reason}\n\n"
            "请用你当前角色的语气，非常简短自然地告诉用户本次拍照失败。"
        )
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                raise RuntimeError("当前会话没有可用 LLM Provider")
            llm_response = await provider.text_chat(prompt=prompt, contexts=[])
            text = str(getattr(llm_response, "completion_text", "") or "").strip()
            if not text:
                raise RuntimeError("LLM 未返回失败解释")
            await self._send_plain_direct(event, text, with_reply=False)
        except Exception as e:
            logger.warning(f"[send_selfie] 生成失败解释失败，将发送降级提示: {e}")
            await self._send_plain_direct(
                event, self._fallback_selfie_failure_message(failure_msg), with_reply=False
            )

    async def handle_image_gen_logic(
        self,
        event: AstrMessageEvent,
        prompt: str,
        is_i2i: bool,
        display_name: str | None = None,
        request_source: Literal["command", "llm_tool"] = "command",
        count: int = 1,
        model_index: Optional[int] = None,
    ):
        if not self.pipeline or not self.sender or not self.usage_guard:
            yield self._quoted_plain_result(event, "❌ 插件尚未完成初始化，请稍后再试。")
            return

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)

        # --- 权限和次数检查 ---
        if quota_error := await self.usage_guard.check_can_use(event, is_master):
            yield self._quoted_plain_result(event, quota_error)
            return
        if quota_error == "":
            return

        # --- 图片获取 (仅图生图) ---

        images_to_process = []
        if is_i2i:
            if not self.iwf or not (img_bytes_list := await self.iwf.get_images(event)):
                yield self._quoted_plain_result(event, "请发送或引用一张图片。")
                return

            MAX_IMAGES = 5
            original_count = len(img_bytes_list)
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
                yield self._quoted_plain_result(
                    event, f"🎨 检测到 {original_count} 张图片，已选取前 {MAX_IMAGES} 张…"
                )
            else:
                images_to_process = img_bytes_list

        # --- 提示语显示 ---
        if not display_name:
            display_name = prompt[:20] + "..." if len(prompt) > 20 else prompt

        concise_mode = self.conf.get("concise_mode", False) and bool(group_id)
        start_msg = f"🎨 收到{'图生图' if is_i2i else '文生图'}请求，正在生成 [{display_name}]..."

        if concise_mode:
            logger.info(start_msg)
            # 尝试贴表情 (ID 124: OK)
            try:
                bot = getattr(event, "bot", None)
                if not bot:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider and hasattr(provider, "bot"):
                        bot = provider.bot

                if bot and hasattr(bot, "set_msg_emoji_like"):
                    await bot.set_msg_emoji_like(
                        message_id=event.message_obj.message_id, emoji_id=124, set=True
                    )
            except Exception as e:
                logger.debug(f"简洁模式贴表情失败: {e}")
        else:
            yield self._quoted_plain_result(event, start_msg)

        # --- 批量前余额检查 ---
        # count > 1 时，提前查余额，避免白嫖 API
        try:
            count = max(1, min(3, int(count)))
        except (TypeError, ValueError):
            count = 1
        available = count
        if count > 1 and not is_master:
            # 估算可用次数：用户余额 + 群余额（若启用群限制）
            user_remain = self.persistence.get_user_count(sender_id) if self.conf.get("enable_user_limit", True) else 0
            group_remain = 0
            if self.conf.get("enable_group_limit", False) and group_id:
                group_remain = self.persistence.get_group_count(group_id)
            total_remain = user_remain + group_remain
            if total_remain < count:
                available = max(1, total_remain) if total_remain > 0 else 1

        # --- API 调用（支持多张） ---
        # 管理员：并发请求（彼此间隔2秒防抖启动），不扣费无竞态
        # 非管理员：串行请求，无间隔（Provider 内部已有重试退避）
        if is_master and count > 1:
            async for res in self._batch_generate_concurrent(
                event, prompt, images_to_process, count, available,
                is_i2i, display_name, concise_mode, request_source, model_index,
            ):
                yield res
        else:
            async for res in self._batch_generate_sequential(
                event, prompt, images_to_process, count, available,
                is_master, is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index,
            ):
                yield res

    async def _batch_generate_sequential(
        self,
        event: AstrMessageEvent,
        prompt: str,
        images_to_process: list,
        count: int,
        available: int,
        is_master: bool,
        is_i2i: bool,
        display_name: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
        sender_id: str,
        group_id: str,
        model_index: Optional[int],
    ):
        """非管理员串行生图，无间隔。管理员单张也走这里。"""
        for i in range(count):
            suffix = f" ({i+1}/{count})" if count > 1 else ""
            if i >= available:
                quota_msg = self._build_quota_msg(group_id)
                yield self._quoted_plain_result(event, f"{quota_msg}{suffix}")
                continue

            start_time = datetime.now()
            res, model_name = await self.pipeline.execute(
                images_to_process, prompt, model_index=model_index
            )
            elapsed = (datetime.now() - start_time).total_seconds()

            async for msg in self._process_single_result(
                event, res, model_name, elapsed, suffix, i, count,
                is_master, is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index,
            ):
                yield msg

    async def _batch_generate_concurrent(
        self,
        event: AstrMessageEvent,
        prompt: str,
        images_to_process: list,
        count: int,
        available: int,
        is_i2i: bool,
        display_name: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
        model_index: Optional[int],
    ):
        """管理员并发生图，彼此间隔2秒防抖启动。不扣费，无竞态。"""
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        async def _single(i: int):
            suffix = f" ({i+1}/{count})" if count > 1 else ""
            if i >= available:
                quota_msg = self._build_quota_msg(group_id)
                return ("quota", i, suffix, quota_msg)

            start_time = datetime.now()
            res, model_name = await self.pipeline.execute(
                images_to_process, prompt, model_index=model_index
            )
            elapsed = (datetime.now() - start_time).total_seconds()
            return ("result", i, suffix, (res, model_name, elapsed))

        # 并发启动，但彼此间隔2秒防抖
        tasks: list[asyncio.Task] = []
        for i in range(count):
            tasks.append(asyncio.create_task(_single(i)))
            if i < count - 1:
                await asyncio.sleep(2)

        # 按启动顺序（即完成顺序）收集结果
        for task in tasks:
            kind, idx, suffix, payload = await task
            if kind == "quota":
                yield self._quoted_plain_result(event, f"{payload}{suffix}")
                continue

            res, model_name, elapsed = payload
            async for msg in self._process_single_result(
                event, res, model_name, elapsed, suffix, idx, count,
                True,  # is_master=True（并发仅管理员）
                is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index,
            ):
                yield msg

    def _build_quota_msg(self, group_id: str) -> str:
        """根据配额配置生成对应的配额不足提示。"""
        if self.conf.get("enable_user_limit", True) and self.conf.get("enable_group_limit", False) and group_id:
            return "❌ 本群和您的个人次数均已用完，请等待次日重置或向管理员索要。"
        if self.conf.get("enable_user_limit", True):
            return "❌ 您的个人使用次数已用完，请等待次日重置或向管理员索要。"
        if self.conf.get("enable_group_limit", False) and group_id:
            return "❌ 本群的使用次数已用完，请等待次日重置或向管理员索要。"
        return "❌ 次数已用完。"

    async def _process_single_result(
        self,
        event: AstrMessageEvent,
        res,
        model_name: Optional[str],
        elapsed: float,
        suffix: str,
        i: int,
        count: int,
        is_master: bool,
        is_i2i: bool,
        display_name: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
        sender_id: str,
        group_id: str,
        model_index: Optional[int],
    ):
        """处理单次 pipeline.execute 的结果，yield 出消息。扣费在非管理员时执行。"""
        image_results = self.sender.normalize_image_results(res)
        if image_results:
            if deduction_error := await self.usage_guard.deduct_after_success(event, is_master):
                yield self._quoted_plain_result(event, f"{deduction_error}{suffix}")
                return
            caption_text = self.sender.build_success_caption(
                elapsed=elapsed,
                is_i2i=is_i2i,
                display_name=display_name,
                is_master=is_master,
                sender_id=sender_id,
                group_id=group_id,
                model_name=model_name,
            )
            caption_text = f"{caption_text}{suffix}"
            logger.info(caption_text)
            async for msg in self.sender.yield_success_images(
                event=event,
                images=image_results,
                caption_text=caption_text,
                concise_mode=concise_mode,
                request_source=request_source,
            ):
                yield msg
        elif isinstance(res, dict) and res.get("type") == "video" and res.get("url"):
            if deduction_error := await self.usage_guard.deduct_after_success(event, is_master):
                yield self._quoted_plain_result(event, f"{deduction_error}{suffix}")
                return
            caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)", "结果类型: 视频"]
            if is_i2i:
                caption_parts.append(f"预设: {display_name}")

            if is_master:
                caption_parts.append("管理员剩余次数: ∞")
            else:
                if self.conf.get("enable_user_limit", True):
                    caption_parts.append(
                        f"个人剩余次数: {self.persistence.get_user_count(sender_id)}"
                    )
                if self.conf.get("enable_group_limit", False) and group_id:
                    caption_parts.append(
                        f"本群剩余次数: {self.persistence.get_group_count(group_id)}"
                    )

            if model_name:
                caption_parts.append(f"模型: {model_name}")

            caption_text = " | ".join(caption_parts) + suffix
            video_component = Video.fromURL(url=res["url"])
            logger.info(caption_text)
            if concise_mode:
                yield event.chain_result(
                    [Reply(id=event.message_obj.message_id), video_component]
                )
            else:
                yield event.chain_result([video_component, Plain(caption_text)])
        else:
            if model_index is not None:
                yield self._quoted_plain_result(
                    event, f"❌ 指定模型生成失败 ({elapsed:.2f}s)\n原因: {res}{suffix}"
                )
            elif concise_mode and str(res).startswith("所有 API 均失败"):
                yield self._quoted_plain_result(
                    event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败{suffix}"
                )
            else:
                yield self._quoted_plain_result(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}{suffix}")

    @filter.command("画图添加模板", aliases={"lma", "lm添加"}, prefix_optional=True)
    async def add_lm_prompt(self, event: AstrMessageEvent):
        async for res in self.commands.add_lm_prompt(event):
            yield res

    @filter.command("画图模型", prefix_optional=True)
    async def on_model_pipeline_command(self, event: AstrMessageEvent):
        async for result in self.commands.on_model_pipeline_command(event):
            yield result

    @filter.command("画图简洁模式", prefix_optional=True)
    async def on_concise_mode_command(self, event: AstrMessageEvent):
        async for res in self.commands.on_concise_mode_command(event):
            yield res

    @filter.command("画图帮助", aliases={"lmh", "lm帮助"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        async for res in self.commands.on_prompt_help(event):
            yield res

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    @filter.command("画图签到", prefix_optional=True)
    async def on_checkin(self, event: AstrMessageEvent):
        async for res in self.commands.on_checkin(event):
            yield res

    @filter.command("画图增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        async for res in self.commands.on_add_user_counts(event):
            yield res

    @filter.command("画图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        async for res in self.commands.on_add_group_counts(event):
            yield res

    @filter.command("画图查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        async for res in self.commands.on_query_counts(event):
            yield res

    # ─────────────────────────── 自拍核心入口 ───────────────────────────


    async def _exec_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        style_id_override: str = "",
        is_llm_tool: bool = False,
        count: int = 1,
        model_index: Optional[int] = None,
    ) -> str:
        """执行自拍生图，返回状态字符串。图片通过 event.send 直接发送。"""
        if not self.pipeline or not self.sender or not self.usage_guard:
            return "自拍失败，原因：插件尚未完成初始化。"

        is_master = self.is_global_admin(event)
        quota_err = await self.usage_guard.check_can_use(event, is_master)
        if quota_err is not None:
            if quota_err and not is_llm_tool:
                await self._send_plain_direct(event, quota_err)
            return f"自拍失败，原因：{quota_err}" if quota_err else ""

        session_id = str(event.unified_msg_origin or "").strip()
        persona = await resolve_persona(self.conf, self.context, event, session_id)
        if not persona:
            return "自拍失败，原因：还没有可用自拍人设，请管理员先用 #自拍人设 添加 创建人设，或在 WebUI 的 selfie_personas 中添加。"

        persona_images = await load_persona_images(persona)
        if not persona_images:
            return f"自拍失败，原因：人设「{persona.get('name', persona.get('id'))}」没有可用的参考图，请检查路径是否正确。"

        extra_images: list[bytes] = []
        if self.iwf:
            try:
                extra_images = await self.iwf.get_selfie_extra_images(event)
            except Exception:
                extra_images = []

        images_to_send = combine_images(persona_images, extra_images)

        style = resolve_style(self.conf, action, style_id_override)
        if style_id_override and not style:
            logger.warning(f"[Selfie] 找不到自拍风格「{style_id_override}」，将以无风格继续生成。")

        prompt = build_selfie_prompt(persona, action, style, bool(extra_images))
        persona_name = persona.get("name", persona.get("id", ""))
        style_name = style.get("name", "") if style else "无风格"
        logger.info(f"[Selfie] 人设={persona_name}, 风格={style_name}, 参考图={len(images_to_send)}张")

        group_id = event.get_group_id()
        concise = True if is_llm_tool else (self.conf.get("concise_mode", False) and bool(group_id))

        if not is_llm_tool and not concise:
            await self._send_plain_direct(event, f"📸 正在生成自拍 [{persona_name}]…")

        # clamp count
        try:
            count = max(1, min(3, int(count)))
        except (TypeError, ValueError):
            count = 1

        # 批量前余额检查
        sender_id = event.get_sender_id()
        available = count
        if count > 1 and not is_master:
            user_remain = self.persistence.get_user_count(sender_id) if self.conf.get("enable_user_limit", True) else 0
            group_remain = 0
            if self.conf.get("enable_group_limit", False) and group_id:
                group_remain = self.persistence.get_group_count(group_id)
            total_remain = user_remain + group_remain
            if total_remain < count:
                available = max(1, total_remain) if total_remain > 0 else 1

        request_source: Literal["command", "llm_tool"] = "llm_tool" if is_llm_tool else "command"
        last_result = ""
        had_success = False

        # 管理员：并发请求（彼此间隔2秒防抖启动），不扣费无竞态
        # 非管理员：串行请求，无间隔
        if is_master and count > 1:
            # 并发启动，彼此间隔2秒防抖
            tasks: list[asyncio.Task] = []
            for i in range(count):
                tasks.append(asyncio.create_task(self._selfie_single(i, count, available, event, images_to_send, prompt, model_index)))
                if i < count - 1:
                    await asyncio.sleep(2)

            for i, task in enumerate(tasks):
                kind, suffix, payload = await task
                if kind == "quota":
                    quota_msg = payload
                    if not is_llm_tool:
                        await self._send_plain_direct(event, f"{quota_msg}{suffix}")
                    last_result = f"自拍失败，原因：{quota_msg}"
                    continue

                res, model_name, elapsed = payload
                image_results = self.sender.normalize_image_results(res)
                if image_results:
                    # 管理员不扣费，无 deduction_error
                    remaining_str = "管理员剩余次数: ∞"
                    caption_text = " | ".join(part for part in [
                        f"✅ 生成成功 ({elapsed:.2f}s)",
                        f"人设: {persona_name}",
                        f"风格: {style_name}",
                        remaining_str,
                        f"模型: {model_name}" if model_name else "",
                    ] if part) + suffix
                    logger.info(caption_text)
                    async for msg in self.sender.yield_success_images(
                        event=event, images=image_results, caption_text=caption_text,
                        concise_mode=concise, request_source=request_source,
                    ):
                        await event.send(msg)
                    last_result = f"已成功为「{persona_name}」生成自拍（{elapsed:.1f}s），已发送给用户。"
                    had_success = True
                else:
                    err = str(res)
                    if not is_llm_tool:
                        if model_index is not None:
                            await self._send_plain_direct(event, f"❌ 指定模型生成失败 ({elapsed:.2f}s)\n原因: {err}{suffix}")
                        elif concise and err.startswith("所有 API 均失败"):
                            await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败{suffix}")
                        else:
                            await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {err}{suffix}")
                    last_result = f"自拍失败，原因：{err}"
        else:
            # 串行
            for i in range(count):
                suffix = f" ({i+1}/{count})" if count > 1 else ""

                # 余额不足
                if i >= available:
                    quota_msg = self._build_quota_msg(group_id)
                    if not is_llm_tool:
                        await self._send_plain_direct(event, f"{quota_msg}{suffix}")
                    last_result = f"自拍失败，原因：{quota_msg}"
                    continue

                start_time = datetime.now()
                res, model_name = await self.pipeline.execute(
                    images_to_send, prompt, model_index=model_index
                )
                elapsed = (datetime.now() - start_time).total_seconds()

                image_results = self.sender.normalize_image_results(res)
                if image_results:
                    deduction_error = await self.usage_guard.deduct_after_success(event, is_master)
                    if deduction_error:
                        if not is_llm_tool:
                            await self._send_plain_direct(event, f"{deduction_error}{suffix}")
                        last_result = f"自拍失败，原因：{deduction_error}"
                        continue
                    if is_master:
                        remaining_str = "管理员剩余次数: ∞"
                    else:
                        parts_r = []
                        if self.conf.get("enable_user_limit", True):
                            parts_r.append(f"个人剩余次数: {self.persistence.get_user_count(event.get_sender_id())}")
                        if self.conf.get("enable_group_limit", False) and group_id:
                            parts_r.append(f"本群剩余次数: {self.persistence.get_group_count(group_id)}")
                        remaining_str = " | ".join(parts_r) if parts_r else ""
                    caption_text = " | ".join(part for part in [
                        f"✅ 生成成功 ({elapsed:.2f}s)",
                        f"人设: {persona_name}",
                        f"风格: {style_name}",
                        remaining_str,
                        f"模型: {model_name}" if model_name else "",
                    ] if part) + suffix
                    logger.info(caption_text)
                    async for msg in self.sender.yield_success_images(
                        event=event, images=image_results, caption_text=caption_text,
                        concise_mode=concise, request_source=request_source,
                    ):
                        await event.send(msg)
                    last_result = f"已成功为「{persona_name}」生成自拍（{elapsed:.1f}s），已发送给用户。"
                    had_success = True
                else:
                    err = str(res)
                    if not is_llm_tool:
                        if model_index is not None:
                            await self._send_plain_direct(event, f"❌ 指定模型生成失败 ({elapsed:.2f}s)\n原因: {err}{suffix}")
                        elif concise and err.startswith("所有 API 均失败"):
                            await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败{suffix}")
                        else:
                            await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {err}{suffix}")
                    last_result = f"自拍失败，原因：{err}"

        # 批量模式：只要有任意一次成功，就视为整体成功，不触发 LLM 失败解释
        if had_success:
            return last_result
        return last_result if last_result else "自拍失败，原因：未知错误"

    async def _selfie_single(
        self,
        i: int,
        count: int,
        available: int,
        event: AstrMessageEvent,
        images_to_send: list,
        prompt: str,
        model_index: Optional[int],
    ):
        """单次自拍生图任务，供并发使用。返回 (kind, suffix, payload)。"""
        suffix = f" ({i+1}/{count})" if count > 1 else ""
        if i >= available:
            group_id = event.get_group_id()
            return ("quota", suffix, self._build_quota_msg(group_id))

        start_time = datetime.now()
        res, model_name = await self.pipeline.execute(
            images_to_send, prompt, model_index=model_index
        )
        elapsed = (datetime.now() - start_time).total_seconds()
        return ("result", suffix, (res, model_name, elapsed))

    async def _run_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        style_id_override: str = "",
        is_llm_tool: bool = False,
        count: int = 1,
        model_index: Optional[int] = None,
    ):
        """命令模式专用 async generator 包装。"""
        result = await self._exec_selfie(event, action, style_id_override, is_llm_tool, count, model_index)
        # 命令模式下错误已在 _exec_selfie 里直接发送，此处无需再 yield
        _ = result  # suppress unused variable warning
        return
        yield  # make it an async generator

    @filter.command("自拍帮助", prefix_optional=True)
    async def on_selfie_help(self, event: AstrMessageEvent):
        async for res in self.commands.on_selfie_help(event):
            yield res

    # ─────────────────────────── #自拍 命令 ───────────────────────────

    @filter.command("自拍", prefix_optional=True)
    async def on_selfie_command(self, event: AstrMessageEvent):
        async for res in self.commands.on_selfie_command(event):
            yield res

    # ─────────────────────────── send_selfie LLM 工具 ───────────────────────────

    @filter.llm_tool(name="send_selfie")
    async def send_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        style_id: str = "",
        count: int = 1,
    ):
        """以你的形象生成图片。理解用户语义，在用户要求生成“有你出镜”的图片时（如自拍、合影、展示形象等）须调用此工具，区别于常规生图。此工具自带你的形象参考图。

        Args:
            action(string): 动作、场景、姿势、服装或情绪描述，例如"在咖啡店窗边喝拿铁"。
            style_id(string): 可选。指定风格 ID 或名称，例如 cinematic、selfie_realistic。留空由插件自动选择。
            count(int): 生图数量（1~3），若不指定，默认为1。除非用户明确要求，否则跳过此参数。
        """
        # clamp count 到 1~3
        try:
            count = max(1, min(3, int(count)))
        except (TypeError, ValueError):
            count = 1

        full_action = action.strip()

        if bool(event.get_group_id()):
            try:
                bot = getattr(event, "bot", None)
                if not bot:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider and hasattr(provider, "bot"):
                        bot = provider.bot
                if bot and hasattr(bot, "set_msg_emoji_like"):
                    await bot.set_msg_emoji_like(
                        message_id=event.message_obj.message_id, emoji_id=66, set=True
                    )
            except Exception as e:
                logger.debug(f"[send_selfie] 贴表情失败: {e}")

        async def _bg():
            try:
                result_msg = await self._exec_selfie(
                    event,
                    full_action,
                    style_id_override=style_id,
                    is_llm_tool=True,
                    count=count,
                )
                if result_msg.startswith("自拍失败"):
                    await self._explain_selfie_failure_with_llm(event, result_msg)
            except Exception as e:
                logger.error(f"[send_selfie] 后台自拍异常: {e}", exc_info=True)
                await self._explain_selfie_failure_with_llm(event, f"自拍失败，原因：{e}")

        try:
            asyncio.create_task(_bg())
        except Exception as e:
            logger.error(f"[send_selfie] 异常: {e}")
            await self._explain_selfie_failure_with_llm(event, f"自拍失败，原因：{e}")

        event.stop_event()

    # ─────────────────────────── 人设管理命令 ───────────────────────────

    @filter.command("自拍人设", prefix_optional=True)
    async def on_selfie_persona_cmd(self, event: AstrMessageEvent):
        async for res in self.commands.on_selfie_persona_cmd(event):
            yield res

    # ─────────────────────────── 风格管理命令 ───────────────────────────

    @filter.command("自拍风格", prefix_optional=True)
    async def on_selfie_style_cmd(self, event: AstrMessageEvent):
        async for res in self.commands.on_selfie_style_cmd(event):
            yield res

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        if self.pipeline:
            await self.pipeline.close()
        logger.info("[astrbot_plugin_free_image] 插件已终止")
