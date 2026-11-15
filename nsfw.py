"""NSFW command parsing, delivery choices, and in-memory APNG covers.

The APNG chunk layout is reused from the verified cover demo.
Generated covers are used for delivery only and are never written to disk."""

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re
import struct
from typing import Literal
import zlib

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))


NSFW_PARAMETER_DESCRIPTION = (
    "默认为 false。若用户明确要求启用，或你认为此次生图可能涉及 NSFW 内容，请设为 true；"
    "否则忽略此参数或填 false。此参数仅控制生成结果的发送方式。"
)
NSFW_COMMAND_HELP = (
    "NSFW 命令格式错误。用法：文生图-NSFW 描述、图生图-nsfw=封面 描述、"
    "手办化-2-NSFW、自拍-1-nsfw=转发。"
    "方式只支持“封面”和“转发”；指定模型时，NSFW 必须位于模型序号之后。"
)


class ImageDeliveryError(ValueError):
    """An explicit delivery failure; never fall back to an unwrapped image."""


@dataclass(frozen=True)
class ImageDelivery:
    """Immutable settings captured before a generation task starts."""

    mode: Literal["normal", "apng", "forward"] = "normal"
    font_path: str | None = None


@dataclass(frozen=True)
class NSFWCommand:
    command: str
    model_index: int | None
    mode: str | None
    prompt: str


def parse_nsfw_command(text: str, candidates: list[str]) -> NSFWCommand | None:
    """Parse only the command token; never interpret NSFW inside a prompt."""
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    token = parts[0]
    prompt = parts[1].strip() if len(parts) > 1 else ""
    # An existing exact template name remains a template, even if it has a suffix.
    if token in candidates:
        return None
    for command in sorted(set(candidates), key=len, reverse=True):
        if not token.startswith(command + "-"):
            continue
        suffix = token[len(command):]
        if "nsfw" not in suffix.lower():
            continue
        match = re.fullmatch(r"(?:-([0-9]+))?-nsfw(?:=(封面|转发))?", suffix, re.IGNORECASE)
        if not match:
            raise ImageDeliveryError(NSFW_COMMAND_HELP)
        try:
            model_index = int(match[1]) if match[1] is not None else None
        except ValueError as exc:
            raise ImageDeliveryError(NSFW_COMMAND_HELP) from exc
        if model_index is not None and model_index < 1:
            raise ImageDeliveryError(NSFW_COMMAND_HELP)
        return NSFWCommand(command, model_index, match[2], prompt)
    return None


def resolve_image_delivery(event, conf, *, nsfw: bool = False, mode: str | None = None) -> ImageDelivery:
    """Validate before quota checks, reference downloads, or provider requests."""
    if not isinstance(nsfw, bool):
        raise ImageDeliveryError("nsfw 参数必须为布尔值 true 或 false。")
    if not nsfw and mode is None:
        return ImageDelivery()
    general = conf.get("general", {})
    selected = mode if mode is not None else general.get("nsfw_send_mode", "APNG 模糊封面")
    if selected in ("转发", "合并转发"):
        # AstrBot's verified Node/Nodes transport is OneBot v11 (aiocqhttp).
        # QQ's official adapter and unrelated platforms must not receive raw images.
        if event.get_platform_name() != "aiocqhttp":
            raise ImageDeliveryError(
                "当前平台不支持合并转发，请改用 -nsfw=封面，或修改后台默认方式。"
                "本次未调用生图接口，未扣除次数。"
            )
        return ImageDelivery(mode="forward")
    if selected in ("封面", "APNG 模糊封面"):
        try:
            font_path = resolve_font_path(general.get("nsfw_font_path", ""))
        except OSError as exc:
            raise ImageDeliveryError(
                "NSFW 封面缺少可用中文字体，请在插件设置中填写“NSFW 封面字体路径”。"
                "本次未调用生图接口，未扣除次数。"
            ) from exc
        return ImageDelivery(mode="apng", font_path=font_path)
    raise ImageDeliveryError("NSFW 发送方式无效，请选择“APNG 模糊封面”或“合并转发”。")


def resolve_font_path(font_path: str | None = None) -> str:
    """Use an existing local CJK font; never download a font implicitly."""
    explicit = str(font_path or "").strip()
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [Path(explicit).expanduser()] if explicit else [
        windows_fonts / "msyh.ttc",
        windows_fonts / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            ImageFont.truetype(str(candidate), 16)
        except (OSError, ValueError):
            continue
        return str(candidate.resolve())
    raise OSError("找不到可用中文字体，请配置现有中文 TTF/OTF/TTC 字体路径。")


def read_image(image_data: bytes) -> Image.Image:
    """读取第一幅画面并纠正 EXIF 方向；返回独立的 RGBA 图像。"""
    with Image.open(BytesIO(image_data)) as decoded:
        decoded.seek(0)
        return ImageOps.exif_transpose(decoded).convert("RGBA")


def _eye_icon(width: int) -> Image.Image:
    """用贝塞尔曲线画眼睛，局部超采样，不需要下载图标。"""
    scale = 4
    height = max(1, round(width * 0.74))
    stroke = max(1, round(width * 0.07))
    margin = stroke + 2
    icon = Image.new("RGBA", ((width + margin * 2) * scale, (height + margin * 2) * scale))
    draw = ImageDraw.Draw(icon)

    def curve(points: tuple[tuple[float, float], ...]) -> list[tuple[int, int]]:
        result = []
        for i in range(49):
            t = i / 48
            x = sum(points[j][0] * c for j, c in enumerate(
                ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3)
            ))
            y = sum(points[j][1] * c for j, c in enumerate(
                ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3)
            ))
            result.append((round((x + margin) * scale), round((y + margin) * scale)))
        return result

    top = curve(((0, height / 2), (width * 0.28, -height * 0.10),
                 (width * 0.72, -height * 0.10), (width, height / 2)))
    bottom = curve(((width, height / 2), (width * 0.72, height * 1.10),
                    (width * 0.28, height * 1.10), (0, height / 2)))
    draw.line(top + bottom + top[:1], fill="white", width=stroke * scale, joint="curve")
    cx, cy = (width / 2 + margin) * scale, (height / 2 + margin) * scale
    radius = width * 0.165 * scale
    draw.ellipse((round(cx - radius), round(cy - radius), round(cx + radius), round(cy + radius)),
                 outline="white", width=stroke * scale)
    return icon.resize((width + margin * 2, height + margin * 2), Image.Resampling.LANCZOS)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def _png_image_data(image: Image.Image) -> tuple[bytes, list[bytes]]:
    """让 Pillow 编码像素，只取 IHDR 和 IDAT，避免复制输入的元数据。"""
    buffer = BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    data = buffer.getvalue()
    header = b""
    compressed = []
    position = len(PNG_SIGNATURE)
    while position < len(data):
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            header = payload
        elif kind == b"IDAT":
            compressed.append(payload)
        position += length + 12
    return header, compressed


def encode_apng(cover: Image.Image, content: Image.Image, *, frame_delay_ms: int = 100) -> bytes:
    """封面不参与播放；内容帧之后叠加 1×1 透明帧，使动画看起来静止。"""
    if cover.size != content.size:
        raise ValueError("封面与内容图必须尺寸相同。")
    if not isinstance(frame_delay_ms, int) or not 1 <= frame_delay_ms <= 65535:
        raise ValueError("frame_delay_ms 必须是 1 到 65535 之间的整数。")
    header, cover_data = _png_image_data(cover)
    _, content_data = _png_image_data(content)
    # PNG 每条扫描线包含 1 字节过滤类型 + RGBA 四个字节。
    transparent_pixel = zlib.compress(b"\x00\x00\x00\x00\x00")
    output = BytesIO()
    output.write(PNG_SIGNATURE)
    output.write(_chunk(b"IHDR", header))
    output.write(_chunk(b"acTL", struct.pack(">II", 2, 0)))  # 2 帧，无限循环
    for payload in cover_data:
        output.write(_chunk(b"IDAT", payload))

    # 首个 fcTL 放在封面 IDAT 之后，这是“封面不参与动画”的关键。
    width, height = content.size
    output.write(_chunk(b"fcTL", struct.pack(">IIIIIHHBB",
                 0, width, height, 0, 0, frame_delay_ms, 1000, 0, 0)))
    sequence = 1
    for payload in content_data:
        output.write(_chunk(b"fdAT", struct.pack(">I", sequence) + payload))
        sequence += 1

    # NONE 保留内容，OVER 叠加透明像素；整张清晰图保持不变。
    output.write(_chunk(b"fcTL", struct.pack(">IIIIIHHBB",
                 sequence, 1, 1, 0, 0, frame_delay_ms, 1000, 0, 1)))
    output.write(_chunk(b"fdAT", struct.pack(">I", sequence + 1) + transparent_pixel))
    output.write(_chunk(b"IEND", b""))
    return output.getvalue()


def _wrap_line(text: str, draw: ImageDraw.ImageDraw, font, max_width: int) -> list[str]:
    """Wrap CJK and long model identifiers without requiring spaces."""
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        box = draw.textbbox((0, 0), candidate, font=font)
        if current and box[2] - box[0] > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def make_cover(
    image: Image.Image,
    *,
    elapsed: float,
    model_name: str | None,
    font_path: str,
    image_index: int = 1,
    image_count: int = 1,
    batch_index: int = 1,
    batch_count: int = 1,
) -> Image.Image:
    """Blur the original and draw a fitted, opaque information cover."""
    width, height = image.size
    short_edge = min(width, height)
    if short_edge < 16:
        raise ValueError("图片尺寸过小，无法绘制完整的封面说明。")
    flat = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image).convert("RGB")
    background = flat.filter(ImageFilter.GaussianBlur(short_edge / 23))
    cover = ImageEnhance.Brightness(background).enhance(0.55).convert("RGBA")
    draw = ImageDraw.Draw(cover)
    model = " ".join(str(model_name or "未提供").split())
    if len(model) > 160:
        model = model[:159] + "…"
    rows = [
        "点击查看原图以浏览",
        f"尺寸：{width} × {height}",
        f"生成耗时：{elapsed:.2f} 秒",
        f"模型：{model}",
    ]
    if image_count > 1:
        rows.append(f"本次图片：{image_index}/{image_count}")
    if batch_count > 1:
        rows.append(f"生成批次：{batch_index}/{batch_count}")
    eye = _eye_icon(max(1, round(short_edge * 0.13)))
    max_width = max(1, int(width * 0.88))
    gap = max(1, round(short_edge * 0.025))
    for size in range(max(1, min(64, round(short_edge * 0.045))), 0, -1):
        font = ImageFont.truetype(font_path, size)
        lines = [line for row in rows for line in _wrap_line(row, draw, font, max_width)]
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + max(1, size // 3)
        total_height = eye.height + gap + line_height * len(lines)
        if total_height <= height * 0.86 and all(
            draw.textbbox((0, 0), line, font=font)[2] - draw.textbbox((0, 0), line, font=font)[0] <= max_width
            for line in lines
        ):
            break
    else:
        raise ValueError("图片尺寸过小，无法绘制完整的封面说明。")
    y = round((height - total_height) / 2)
    cover.alpha_composite(eye, (round((width - eye.width) / 2), y))
    y += eye.height + gap
    shadow = max(1, round(short_edge / 400))
    for line in lines:
        left, top, right, _ = draw.textbbox((0, 0), line, font=font)
        x = (width - (right - left)) / 2 - left
        draw.text((x + shadow, y - top + shadow), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y - top), line, font=font, fill="white")
        y += line_height
    return cover


def create_apng(
    image_data: bytes,
    *,
    elapsed: float,
    model_name: str | None,
    font_path: str,
    image_index: int = 1,
    image_count: int = 1,
    batch_index: int = 1,
    batch_count: int = 1,
) -> bytes:
    """Build a delivery copy; caller retains the untouched provider bytes."""
    with read_image(image_data) as content:
        with make_cover(
            content, elapsed=elapsed, model_name=model_name, font_path=font_path,
            image_index=image_index, image_count=image_count,
            batch_index=batch_index, batch_count=batch_count,
        ) as cover:
            return encode_apng(cover, content)
