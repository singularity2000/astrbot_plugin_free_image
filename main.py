import asyncio
import base64
from io import BytesIO
import json
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import quote

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image, Plain, Reply, Video
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.api.web import request as web_request
from quart import jsonify, request, send_file

from .commands import CommandHandlers
from .history_cache import ImageHistoryCache
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


PLUGIN_NAME = "astrbot_plugin_free_image"

# Pages「设置」页承载的配置分组。管线、模板、缓存、自拍各有专属页面，不放这里。
SETTINGS_GROUPS = ("general", "access_control", "quota", "checkin", "llm_tools")


@register(
    PLUGIN_NAME,
    "Singularity2000",
    "文生图、图生图，可自定义提示词模板，兼容多种端点",
    "3.5.4",
    "https://github.com/singularity2000/astrbot_plugin_free_image",
)
class ImageGenerationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = StarTools.get_data_dir()
        self.persistence = PersistenceManager(config, self.data_dir)
        self.history_cache = ImageHistoryCache(config, self.data_dir)
        self.pipeline: Optional[ImageGenPipeline] = None
        self.iwf: Optional[ImageWorkflow] = None
        self.sender: Optional[ImageResultSender] = None
        self.usage_guard: Optional[UsageGuard] = None
        self.commands = CommandHandlers(self)
        self.prompt_map: Dict[str, str] = {}
        self._register_page_apis()

    async def initialize(self):
        self.iwf = ImageWorkflow(self.conf)

        self.pipeline = ImageGenPipeline(self.conf, self.iwf)
        self.sender = ImageResultSender(self.conf, self.persistence)
        self._rebuild_runtime_from_config()

        await self.persistence.load_all()
        await self.history_cache.load_all()
        await self.history_cache.enforce_limits(reason="startup")
        await self.commands.load_prompt_map()
        self._refresh_llm_tool_descriptions()

        logger.info("astrbot_plugin_free_image 插件已加载")

    def _refresh_llm_tool_descriptions(self) -> None:
        """同步配置到自动注册的 LLM 工具描述。"""
        llm_tools = self.conf.get("llm_tools", {})
        tool = self.context.get_llm_tool_manager().get_func("image_generation")
        if tool:
            tool.description = llm_tools.get(
                "llm_tool_description",
                "专业的文生图、图生图工具。理解用户语义，仅当用户需要你生图，或修改图片内容时才调用此工具。",
            )

            custom_prompt_desc = llm_tools.get(
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

        selfie_tool = self.context.get_llm_tool_manager().get_func("send_selfie")
        if selfie_tool:
            selfie_tool.description = llm_tools.get(
                "selfie_tool_description",
                "以你的形象生成图片。理解用户语义，在用户要求生成“有你出镜”的图片时（如自拍、合影、展示形象等）须调用此工具，区别于常规生图。此工具自带你的形象参考图。",
            )
            guidance = llm_tools.get(
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

    def _rebuild_runtime_from_config(self) -> None:
        if self.pipeline:
            self.pipeline.build(self.conf.get("api_pipeline", []))
            self.usage_guard = UsageGuard(self.conf, self.persistence, self.pipeline)
        self._refresh_llm_tool_descriptions()

    async def save_config_and_refresh_runtime(self) -> None:
        self.conf.save_config()
        self._rebuild_runtime_from_config()
        await self.commands.load_prompt_map()

    def _register_page_apis(self) -> None:
        routes = [
            ("get_config_bundle", self.page_get_config_bundle, ["GET"], "获取 FreeImage Pages 配置包"),
            ("save_page_prefs", self.page_save_page_prefs, ["POST"], "保存 FreeImage Pages 偏好设置"),
            ("save_pipeline", self.page_save_pipeline, ["POST"], "保存 FreeImage API 管线"),
            ("save_templates", self.page_save_templates, ["POST"], "保存 FreeImage 提示词模板"),
            ("save_cache_config", self.page_save_cache_config, ["POST"], "保存 FreeImage 缓存配置"),
            ("save_settings", self.page_save_settings, ["POST"], "保存 FreeImage 通用设置"),
            ("save_config_bundle", self.page_save_config_bundle, ["POST"], "统一保存 FreeImage Pages 配置"),
            ("set_cache_enabled", self.page_set_cache_enabled, ["POST"], "切换 FreeImage 缓存开关"),
            ("get_history", self.page_get_history, ["GET"], "获取 FreeImage 生图历史"),
            ("get_cache", self.page_get_cache, ["GET"], "获取 FreeImage 缓存列表"),
            ("clear_cache", self.page_clear_cache, ["POST"], "清理 FreeImage 缓存"),
            ("delete_cache_image", self.page_delete_cache_image, ["POST"], "删除 FreeImage 单张缓存图片"),
            ("save_personas", self.page_save_personas, ["POST"], "保存 FreeImage 自拍人设"),
            ("save_styles", self.page_save_styles, ["POST"], "保存 FreeImage 自拍风格"),
            ("upload_persona_image", self.page_upload_persona_image, ["POST"], "上传 FreeImage 自拍参考图"),
            ("delete_persona_image", self.page_delete_persona_image, ["POST"], "删除 FreeImage 自拍参考图"),
            ("get_image", self.page_get_image, ["GET"], "获取 FreeImage Pages 图片预览"),
            ("get_image_data", self.page_get_image_data, ["GET"], "获取 FreeImage Pages 图片 data URL"),
        ]
        for endpoint, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                desc,
            )

    def _load_conf_schema(self) -> dict[str, Any]:
        schema_path = Path(__file__).with_name("_conf_schema.json")
        try:
            data = json.loads(schema_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.error(f"[FreeImage Pages] 读取配置 schema 失败: {exc}")
            return {}

    def _schema_entry(self, key: str) -> dict[str, Any]:
        entry = self._load_conf_schema().get(key, {})
        return entry if isinstance(entry, dict) else {}

    def _prompt_templates_for_page(self) -> list[dict[str, str]]:
        templates: list[dict[str, str]] = []
        for item in self.conf.get("prompt_list", []) or []:
            text = str(item or "")
            if ":" not in text:
                continue
            trigger, prompt = text.split(":", 1)
            templates.append({"trigger": trigger.strip(), "prompt": prompt.strip()})
        return templates

    def _normalize_prompt_templates(self, raw_templates: Any) -> list[str]:
        if not isinstance(raw_templates, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw_templates:
            if not isinstance(item, dict):
                continue
            trigger = str(item.get("trigger") or item.get("key") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not trigger or not prompt or trigger in seen:
                continue
            seen.add(trigger)
            result.append(f"{trigger}:{prompt}")
        return result

    def _normalize_pipeline(self, raw_pipeline: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_pipeline, list):
            return []
        templates = self._schema_entry("api_pipeline").get("templates", {})
        allowed_keys = set(templates.keys()) if isinstance(templates, dict) else set()
        pipeline: list[dict[str, Any]] = []
        for node in raw_pipeline:
            if not isinstance(node, dict):
                continue
            template_key = str(node.get("__template_key", "")).strip()
            if allowed_keys and template_key not in allowed_keys:
                logger.warning(f"[FreeImage Pages] 跳过未知管线模板: {template_key}")
                continue
            clean_node = dict(node)
            clean_node["__template_key"] = template_key
            pipeline.append(clean_node)
        return pipeline

    def _normalize_str_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _normalize_personas(self, raw_personas: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_personas, list):
            return []
        personas: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_personas:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            persona = dict(item)
            persona["__template_key"] = "selfie_persona"
            persona["id"] = pid
            persona["name"] = str(item.get("name") or pid).strip()
            persona["description"] = str(item.get("description") or "")
            persona["ref_images"] = self._normalize_str_list(item.get("ref_images"))
            persona["bound_sids"] = self._normalize_str_list(item.get("bound_sids"))
            persona["bound_astrbot_personas"] = self._normalize_str_list(
                item.get("bound_astrbot_personas")
            )
            personas.append(persona)
        return personas

    def _normalize_styles(self, raw_styles: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_styles, list):
            return []
        styles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_styles:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            style = dict(item)
            style["__template_key"] = "selfie_style"
            style["id"] = sid
            style["name"] = str(item.get("name") or sid).strip()
            style["prompt"] = str(item.get("prompt") or "")
            style["keywords"] = self._normalize_str_list(item.get("keywords"))
            style["enabled"] = bool(item.get("enabled", True))
            styles.append(style)
        return styles

    def _settings_schema(self) -> dict[str, Any]:
        """设置页需要的 schema 分组，键名与 _conf_schema.json 顶层保持一致。"""
        schema = self._load_conf_schema()
        result: dict[str, Any] = {}
        for group in SETTINGS_GROUPS:
            entry = schema.get(group, {})
            if isinstance(entry, dict) and isinstance(entry.get("items"), dict):
                result[group] = entry
        return result

    def _settings_for_page(self) -> dict[str, dict[str, Any]]:
        """按 schema 逐字段取当前值，缺失时用 schema 默认值补齐。"""
        result: dict[str, dict[str, Any]] = {}
        for group, entry in self._settings_schema().items():
            current = self.conf.get(group, {})
            current = current if isinstance(current, dict) else {}
            values: dict[str, Any] = {}
            for key, field in entry.get("items", {}).items():
                if not isinstance(field, dict):
                    continue
                values[key] = current.get(key, field.get("default"))
            result[group] = values
        return result

    def _coerce_setting_value(self, field: dict[str, Any], value: Any, fallback: Any) -> Any:
        """按 schema 声明的类型强制转换，转换失败时回退到当前值。"""
        field_type = str(field.get("type") or "string")
        if field_type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if field_type in ("int", "float"):
            try:
                number = float(str(value).strip() or 0)
            except (TypeError, ValueError):
                return fallback
            return int(number) if field_type == "int" else number
        if field_type == "list":
            return self._normalize_str_list(value)
        if field_type == "object":
            if isinstance(value, dict):
                return dict(value)
            return dict(fallback) if isinstance(fallback, dict) else {}
        text = "" if value is None else str(value)
        options = field.get("options")
        if isinstance(options, list) and options and text not in [str(item) for item in options]:
            logger.warning(f"[FreeImage Pages] 忽略非法配置值: {field.get('description', '')}={text!r}")
            return fallback
        return text

    def _normalize_settings(self, raw_settings: Any) -> dict[str, dict[str, Any]]:
        """只接受 schema 中声明过的分组和字段，其余一律丢弃。"""
        if not isinstance(raw_settings, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for group, entry in self._settings_schema().items():
            payload = raw_settings.get(group)
            if not isinstance(payload, dict):
                continue
            current = self.conf.get(group, {})
            current = current if isinstance(current, dict) else {}
            values: dict[str, Any] = {}
            for key, field in entry.get("items", {}).items():
                if not isinstance(field, dict) or key not in payload:
                    continue
                fallback = current.get(key, field.get("default"))
                values[key] = self._coerce_setting_value(field, payload.get(key), fallback)
            if values:
                result[group] = values
        return result

    def _apply_settings(self, raw_settings: Any) -> list[str]:
        """写入设置分组，返回实际发生写入的分组名，供前端提示。"""
        applied: list[str] = []
        for group, values in self._normalize_settings(raw_settings).items():
            group_conf = self.conf.setdefault(group, {})
            if not isinstance(group_conf, dict):
                continue
            group_conf.update(values)
            applied.append(group)
        return applied

    def _persona_preview_url(self, path_str: str) -> str:
        return f"/api/plug/{PLUGIN_NAME}/get_image?persona_path={quote(path_str, safe='')}"

    def _personas_for_page(self) -> list[dict[str, Any]]:
        personas = []
        for persona in self.conf.get("selfie", {}).get("selfie_personas", []) or []:
            if not isinstance(persona, dict):
                continue
            page_persona = dict(persona)
            items = []
            for path_str in self._normalize_str_list(persona.get("ref_images")):
                path = Path(path_str)
                items.append(
                    {
                        "path": path_str,
                        "url": self._persona_preview_url(path_str),
                        "exists": path.is_file(),
                    }
                )
            page_persona["ref_image_items"] = items
            personas.append(page_persona)
        return personas

    def _allowed_persona_image_paths(self) -> set[Path]:
        allowed: set[Path] = set()
        for persona in self.conf.get("selfie", {}).get("selfie_personas", []) or []:
            if not isinstance(persona, dict):
                continue
            for path_str in self._normalize_str_list(persona.get("ref_images")):
                try:
                    allowed.add(Path(path_str).resolve())
                except OSError:
                    continue
        uploads_dir = self.data_dir / "selfie_personas" / "pages_uploads"
        if uploads_dir.exists():
            for path in uploads_dir.rglob("*"):
                if path.is_file():
                    try:
                        allowed.add(path.resolve())
                    except OSError:
                        continue
        return allowed

    def _resolve_page_image_path(self, *, cache_id: str = "", persona_path: str = "") -> Path | None:
        cache_id = str(cache_id or "").strip()
        if cache_id:
            return self.history_cache.get_cache_image_path(cache_id)

        persona_path = str(persona_path or "").strip()
        if persona_path:
            try:
                path = Path(persona_path).resolve()
            except OSError:
                return None
            if path in self._allowed_persona_image_paths() and path.is_file():
                return path
        return None

    @staticmethod
    def _page_query_int(key: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
        try:
            value = int(request.args.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _thumbnail_image_bytes(path: Path, max_side: int = 360) -> tuple[bytes, str]:
        from PIL import Image as PILImage

        with PILImage.open(path) as image:
            image.thumbnail((max_side, max_side))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            buffer = BytesIO()
            image.save(buffer, format="WEBP", quality=72, method=4)
            return buffer.getvalue(), "image/webp"

    async def _image_data_url_payload(self, path: Path, *, thumbnail: bool = False) -> dict[str, Any]:
        if thumbnail:
            try:
                image_bytes, mime_type = await asyncio.to_thread(self._thumbnail_image_bytes, path)
            except Exception as exc:
                logger.warning(f"[FreeImage Pages] 生成缩略图失败，回退原图: {path} - {exc}")
                image_bytes = await asyncio.to_thread(path.read_bytes)
                mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        else:
            image_bytes = await asyncio.to_thread(path.read_bytes)
            mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return {
            "success": True,
            "data_url": f"data:{mime_type};base64,{encoded}",
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "thumbnail": thumbnail,
        }

    def _is_managed_selfie_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            root = (self.data_dir / "selfie_personas").resolve()
            return root == resolved or root in resolved.parents
        except OSError:
            return False

    @staticmethod
    def _page_pref_int(page_prefs: dict[str, Any], key: str, default: int) -> int:
        try:
            return int(page_prefs.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _config_bundle_for_page(self) -> dict[str, Any]:
        page_prefs = self.history_cache.page_prefs.get("__default__", {})
        if not isinstance(page_prefs, dict):
            page_prefs = {}
        cache_conf = self.conf.get("cache", {})
        selfie_conf = self.conf.get("selfie", {})
        return {
            "pipeline": self.conf.get("api_pipeline", []) or [],
            "prompt_templates": self._prompt_templates_for_page(),
            "cache": {
                "enabled": bool(cache_conf.get("enable_image_cache", False)),
                "max_mb": str(cache_conf.get("image_cache_max_size_mb", "") or ""),
                "max_hours": str(cache_conf.get("image_cache_max_age_hours", "") or ""),
                "max_count": str(cache_conf.get("image_cache_max_count", "") or ""),
            },
            "selfie": {
                "binding_mode": selfie_conf.get("selfie_binding_mode", "优先 AstrBot persona"),
                "manual_override": selfie_conf.get("selfie_persona_manual_override", ""),
                "default_persona_id": selfie_conf.get("selfie_default_persona_id", ""),
                "style_mode": selfie_conf.get("selfie_style_mode", "自动"),
                "selected_style_id": selfie_conf.get("selfie_selected_style_id", ""),
                "personas": self._personas_for_page(),
                "styles": selfie_conf.get("selfie_styles", []) or [],
            },
            "settings": self._settings_for_page(),
            "page_prefs": {
                "theme": str(page_prefs.get("theme") or "system"),
                "cache_page_size": self._page_pref_int(page_prefs, "cache_page_size", 24),
                "history_page_size": self._page_pref_int(page_prefs, "history_page_size", 20),
                "last_tab": str(page_prefs.get("last_tab") or "pipeline"),
            },
        }

    async def page_get_config_bundle(self):
        username = web_request.username
        schema = self._load_conf_schema()
        selfie_schema = schema.get("selfie", {})
        selfie_items = selfie_schema.get("items", {}) if isinstance(selfie_schema, dict) else {}
        config_bundle = self._config_bundle_for_page()
        config_bundle["page_prefs"] = await self.history_cache.get_page_prefs(username)
        return jsonify(
            {
                "success": True,
                "config": config_bundle,
                "schema": {
                    "api_pipeline": schema.get("api_pipeline", {}),
                    "selfie": selfie_schema,
                    "selfie_personas": selfie_items.get("selfie_personas", {}),
                    "selfie_styles": selfie_items.get("selfie_styles", {}),
                    "settings": self._settings_schema(),
                },
            }
        )

    async def page_save_page_prefs(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        prefs = await self.history_cache.save_page_prefs(payload, web_request.username)
        return jsonify({"success": True, "prefs": prefs})

    async def page_save_config_bundle(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400

        saved_sections: list[str] = []
        cache_changed = False

        if "pipeline" in payload:
            self.conf["api_pipeline"] = self._normalize_pipeline(payload.get("pipeline"))
            saved_sections.append("pipeline")

        raw_templates = payload.get("prompt_templates", payload.get("templates", None))
        if raw_templates is not None:
            self.conf["prompt_list"] = self._normalize_prompt_templates(raw_templates)
            saved_sections.append("templates")

        if isinstance(payload.get("cache"), dict):
            cache_payload = payload.get("cache") or {}
            cache_conf = self.conf.setdefault("cache", {})
            cache_conf["enable_image_cache"] = bool(
                cache_payload.get("enabled", cache_conf.get("enable_image_cache", False))
            )
            cache_conf["image_cache_max_size_mb"] = str(cache_payload.get("max_mb") or "").strip()
            cache_conf["image_cache_max_age_hours"] = str(cache_payload.get("max_hours") or "").strip()
            cache_conf["image_cache_max_count"] = str(cache_payload.get("max_count") or "").strip()
            saved_sections.append("cache")
            cache_changed = True

        if isinstance(payload.get("settings"), dict):
            if self._apply_settings(payload.get("settings")):
                saved_sections.append("settings")

        if isinstance(payload.get("selfie"), dict):
            selfie_payload = payload.get("selfie") or {}
            selfie_conf = self.conf.setdefault("selfie", {})
            if "personas" in selfie_payload:
                selfie_conf["selfie_personas"] = self._normalize_personas(selfie_payload.get("personas"))
                saved_sections.append("personas")
            if "binding_mode" in selfie_payload:
                selfie_conf["selfie_binding_mode"] = str(
                    selfie_payload.get("binding_mode") or "优先 AstrBot persona"
                )
            if "manual_override" in selfie_payload:
                selfie_conf["selfie_persona_manual_override"] = str(
                    selfie_payload.get("manual_override") or ""
                )
            if "default_persona_id" in selfie_payload:
                selfie_conf["selfie_default_persona_id"] = str(
                    selfie_payload.get("default_persona_id") or ""
                )

            if "styles" in selfie_payload:
                selfie_conf["selfie_styles"] = self._normalize_styles(selfie_payload.get("styles"))
                saved_sections.append("styles")
            if "style_mode" in selfie_payload or "mode" in selfie_payload:
                selfie_conf["selfie_style_mode"] = str(
                    selfie_payload.get("style_mode", selfie_payload.get("mode", "自动")) or "自动"
                )
            if "selected_style_id" in selfie_payload:
                selfie_conf["selfie_selected_style_id"] = str(
                    selfie_payload.get("selected_style_id") or ""
                )

        # 去重并保持原始顺序，便于前端提示。
        saved_sections = list(dict.fromkeys(saved_sections))
        if not saved_sections:
            return jsonify({"success": True, "message": "没有需要保存的配置。", "saved_sections": []})

        await self.save_config_and_refresh_runtime()
        cleanup = await self.history_cache.enforce_limits(reason="config") if cache_changed else None
        return jsonify(
            {
                "success": True,
                "message": "配置已保存。",
                "saved_sections": saved_sections,
                "cleanup": cleanup,
            }
        )

    async def page_save_pipeline(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        self.conf["api_pipeline"] = self._normalize_pipeline(payload.get("pipeline"))
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "message": "管线已保存。"})

    async def page_save_templates(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        self.conf["prompt_list"] = self._normalize_prompt_templates(payload.get("templates"))
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "message": "模板已保存。"})

    async def page_save_cache_config(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        cache_conf = self.conf.setdefault("cache", {})
        cache_conf["enable_image_cache"] = bool(payload.get("enabled", cache_conf.get("enable_image_cache", False)))
        cache_conf["image_cache_max_size_mb"] = str(payload.get("max_mb") or "").strip()
        cache_conf["image_cache_max_age_hours"] = str(payload.get("max_hours") or "").strip()
        cache_conf["image_cache_max_count"] = str(payload.get("max_count") or "").strip()
        await self.save_config_and_refresh_runtime()
        cleanup = await self.history_cache.enforce_limits(reason="config")
        return jsonify({"success": True, "message": "缓存配置已保存。", "cleanup": cleanup})

    async def page_save_settings(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        applied = self._apply_settings(payload.get("settings", payload))
        if not applied:
            return jsonify({"success": True, "message": "没有需要保存的设置。", "saved_groups": []})
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "message": "设置已保存。", "saved_groups": applied})

    async def page_set_cache_enabled(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        cache_conf = self.conf.setdefault("cache", {})
        cache_conf["enable_image_cache"] = bool(payload.get("enabled", False))
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "enabled": bool(cache_conf.get("enable_image_cache", False))})

    async def page_get_history(self):
        filters = {
            "start": request.args.get("start", ""),
            "end": request.args.get("end", ""),
            "user": request.args.get("user", ""),
            "mode": request.args.get("mode", ""),
            "model": request.args.get("model", ""),
        }
        result = await self.history_cache.get_history_for_page(
            page=self._page_query_int("page", 1),
            page_size=self._page_query_int("page_size", 20),
            filters=filters,
        )
        return jsonify({"success": True, **result})

    async def page_get_cache(self):
        cache = await self.history_cache.get_cache_for_page(
            page=self._page_query_int("page", 1),
            page_size=self._page_query_int("page_size", 24),
        )
        return jsonify({"success": True, **cache})

    async def page_clear_cache(self):
        result = await self.history_cache.clear_cache(reason="webui")
        cache = await self.history_cache.get_cache_for_page()
        return jsonify({"success": True, "cleanup": result, **cache})

    async def page_delete_cache_image(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        cache_id = str(payload.get("cache_id") or "").strip()
        if not cache_id:
            return jsonify({"success": False, "message": "缺少缓存图片 ID。"}), 400
        result = await self.history_cache.delete_cache_image(cache_id, reason="webui")
        cache = await self.history_cache.get_cache_for_page()
        return jsonify({"success": True, "cleanup": result, **cache})

    async def page_save_personas(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        selfie_conf = self.conf.setdefault("selfie", {})
        selfie_conf["selfie_personas"] = self._normalize_personas(payload.get("personas"))
        if "binding_mode" in payload:
            selfie_conf["selfie_binding_mode"] = str(payload.get("binding_mode") or "优先 AstrBot persona")
        if "manual_override" in payload:
            selfie_conf["selfie_persona_manual_override"] = str(payload.get("manual_override") or "")
        if "default_persona_id" in payload:
            selfie_conf["selfie_default_persona_id"] = str(payload.get("default_persona_id") or "")
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "message": "自拍人设已保存。"})

    async def page_save_styles(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        selfie_conf = self.conf.setdefault("selfie", {})
        selfie_conf["selfie_styles"] = self._normalize_styles(payload.get("styles"))
        if "mode" in payload:
            selfie_conf["selfie_style_mode"] = str(payload.get("mode") or "自动")
        if "selected_style_id" in payload:
            selfie_conf["selfie_selected_style_id"] = str(payload.get("selected_style_id") or "")
        await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "message": "自拍风格已保存。"})

    async def page_upload_persona_image(self):
        files = await request.files
        uploaded = next(iter(files.values()), None) if files else None
        if not uploaded:
            return jsonify({"success": False, "message": "没有收到上传文件。"}), 400
        read_result = uploaded.read()
        if asyncio.iscoroutine(read_result):
            image_bytes = await read_result
        else:
            image_bytes = read_result
        if not image_bytes:
            return jsonify({"success": False, "message": "上传文件为空。"}), 400
        mime_type, extension = ImageHistoryCache._detect_image_type(image_bytes)
        if extension == "png" and not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return jsonify({"success": False, "message": "仅支持 PNG、JPEG、WEBP、GIF 图片。"}), 400
        save_dir = self.data_dir / "selfie_personas" / "pages_uploads"
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"{uuid.uuid4().hex}.{extension}"
        path.write_bytes(image_bytes)
        path_str = str(path)
        return jsonify(
            {
                "success": True,
                "path": path_str,
                "url": self._persona_preview_url(path_str),
                "mime_type": mime_type,
            }
        )

    async def page_delete_persona_image(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "message": "请求体必须是 JSON 对象。"}), 400
        persona_id = str(payload.get("persona_id") or "").strip()
        path_str = str(payload.get("path") or "").strip()
        if not persona_id or not path_str:
            return jsonify({"success": False, "message": "缺少人设 ID 或图片路径。"}), 400

        selfie_conf = self.conf.setdefault("selfie", {})
        personas = list(selfie_conf.get("selfie_personas", []) or [])
        removed = False
        for persona in personas:
            if not isinstance(persona, dict) or str(persona.get("id")) != persona_id:
                continue
            refs = self._normalize_str_list(persona.get("ref_images"))
            if path_str in refs:
                refs.remove(path_str)
                persona["ref_images"] = refs
                removed = True
            break

        if removed:
            path = Path(path_str)
            if self._is_managed_selfie_path(path) and path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(f"[FreeImage Pages] 删除自拍参考图文件失败: {path} - {exc}")
            selfie_conf["selfie_personas"] = personas
            await self.save_config_and_refresh_runtime()
        return jsonify({"success": True, "removed": removed})

    async def page_get_image(self):
        cache_id = str(request.args.get("cache_id", "")).strip()
        persona_path = str(request.args.get("persona_path", "")).strip()
        if not cache_id and not persona_path:
            return jsonify({"success": False, "message": "缺少图片参数。"}), 400
        path = self._resolve_page_image_path(cache_id=cache_id, persona_path=persona_path)
        if not path:
            return jsonify({"success": False, "message": "图片不存在或未被配置。"}), 404
        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        return await send_file(path, mimetype=mime_type)

    async def page_get_image_data(self):
        cache_id = str(request.args.get("cache_id", "")).strip()
        persona_path = str(request.args.get("persona_path", "")).strip()
        thumbnail = str(request.args.get("thumbnail", "")).strip().lower() in {"1", "true", "yes"}
        if not cache_id and not persona_path:
            return jsonify({"success": False, "message": "缺少图片参数。"}), 400
        path = self._resolve_page_image_path(cache_id=cache_id, persona_path=persona_path)
        if not path:
            return jsonify({"success": False, "message": "图片不存在或未被配置。"}), 404
        return jsonify(await self._image_data_url_payload(path, thumbnail=thumbnail))

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
        # 智能决策：有显式图片/引用图则图生图；@ 头像和发送者头像兜底不触发 LLM 图生图。
        # AstrBot v4.26+ 的 LLM 场景中，引用图可能不在 Reply.chain，而在 provider_request/quoted extractor 中。
        is_i2i = bool(self.iwf and await self.iwf.has_context_images(event))

        # clamp count 到 1~3
        try:
            count = max(1, min(3, int(count)))
        except (TypeError, ValueError):
            count = 1

        # 异步启动后台任务，避免阻塞 LLM 导致超时
        async def _run_background_gen():
            try:
                async for result in self.handle_image_gen_logic(
                    event,
                    prompt,
                    is_i2i=is_i2i,
                    request_source="llm_tool",
                    count=count,
                    generation_mode="image2img" if is_i2i else "text2img",
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

    async def _try_set_msg_emoji_like(
        self, event: AstrMessageEvent, emoji_id: int, log_prefix: str
    ) -> None:
        try:
            bot = getattr(event, "bot", None)
            if not bot:
                provider = self.context.get_using_provider(event.unified_msg_origin)
                if provider and hasattr(provider, "bot"):
                    bot = provider.bot
            if bot and hasattr(bot, "set_msg_emoji_like"):
                await bot.set_msg_emoji_like(
                    message_id=event.message_obj.message_id, emoji_id=emoji_id, set=True
                )
        except Exception as e:
            logger.debug(f"{log_prefix}贴表情失败: {e}")

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
        generation_mode: str | None = None,
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

            MAX_IMAGES = 10
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

        general_conf = self.conf.get("general", {})
        quota_conf = self.conf.get("quota", {})
        concise_mode = general_conf.get("concise_mode", False) and bool(group_id)
        start_msg = f"🎨 收到{'图生图' if is_i2i else '文生图'}请求，正在生成 [{display_name}]..."
        generation_mode = generation_mode or ("image2img" if is_i2i else "text2img")

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
            user_remain = self.persistence.get_user_count(sender_id) if quota_conf.get("enable_user_limit", True) else 0
            group_remain = 0
            if quota_conf.get("enable_group_limit", False) and group_id:
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
                generation_mode,
            ):
                yield res
        else:
            async for res in self._batch_generate_sequential(
                event, prompt, images_to_process, count, available,
                is_master, is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index, generation_mode,
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
        generation_mode: str,
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
                event, prompt, res, model_name, elapsed, suffix, i, count,
                is_master, is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index, generation_mode,
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
        generation_mode: str,
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
                event, prompt, res, model_name, elapsed, suffix, idx, count,
                True,  # is_master=True（并发仅管理员）
                is_i2i, display_name, concise_mode,
                request_source, sender_id, group_id, model_index, generation_mode,
            ):
                yield msg

    def _build_quota_msg(self, group_id: str) -> str:
        """根据配额配置生成对应的配额不足提示。"""
        quota_conf = self.conf.get("quota", {})
        if quota_conf.get("enable_user_limit", True) and quota_conf.get("enable_group_limit", False) and group_id:
            return "❌ 本群和您的个人次数均已用完，请等待次日重置或向管理员索要。"
        if quota_conf.get("enable_user_limit", True):
            return "❌ 您的个人使用次数已用完，请等待次日重置或向管理员索要。"
        if quota_conf.get("enable_group_limit", False) and group_id:
            return "❌ 本群的使用次数已用完，请等待次日重置或向管理员索要。"
        return "❌ 次数已用完。"

    async def _record_generation_success(
        self,
        *,
        event: AstrMessageEvent,
        prompt: str,
        images: list[bytes],
        elapsed: float,
        model_name: str | None,
        display_name: str,
        mode: str,
        request_source: Literal["command", "llm_tool"],
        model_index: Optional[int],
        media_type: str = "image",
        media_url: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.history_cache.record_generation(
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                group_id=event.get_group_id() or "",
                mode=mode,
                request_source=request_source,
                prompt=prompt,
                elapsed=elapsed,
                model=model_name,
                display_name=display_name,
                model_index=model_index,
                images=images,
                media_type=media_type,
                media_url=media_url,
                extra=extra,
            )
        except Exception as exc:
            logger.error(f"[FreeImage History] 记录生图历史失败: {exc}", exc_info=True)

    async def _process_single_result(
        self,
        event: AstrMessageEvent,
        prompt: str,
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
        generation_mode: str,
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
            await self._record_generation_success(
                event=event,
                prompt=prompt,
                images=image_results,
                elapsed=elapsed,
                model_name=model_name,
                display_name=display_name,
                mode=generation_mode,
                request_source=request_source,
                model_index=model_index,
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
                yield self._quoted_plain_result(event, f"{deduction_error}{suffix}")
                return
            caption_parts = [f"✅ 生成成功 ({elapsed:.2f}s)", "结果类型: 视频"]
            if is_i2i:
                caption_parts.append(f"预设: {display_name}")

            if is_master:
                caption_parts.append("管理员剩余次数: ∞")
            else:
                quota_conf = self.conf.get("quota", {})
                if quota_conf.get("enable_user_limit", True):
                    caption_parts.append(
                        f"个人剩余次数: {self.persistence.get_user_count(sender_id)}"
                    )
                if quota_conf.get("enable_group_limit", False) and group_id:
                    caption_parts.append(
                        f"本群剩余次数: {self.persistence.get_group_count(group_id)}"
                    )

            if model_name:
                caption_parts.append(f"模型: {model_name}")

            caption_text = " | ".join(caption_parts) + suffix
            video_component = Video.fromURL(url=res["url"])
            await self._record_generation_success(
                event=event,
                prompt=prompt,
                images=[],
                elapsed=elapsed,
                model_name=model_name,
                display_name=display_name,
                mode=generation_mode,
                request_source=request_source,
                model_index=model_index,
                media_type="video",
                media_url=str(res.get("url", "")),
            )
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

    @filter.command("画图缓存", prefix_optional=True)
    async def on_image_cache_command(self, event: AstrMessageEvent):
        async for res in self.commands.on_image_cache_command(event):
            yield res

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
        group_id = event.get_group_id()
        general_conf = self.conf.get("general", {})
        quota_conf = self.conf.get("quota", {})
        concise = True if is_llm_tool else (general_conf.get("concise_mode", False) and bool(group_id))

        if not is_llm_tool and concise:
            await self._try_set_msg_emoji_like(event, 66, "[#自拍] ")

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

        prompt = build_selfie_prompt(persona, action, style)
        persona_name = persona.get("name", persona.get("id", ""))
        style_name = style.get("name", "") if style else "无风格"
        logger.info(f"[Selfie] 人设={persona_name}, 风格={style_name}, 参考图={len(images_to_send)}张（人设{len(persona_images)}张，额外{len(extra_images)}张）")

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
            user_remain = self.persistence.get_user_count(sender_id) if quota_conf.get("enable_user_limit", True) else 0
            group_remain = 0
            if quota_conf.get("enable_group_limit", False) and group_id:
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
                    await self._record_generation_success(
                        event=event,
                        prompt=prompt,
                        images=image_results,
                        elapsed=elapsed,
                        model_name=model_name,
                        display_name=persona_name,
                        mode="selfie",
                        request_source=request_source,
                        model_index=model_index,
                        extra={"persona_name": persona_name, "style_name": style_name},
                    )
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
                        if quota_conf.get("enable_user_limit", True):
                            parts_r.append(f"个人剩余次数: {self.persistence.get_user_count(event.get_sender_id())}")
                        if quota_conf.get("enable_group_limit", False) and group_id:
                            parts_r.append(f"本群剩余次数: {self.persistence.get_group_count(group_id)}")
                        remaining_str = " | ".join(parts_r) if parts_r else ""
                    caption_text = " | ".join(part for part in [
                        f"✅ 生成成功 ({elapsed:.2f}s)",
                        f"人设: {persona_name}",
                        f"风格: {style_name}",
                        remaining_str,
                        f"模型: {model_name}" if model_name else "",
                    ] if part) + suffix
                    await self._record_generation_success(
                        event=event,
                        prompt=prompt,
                        images=image_results,
                        elapsed=elapsed,
                        model_name=model_name,
                        display_name=persona_name,
                        mode="selfie",
                        request_source=request_source,
                        model_index=model_index,
                        extra={"persona_name": persona_name, "style_name": style_name},
                    )
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
            action(string): 按照用户要求，合理补充照片的细节，可选：服装、姿势、场景、神态。可用第一人称的“我”称呼自己。避免描述人物的身份、头部外貌，以防止和参考图矛盾。
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
