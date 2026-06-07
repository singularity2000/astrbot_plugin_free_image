import asyncio
import random
import re
from datetime import datetime
from typing import Dict, Literal, Optional

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply, Video
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .persistence import PersistenceManager
from .pipeline import ImageGenPipeline
from .selfie import (
    build_selfie_prompt,
    combine_images,
    find_persona,
    find_style,
    load_persona_images,
    resolve_persona,
    resolve_style,
)
from .workflow import ImageWorkflow


@register(
    "astrbot_plugin_free_image",
    "Singularity2000",
    "文生图、图生图，可自定义提示词模板，兼容多种端点",
    "2.0.0",
    "https://github.com/singularity2000/astrbot_plugin_free_image",
)
class ImageGenerationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.persistence = PersistenceManager(config, StarTools.get_data_dir())
        self.pipeline: Optional[ImageGenPipeline] = None
        self.iwf: Optional[ImageWorkflow] = None
        self.prompt_map: Dict[str, str] = {}

    async def initialize(self):
        self.iwf = ImageWorkflow(self.conf)

        # --- 构建 Pipeline ---
        self.pipeline = ImageGenPipeline(self.conf, self.iwf)
        pipeline_config = self.conf.get("api_pipeline", [])
        self.pipeline.build(pipeline_config)

        await self.persistence.load_all()
        await self._load_prompt_map()

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

    async def _load_prompt_map(self):
        logger.info("正在加载 prompts...")
        self.prompt_map.clear()
        seen_keys = set()
        prompt_list = self.conf.get("prompt_list", [])
        for item in prompt_list:
            try:
                if ":" in item:
                    key, value = item.split(":", 1)
                    key = key.strip()
                    if key in seen_keys:
                        logger.warning(
                            f"检测到重复的预设指令“{key}”，配置中仅新增的模板将会生效。"
                        )
                    seen_keys.add(key)
                    self.prompt_map[key] = value.strip()
                else:
                    logger.warning(f"跳过格式错误的 prompt (缺少冒号): {item}")
            except ValueError:
                logger.warning(f"跳过格式错误的 prompt: {item}")
        logger.info(f"加载了 {len(self.prompt_map)} 个 prompts。")

    def _admin_denied_message(self) -> str:
        return "你没有权限使用此命令。"

    def _get_api_pipeline_config(self) -> list:
        pipeline_config = self.conf.get("api_pipeline", [])
        return pipeline_config if isinstance(pipeline_config, list) else []

    def _get_model_display_name(self, node: dict) -> str:
        model_name = str(node.get("model", "")).strip()
        if model_name:
            return model_name
        template_key = str(node.get("__template_key", "")).strip()
        return template_key or "未命名模型"

    def _format_model_pipeline_message(self, prefix: str = "") -> str:
        pipeline_config = self._get_api_pipeline_config()
        lines = []
        if prefix:
            lines.append(prefix)
        lines.append("当前模型回退顺序为：")
        lines.append("")

        if pipeline_config:
            for index, node in enumerate(pipeline_config, start=1):
                status = "🟢" if node.get("enabled", True) else "🔴"
                lines.append(f"{index}{status}{self._get_model_display_name(node)}")
        else:
            lines.append("当前 API 管线为空，请先在 WebUI 配置 api_pipeline。")

        lines.extend(
            [
                "",
                "画图模型 置顶 <序号> 将该模型置顶到管线顶部",
                "画图模型 开启/关闭 <序号> 将该模型启用或关闭",
            ]
        )
        return "\n".join(lines)

    def _parse_model_command(self, raw_args: str) -> tuple[str | None, int | None]:
        if not raw_args:
            return None, None
        parts = raw_args.split()
        if len(parts) != 2:
            return "", None
        action, index_text = parts
        if action not in {"置顶", "开启", "关闭"} or not index_text.isdigit():
            return "", None
        return action, int(index_text)

    def _save_and_rebuild_pipeline(self) -> None:
        self.conf["api_pipeline"] = self._get_api_pipeline_config()
        self.conf.save_config()
        if self.pipeline:
            self.pipeline.build(self.conf.get("api_pipeline", []))

    async def _yield_model_pipeline_error(self, event: AstrMessageEvent):
        yield event.plain_result(
            self._format_model_pipeline_message("命令格式或参数错误，请重试。")
        )
        event.stop_event()

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

    async def _handle_model_pipeline_command(self, event: AstrMessageEvent, raw_args: str):
        if not self.is_global_admin(event):
            yield event.plain_result(self._admin_denied_message())
            event.stop_event()
            return

        action, index = self._parse_model_command(raw_args)
        pipeline_config = self._get_api_pipeline_config()
        if action is None:
            yield event.plain_result(self._format_model_pipeline_message())
            event.stop_event()
            return
        if not action or index is None or index < 1 or index > len(pipeline_config):
            async for result in self._yield_model_pipeline_error(event):
                yield result
            return

        node_index = index - 1
        if action == "置顶":
            node = pipeline_config.pop(node_index)
            pipeline_config.insert(0, node)
        elif action == "开启":
            pipeline_config[node_index]["enabled"] = True
        elif action == "关闭":
            pipeline_config[node_index]["enabled"] = False
        else:
            async for result in self._yield_model_pipeline_error(event):
                yield result
            return

        self._save_and_rebuild_pipeline()
        yield event.plain_result(
            self._format_model_pipeline_message("操作成功。")
        )
        event.stop_event()

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
        if self.conf.get("prefix", True) and not event.is_at_or_wake_command:
            return
        text = self._get_plain_message_text(event, strip_wake_prefix=True)
        if not text:
            return
        if text.startswith("画图模型") and not (
            text == "画图模型" or text.startswith("画图模型 ")
        ):
            async for result in self._handle_model_pipeline_command(
                event, text.removeprefix("画图模型").strip()
            ):
                yield result
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0].strip()
        extra_text = parts[1].strip() if len(parts) > 1 else ""
        user_prompt = ""
        if cmd == "图生图":
            user_prompt = text.removeprefix(cmd).strip()
            if not user_prompt:
                return
            # 图生图命令交由专用 command 处理，避免重复触发
            return
        elif cmd in self.prompt_map:
            base_prompt = self.prompt_map.get(cmd)
            if base_prompt is None:
                return
            user_prompt = f"{base_prompt} {extra_text}" if extra_text else base_prompt
            display_cmd = f"{cmd} {extra_text}".strip()
        else:
            return

        async for res in self.handle_image_gen_logic(
            event,
            user_prompt,
            is_i2i=True,
            display_name=display_cmd,
            request_source="command",
        ):
            yield res
        event.stop_event()

    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        if prompt.startswith("文生图"):
            prompt = prompt.removeprefix("文生图").strip()
        if not prompt:
            yield event.plain_result("请提供文生图的描述。用法: #文生图 <描述>")
            return

        async for res in self.handle_image_gen_logic(
            event, prompt, is_i2i=False, request_source="command"
        ):
            yield res
        event.stop_event()

    @filter.command("图生图", prefix_optional=True)
    async def on_image_to_image_request(self, event: AstrMessageEvent):
        prompt = self._strip_command_prefix(
            self._get_plain_message_text(event, strip_wake_prefix=True), "图生图"
        )
        if not prompt:
            yield event.plain_result(
                "请提供图生图的描述。用法: #图生图 <描述>（并发送或引用图片）"
            )
            return

        async for res in self.handle_image_gen_logic(
            event, prompt, is_i2i=True, request_source="command"
        ):
            yield res
        event.stop_event()

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    def _normalize_image_results(self, res) -> list[bytes]:
        if isinstance(res, bytes):
            return [res]
        if isinstance(res, list):
            return [item for item in res if isinstance(item, bytes)]
        return []

    def _build_success_caption(
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

        return " | ".join(caption_parts)

    def _should_quote_reply(
        self,
        *,
        request_source: Literal["command", "llm_tool"],
    ) -> bool:
        mode = self.conf.get("quote_reply_mode", "始终引用回复")
        if mode == "始终单独发送":
            return False
        if mode == "命令引用回复，函数调用单独发送":
            return request_source == "command"
        if mode == "命令单独发送，函数调用引用回复":
            return request_source == "llm_tool"
        return True

    def _should_split_multi_images(self, *, event: AstrMessageEvent) -> bool:
        mode = self.conf.get("multi_image_send_mode", "始终不分条")
        is_group = self._is_group_chat(event)
        if mode == "始终分条":
            return True
        if mode == "群聊不分条，私聊分条":
            return not is_group
        if mode == "群聊分条，私聊不分条":
            return is_group
        return False

    async def _yield_success_images(
        self,
        *,
        event: AstrMessageEvent,
        images: list[bytes],
        caption_text: str,
        concise_mode: bool,
        request_source: Literal["command", "llm_tool"],
    ):
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

    async def handle_image_gen_logic(
        self,
        event: AstrMessageEvent,
        prompt: str,
        is_i2i: bool,
        display_name: str | None = None,
        request_source: Literal["command", "llm_tool"] = "command",
    ):
        if not self.pipeline:
            yield event.plain_result("❌ 插件尚未完成初始化，请稍后再试。")
            return

        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)

        # --- 权限和次数检查 ---
        if not is_master:
            if sender_id in self.conf.get("user_blacklist", []):
                return
            if group_id and group_id in self.conf.get("group_blacklist", []):
                return
            if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get(
                "user_whitelist", []
            ):
                return
            if (
                group_id
                and self.conf.get("group_whitelist", [])
                and group_id not in self.conf.get("group_whitelist", [])
            ):
                return

            # 频率限制检查
            if error_msg := await self.pipeline.check_rate_limit():
                yield event.plain_result(error_msg)
                return

            # 原子化扣费检查
            if deduction_error := await self.persistence.check_and_deduct_count(
                sender_id, group_id
            ):
                yield event.plain_result(deduction_error)
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

        image_results = self._normalize_image_results(res)
        if image_results:
            caption_text = self._build_success_caption(
                elapsed=elapsed,
                is_i2i=is_i2i,
                display_name=display_name,
                is_master=is_master,
                sender_id=sender_id,
                group_id=group_id,
                model_name=model_name,
            )
            logger.info(caption_text)
            async for msg in self._yield_success_images(
                event=event,
                images=image_results,
                caption_text=caption_text,
                concise_mode=concise_mode,
                request_source=request_source,
            ):
                yield msg
        elif isinstance(res, dict) and res.get("type") == "video" and res.get("url"):
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
        if not self.is_global_admin(event):
            yield event.plain_result(self._admin_denied_message())
            event.stop_event()
            return
        raw = event.message_str.strip()
        # 移除命令名本身（支持别名）
        for cmd in ("画图添加模板", "lma", "lm添加"):
            if raw.startswith(cmd):
                raw = raw.removeprefix(cmd).strip()
                break
        if ":" not in raw:
            yield event.plain_result(
                "格式错误, 正确示例:\n#画图添加模板 姿势表:为这幅图创建一个姿势表, 摆出各种姿势"
            )
            event.stop_event()
            return

        key, new_value = map(str.strip, raw.split(":", 1))
        prompt_list = self.conf.get("prompt_list", [])
        for item in prompt_list:
            if item.strip().startswith(key + ":"):
                yield event.plain_result(f"预设指令“{key}”已存在，已取消添加。")
                event.stop_event()
                return

        prompt_list.append(f"{key}:{new_value}")

        self.conf["prompt_list"] = prompt_list
        self.conf.save_config()
        await self._load_prompt_map()
        yield event.plain_result(f"已保存生图提示语模板:\n{key}:{new_value}")
        event.stop_event()

    @filter.command("画图模型", prefix_optional=True)
    async def on_model_pipeline_command(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        if raw.startswith("画图模型"):
            raw = raw.removeprefix("画图模型").strip()
        async for result in self._handle_model_pipeline_command(event, raw):
            yield result

    @filter.command("画图简洁模式", prefix_optional=True)
    async def on_concise_mode_command(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result(self._admin_denied_message())
            event.stop_event()
            return

        raw = event.message_str.strip()
        if raw.startswith("画图简洁模式"):
            raw = raw.removeprefix("画图简洁模式").strip()

        if raw not in {"开启", "关闭"}:
            yield event.plain_result("命令格式或参数错误，请重试。")
            event.stop_event()
            return

        self.conf["concise_mode"] = raw == "开启"
        self.conf.save_config()
        yield event.plain_result("操作成功。")
        event.stop_event()

    @filter.command("画图帮助", aliases={"lmh", "lm帮助"}, prefix_optional=True)
    async def on_prompt_help(self, event: AstrMessageEvent):
        keyword = event.message_str.strip()
        # 移除指令名本身（支持别名）
        for cmd in ("画图帮助", "lmh", "lm帮助"):
            if keyword.startswith(cmd):
                keyword = keyword.removeprefix(cmd).strip()
                break
        if not keyword:
            msg = "图生图预设指令: \n"
            msg += "、".join(self.prompt_map.keys())
            msg += "\n\n#画图帮助 <预设指令> 来查看对应模板的详细内容"
            msg += "\n\n生图指令: \n#文生图 <你的描述> \n#图生图 <你的描述>（发送图片 或 引用图片 或 @用户,若为空则使用你的头像）"
            msg += "\n#预设指令 + 发送图片 或 引用图片 或 @用户 来使用模板图生图"
            yield event.plain_result(msg)
            return

        prompt = self.prompt_map.get(keyword)
        if not prompt:
            yield event.plain_result("未找到此预设指令")
            return
        yield event.plain_result(f"预设 [{keyword}] 的内容:\n{prompt}")

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    @filter.command("画图签到", prefix_optional=True)
    async def on_checkin(self, event: AstrMessageEvent):
        if not self.conf.get("enable_checkin", False):
            yield event.plain_result("📅 本机器人未开启签到功能。")
            return
        user_id = event.get_sender_id()
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.persistence.user_checkin_data.get(user_id) == today_str:
            yield event.plain_result(
                f"您今天已经签到过了。\n剩余次数: {self.persistence.get_user_count(user_id)}"
            )
            return

        reward = 0
        if str(self.conf.get("enable_random_checkin", False)).lower() == "true":
            max_reward = max(1, int(self.conf.get("checkin_random_reward_max", 5)))
            reward = random.randint(1, max_reward)
        else:
            reward = int(self.conf.get("checkin_fixed_reward", 3))

        # 签到奖励只增加永久次数
        await self.persistence.add_permanent_user_count(user_id, reward)
        await self.persistence.save_user_checkin(user_id, today_str)

        # 回复时显示总次数
        new_total_count = self.persistence.get_user_count(user_id)
        yield event.plain_result(
            f"🎉 签到成功！获得 {reward} 次（永久），当前总剩余: {new_total_count} 次。"
        )

    @filter.command("画图增加用户次数", prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result(self._admin_denied_message())
            event.stop_event()
            return
        cmd_text = event.message_str.strip()
        at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
        target_qq, count = None, 0
        if at_seg:
            target_qq = str(at_seg.qq)
            match = re.search(r"(\d+)\s*$", cmd_text)
            if match:
                count = int(match.group(1))
        else:
            match = re.search(r"(\d+)\s+(\d+)", cmd_text)
            if match:
                target_qq, count = match.group(1), int(match.group(2))
        if not target_qq or count <= 0:
            yield event.plain_result(
                "格式错误:\n#画图增加用户次数 @用户 <次数>\n或 #画图增加用户次数 <QQ号> <次数>"
            )
            event.stop_event()
            return

        # 管理员增加的是永久次数
        await self.persistence.add_permanent_user_count(target_qq, count)

        # 回复时显示总次数
        new_total_count = self.persistence.get_user_count(target_qq)
        yield event.plain_result(
            f"✅ 已为用户 {target_qq} 增加 {count} 次（永久），TA当前总剩余 {new_total_count} 次。"
        )
        event.stop_event()

    @filter.command("画图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result(self._admin_denied_message())
            event.stop_event()
            return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result("格式错误: #画图增加群组次数 <群号> <次数>")
            event.stop_event()
            return
        target_group, count = match.group(1), int(match.group(2))

        # 管理员增加的是永久次数
        await self.persistence.add_permanent_group_count(target_group, count)

        # 回复时显示总次数
        new_total_count = self.persistence.get_group_count(target_group)
        yield event.plain_result(
            f"✅ 已为群组 {target_group} 增加 {count} 次（永久），该群当前总剩余 {new_total_count} 次。"
        )
        event.stop_event()

    @filter.command("画图查询次数", prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        user_id_to_query = event.get_sender_id()
        if self.is_global_admin(event):
            at_seg = next(
                (s for s in event.message_obj.message if isinstance(s, At)), None
            )
            if at_seg:
                user_id_to_query = str(at_seg.qq)
            else:
                match = re.search(r"(\d+)", event.message_str)
                if match:
                    user_id_to_query = match.group(1)
        user_count = self.persistence.get_user_count(user_id_to_query)
        reply_msg = f"用户 {user_id_to_query} 个人剩余次数为: {user_count}"
        if user_id_to_query == event.get_sender_id():
            reply_msg = f"您好，您当前个人剩余次数为: {user_count}"
        if group_id := event.get_group_id():
            reply_msg += (
                f"\n本群共享剩余次数为: {self.persistence.get_group_count(group_id)}"
            )
        yield event.plain_result(reply_msg)

    # ─────────────────────────── 自拍核心入口 ───────────────────────────


    async def _check_selfie_quota(
        self, event: AstrMessageEvent, is_master: bool
    ) -> Optional[str]:
        """执行权限/黑白名单/冷却/配额检查，返回错误信息或 None。"""
        if is_master:
            return None
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        if sender_id in self.conf.get("user_blacklist", []):
            return ""  # 黑名单静默
        if group_id and group_id in self.conf.get("group_blacklist", []):
            return ""
        if self.conf.get("user_whitelist", []) and sender_id not in self.conf.get("user_whitelist", []):
            return ""
        if group_id and self.conf.get("group_whitelist", []) and group_id not in self.conf.get("group_whitelist", []):
            return ""
        if error_msg := await self.pipeline.check_rate_limit():
            return error_msg
        if deduction_error := await self.persistence.check_and_deduct_count(sender_id, group_id):
            return deduction_error
        return None

    async def _exec_selfie(
        self,
        event: AstrMessageEvent,
        action: str,
        style_id_override: str = "",
        is_llm_tool: bool = False,
    ) -> str:
        """执行自拍生图，返回状态字符串。图片通过 event.send 直接发送。"""
        if not self.pipeline:
            return "自拍失败，原因：插件尚未完成初始化。"

        is_master = self.is_global_admin(event)
        quota_err = await self._check_selfie_quota(event, is_master)
        if quota_err is not None:
            if quota_err and not is_llm_tool:
                await event.send(event.plain_result(quota_err))
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
            await event.send(event.plain_result(f"📸 正在生成自拍 [{persona_name}]…"))

        start_time = datetime.now()
        res, model_name = await self.pipeline.execute(images_to_send, prompt)
        elapsed = (datetime.now() - start_time).total_seconds()

        image_results = self._normalize_image_results(res)
        if image_results:
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
            caption_text = " | ".join(filter(None, [
                f"✅ 生成成功 ({elapsed:.2f}s)",
                f"人设: {persona_name}",
                f"风格: {style_name}",
                remaining_str,
                f"模型: {model_name}",
            ]))
            logger.info(caption_text)
            async for msg in self._yield_success_images(
                event=event, images=image_results, caption_text=caption_text,
                concise_mode=concise, request_source=request_source,
            ):
                await event.send(msg)
            return f"已成功为「{persona_name}」生成自拍（{elapsed:.1f}s），已发送给用户。"
        else:
            err = str(res)
            if not is_llm_tool:
                if concise and err.startswith("所有 API 均失败"):
                    await event.send(event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: 所有API均失败"))
                else:
                    await event.send(event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {err}"))
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
        mode = self.conf.get("selfie_style_mode", "自动")
        binding = self.conf.get("selfie_binding_mode", "优先 AstrBot persona")
        yield event.plain_result(
            "📸 自拍命令\n"
            f"当前风格模式：{mode}  绑定模式：{binding}\n\n"
            "#自拍 <描述>\n"
            "#自拍人设 查看 / 列表 / 添加 <ID> <名称> / 绑定 <ID或名称> / 默认 <ID或名称>\n"
            "#自拍风格 查看 / 列表 / 添加 <ID> <名称> <提示词> / 模式 <不注入/自动/指定> / 选择 <ID或名称>"
        )
        event.stop_event()

    # ─────────────────────────── #自拍 命令 ───────────────────────────

    @filter.command("自拍", prefix_optional=True)
    async def on_selfie_command(self, event: AstrMessageEvent):
        action = self._strip_command_prefix(
            self._get_plain_message_text(event, strip_wake_prefix=True), "自拍"
        ).strip()
        if self.conf.get("concise_mode", False) and bool(event.get_group_id()):
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
                logger.debug(f"[#自拍] 贴表情失败: {e}")
        async for msg in self._run_selfie(event, action, is_llm_tool=False):
            yield msg
        event.stop_event()

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

        result_msg = ""
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
        try:
            async def _bg():
                nonlocal result_msg
                result_msg = await self._exec_selfie(event, full_action, style_id_override=style_id, is_llm_tool=True)

            asyncio.create_task(_bg())
        except Exception as e:
            result_msg = f"自拍失败，原因：{e}"
            logger.error(f"[send_selfie] 异常: {e}")

        event.stop_event()
        return "系统提示：自拍生成中，完成后将直接发给用户。"

    # ─────────────────────────── 人设管理命令 ───────────────────────────

    @filter.command("自拍人设", prefix_optional=True)
    async def on_selfie_persona_cmd(self, event: AstrMessageEvent):
        raw = self._strip_command_prefix(
            self._get_plain_message_text(event, strip_wake_prefix=True), "自拍人设"
        ).strip()
        parts = raw.split(maxsplit=1)
        sub = parts[0].strip() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""

        if sub == "查看":
            yield event.plain_result(await self._selfie_persona_info(args, event))
        elif sub == "列表":
            yield event.plain_result(self._selfie_persona_list())
        elif sub == "添加":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(await self._selfie_persona_add(args, event))
        elif sub == "绑定":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self._selfie_persona_bind(args, event))
        elif sub == "默认":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self._selfie_persona_set_default(args))
        else:
            yield event.plain_result(
                "#自拍人设 查看 [ID或名称]\n"
                "#自拍人设 列表\n"
                "#自拍人设 添加 <ID> <名称>  （同时发送/引用图片）\n"
                "#自拍人设 绑定 <ID或名称>\n"
                "#自拍人设 默认 <ID或名称>"
            )
        event.stop_event()

    def _selfie_persona_list(self) -> str:
        from .selfie import _all_personas
        personas = _all_personas(self.conf)
        if not personas:
            return "当前还没有自拍人设。"
        default_id = self.conf.get("selfie_default_persona_id", "")
        lines = ["自拍人设列表："]
        for p in personas:
            pid = p.get("id", "")
            flag = "★" if pid == default_id else " "
            lines.append(f"{flag} [{pid}] {p.get('name', '')}  参考图 {len(p.get('ref_images') or [])} 张")
        return "\n".join(lines)

    async def _selfie_persona_info(self, query: str, event: AstrMessageEvent) -> str:
        if query:
            p = find_persona(self.conf, query)
            if not p:
                return f"找不到自拍人设：{query}。"
        else:
            session_id = str(event.unified_msg_origin or "")
            p = await resolve_persona(self.conf, self.context, event, session_id)
            if not p:
                return "当前没有命中任何自拍人设，请先配置全局默认人设。"
        return (
            f"人设 ID：{p.get('id')}\n"
            f"名称：{p.get('name')}\n"
            f"描述：{p.get('description') or '（无）'}\n"
            f"参考图：{len(p.get('ref_images') or [])} 张\n"
            f"路径：{'; '.join(p.get('ref_images') or []) or '（无）'}"
        )

    async def _selfie_persona_add(self, args: str, event: AstrMessageEvent) -> str:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return "格式：#自拍人设 添加 <ID> <名称>（同时发送/引用图片）"
        pid, name = parts[0].strip(), parts[1].strip()
        if not pid or not name:
            return "ID 和名称不能为空。"
        from .selfie import _all_personas
        if any(p.get("id") == pid for p in _all_personas(self.conf)):
            return f"自拍人设 ID 已存在：{pid}。"

        # 收集本次消息图片并保存到数据目录
        imgs: list[bytes] = []
        if self.iwf:
            imgs = await self.iwf.get_images(event)
        if not imgs:
            return "请随消息发送或引用至少一张图片作为参考图。"

        save_dir = StarTools.get_data_dir() / "selfie_personas" / pid
        save_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        loop = asyncio.get_running_loop()
        for i, img_bytes in enumerate(imgs):
            path = save_dir / f"ref_{i+1}.png"
            await loop.run_in_executor(None, path.write_bytes, img_bytes)
            saved_paths.append(str(path))

        personas = list(self.conf.get("selfie_personas", []) or [])
        personas.append({
            "__template_key": "selfie_persona",
            "id": pid,
            "name": name,
            "description": "",
            "ref_images": saved_paths,
        })
        self.conf["selfie_personas"] = personas
        self.conf.save_config()
        return f"✅ 已保存自拍人设「{name}」（{pid}），参考图 {len(saved_paths)} 张。"

    def _selfie_persona_bind(self, args: str, event: AstrMessageEvent) -> str:
        p = find_persona(self.conf, args)
        if not p:
            return f"找不到自拍人设：{args}。"
        sid = str(event.unified_msg_origin or "").strip()
        if not sid:
            return "无法获取当前会话 SID，绑定失败。"
        personas = list(self.conf.get("selfie_personas") or [])
        pid = p.get("id")
        for entry in personas:
            if not isinstance(entry, dict):
                continue
            sids = list(entry.get("bound_sids") or [])
            if entry.get("id") == pid:
                if sid not in sids:
                    sids.append(sid)
                    entry["bound_sids"] = sids
            else:
                # 从其他人设中移除此 SID（一个 SID 只绑一个人设）
                if sid in sids:
                    sids.remove(sid)
                    entry["bound_sids"] = sids
        self.conf["selfie_personas"] = personas
        self.conf.save_config()
        return f"✅ 已将当前会话（{sid}）绑定至人设「{p.get('name')}」。"

    def _selfie_persona_set_default(self, args: str) -> str:
        p = find_persona(self.conf, args)
        if not p:
            return f"找不到自拍人设：{args}。"
        self.conf["selfie_default_persona_id"] = p.get("id")
        self.conf.save_config()
        return f"✅ 已将全局默认人设设为「{p.get('name')}」。"

    # ─────────────────────────── 风格管理命令 ───────────────────────────

    @filter.command("自拍风格", prefix_optional=True)
    async def on_selfie_style_cmd(self, event: AstrMessageEvent):
        raw = self._strip_command_prefix(
            self._get_plain_message_text(event, strip_wake_prefix=True), "自拍风格"
        ).strip()
        parts = raw.split(maxsplit=1)
        sub = parts[0].strip() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""

        if sub == "查看":
            yield event.plain_result(self._selfie_style_info(args, event))
        elif sub == "列表":
            yield event.plain_result(self._selfie_style_list())
        elif sub == "添加":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self._selfie_style_add(args))
        elif sub == "模式":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self._selfie_style_set_mode(args))
        elif sub == "选择":
            if not self.is_global_admin(event):
                yield event.plain_result(self._admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self._selfie_style_select(args))
        else:
            yield event.plain_result(
                "#自拍风格 查看 [ID或名称]\n"
                "#自拍风格 列表\n"
                "#自拍风格 添加 <ID> <名称> <提示词>  （关键词用竖线分隔追加，可省略）\n"
                "#自拍风格 模式 <不注入/自动/指定>\n"
                "#自拍风格 选择 <ID或名称>"
            )
        event.stop_event()

    def _selfie_style_list(self) -> str:
        from .selfie import _all_styles
        styles = _all_styles(self.conf)
        if not styles:
            return "当前风格模板库为空。"
        selected = self.conf.get("selfie_selected_style_id", "")
        mode = self.conf.get("selfie_style_mode", "自动")
        lines = [f"自拍风格列表（当前模式：{mode}）："]
        for s in styles:
            flag = "→" if s.get("id") == selected else " "
            lines.append(f"{flag} [{s.get('id')}] {s.get('name')}")
        return "\n".join(lines)

    def _selfie_style_info(self, query: str, event: AstrMessageEvent) -> str:
        if query:
            s = find_style(self.conf, query)
            if not s:
                return f"找不到自拍风格：{query}。"
        else:
            mode = self.conf.get("selfie_style_mode", "自动")
            selected = self.conf.get("selfie_selected_style_id", "")
            s = find_style(self.conf, selected) if selected else None
            if not s:
                return f"当前模式：{mode}，未指定默认风格。"
        return (
            f"风格 ID：{s.get('id')}\n"
            f"名称：{s.get('name')}\n"
            f"关键词：{', '.join(s.get('keywords') or []) or '（无）'}\n"
            f"提示词：{s.get('prompt', '')}"
        )

    def _selfie_style_add(self, args: str) -> str:
        # 格式：<ID> <名称> <提示词> [|关键词1|关键词2]
        # 关键词部分可选，用竖线开头
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            return "格式：#自拍风格 添加 <ID> <名称> <提示词>"
        sid, name = parts[0].strip(), parts[1].strip()
        rest = parts[2].strip()
        keywords: list[str] = []
        if "|" in rest:
            prompt_part, _, kw_part = rest.partition("|")
            prompt_str = prompt_part.strip()
            keywords = [k.strip() for k in kw_part.split("|") if k.strip()]
        else:
            prompt_str = rest
        if not sid or not name or not prompt_str:
            return "ID、名称和提示词不能为空。"
        from .selfie import _all_styles
        if any(s.get("id") == sid for s in _all_styles(self.conf)):
            return f"自拍风格 ID 已存在：{sid}。"
        styles = list(self.conf.get("selfie_styles", []) or [])
        styles.append({
            "__template_key": "selfie_style",
            "id": sid,
            "name": name,
            "prompt": prompt_str,
            "keywords": keywords,
            "enabled": True,
        })
        self.conf["selfie_styles"] = styles
        self.conf.save_config()
        return f"✅ 已添加自拍风格「{name}」（{sid}）。"

    def _selfie_style_set_mode(self, args: str) -> str:
        mode_map = {"不注入": "不注入", "自动": "自动", "指定": "指定"}
        m = mode_map.get(args.strip())
        if not m:
            return "支持的模式：不注入、自动、指定。"
        self.conf["selfie_style_mode"] = m
        self.conf.save_config()
        return f"✅ 自拍风格注入模式已设为「{m}」。"

    def _selfie_style_select(self, args: str) -> str:
        s = find_style(self.conf, args)
        if not s:
            return f"找不到自拍风格：{args}。"
        self.conf["selfie_selected_style_id"] = s.get("id")
        self.conf.save_config()
        return f"✅ 已将指定风格设为「{s.get('name')}」（{s.get('id')}）。"

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        if self.pipeline:
            await self.pipeline.close()
        logger.info("[astrbot_plugin_free_image] 插件已终止")
