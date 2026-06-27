import asyncio
import random
import re
from datetime import datetime

from astrbot import logger
from astrbot.core.message.components import At, Image, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .selfie import find_persona, find_style, resolve_persona


class CommandHandlers:
    """Chat command handlers kept outside main.py while decorators stay in main.py."""

    def __init__(self, plugin):
        self.plugin = plugin

    async def load_prompt_map(self):
        p = self.plugin
        logger.info("正在加载 prompts...")
        p.prompt_map.clear()
        seen_keys = set()
        prompt_list = p.conf.get("prompt_list", [])
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
                    p.prompt_map[key] = value.strip()
                else:
                    logger.warning(f"跳过格式错误的 prompt (缺少冒号): {item}")
            except ValueError:
                logger.warning(f"跳过格式错误的 prompt: {item}")
        logger.info(f"加载了 {len(p.prompt_map)} 个 prompts。")

    def admin_denied_message(self) -> str:
        return "你没有权限使用此命令。"

    def get_api_pipeline_config(self) -> list:
        pipeline_config = self.plugin.conf.get("api_pipeline", [])
        return pipeline_config if isinstance(pipeline_config, list) else []

    @staticmethod
    def get_model_display_name(node: dict) -> str:
        model_name = str(node.get("model", "")).strip()
        if model_name:
            return model_name
        template_key = str(node.get("__template_key", "")).strip()
        return template_key or "未命名模型"

    def format_model_pipeline_message(self, prefix: str = "", is_admin: bool = True) -> str:
        pipeline_config = self.get_api_pipeline_config()
        lines = []
        if prefix:
            lines.append(prefix)
        lines.append("当前模型回退顺序为：")
        lines.append("")

        if pipeline_config:
            for index, node in enumerate(pipeline_config, start=1):
                status = "🟢" if node.get("enabled", True) else "🔴"
                lines.append(f"{index}{status}{self.get_model_display_name(node)}")
        else:
            lines.append("当前 API 管线为空，请先在 WebUI 配置 api_pipeline。")

        lines.append("")
        if is_admin:
            lines.append("画图模型 置顶 <序号> 将该模型置顶到管线顶部")
            lines.append("画图模型 开启/关闭 <序号> 将该模型启用或关闭")
        lines.append("文生图-<序号> 指定单个模型生图（图生图、模板、自拍同理）")
        return "\n".join(lines)

    @staticmethod
    def parse_model_command(raw_args: str) -> tuple[str | None, int | None]:
        if not raw_args:
            return None, None
        parts = raw_args.split()
        if len(parts) != 2:
            return "", None
        action, index_text = parts
        if action not in {"置顶", "开启", "关闭"} or not index_text.isdigit():
            return "", None
        return action, int(index_text)

    async def save_and_rebuild_pipeline(self) -> None:
        p = self.plugin
        p.conf["api_pipeline"] = self.get_api_pipeline_config()
        await p.save_config_and_refresh_runtime()

    async def handle_model_pipeline_command(self, event: AstrMessageEvent, raw_args: str):
        p = self.plugin
        is_admin = p.is_global_admin(event)

        action, index = self.parse_model_command(raw_args)
        pipeline_config = self.get_api_pipeline_config()

        # 无参：所有人可看列表
        if action is None:
            yield event.plain_result(self.format_model_pipeline_message(is_admin=is_admin))
            event.stop_event()
            return

        # 带参（置顶/开启/关闭）：仅管理员
        if not is_admin:
            yield event.plain_result(self.admin_denied_message())
            event.stop_event()
            return

        if not action or index is None or index < 1 or index > len(pipeline_config):
            yield event.plain_result(
                self.format_model_pipeline_message("命令格式或参数错误，请重试。", is_admin=True)
            )
            event.stop_event()
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
            yield event.plain_result(
                self.format_model_pipeline_message("命令格式或参数错误，请重试。", is_admin=True)
            )
            event.stop_event()
            return

        await self.save_and_rebuild_pipeline()
        yield event.plain_result(self.format_model_pipeline_message("操作成功。", is_admin=True))
        event.stop_event()

    def _parse_model_index_command(self, text: str, candidates: list[str]) -> tuple[str | None, int | None, str]:
        """解析 <命令>-<序号> [剩余参数] 模式。
        返回 (matched_cmd, model_index, remaining_text)。
        matched_cmd 为 None 表示未命中；model_index 为 None 表示未命中序号。
        candidates 必须按长度降序传入，避免前缀冲突。
        """
        for cmd in candidates:
            if not text.startswith(cmd):
                continue
            rest = text[len(cmd):]
            # rest 必须以 "-<数字>" 开头，后面跟空格或结束
            m = re.match(r"^-(\d+)(?:\s|$)", rest)
            if not m:
                continue
            idx = int(m.group(1))
            if idx < 1:
                continue
            # 剥离 "<命令>-<序号>"，保留剩余文本
            remaining = rest[m.end():].strip()
            return cmd, idx, remaining
        return None, None, text

    async def on_image_gen_request(self, event: AstrMessageEvent):
        p = self.plugin
        if p.conf.get("prefix", True) and not event.is_at_or_wake_command:
            return
        text = p._get_plain_message_text(event, strip_wake_prefix=True)
        if not text:
            return
        if text.startswith("画图模型") and not (
            text == "画图模型" or text.startswith("画图模型 ")
        ):
            async for result in self.handle_model_pipeline_command(
                event, text.removeprefix("画图模型").strip()
            ):
                yield result
            return

        # --- <命令>-<序号> 模式解析 ---
        candidates = ["文生图", "图生图", "自拍"] + list(p.prompt_map.keys())
        candidates_sorted = sorted(set(candidates), key=len, reverse=True)
        matched_cmd, model_index, remaining = self._parse_model_index_command(
            text, candidates_sorted
        )

        if matched_cmd is not None:
            pipeline_config = self.get_api_pipeline_config()
            is_admin = p.is_global_admin(event)
            # 序号超范围：按你确认的规则，报"命令格式或参数错误"+列表
            if model_index > len(pipeline_config):
                yield event.plain_result(
                    self.format_model_pipeline_message(
                        prefix="命令格式或参数错误，请重试。", is_admin=is_admin
                    )
                )
                event.stop_event()
                return
            node = pipeline_config[model_index - 1]
            if not node.get("enabled", True):
                yield p._quoted_plain_result(
                    event, f"模型 {model_index}🔴{self.get_model_display_name(node)} 已关闭，请选择其他模型。"
                )
                event.stop_event()
                return

            if matched_cmd == "文生图":
                if not remaining:
                    yield p._quoted_plain_result(
                        event, f"请提供文生图的描述。用法: 文生图-{model_index} <描述>"
                    )
                    event.stop_event()
                    return
                async for res in p.handle_image_gen_logic(
                    event, remaining, is_i2i=False,
                    request_source="command", model_index=model_index,
                    generation_mode="text2img",
                ):
                    yield res
                event.stop_event()
                return

            if matched_cmd == "图生图":
                if not remaining:
                    yield p._quoted_plain_result(
                        event, f"请提供图生图的描述。用法: 图生图-{model_index} <描述>（并发送或引用图片）"
                    )
                    event.stop_event()
                    return
                async for res in p.handle_image_gen_logic(
                    event, remaining, is_i2i=True,
                    request_source="command", model_index=model_index,
                    generation_mode="image2img",
                ):
                    yield res
                event.stop_event()
                return

            if matched_cmd == "自拍":
                async for _ in p._run_selfie(
                    event, remaining, is_llm_tool=False, model_index=model_index,
                ):
                    yield _
                event.stop_event()
                return

            # 模板触发
            if matched_cmd in p.prompt_map:
                base_prompt = p.prompt_map.get(matched_cmd)
                if base_prompt is None:
                    return
                user_prompt = f"{base_prompt} {remaining}" if remaining else base_prompt
                display_name = f"{matched_cmd}-{model_index} {remaining}".strip()
                async for res in p.handle_image_gen_logic(
                    event, user_prompt, is_i2i=True,
                    display_name=display_name, request_source="command",
                    model_index=model_index, generation_mode="template",
                ):
                    yield res
                event.stop_event()
                return

        # --- 原有：无序号模式 ---
        parts = text.split(maxsplit=1)
        cmd = parts[0].strip()
        extra_text = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "图生图":
            if not text.removeprefix(cmd).strip():
                return
            return
        if cmd not in p.prompt_map:
            return

        base_prompt = p.prompt_map.get(cmd)
        if base_prompt is None:
            return
        user_prompt = f"{base_prompt} {extra_text}" if extra_text else base_prompt
        display_cmd = f"{cmd} {extra_text}".strip()
        async for res in p.handle_image_gen_logic(
            event,
            user_prompt,
            is_i2i=True,
            display_name=display_cmd,
            request_source="command",
            generation_mode="template",
        ):
            yield res
        event.stop_event()

    async def on_text_to_image_request(self, event: AstrMessageEvent):
        p = self.plugin
        prompt = event.message_str.strip()
        if prompt.startswith("文生图"):
            prompt = prompt.removeprefix("文生图").strip()
        if not prompt:
            yield p._quoted_plain_result(event, "请提供文生图的描述。用法: #文生图 <描述>")
            return
        async for res in p.handle_image_gen_logic(
            event, prompt, is_i2i=False, request_source="command", generation_mode="text2img"
        ):
            yield res
        event.stop_event()

    async def on_image_to_image_request(self, event: AstrMessageEvent):
        p = self.plugin
        prompt = p._strip_command_prefix(
            p._get_plain_message_text(event, strip_wake_prefix=True), "图生图"
        )
        if not prompt:
            yield p._quoted_plain_result(
                event, "请提供图生图的描述。用法: #图生图 <描述>（并发送或引用图片）"
            )
            return
        async for res in p.handle_image_gen_logic(
            event, prompt, is_i2i=True, request_source="command", generation_mode="image2img"
        ):
            yield res
        event.stop_event()

    async def add_lm_prompt(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.is_global_admin(event):
            yield event.plain_result(self.admin_denied_message())
            event.stop_event()
            return
        raw = event.message_str.strip()
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
        prompt_list = p.conf.get("prompt_list", [])
        for item in prompt_list:
            if item.strip().startswith(key + ":"):
                yield event.plain_result(f"预设指令“{key}”已存在，已取消添加。")
                event.stop_event()
                return

        prompt_list.append(f"{key}:{new_value}")
        p.conf["prompt_list"] = prompt_list
        p.conf.save_config()
        await self.load_prompt_map()
        yield event.plain_result(f"已保存生图提示语模板:\n{key}:{new_value}")
        event.stop_event()

    async def on_model_pipeline_command(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        if raw.startswith("画图模型"):
            raw = raw.removeprefix("画图模型").strip()
        async for result in self.handle_model_pipeline_command(event, raw):
            yield result

    def image_cache_help(self) -> str:
        return (
            "画图缓存命令：\n"
            "#画图缓存 状态\n"
            "#画图缓存 开启\n"
            "#画图缓存 关闭\n"
            "#画图缓存 清理"
        )

    async def on_image_cache_command(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.is_global_admin(event):
            yield event.plain_result(self.admin_denied_message())
            event.stop_event()
            return

        raw = event.message_str.strip()
        if raw.startswith("画图缓存"):
            raw = raw.removeprefix("画图缓存").strip()

        if not raw:
            yield event.plain_result(self.image_cache_help())
            event.stop_event()
            return

        if raw == "开启":
            p.conf["enable_image_cache"] = True
            await p.save_config_and_refresh_runtime()
            yield event.plain_result("✅ 画图缓存已开启。")
            event.stop_event()
            return

        if raw == "关闭":
            p.conf["enable_image_cache"] = False
            await p.save_config_and_refresh_runtime()
            yield event.plain_result("✅ 画图缓存已关闭。")
            event.stop_event()
            return

        if raw == "清理":
            result = await p.history_cache.clear_cache(reason="command")
            yield event.plain_result(
                f"✅ 已清理画图缓存：删除 {result['deleted_count']} 张，释放 {result['deleted_bytes']} bytes。"
            )
            event.stop_event()
            return

        if raw == "状态":
            stats = await p.history_cache.get_cache_for_page()
            enabled_text = "开启" if stats.get("enabled") else "关闭"
            limit_parts = []
            limit_parts.append(f"最大大小: {stats.get('max_mb') or '不限制'} MB")
            limit_parts.append(f"最长保存: {stats.get('max_hours') or '不限制'} 小时")
            limit_parts.append(f"最多张数: {stats.get('max_count') or '不限制'} 张")
            yield event.plain_result(
                "画图缓存状态：\n"
                f"状态: {enabled_text}\n"
                f"当前缓存: {stats.get('total_count', 0)} 张，{stats.get('total_bytes', 0)} bytes\n"
                + "\n".join(limit_parts)
            )
            event.stop_event()
            return

        yield event.plain_result(self.image_cache_help())
        event.stop_event()

    async def on_concise_mode_command(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.is_global_admin(event):
            yield event.plain_result(self.admin_denied_message())
            event.stop_event()
            return
        raw = event.message_str.strip()
        if raw.startswith("画图简洁模式"):
            raw = raw.removeprefix("画图简洁模式").strip()
        if raw not in {"开启", "关闭"}:
            yield event.plain_result("命令格式或参数错误，请重试。")
            event.stop_event()
            return
        p.conf["concise_mode"] = raw == "开启"
        p.conf.save_config()
        yield event.plain_result("操作成功。")
        event.stop_event()

    async def on_prompt_help(self, event: AstrMessageEvent):
        p = self.plugin
        keyword = event.message_str.strip()
        for cmd in ("画图帮助", "lmh", "lm帮助"):
            if keyword.startswith(cmd):
                keyword = keyword.removeprefix(cmd).strip()
                break
        if not keyword:
            msg = "图生图预设指令: \n"
            msg += "、".join(p.prompt_map.keys())
            msg += "\n\n#画图帮助 <预设指令> 来查看对应模板的详细内容"
            msg += "\n\n生图指令: \n#文生图 <你的描述> \n#图生图 <你的描述>（发送图片 或 引用图片 或 @用户,若为空则使用你的头像）"
            msg += "\n#预设指令 + 发送图片 或 引用图片 或 @用户 来使用模板图生图"
            yield event.plain_result(msg)
            return
        prompt = p.prompt_map.get(keyword)
        if not prompt:
            yield event.plain_result("未找到此预设指令")
            return
        yield event.plain_result(f"预设 [{keyword}] 的内容:\n{prompt}")

    async def on_checkin(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.conf.get("enable_checkin", False):
            yield event.plain_result("📅 本机器人未开启签到功能。")
            return
        user_id = event.get_sender_id()
        today_str = datetime.now().strftime("%Y-%m-%d")
        if p.persistence.user_checkin_data.get(user_id) == today_str:
            yield event.plain_result(
                f"您今天已经签到过了。\n剩余次数: {p.persistence.get_user_count(user_id)}"
            )
            return
        if str(p.conf.get("enable_random_checkin", False)).lower() == "true":
            max_reward = max(1, int(p.conf.get("checkin_random_reward_max", 5)))
            reward = random.randint(1, max_reward)
        else:
            reward = int(p.conf.get("checkin_fixed_reward", 3))
        await p.persistence.add_permanent_user_count(user_id, reward)
        await p.persistence.save_user_checkin(user_id, today_str)
        new_total_count = p.persistence.get_user_count(user_id)
        yield event.plain_result(
            f"🎉 签到成功！获得 {reward} 次（永久），当前总剩余: {new_total_count} 次。"
        )

    async def on_add_user_counts(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.is_global_admin(event):
            yield event.plain_result(self.admin_denied_message())
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
        await p.persistence.add_permanent_user_count(target_qq, count)
        new_total_count = p.persistence.get_user_count(target_qq)
        yield event.plain_result(
            f"✅ 已为用户 {target_qq} 增加 {count} 次（永久），TA当前总剩余 {new_total_count} 次。"
        )
        event.stop_event()

    async def on_add_group_counts(self, event: AstrMessageEvent):
        p = self.plugin
        if not p.is_global_admin(event):
            yield event.plain_result(self.admin_denied_message())
            event.stop_event()
            return
        match = re.search(r"(\d+)\s+(\d+)", event.message_str.strip())
        if not match:
            yield event.plain_result("格式错误: #画图增加群组次数 <群号> <次数>")
            event.stop_event()
            return
        target_group, count = match.group(1), int(match.group(2))
        await p.persistence.add_permanent_group_count(target_group, count)
        new_total_count = p.persistence.get_group_count(target_group)
        yield event.plain_result(
            f"✅ 已为群组 {target_group} 增加 {count} 次（永久），该群当前总剩余 {new_total_count} 次。"
        )
        event.stop_event()

    async def on_query_counts(self, event: AstrMessageEvent):
        p = self.plugin
        user_id_to_query = event.get_sender_id()
        if p.is_global_admin(event):
            at_seg = next((s for s in event.message_obj.message if isinstance(s, At)), None)
            if at_seg:
                user_id_to_query = str(at_seg.qq)
            else:
                match = re.search(r"(\d+)", event.message_str)
                if match:
                    user_id_to_query = match.group(1)
        user_count = p.persistence.get_user_count(user_id_to_query)
        reply_msg = f"用户 {user_id_to_query} 个人剩余次数为: {user_count}"
        if user_id_to_query == event.get_sender_id():
            reply_msg = f"您好，您当前个人剩余次数为: {user_count}"
        if group_id := event.get_group_id():
            reply_msg += f"\n本群共享剩余次数为: {p.persistence.get_group_count(group_id)}"
        yield event.plain_result(reply_msg)

    async def on_selfie_help(self, event: AstrMessageEvent):
        p = self.plugin
        mode = p.conf.get("selfie_style_mode", "自动")
        binding = p.conf.get("selfie_binding_mode", "优先 AstrBot persona")
        yield event.plain_result(
            "📸 自拍命令\n"
            f"当前风格模式：{mode}  绑定模式：{binding}\n\n"
            "#自拍 <描述>\n"
            "#自拍人设 查看 / 列表 / 添加 <ID> <名称> / 绑定 <ID或名称> / 默认 <ID或名称>\n"
            "#自拍风格 查看 / 列表 / 添加 <ID> <名称> <提示词> / 模式 <不注入/自动/指定> / 选择 <ID或名称>"
        )
        event.stop_event()

    async def on_selfie_command(self, event: AstrMessageEvent):
        p = self.plugin
        action = p._strip_command_prefix(
            p._get_plain_message_text(event, strip_wake_prefix=True), "自拍"
        ).strip()
        if p.conf.get("concise_mode", False) and bool(event.get_group_id()):
            try:
                bot = getattr(event, "bot", None)
                if not bot:
                    provider = p.context.get_using_provider(event.unified_msg_origin)
                    if provider and hasattr(provider, "bot"):
                        bot = provider.bot
                if bot and hasattr(bot, "set_msg_emoji_like"):
                    await bot.set_msg_emoji_like(
                        message_id=event.message_obj.message_id, emoji_id=66, set=True
                    )
            except Exception as e:
                logger.debug(f"[#自拍] 贴表情失败: {e}")
        _ = await p._exec_selfie(event, action, is_llm_tool=False)
        event.stop_event()
        return
        yield

    async def on_selfie_persona_cmd(self, event: AstrMessageEvent):
        p = self.plugin
        raw = p._strip_command_prefix(
            p._get_plain_message_text(event, strip_wake_prefix=True), "自拍人设"
        ).strip()
        parts = raw.split(maxsplit=1)
        sub = parts[0].strip() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if sub == "查看":
            yield event.plain_result(await self.selfie_persona_info(args, event))
        elif sub == "列表":
            yield event.plain_result(self.selfie_persona_list())
        elif sub == "添加":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(await self.selfie_persona_add(args, event))
        elif sub == "绑定":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self.selfie_persona_bind(args, event))
        elif sub == "默认":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self.selfie_persona_set_default(args))
        else:
            yield event.plain_result(
                "#自拍人设 查看 [ID或名称]\n"
                "#自拍人设 列表\n"
                "#自拍人设 添加 <ID> <名称>  （同时发送/引用图片）\n"
                "#自拍人设 绑定 <ID或名称>\n"
                "#自拍人设 默认 <ID或名称>"
            )
        event.stop_event()

    def selfie_persona_list(self) -> str:
        from .selfie import _all_personas

        p = self.plugin
        personas = _all_personas(p.conf)
        if not personas:
            return "当前还没有自拍人设。"
        default_id = p.conf.get("selfie_default_persona_id", "")
        lines = ["自拍人设列表："]
        for persona in personas:
            pid = persona.get("id", "")
            flag = "★" if pid == default_id else " "
            lines.append(
                f"{flag} [{pid}] {persona.get('name', '')}  参考图 {len(persona.get('ref_images') or [])} 张"
            )
        return "\n".join(lines)

    async def selfie_persona_info(self, query: str, event: AstrMessageEvent) -> str:
        p = self.plugin
        if query:
            persona = find_persona(p.conf, query)
            if not persona:
                return f"找不到自拍人设：{query}。"
        else:
            session_id = str(event.unified_msg_origin or "")
            persona = await resolve_persona(p.conf, p.context, event, session_id)
            if not persona:
                return "当前没有命中任何自拍人设，请先配置全局默认人设。"
        return (
            f"人设 ID：{persona.get('id')}\n"
            f"名称：{persona.get('name')}\n"
            f"描述：{persona.get('description') or '（无）'}\n"
            f"参考图：{len(persona.get('ref_images') or [])} 张\n"
            f"路径：{'; '.join(persona.get('ref_images') or []) or '（无）'}"
        )

    async def selfie_persona_add(self, args: str, event: AstrMessageEvent) -> str:
        p = self.plugin
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return "格式：#自拍人设 添加 <ID> <名称>（同时发送/引用图片）"
        pid, name = parts[0].strip(), parts[1].strip()
        if not pid or not name:
            return "ID 和名称不能为空。"
        from .selfie import _all_personas

        if any(persona.get("id") == pid for persona in _all_personas(p.conf)):
            return f"自拍人设 ID 已存在：{pid}。"
        imgs: list[bytes] = []
        if p.iwf:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            if reply_seg.url and (img := await p.iwf._load_bytes(reply_seg.url)):
                                imgs.append(img)
                            elif reply_seg.file and (img := await p.iwf._load_bytes(reply_seg.file)):
                                imgs.append(img)
            for seg in event.message_obj.message:
                if isinstance(seg, Image):
                    if seg.url and (img := await p.iwf._load_bytes(seg.url)):
                        imgs.append(img)
                    elif seg.file and (img := await p.iwf._load_bytes(seg.file)):
                        imgs.append(img)
        if not imgs:
            return "请随消息发送或引用至少一张图片作为参考图。"

        save_dir = p.persistence.data_dir / "selfie_personas" / pid
        save_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        loop = asyncio.get_running_loop()
        for i, img_bytes in enumerate(imgs):
            path = save_dir / f"ref_{i+1}.png"
            await loop.run_in_executor(None, path.write_bytes, img_bytes)
            saved_paths.append(str(path))

        personas = list(p.conf.get("selfie_personas", []) or [])
        personas.append(
            {
                "__template_key": "selfie_persona",
                "id": pid,
                "name": name,
                "description": "",
                "ref_images": saved_paths,
            }
        )
        p.conf["selfie_personas"] = personas
        p.conf.save_config()
        return f"✅ 已保存自拍人设「{name}」（{pid}），参考图 {len(saved_paths)} 张。"

    def selfie_persona_bind(self, args: str, event: AstrMessageEvent) -> str:
        p = self.plugin
        persona = find_persona(p.conf, args)
        if not persona:
            return f"找不到自拍人设：{args}。"
        sid = str(event.unified_msg_origin or "").strip()
        if not sid:
            return "无法获取当前会话 SID，绑定失败。"
        personas = list(p.conf.get("selfie_personas") or [])
        pid = persona.get("id")
        for entry in personas:
            if not isinstance(entry, dict):
                continue
            sids = list(entry.get("bound_sids") or [])
            if entry.get("id") == pid:
                if sid not in sids:
                    sids.append(sid)
                    entry["bound_sids"] = sids
            elif sid in sids:
                sids.remove(sid)
                entry["bound_sids"] = sids
        p.conf["selfie_personas"] = personas
        p.conf.save_config()
        return f"✅ 已将当前会话（{sid}）绑定至人设「{persona.get('name')}」。"

    def selfie_persona_set_default(self, args: str) -> str:
        p = self.plugin
        persona = find_persona(p.conf, args)
        if not persona:
            return f"找不到自拍人设：{args}。"
        p.conf["selfie_default_persona_id"] = persona.get("id")
        p.conf.save_config()
        return f"✅ 已将全局默认人设设为「{persona.get('name')}」。"

    async def on_selfie_style_cmd(self, event: AstrMessageEvent):
        p = self.plugin
        raw = p._strip_command_prefix(
            p._get_plain_message_text(event, strip_wake_prefix=True), "自拍风格"
        ).strip()
        parts = raw.split(maxsplit=1)
        sub = parts[0].strip() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if sub == "查看":
            yield event.plain_result(self.selfie_style_info(args))
        elif sub == "列表":
            yield event.plain_result(self.selfie_style_list())
        elif sub == "添加":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self.selfie_style_add(args))
        elif sub == "模式":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self.selfie_style_set_mode(args))
        elif sub == "选择":
            if not p.is_global_admin(event):
                yield event.plain_result(self.admin_denied_message())
                event.stop_event()
                return
            yield event.plain_result(self.selfie_style_select(args))
        else:
            yield event.plain_result(
                "#自拍风格 查看 [ID或名称]\n"
                "#自拍风格 列表\n"
                "#自拍风格 添加 <ID> <名称> <提示词>  （关键词用竖线分隔追加，可省略）\n"
                "#自拍风格 模式 <不注入/自动/指定>\n"
                "#自拍风格 选择 <ID或名称>"
            )
        event.stop_event()

    def selfie_style_list(self) -> str:
        from .selfie import _all_styles

        p = self.plugin
        styles = _all_styles(p.conf)
        if not styles:
            return "当前风格模板库为空。"
        selected = p.conf.get("selfie_selected_style_id", "")
        mode = p.conf.get("selfie_style_mode", "自动")
        lines = [f"自拍风格列表（当前模式：{mode}）："]
        for style in styles:
            flag = "→" if style.get("id") == selected else " "
            lines.append(f"{flag} [{style.get('id')}] {style.get('name')}")
        return "\n".join(lines)

    def selfie_style_info(self, query: str) -> str:
        p = self.plugin
        if query:
            style = find_style(p.conf, query)
            if not style:
                return f"找不到自拍风格：{query}。"
        else:
            mode = p.conf.get("selfie_style_mode", "自动")
            selected = p.conf.get("selfie_selected_style_id", "")
            style = find_style(p.conf, selected) if selected else None
            if not style:
                return f"当前模式：{mode}，未指定默认风格。"
        return (
            f"风格 ID：{style.get('id')}\n"
            f"名称：{style.get('name')}\n"
            f"关键词：{', '.join(style.get('keywords') or []) or '（无）'}\n"
            f"提示词：{style.get('prompt', '')}"
        )

    def selfie_style_add(self, args: str) -> str:
        p = self.plugin
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

        if any(style.get("id") == sid for style in _all_styles(p.conf)):
            return f"自拍风格 ID 已存在：{sid}。"
        styles = list(p.conf.get("selfie_styles", []) or [])
        styles.append(
            {
                "__template_key": "selfie_style",
                "id": sid,
                "name": name,
                "prompt": prompt_str,
                "keywords": keywords,
                "enabled": True,
            }
        )
        p.conf["selfie_styles"] = styles
        p.conf.save_config()
        return f"✅ 已添加自拍风格「{name}」（{sid}）。"

    def selfie_style_set_mode(self, args: str) -> str:
        p = self.plugin
        mode_map = {"不注入": "不注入", "自动": "自动", "指定": "指定"}
        mode = mode_map.get(args.strip())
        if not mode:
            return "支持的模式：不注入、自动、指定。"
        p.conf["selfie_style_mode"] = mode
        p.conf.save_config()
        return f"✅ 自拍风格注入模式已设为「{mode}」。"

    def selfie_style_select(self, args: str) -> str:
        p = self.plugin
        style = find_style(p.conf, args)
        if not style:
            return f"找不到自拍风格：{args}。"
        p.conf["selfie_selected_style_id"] = style.get("id")
        p.conf.save_config()
        return f"✅ 已将指定风格设为「{style.get('name')}」（{style.get('id')}）。"
