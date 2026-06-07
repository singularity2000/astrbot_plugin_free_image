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
                "以此 AI 助理的固定形象生成一张自拍图片。当用户要求机器人自拍、合影、展示形象等时调用此工具，不用于普通画图或改图。",
            )
            guidance = self.conf.get(
                "selfie_prompt_guidance",
                "Describe the selfie action, scene, posture, clothing, and mood in natural language. Keep the character's established identity; the action parameter should focus on what the character is doing or where they are.",
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
    async def image_generation(self, event: AstrMessageEvent, prompt: str):
        """专业的文生图、图生图工具。理解用户语义，仅当用户需要你生图，或修改图片内容时才调用此工具。

        Args:
            prompt(string): Change the user's input into a professional image generation prompt while strictly preserving the original intent.
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

        # 异步启动后台任务，避免阻塞 LLM 导致超时
        async def _run_background_gen():
            try:
                async for result in self.handle_image_gen_logic(
                    event, prompt, is_i2i=is_i2i, request_source="llm_tool"
                ):
                    await event.send(result)
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

    async def _send_plain_direct(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain(chain=[Plain(text)]))

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
            await self._send_plain_direct(event, text)
        except Exception as e:
            logger.warning(f"[send_selfie] 生成失败解释失败，将发送降级提示: {e}")
            await self._send_plain_direct(
                event, self._fallback_selfie_failure_message(failure_msg)
            )

    async def handle_image_gen_logic(
        self,
        event: AstrMessageEvent,
        prompt: str,
        is_i2i: bool,
        display_name: str | None = None,
        request_source: Literal["command", "llm_tool"] = "command",
    ):
        if not self.pipeline or not self.sender or not self.usage_guard:
            yield event.plain_result("❌ 插件尚未完成初始化，请稍后再试。")
            return

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)

        # --- 权限和次数检查 ---
        if quota_error := await self.usage_guard.check_can_use(event, is_master):
            yield event.plain_result(quota_error)
            return
        if quota_error == "":
            return

        # --- 图片获取 (仅图生图) ---

        images_to_process = []
        if is_i2i:
            if not self.iwf or not (img_bytes_list := await self.iwf.get_images(event)):
                yield event.plain_result("请发送或引用一张图片。")
                return

            MAX_IMAGES = 5
            original_count = len(img_bytes_list)
            if original_count > MAX_IMAGES:
                images_to_process = img_bytes_list[:MAX_IMAGES]
                yield event.plain_result(
                    f"🎨 检测到 {original_count} 张图片，已选取前 {MAX_IMAGES} 张…"
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
            yield event.plain_result(start_msg)

        # --- API 调用 ---
        start_time = datetime.now()
        res, model_name = await self.pipeline.execute(images_to_process, prompt)
        elapsed = (datetime.now() - start_time).total_seconds()

        image_results = self.sender.normalize_image_results(res)
        if image_results:
            if deduction_error := await self.usage_guard.deduct_after_success(event, is_master):
                yield event.plain_result(deduction_error)
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
                yield event.plain_result(deduction_error)
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

            caption_text = " | ".join(caption_parts)
            video_component = Video.fromURL(url=res["url"])
            logger.info(caption_text)
            if concise_mode:
                yield event.chain_result(
                    [Reply(id=event.message_obj.message_id), video_component]
                )
            else:
                yield event.chain_result([video_component, Plain(caption_text)])
        else:
            if concise_mode and str(res).startswith("所有 API 均失败"):
                yield event.plain_result(
                    f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败"
                )
                return
            yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")

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

        start_time = datetime.now()
        res, model_name = await self.pipeline.execute(images_to_send, prompt)
        elapsed = (datetime.now() - start_time).total_seconds()

        image_results = self.sender.normalize_image_results(res)
        if image_results:
            deduction_error = await self.usage_guard.deduct_after_success(event, is_master)
            if deduction_error:
                if not is_llm_tool:
                    await self._send_plain_direct(event, deduction_error)
                return f"自拍失败，原因：{deduction_error}"
            request_source: Literal["command", "llm_tool"] = "llm_tool" if is_llm_tool else "command"
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
            ] if part)
            logger.info(caption_text)
            async for msg in self.sender.yield_success_images(
                event=event, images=image_results, caption_text=caption_text,
                concise_mode=concise, request_source=request_source,
            ):
                await event.send(msg)
            return f"已成功为「{persona_name}」生成自拍（{elapsed:.1f}s），已发送给用户。"
        else:
            err = str(res)
            if not is_llm_tool:
                if concise and err.startswith("所有 API 均失败"):
                    await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败")
                else:
                    await self._send_plain_direct(event, f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {err}")
            return f"自拍失败，原因：{err}"

    async def _run_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        style_id_override: str = "",
        is_llm_tool: bool = False,
    ):
        """命令模式专用 async generator 包装。"""
        result = await self._exec_selfie(event, action, style_id_override, is_llm_tool)
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
        aspect_ratio: str = "",
    ):
        """以此 AI 助理的固定形象生成一张自拍图片。当用户要求机器人自拍、合影、展示形象等时调用此工具，不用于普通画图或改图。

        Args:
            action(string): 动作、场景、姿势、服装或情绪描述，例如"在咖啡店窗边喝拿铁"。
            style_id(string): 可选。指定风格 ID 或名称，例如 cinematic、selfie_realistic。留空由插件自动选择。
            aspect_ratio(string): 可选。宽高比，例如 9:16、16:9、1:1。
        """
        full_action = action.strip()
        if aspect_ratio:
            full_action = f"{full_action}, aspect ratio {aspect_ratio}" if full_action else f"aspect ratio {aspect_ratio}"

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
