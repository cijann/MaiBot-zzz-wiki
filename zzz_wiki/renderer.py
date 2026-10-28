"""使用本地中文字体生成 BWIKI 查询卡片，不执行网络请求。"""
from __future__ import annotations

import os
import re
import warnings
from io import BytesIO
from itertools import islice
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


_BG = "#F3F5F9"
_TEXT = "#253047"
_MUTED = "#536078"
_COLORS = {"zzz": "#906218", "绝区零": "#906218",
           "sr": "#5459A6", "星穹铁道": "#5459A6", "崩坏：星穹铁道": "#5459A6"}
_MAX_ART_BYTES = 16 * 1024 * 1024
_MAX_ART_PIXELS = 20_000_000
_MAX_ART_SIDE = 8192


def _font_paths(font_path: str):
    return [
        *([Path(font_path).expanduser()] if font_path else []),
        Path(__file__).resolve().parents[1] / "assets" / "NotoSansSC.ttf",
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
    ]


def _mask_signature(font, char):
    mask = font.getmask(char)
    return mask.size, bytes(mask)


def _chinese(char):
    return ("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
            or "\U00020000" <= char <= "\U000323af")


def _clean(text: str) -> str:
    text = re.sub(r"https?://\S+", "［链接见原文］", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(c for c in text if c == "\n" or ord(c) >= 32 and ord(c) != 127)


def _wrap(text: str, font, width: int):
    """按像素折行，优先保留英文单词，超长单词逐字拆分。"""
    for paragraph in text.split("\n"):
        line = ""
        for token in re.findall(r"[A-Za-z0-9_]+|[^\S\n]+|.", paragraph):
            if font.getlength(line + token) <= width:
                line += token
                continue
            if line:
                yield line.rstrip()
                line = ""
            if token.isspace():
                continue
            for char in token:
                if line and font.getlength(line + char) > width:
                    yield line
                    line = ""
                line += char
        yield line.rstrip()


def _limited_lines(text: str, font, width: int, count: int):
    lines = list(islice(_wrap(text, font, width), count + 1))
    truncated = len(lines) > count
    lines = lines[:count]
    if truncated:
        last = lines[-1].rstrip()
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines, truncated


def _load_art(art: bytes | None):
    if not art or len(art) > _MAX_ART_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(art), formats=("PNG", "JPEG", "WEBP", "GIF")) as source:
                width, height = source.size
                if (width * height > _MAX_ART_PIXELS or max(width, height) > _MAX_ART_SIDE):
                    return None
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((200, 224), Image.Resampling.LANCZOS)
                return image
    except (OSError, ValueError, SyntaxError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


class CardRenderer:
    def __init__(self, font_path: str = "", max_chars: int = 1800):
        self.max_chars = int(max_chars)
        if self.max_chars <= 0:
            raise ValueError("max_chars 必须为正整数")
        for path in _font_paths(font_path):
            try:
                font = ImageFont.truetype(str(path), 24)
                missing = _mask_signature(font, "\U0010ffff")
                if any(_mask_signature(font, c) == missing for c in "中文属性技能养成来源缓存版权归原作者"):
                    continue
                self._body_font = font
                self._title_font = ImageFont.truetype(str(path), 36)
                self._small_font = ImageFont.truetype(str(path), 18)
                for loaded in (self._body_font, self._title_font, self._small_font):
                    try:
                        loaded.set_variation_by_name("Regular")
                    except (OSError, ValueError, AttributeError):
                        pass
                break
            except (OSError, ValueError):
                continue
        else:
            raise ValueError("未找到可用的中文字体，请设置 font_path 或安装 assets/NotoSansSC.ttf；请回退为文本消息。")

    def render(self, result, game_name: str, query_type: str, art: bytes | None = None) -> bytes:
        body = _clean(result.text or ("暂无可展示内容。" if result.success else "查询未成功。"))
        if result.candidates:
            body += "\n" + "\n".join(f"{i}. {name}" for i, name in enumerate(result.candidates, 1))
            body = _clean(body)
        truncated = len(body) > self.max_chars or body.endswith("…")
        body = body[:self.max_chars].rstrip()
        title = _clean(result.title or "查询结果")
        heading = _clean(f"{game_name} · {query_type}")
        image = _load_art(art)
        width, padding = 960, 48
        content_width = width - 2 * padding
        title_x = padding + (224 if image else 0)
        title_width = width - padding - title_x
        heading_lines, heading_cut = _limited_lines(heading[:240], self._small_font, title_width, 2)
        title_lines, title_cut = _limited_lines(title[:300], self._title_font, title_width, 3)
        truncated = truncated or heading_cut or title_cut or len(title) > 300 or len(heading) > 240
        body_lines, body_cut = _limited_lines(body, self._body_font, content_width, 56)
        truncated = truncated or body_cut
        notice = "内容已截断，请查看原文。" if truncated else ""
        footer = "来源：BWIKI" + (" · 缓存（可能已过期）" if result.stale else "")
        copyright_text = "版权归原作者"
        visible = "".join(heading_lines + title_lines + body_lines) + notice + footer + copyright_text
        missing = _mask_signature(self._body_font, "\U0010ffff")
        if any(_mask_signature(self._body_font, c) == missing for c in set(visible) if _chinese(c)):
            raise ValueError("当前中文字体缺少卡片所需汉字，请更换 font_path；请回退为文本消息。")

        header_height = max(len(heading_lines) * 28 + 12 + len(title_lines) * 50,
                            image.height if image else 0)
        body_y = padding + header_height + 32
        footer_y = body_y + len(body_lines) * 38 + (48 if notice else 16)
        canvas = Image.new("RGB", (width, footer_y + 100), _BG)
        draw = ImageDraw.Draw(canvas)
        accent = _COLORS.get(game_name, "#356D91")
        draw.rounded_rectangle((padding, 24, width - padding, 30), radius=3, fill=accent)
        if image:
            canvas.paste(image, (padding + (200 - image.width) // 2, padding), image)
        y = padding
        for line in heading_lines:
            draw.text((title_x, y), line, font=self._small_font, fill=_MUTED, anchor="lt")
            y += 28
        y += 12
        for line in title_lines:
            draw.text((title_x, y), line, font=self._title_font, fill=accent, anchor="lt")
            y += 50
        y = body_y
        for line in body_lines:
            draw.text((padding, y), line, font=self._body_font, fill=_TEXT, anchor="lt")
            y += 38
        if notice:
            draw.text((padding, y + 10), notice, font=self._small_font, fill=accent, anchor="lt")
        draw.line((padding, footer_y, width - padding, footer_y), fill="#D3D9E3", width=1)
        draw.text((padding, footer_y + 20), footer, font=self._small_font, fill=_MUTED, anchor="lt")
        draw.text((padding, footer_y + 50), copyright_text, font=self._small_font, fill=_MUTED, anchor="lt")
        output = BytesIO()
        canvas.save(output, format="PNG")
        return output.getvalue()
