import asyncio
import random
import re
from datetime import datetime
from typing import Dict, Optional

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply, Video
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .migration import migrate_legacy_config
from .persistence import PersistenceManager
from .pipeline import ImageGenPipeline
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

        # --- 向后兼容迁移：旧的单选配置 → 新的 api_pipeline ---
        await migrate_legacy_config(self.conf)

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

    async def _load_prompt_map(self):
        logger.info("正在加载 prompts...")
        self.prompt_map.clear()
        prompt_list = self.conf.get("prompt_list", [])
        for item in prompt_list:
            try:
                if ":" in item:
                    key, value = item.split(":", 1)
                    self.prompt_map[key.strip()] = value.strip()
                else:
                    logger.warning(f"跳过格式错误的 prompt (缺少冒号): {item}")
            except ValueError:
                logger.warning(f"跳过格式错误的 prompt: {item}")
        logger.info(f"加载了 {len(self.prompt_map)} 个 prompts。")

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
                    event, prompt, is_i2i=is_i2i
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
        text = event.message_str.strip()
        if not text:
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0].strip()
        extra_text = parts[1].strip() if len(parts) > 1 else ""
        bnn_command = "图生图"
        user_prompt = ""
        if cmd == bnn_command:
            user_prompt = text.removeprefix(cmd).strip()
            if not user_prompt:
                return
            display_cmd = (
                user_prompt[:10] + "..." if len(user_prompt) > 10 else user_prompt
            )
            # 图生图命令交由专用 command 处理，避免重复触发
            return
        elif cmd in self.prompt_map:
            base_prompt = self.prompt_map.get(cmd)
            user_prompt = f"{base_prompt} {extra_text}" if extra_text else base_prompt
            display_cmd = f"{cmd} {extra_text}".strip()
        else:
            return

        async for res in self.handle_image_gen_logic(
            event, user_prompt, is_i2i=True, display_name=display_cmd
        ):
            yield res
        event.stop_event()

    @filter.command("文生图", prefix_optional=True)
    async def on_text_to_image_request(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        if not prompt:
            yield event.plain_result("请提供文生图的描述。用法: #文生图 <描述>")
            return

        async for res in self.handle_image_gen_logic(event, prompt, is_i2i=False):
            yield res
        event.stop_event()

    @filter.command("图生图", prefix_optional=True)
    async def on_image_to_image_request(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        if prompt.startswith("图生图"):
            prompt = prompt.removeprefix("图生图").strip()
        if not prompt:
            yield event.plain_result(
                "请提供图生图的描述。用法: #图生图 <描述>（并发送或引用图片）"
            )
            return

        async for res in self.handle_image_gen_logic(event, prompt, is_i2i=True):
            yield res
        event.stop_event()

    async def handle_image_gen_logic(
        self,
        event: AstrMessageEvent,
        prompt: str,
        is_i2i: bool,
        display_name: str = None,
    ):
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
        res = await self.pipeline.execute(images_to_process, prompt)
        elapsed = (datetime.now() - start_time).total_seconds()

        if isinstance(res, bytes):
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

            caption_text = " | ".join(caption_parts)
            if concise_mode:
                logger.info(caption_text)
                yield event.chain_result(
                    [Reply(id=event.message_obj.message_id), Image.fromBytes(res)]
                )
            else:
                yield event.chain_result([Image.fromBytes(res), Plain(caption_text)])
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

            caption_text = " | ".join(caption_parts)
            video_component = Video.fromURL(url=res["url"])
            if concise_mode:
                logger.info(caption_text)
                yield event.chain_result(
                    [Reply(id=event.message_obj.message_id), video_component]
                )
            else:
                yield event.chain_result([video_component, Plain(caption_text)])
        else:
            yield event.plain_result(f"❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}")

    @filter.command("画图添加模板", aliases={"lma", "lm添加"}, prefix_optional=True)
    async def add_lm_prompt(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
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
            return

        key, new_value = map(str.strip, raw.split(":", 1))
        prompt_list = self.conf.get("prompt_list", [])
        found = False
        for idx, item in enumerate(prompt_list):
            if item.strip().startswith(key + ":"):
                prompt_list[idx] = f"{key}:{new_value}"
                found = True
                break
        if not found:
            prompt_list.append(f"{key}:{new_value}")

        self.conf["prompt_list"] = prompt_list
        self.conf.save_config()
        await self._load_prompt_map()
        yield event.plain_result(f"已保存生图提示语模板:\n{key}:{new_value}")

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
            return

        # 管理员增加的是永久次数
        await self.persistence.add_permanent_user_count(target_qq, count)

        # 回复时显示总次数
        new_total_count = self.persistence.get_user_count(target_qq)
        yield event.plain_result(
            f"✅ 已为用户 {target_qq} 增加 {count} 次（永久），TA当前总剩余 {new_total_count} 次。"
        )

    @filter.command("画图增加群组次数", prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result("格式错误: #画图增加群组次数 <群号> <次数>")
            return
        target_group, count = match.group(1), int(match.group(2))

        # 管理员增加的是永久次数
        await self.persistence.add_permanent_group_count(target_group, count)

        # 回复时显示总次数
        new_total_count = self.persistence.get_group_count(target_group)
        yield event.plain_result(
            f"✅ 已为群组 {target_group} 增加 {count} 次（永久），该群当前总剩余 {new_total_count} 次。"
        )

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

    @filter.command("画图添加key", prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return
        new_keys = event.message_str.strip().split()
        if not new_keys:
            yield event.plain_result("格式错误，请提供要添加的Key。")
            return
        provider = self.pipeline.get_first_keyed_provider() if self.pipeline else None
        if not provider:
            yield event.plain_result(
                "❌ 管线中没有需要 API Key 的提供商。请在 WebUI 配置页面管理。"
            )
            return
        api_keys = provider.node.get("api_keys", [])
        added_keys = [key for key in new_keys if key not in api_keys]
        api_keys.extend(added_keys)
        provider.node["api_keys"] = api_keys
        # 回写到持久化配置
        self.conf["api_pipeline"] = self.conf.get("api_pipeline", [])
        self.conf.save_config()
        yield event.plain_result(
            f"✅ 操作完成（{provider.name}），新增 {len(added_keys)} 个Key，当前共 {len(api_keys)} 个。"
        )

    @filter.command("画图key列表", prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return
        provider = self.pipeline.get_first_keyed_provider() if self.pipeline else None
        if not provider:
            yield event.plain_result("📝 管线中没有需要 API Key 的提供商。")
            return
        api_keys = provider.node.get("api_keys", [])
        if not api_keys:
            yield event.plain_result("📝 暂未配置任何 API Key。")
            return
        key_list_str = "\n".join(
            f"{i + 1}. {key[:8]}...{key[-4:]}" for i, key in enumerate(api_keys)
        )
        yield event.plain_result(f"🔑 API Key 列表（{provider.name}）:\n{key_list_str}")

    @filter.command("画图删除key", prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            return
        param = event.message_str.strip()
        provider = self.pipeline.get_first_keyed_provider() if self.pipeline else None
        if not provider:
            yield event.plain_result("❌ 管线中没有需要 API Key 的提供商。")
            return
        api_keys = provider.node.get("api_keys", [])
        if param.lower() == "all":
            provider.node["api_keys"] = []
            self.conf["api_pipeline"] = self.conf.get("api_pipeline", [])
            self.conf.save_config()
            yield event.plain_result(f"✅ 已删除全部 {len(api_keys)} 个 Key。")
        elif param.isdigit() and 1 <= int(param) <= len(api_keys):
            removed_key = api_keys.pop(int(param) - 1)
            provider.node["api_keys"] = api_keys
            self.conf["api_pipeline"] = self.conf.get("api_pipeline", [])
            self.conf.save_config()
            yield event.plain_result(f"✅ 已删除 Key: {removed_key[:8]}...")
        else:
            yield event.plain_result("格式错误，请使用 #画图删除key <序号|all>")

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        if self.pipeline:
            await self.pipeline.close()
        logger.info("[astrbot_plugin_free_image] 插件已终止")
