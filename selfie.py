"""自拍核心模块：人设查找、风格选择、prompt 拼接、图片组合。"""
import asyncio
from pathlib import Path
from typing import Optional

from astrbot import logger
from astrbot.core import AstrBotConfig


# ─────────────────────────── 人设 ───────────────────────────

def _all_personas(conf: AstrBotConfig) -> list[dict]:
    raw = conf.get("selfie", {}).get("selfie_personas", [])
    return [p for p in raw if isinstance(p, dict) and str(p.get("id", "")).strip()]


def find_persona(conf: AstrBotConfig, query: str) -> Optional[dict]:
    """按 ID 或名称查找人设（精确优先，再部分匹配）。"""
    q = query.strip().lower()
    personas = _all_personas(conf)
    for p in personas:
        if p.get("id", "").lower() == q or p.get("name", "").lower() == q:
            return p
    for p in personas:
        if q in p.get("id", "").lower() or q in p.get("name", "").lower():
            return p
    return None


async def resolve_persona(conf: AstrBotConfig, context, event, session_id: str) -> Optional[dict]:
    """按绑定模式从配置中解析当前应使用的人设。返回 None 表示未配置。"""
    selfie_conf = conf.get("selfie", {})
    mode = selfie_conf.get("selfie_binding_mode", "优先 AstrBot persona")
    personas = _all_personas(conf)
    if not personas:
        return None

    def by_id(pid: str) -> Optional[dict]:
        pid = str(pid).strip()
        return next((p for p in personas if p.get("id") == pid), None)

    default_id = selfie_conf.get("selfie_default_persona_id", "")
    default_persona = by_id(default_id) if default_id else (personas[0] if personas else None)

    def by_sid(sid: str) -> Optional[dict]:
        """在各人设的 bound_sids 中查找。"""
        sid = str(sid).strip()
        if not sid:
            return None
        matches = [p for p in personas if sid in [str(s).strip() for s in (p.get("bound_sids") or [])]]
        if len(matches) > 1:
            logger.warning(f"[Selfie] SID「{sid}」被多个人设绑定：{[p.get('id') for p in matches]}，将使用第一个「{matches[0].get('id')}」。")
        return matches[0] if matches else None

    # 获取当前会话实际使用的 AstrBot persona ID
    active_astrbot_persona_id: str = ""
    try:
        conv_mgr = context.conversation_manager
        persona_mgr = context.persona_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
        conv = await conv_mgr.get_conversation(event.unified_msg_origin, curr_cid) if curr_cid else None
        conv_persona_id = conv.persona_id if conv else None
        persona_id, _, _, _ = await persona_mgr.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=conv_persona_id,
            platform_name=event.get_platform_name(),
        )
        active_astrbot_persona_id = str(persona_id or "").strip()
    except Exception as e:
        logger.debug(f"[Selfie] 获取 AstrBot persona 失败: {e}")

    def by_astrbot_persona(astrbot_pid: str) -> Optional[dict]:
        """在各人设的 bound_astrbot_personas 列表中查找。"""
        if not astrbot_pid:
            return None
        matches = []
        for p in personas:
            bound = p.get("bound_astrbot_personas") or []
            if isinstance(bound, str):
                bound = [bound] if bound else []
            if any(str(b).strip() == astrbot_pid for b in bound):
                matches.append(p)
        if not matches:
            logger.debug(f"[Selfie] AstrBot persona「{astrbot_pid}」未命中任何自拍人设绑定，将继续 fallback。")
            return None
        if len(matches) > 1:
            logger.warning(f"[Selfie] AstrBot persona「{astrbot_pid}」被多个自拍人设绑定：{[p.get('id') for p in matches]}，将使用第一个「{matches[0].get('id')}」。")
        return matches[0]

    if mode == "优先 AstrBot persona":
        return by_astrbot_persona(active_astrbot_persona_id) or by_sid(session_id) or default_persona
    if mode == "优先会话 SID":
        return by_sid(session_id) or by_astrbot_persona(active_astrbot_persona_id) or default_persona
    if mode == "只使用手动指定的selfie人设":
        override_id = str(selfie_conf.get("selfie_persona_manual_override", "") or "").strip()
        return by_id(override_id) or default_persona
    return default_persona


async def load_persona_images(persona: dict) -> list[bytes]:
    """读取人设参考图列表，跳过不存在或损坏的文件。"""
    results: list[bytes] = []
    for path_str in (persona.get("ref_images") or []):
        p = Path(str(path_str).strip())
        if not p.is_file():
            logger.warning(f"[Selfie] 人设参考图不存在: {p}")
            continue
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, p.read_bytes)
            results.append(data)
        except Exception as e:
            logger.warning(f"[Selfie] 人设参考图读取失败: {p} — {e}")
    return results


# ─────────────────────────── 风格 ───────────────────────────

def _all_styles(conf: AstrBotConfig) -> list[dict]:
    raw = conf.get("selfie", {}).get("selfie_styles", [])
    return [s for s in raw if isinstance(s, dict) and str(s.get("id", "")).strip() and s.get("enabled", True)]


def find_style(conf: AstrBotConfig, query: str) -> Optional[dict]:
    """按 ID 或名称查找风格（精确优先，再部分匹配）。"""
    q = query.strip().lower()
    styles = _all_styles(conf)
    for s in styles:
        if s.get("id", "").lower() == q or s.get("name", "").lower() == q:
            return s
    for s in styles:
        sid = s.get("id", "").lower()
        sname = s.get("name", "").lower()
        if q in sid or q in sname or sid in q or sname in q:
            return s
    return None


# 自动风格选择的关键词规则（按优先级排列，先命中先用）
_AUTO_RULES: list[tuple[list[str], str]] = [
    (["电影", "大片", "光影", "镜头", "胶片", "氛围感", "cinematic"], "cinematic"),
    (["动漫", "二次元", "插画", "日系", "立绘", "anime", "illustration"], "anime_illustration"),
    (["手办", "盲盒", "q版", "3d", "玩具", "潮玩", "blindbox"], "blindbox_3d"),
    (["自拍", "看看你", "你自己", "真实", "手机", "selfie", "随手拍", "日常"], "selfie_realistic"),
    (["暖光", "窗光", "daily", "phone"], "daily_phone"),
]


def auto_select_style(conf: AstrBotConfig, text: str) -> Optional[dict]:
    """自动模式：先扫描用户自定义模板关键词，再按内置规则，返回第一个命中的风格。"""
    text_lower = text.lower()
    styles = _all_styles(conf)

    # 用户 / 内置模板 keywords 字段扫描（关键词越多命中越精准）
    best: tuple[int, dict | None] = (0, None)
    for s in styles:
        kws = s.get("keywords") or []
        hits = sum(1 for kw in kws if str(kw).lower() in text_lower)
        if hits > best[0]:
            best = (hits, s)
    if best[0] > 0 and best[1]:
        return best[1]

    # 兜底：内置规则 → 匹配 style id
    for kws, sid in _AUTO_RULES:
        if any(kw in text_lower for kw in kws):
            found = find_style(conf, sid)
            if found:
                return found

    return None


def resolve_style(conf: AstrBotConfig, text: str, style_id_override: str = "") -> Optional[dict]:
    """根据配置模式解析最终风格。style_id_override 来自 LLM 或命令显式传入。"""
    if style_id_override:
        return find_style(conf, style_id_override)

    selfie_conf = conf.get("selfie", {})
    mode = selfie_conf.get("selfie_style_mode", "自动")
    if mode == "不注入":
        return None
    if mode == "指定":
        selected = selfie_conf.get("selfie_selected_style_id", "")
        return find_style(conf, selected) if selected else None
    # 自动
    return auto_select_style(conf, text)


# ─────────────────────────── Prompt 拼接 ───────────────────────────

def build_selfie_prompt(
    persona: dict,
    action: str,
    style: Optional[dict],
    has_extra_images: bool,
) -> str:
    """拼接最终自拍 prompt。"""
    parts: list[str] = []

    desc = str(persona.get("description", "")).strip()
    if desc:
        parts.append(desc)

    if action:
        parts.append(action.strip())

    if has_extra_images:
        parts.append(
            "Use the additional reference image(s) only for pose, outfit, or scene reference; "
            "preserve the character's established identity from the persona reference image."
        )

    if style:
        style_prompt = str(style.get("prompt", "")).strip()
        if style_prompt:
            parts.append(style_prompt)

    return ", ".join(parts)


# ─────────────────────────── 图片组合 ───────────────────────────

MAX_TOTAL_IMAGES = 5  # 与现有 handle_image_gen_logic 保持一致


def combine_images(
    persona_images: list[bytes],
    extra_images: list[bytes],
) -> list[bytes]:
    """人设图优先，补充用户额外图，总数不超过 MAX_TOTAL_IMAGES。"""
    combined = list(persona_images)
    remaining = MAX_TOTAL_IMAGES - len(combined)
    if remaining > 0:
        combined.extend(extra_images[:remaining])
    return combined
