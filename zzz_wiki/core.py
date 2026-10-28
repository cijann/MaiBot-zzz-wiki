"""可独立复制到另一插件的异步 BWIKI 文本查询核心。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag


@dataclass
class WikiResult:
    success: bool
    title: str = ""
    text: str = ""
    source_url: str = ""
    image_url: str = ""
    candidates: list[str] = field(default_factory=list)
    stale: bool = False


class _WikiError(Exception):
    def __init__(self, message: str, transient: bool = False, missing: bool = False):
        super().__init__(message)
        self.transient = transient
        self.missing = missing


_SECTIONS = {
    "属性": ("属性数据", "基础属性", "基本信息", "详细属性", "详细面板", "角色属性"),
    "技能": ("技能", "行迹", "技能介绍", "技能效果", "音擎效果"),
    "养成": ("角色晋阶材料", "技能晋阶材料", "技能升级材料", "突破所需材料", "晋阶材料", "升级材料", "养成材料"),
    "配装": ("配装推荐", "搭配推荐", "角色搭配推荐", "遗器推荐", "光锥推荐", "装备推荐"),
    "星魂": ("星魂",),
    "影画": ("意象影画", "影画"),
    "词条": ("词条推荐", "主词条", "副词条", "词条"),
    "语音": ("角色语音", "语音", "战斗语音", "日常语音", "正式语音"),
}
_BLOCKS = {"div", "p", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
_NOISE = (
    "script, style, noscript, nav, .toc, #toc, .mw-editsection, .navbox, "
    ".navigation-not-searchable, .catlinks, .bread-edit, #bread-edit, .map-dh, "
    ".smwttcontent, .smwtticon, .smw-highlighter .smwttcontent, .char-infobox-back, "
    ".zzz-char-tab-nav, .zzz-mindscape-nav, .resp-tabs-list, .xingji-box, "
    ".visible-xs.alert, [role='navigation']"
)


def _label(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("[]：:")


def _heading(node: Tag) -> str:
    headline = node.select_one(".mw-headline")
    return _label((headline or node).get_text(" ", strip=True))


def _clean(root: Tag) -> None:
    for tab in root.select(".zzz-char-tab-nav li[data-target]"):
        target = tab.get("data-target", "")
        if re.fullmatch(r"\.[a-zA-Z0-9_-]+", target):
            panel = tab.parent.parent.select_one(target)
            if panel:
                panel.insert(0, "\n" + tab.get_text(" ", strip=True) + "\n")
    for node in list(root.select(_NOISE)):
        if node.parent is not None:
            node.decompose()
    for node in list(root.select("[hidden], [style]")):
        # 隐藏的 tab 包含真实数据，不能一律删掉 display:none。
        if node.parent is not None and (node.has_attr("hidden") or ("display:none" in node.get("style", "").replace(" ", "") and node.name == "tr")):
            node.decompose()
    for img in list(root.select("img")):
        alt = img.get("alt", "")
        if alt and not re.search(r"立绘|header|背景|头像|站立动作", alt, re.I):
            label = re.sub(r"\.(png|webp|jpg|gif)$", "", alt, flags=re.I)
            if re.search(r"图标|Banner|展示|^驱动盘-", label, re.I):
                img.decompose()
            else:
                img.replace_with(" " + label + " ")
        else:
            img.decompose()


def _text(nodes) -> str:
    bits = []
    active_row = None
    for node in nodes:
        if active_row is not None:
            if any(parent is active_row for parent in node.parents):
                continue
            active_row = None
        if isinstance(node, Tag) and node.name == "tr":
            cells = node.find_all(["th", "td"], recursive=False)
            bits.append("\n" + " | ".join(c.get_text(" ", strip=True) for c in cells) + "\n")
            active_row = node
        elif isinstance(node, Tag) and node.name in _BLOCKS:
            bits.append("\n")
        elif isinstance(node, NavigableString):
            bits.append(str(node))
    lines = []
    for line in " ".join(bits).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if any(x in line for x in ("Ctrl+D", "WIKI功能", "请上传文件", "推荐给朋友", "本条目介绍的是")):
            continue
        lines.append(line)
    return "\n".join(lines)


def _section(heading: Tag) -> str:
    level = int(heading.name[1])
    nodes = []
    for node in heading.next_elements:
        if isinstance(node, Tag) and re.fullmatch(r"h[1-6]", node.name) and int(node.name[1]) <= level:
            break
        nodes.append(node)
    text = _text(nodes)
    name = heading.get_text(" ", strip=True)
    return text.removeprefix(name).strip()


def _safe_image(value: str, base: str) -> str:
    try:
        url = urljoin(base, value)
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"patchwiki.biligame.com", "wiki.biligame.com"}
                or parts.username or parts.password or parts.port not in (None, 443)):
            return ""
        if not re.search(r"\.(?:png|jpe?g|webp)(?:$|[/?])", unquote(parts.path), re.I):
            return ""
        # MediaWiki 缩略图路径可直接还原原图，不需要跟随文件页链接。
        if "/thumb/" in parts.path:
            path = parts.path.replace("/thumb/", "/", 1).rsplit("/", 1)[0]
            url = parts._replace(path=path, query="", fragment="").geturl()
        return url
    except (TypeError, ValueError):
        return ""


def _image(root: Tag, title: str, base: str) -> str:
    choices = []
    for order, img in enumerate(root.select("img")):
        alt = unquote(str(img.get("alt", "")))
        classes = " ".join(img.get("class", []))
        if re.search(r"header|背景|壁纸|图标|头像|技能|星魂|影画|站立|动画|[-_]bg|icon", alt + " " + classes, re.I):
            continue
        try:
            width = float(img.get("width", 0))
            height = float(img.get("height", 0))
        except (TypeError, ValueError):
            continue
        named = _label(title) in _label(alt)
        art = "立绘" in alt or "char-painting" in classes
        if min(width, height) < 120 or (not named and not art):
            continue
        url = _safe_image(img.get("data-src") or img.get("src", ""), base)
        if url:
            score = (100 if named else 0) + (200 if art else 0) + min(width * height / 10000, 100)
            choices.append((score, -order, url))
    return max(choices, default=(0, 0, ""))[2]


class WikiClient:
    def __init__(self, game: str, cache_dir: Path, timeout: float = 15.0,
                 cache_ttl: int = 3600, max_cache_mb: int = 100,
                 max_chars: int = 5000, aliases: dict | None = None):
        if game not in {"zzz", "sr"}:
            raise ValueError("game 仅支持 zzz 或 sr")
        self.game = game
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_cache_mb = max_cache_mb
        self.max_chars = max_chars
        self.aliases = dict(aliases or {})
        self._base = f"https://wiki.biligame.com/{game}/"
        self._api = self._base + "api.php"
        self._suffix = f".{game}-bwiki.json"
        self._semaphore = asyncio.Semaphore(4)
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={"User-Agent": "MaiBot-BWikiCore/1.0"},
        )

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout 必须为有限正数")
        self._timeout = value

    @property
    def cache_ttl(self) -> int:
        return self._cache_ttl

    @cache_ttl.setter
    def cache_ttl(self, value: int) -> None:
        self._cache_ttl = max(0, int(value))

    @property
    def max_cache_mb(self) -> int:
        return self._max_cache_mb

    @max_cache_mb.setter
    def max_cache_mb(self, value: int) -> None:
        self._max_cache_mb = max(0, int(value))
        if hasattr(self, "_suffix"):
            self._prune()

    @property
    def max_chars(self) -> int:
        return self._max_chars

    @max_chars.setter
    def max_chars(self, value: int) -> None:
        self._max_chars = max(1, int(value))

    async def close(self) -> None:
        await self._http.aclose()

    def _cache_path(self, params: dict) -> Path:
        key = json.dumps([1, self.game, params], ensure_ascii=False, sort_keys=True)
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + self._suffix)

    def _files(self) -> list[Path]:
        return [p for p in self.cache_dir.glob("*" + self._suffix)
                if re.fullmatch(r"[a-f0-9]{64}" + re.escape(self._suffix), p.name)
                and p.is_file() and not p.is_symlink()]

    def clear_cache(self) -> int:
        removed = 0
        for path in self._files():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _prune(self) -> None:
        try:
            entries = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in self._files())
            size = sum(item[1] for item in entries)
            for _, length, path in entries:
                if size <= self.max_cache_mb * 1024 * 1024:
                    break
                path.unlink(missing_ok=True)
                size -= length
        except OSError:
            pass

    def _read(self, path: Path, params: dict):
        try:
            if self.max_cache_mb <= 0 or path.is_symlink():
                return None
            if path.stat().st_size > self.max_cache_mb * 1024 * 1024:
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry["params"] != params or entry["game"] != self.game:
                return None
            self._validate(entry["data"], params)
            timestamp = float(entry["time"])
            if not math.isfinite(timestamp) or timestamp > time.time():
                return None
            return entry["data"], timestamp
        except (OSError, ValueError, TypeError, KeyError, _WikiError):
            return None

    def _write(self, path: Path, params: dict, data: dict) -> None:
        temp = None
        try:
            content = json.dumps({"game": self.game, "params": params, "time": time.time(), "data": data}, ensure_ascii=False)
            if len(content.encode()) > self.max_cache_mb * 1024 * 1024:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".bwiki-", suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            self._prune()
        except OSError:
            pass
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _validate(data: dict, params: dict) -> None:
        if not isinstance(data, dict):
            raise _WikiError("上游返回了无效 JSON", transient=True)
        if "error" in data:
            error = data["error"]
            code = str(error.get("code", "")) if isinstance(error, dict) else "unknown"
            raise _WikiError(f"BWIKI API 错误：{code}", transient=code in {"maxlag", "readonly", "ratelimited", "internal_api_error"}, missing=code == "missingtitle")
        if params["action"] == "parse":
            page = data.get("parse")
            if (not isinstance(page, dict) or not isinstance(page.get("text"), str)
                    or not page["text"].strip() or not isinstance(page.get("title"), str)
                    or not isinstance(page.get("sections", []), list)):
                raise _WikiError("上游未返回可解析的页面正文", transient=True)
        else:
            query = data.get("query")
            if not isinstance(query, dict) or not isinstance(query.get("search"), list):
                raise _WikiError("上游未返回有效的搜索结果", transient=True)

    async def _request(self, params: dict) -> tuple[dict, bool]:
        params = {**params, "format": "json", "formatversion": 2}
        path = self._cache_path(params)
        cached = self._read(path, params)
        if cached and time.time() - cached[1] < self.cache_ttl:
            return cached[0], False
        try:
            # 同时限制连接、排队和整次请求耗时；热更新在每次请求时生效。
            async def fetch():
                async with self._semaphore:
                    response = await self._http.get(self._api, params=params, timeout=self.timeout)
                    status = response.status_code
                    if status != 200:
                        raise _WikiError(f"BWIKI 请求失败（HTTP {status}）", transient=status >= 500 or status == 429)
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise _WikiError("上游返回非 JSON 响应", transient=True) from exc
                    self._validate(data, params)
                    return data
            data = await asyncio.wait_for(fetch(), timeout=self.timeout)
            self._write(path, params, data)
            return data, False
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            error = _WikiError("BWIKI 网络异常或请求超时", transient=True)
            if cached:
                return cached[0], True
            raise error from exc
        except _WikiError as exc:
            if cached and exc.transient:
                return cached[0], True
            raise

    def _keyword(self, keyword: str) -> str:
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("请输入查询词")
        keyword = keyword.strip()
        if len(keyword) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in keyword):
            raise ValueError("查询词过长或包含控制字符")
        # 用户输入只进入 API 参数，不拼接文件路径，也不作为请求 URL。
        return keyword

    async def search(self, keyword) -> WikiResult:
        try:
            keyword = self._keyword(keyword)
            data, stale = await self._request({"action": "query", "list": "search", "srsearch": keyword,
                                                "srnamespace": 0, "srlimit": 10})
            candidates = list(dict.fromkeys(row["title"] for row in data["query"]["search"]
                                             if isinstance(row, dict) and isinstance(row.get("title"), str)))
            return WikiResult(False, title=keyword,
                              text="请选择准确条目：" if candidates else "未找到相关条目。",
                              source_url=self._base + "index.php?search=" + quote(keyword, safe=""),
                              candidates=candidates, stale=stale)
        except (ValueError, _WikiError) as exc:
            return WikiResult(False, text=str(exc))

    async def query(self, keyword: str, query_type: str = "属性") -> WikiResult:
        try:
            keyword = self._keyword(keyword)
            if query_type not in {*_SECTIONS, "大图"}:
                return WikiResult(False, title=keyword, text=f"不支持的查询类型：{query_type}")
            target = self._keyword(self.aliases.get(keyword, keyword))
            try:
                data, stale = await self._request({"action": "parse", "page": target, "redirects": 1,
                                                   "prop": "text|sections|displaytitle"})
            except _WikiError as exc:
                if not exc.missing:
                    raise
                result = await self.search(target)
                result.text = f"未找到精确条目「{target}」。\n" + result.text
                return result
            page = data["parse"]
            title = page["title"]
            normalized = lambda s: s.replace("_", " ").strip()
            if normalized(title) != normalized(target):
                redirects = page.get("redirects", [])
                # parse 的 redirects 由 MediaWiki 生成；没有重定向证据时拒绝替用户选人。
                if not any(isinstance(r, dict) and normalized(str(r.get("from", ""))) == normalized(target)
                           for r in redirects):
                    return WikiResult(False, title=target, text="返回条目与查询词不符，请选择准确名称。",
                                      candidates=[title], stale=stale)
            source = self._base + "index.php?title=" + quote(title, safe="")
            root = BeautifulSoup(page["text"], "html.parser")
            root = root.select_one(".mw-parser-output") or root
            image = _image(root, title, self._base)
            if query_type == "大图":
                return WikiResult(bool(image), title, "已找到条目图片。" if image else "该条目未找到可信的角色或装备大图。", source, image, stale=stale)
            voice_page = None
            if query_type == "语音":
                for link in root.select("a[title][href]"):
                    candidate = link.get("title", "")
                    href = urlsplit(urljoin(self._base, link.get("href", "")))
                    if (candidate == title + "/语音" and href.scheme == "https"
                            and href.netloc == "wiki.biligame.com"
                            and unquote(href.path) == f"/{self.game}/{candidate}"):
                        voice_page = candidate
                        break
            _clean(root)
            text = self._extract(root, title, query_type)
            if not text and voice_page:
                try:
                    voice_data, voice_stale = await self._request({"action": "parse", "page": voice_page, "redirects": 1,
                                                                 "prop": "text|sections|displaytitle"})
                except _WikiError as exc:
                    if not exc.missing:
                        raise
                else:
                    voice = voice_data["parse"]
                    if voice["title"] == voice_page:
                        voice_root = BeautifulSoup(voice["text"], "html.parser")
                        _clean(voice_root)
                        text = self._extract(voice_root, voice_page, "语音")
                        source = self._base + "index.php?title=" + quote(voice_page, safe="")
                        stale = stale or voice_stale
            if not text:
                return WikiResult(False, title, f"该条目未提供可提取的「{query_type}」章节或数据。", source, image, stale=stale)
            if len(text) > self.max_chars:
                text = text[:max(0, self.max_chars - 1)].rstrip() + "…"
            return WikiResult(True, title, text, source, image, stale=stale)
        except (ValueError, _WikiError) as exc:
            return WikiResult(False, text=str(exc))

    @staticmethod
    def _extract(root: Tag, title: str, query_type: str) -> str:
        headings = root.find_all(re.compile(r"^h[1-6]$"))
        if query_type == "语音":
            chinese = root.select(".voice_text_chs_m, .voice_text_chs")
            if chinese:
                lines = []
                for node in chinese:
                    table = node.find_parent("table")
                    label = table.find("th") if table else None
                    text = node.get_text(" ", strip=True)
                    if text:
                        lines.append((label.get_text(" ", strip=True) + "：" if label else "") + text)
                return "\n".join(dict.fromkeys(lines))
        if query_type == "技能":
            skills = root.select(".skill-more-box[data-skilltype]")
            if skills:
                return "\n\n".join(_text(n.descendants) for n in skills if n.get_text(strip=True))
        names = _SECTIONS[query_type]
        matched = [h for h in headings if _heading(h) in names]
        # 只提取最外层命中章节，避免父章节与子章节重复。
        selected = []
        matched_ids = {id(h) for h in matched}
        active_level = None
        for h in headings:
            level = int(h.name[1])
            if active_level is not None and level <= active_level:
                active_level = None
            if id(h) in matched_ids and active_level is None:
                selected.append(h)
                active_level = level
        chunks = []
        for h in selected:
            value = _section(h)
            if value:
                chunks.append(f"{_heading(h)}\n{value}")
        if query_type == "属性":
            # 移动端与桌面信息常重复；只取一个角色信息块，再接数值表。
            info = root.select_one(".char-infobox")
            if info:
                copy = BeautifulSoup(str(info), "html.parser")
                for n in copy.select(".resp-tabs, .resp-tabs-container"):
                    if n.parent:
                        n.decompose()
                summary = _text(copy.descendants)
                if summary:
                    chunks.insert(0, summary)
            elif not any(_heading(h) in {"基本信息", "基础属性"} for h in selected):
                name_heading = next((h for h in headings if _heading(h) == _label(title)), None)
                if name_heading:
                    chunks.insert(0, _section(name_heading))
        if not chunks:
            # 光锥/音擎效果常是表格行，不能因整页提到“技能”就误判命中。
            row_names = {"技能": ("技能名称", "技能效果", "音擎效果"), "词条": ("主词条", "副词条", "词条推荐")}.get(query_type, ())
            for row in root.select("tr"):
                cells = row.find_all(["th", "td"], recursive=False)
                if cells and _label(cells[0].get_text()) in row_names and len(cells) > 1:
                    chunks.append(_text(row.descendants))
        return "\n\n".join(dict.fromkeys(c for c in chunks if c.strip()))
