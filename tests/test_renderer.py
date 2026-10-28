"""离线验证中文字体、布局、截断及不可信图片处理。"""
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


renderer = _load("bwiki_renderer_under_test", ROOT / "zzz_wiki" / "renderer.py")
WikiResult = _load("bwiki_renderer_core_under_test", ROOT / "zzz_wiki" / "core.py").WikiResult


@pytest.fixture(scope="module")
def font_path():
    paths = renderer._font_paths("") + [
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    ]
    for path in paths:
        if not path.is_file():
            continue
        try:
            renderer.CardRenderer(str(path))
            return str(path)
        except ValueError:
            pass
    pytest.skip("本机未安装中文字体，保留无字体分支测试")


@pytest.fixture
def card(font_path):
    return renderer.CardRenderer(font_path)


def _art(size=(400, 800), mode="RGB"):
    stream = BytesIO()
    Image.new(mode, size, "red").save(stream, format="PNG")
    return stream.getvalue()


def _texts(monkeypatch):
    calls = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        calls.append((xy, text, kwargs))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    return calls


def test_font_order_and_custom_priority(font_path, monkeypatch):
    paths = renderer._font_paths(font_path)
    assert paths[:5] == [Path(font_path), ROOT / "assets" / "NotoSansSC.ttf",
                         Path("/System/Library/Fonts/PingFang.ttc"),
                         Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                         Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    assert paths[-1].name == "msyh.ttc"
    original = ImageFont.truetype
    with patch.object(renderer.ImageFont, "truetype", wraps=original) as load:
        renderer.CardRenderer(font_path)
    assert all(args.args[0] == font_path for args in load.call_args_list)
    monkeypatch.setattr(renderer, "_font_paths", lambda _: [Path("missing.ttf"), Path(font_path)])
    assert renderer.CardRenderer("bad.ttf").render(WikiResult(True, text="中文"), "zzz", "属性")


def test_no_font_raises_clear_error(monkeypatch):
    monkeypatch.setattr(renderer.ImageFont, "truetype", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ValueError, match="中文字体.*font_path.*回退"):
        renderer.CardRenderer()


def test_latin_only_font_is_rejected(monkeypatch):
    class LatinFont:
        def getmask(self, char):
            return ImageFont.load_default().getmask("A" if char.isascii() else "?")

    default_font = ImageFont.load_default()
    monkeypatch.setattr(renderer.ImageFont, "truetype", lambda *a, **kw: LatinFont())
    # 避免 load_default 内部再次调用被替换的 truetype。
    monkeypatch.setattr(renderer.ImageFont, "load_default", lambda: default_font)
    with pytest.raises(ValueError, match="中文字体"):
        renderer.CardRenderer()


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_max_chars(limit):
    with pytest.raises(ValueError, match="max_chars"):
        renderer.CardRenderer(max_chars=limit)


def test_png_labels_colors_and_no_urls(card, monkeypatch):
    calls = _texts(monkeypatch)
    result = WikiResult(True, title="艾莲 Ellen Joe", text="攻击力 863\n冰属性伤害提升25%",
                        source_url="https://example.invalid/" + "long" * 1000,
                        image_url="https://example.invalid/art.png", stale=True)
    png = card.render(result, "绝区零", "属性")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(png)) as image:
        image.load()
        assert image.format == "PNG" and image.mode == "RGB" and image.width == 960
        assert image.getpixel((0, 0)) == (243, 245, 249)
    labels = "\n".join(text for _, text, _ in calls)
    assert all(s in labels for s in ["艾莲", "绝区零", "属性", "BWIKI", "缓存", "版权归原作者"])
    assert "https://" not in labels and result.source_url not in labels
    assert next(kw["fill"] for _, text, kw in calls if text.startswith("艾莲")) == renderer._COLORS["zzz"]


def test_pixel_wrap_chinese_and_english(card):
    text = "中文属性 " + "Ellen Joe attacks " * 4 + "X" * 200 + "\n末尾"
    lines = list(renderer._wrap(text, card._body_font, 160))
    assert len(lines) > 10 and lines[-1] == "末尾"
    assert all(card._body_font.getlength(line) <= 160 for line in lines)
    assert "".join(lines).replace(" ", "") == text.replace(" ", "").replace("\n", "")
    assert list(renderer._wrap("Ellen Joe", card._body_font, 95)) == ["Ellen", "Joe"]


@pytest.mark.parametrize("body,limit", [("技能效果" * 100, 40), ("属性\n" * 500, 1800), ("上游省略…", 1800)])
def test_explicit_truncation(font_path, monkeypatch, body, limit):
    calls = _texts(monkeypatch)
    png = renderer.CardRenderer(font_path, limit).render(WikiResult(True, title="角色", text=body), "sr", "技能")
    assert any("内容已截断" in text for _, text, _ in calls)
    assert Image.open(BytesIO(png)).height < 3000


def test_long_headers_and_links_stay_inside_card(card, monkeypatch):
    calls = _texts(monkeypatch)
    card.render(WikiResult(True, title="角色" * 500, text="参考 https://example.invalid/" + "x" * 3000),
                "崩坏：星穹铁道" * 100, "属性" * 100)
    for (x, _), text, kw in calls:
        assert x + kw["font"].getlength(text) <= 912
        assert "https://" not in text
    assert any("内容已截断" in text for _, text, _ in calls)


@pytest.mark.parametrize("size", [(400, 800), (800, 400), (40, 40)])
def test_art_preserves_ratio(card, size):
    art = _art(size)
    thumb = renderer._load_art(art)
    assert thumb.width <= 200 and thumb.height <= 224
    assert abs(thumb.width / thumb.height - size[0] / size[1]) < 0.02
    png = card.render(WikiResult(True, title="角色", text="属性"), "sr", "属性", art)
    image = Image.open(BytesIO(png))
    assert image.getpixel((48 + 100, 48 + thumb.height // 2)) == (255, 0, 0)


def test_bad_bytes_and_truncated_image_fall_back(card):
    result = WikiResult(True, title="角色", text="中文属性")
    expected = card.render(result, "zzz", "属性")
    for art in [b"not an image", _art()[:80], b"\x89PNG\r\n\x1a\n"]:
        assert card.render(result, "zzz", "属性", art) == expected


def test_image_limits_before_decode(card, monkeypatch):
    result = WikiResult(True, text="属性")
    expected = card.render(result, "zzz", "属性")
    art = _art((20, 20))
    monkeypatch.setattr(renderer, "_MAX_ART_PIXELS", 399)
    with patch.object(Image.Image, "load", side_effect=AssertionError("超限图片不应解码")):
        assert renderer._load_art(art) is None
    monkeypatch.setattr(renderer, "_MAX_ART_BYTES", 1)
    with patch.object(Image, "open", side_effect=AssertionError("超限字节不应打开")):
        assert card.render(result, "zzz", "属性", art) == expected


@pytest.mark.parametrize("pixel_limit", [300_000, 100_000])
def test_pillow_bombs_fall_back(card, monkeypatch, pixel_limit):
    art = _art((800, 500))
    result = WikiResult(True, text="属性")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pixel_limit)
    assert card.render(result, "zzz", "属性", art) == card.render(result, "zzz", "属性")
    assert Image.MAX_IMAGE_PIXELS == pixel_limit


def test_error_candidates_and_empty_result(card, monkeypatch):
    calls = _texts(monkeypatch)
    card.render(WikiResult(False, text="请选择准确条目", candidates=["艾莲", "姬子"]), "sr", "属性")
    assert any("1. 艾莲" in text for _, text, _ in calls)
    assert any("2. 姬子" in text for _, text, _ in calls)
    calls.clear()
    card.render(WikiResult(False), "未知游戏", "属性")
    assert any("查询未成功" in text for _, text, _ in calls)
    assert not any("缓存" in text for _, text, _ in calls)


def test_missing_content_glyph_raises(card, monkeypatch):
    original = renderer._mask_signature
    missing = original(card._body_font, "\U0010ffff")
    monkeypatch.setattr(renderer, "_mask_signature", lambda font, c: missing if c == "龘" else original(font, c))
    with pytest.raises(ValueError, match="缺少.*回退"):
        card.render(WikiResult(True, text="龘"), "zzz", "属性")
