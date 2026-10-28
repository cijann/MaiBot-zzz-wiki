"""MaiBot SDK v2 绝区零 Wiki 插件入口。"""
from __future__ import annotations

import asyncio
import base64
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from zzz_wiki.core import WikiClient, WikiResult
from zzz_wiki.renderer import CardRenderer

GAME = "zzz"
GAME_NAME = "绝区零"
PREFIXES = r"zzz(?:wiki)?|绝区零(?:wiki)?|绝区零百科"
DEFAULT_ALIASES = {"鲨鱼妹": "艾莲", "艾莲乔": "艾莲", "雅": "星见雅", "朱队": "朱鸢"}
TYPES = {"属性": "属性", "技能": "技能", "行迹": "技能", "养成": "养成", "材料": "养成", "配装": "配装", "攻略": "配装", "词条": "词条", "星魂": "星魂", "影画": "影画", "大图": "大图", "立绘": "大图", "语音": "语音"}


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用")
    config_version: str = Field(default="1.0.0", description="配置版本")
    request_timeout: float = Field(default=15.0, description="网络超时秒数，范围1至60")
    cache_ttl: int = Field(default=3600, description="页面缓存有效期（秒）；0为每次刷新")
    max_cache_mb: int = Field(default=100, description="本插件页面缓存上限MB；0不缓存")
    image_reply: bool = Field(default=True, description="命令查询以图片卡片回复；失败回退文本")
    font_path: str = Field(default="", description="中文字体绝对路径；留空自动寻找系统字体")
    max_text_chars: int = Field(default=5000, description="文本/模型工具内容长度上限")
    max_card_chars: int = Field(default=1800, description="卡片文字长度上限，长内容请打开来源页")
    admin_users: list[str] = Field(default_factory=list, description="允许清理本插件缓存的用户ID，留空则无人可用")
    aliases: dict[str, str] = Field(default_factory=dict, description="自定义别名到BWIKI准确词条名称的映射")


class WikiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


def parse_args(text: str) -> tuple[str, str]:
    text = text.strip()
    if not text:
        return "", "属性"
    # 空格后缀优先；无空格写法只剥离明确查询类型。
    for suffix in sorted(TYPES, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[:-len(suffix)].strip(), TYPES[suffix]
    return text, "属性"


def extract_args(kwargs) -> str:
    groups = kwargs.get("matched_groups")
    if isinstance(groups, dict) and isinstance(groups.get("args"), str):
        return groups["args"].strip()
    for key in ("raw_message", "text", "content", "message"):
        value = kwargs.get(key)
        if isinstance(value, dict):
            value = value.get("raw_message") or value.get("text") or value.get("content")
        if isinstance(value, str):
            match = re.search(r"(?i)(?:" + PREFIXES + r")(?P<args>.*)$", value)
            if match:
                return match.group("args").strip()
    return ""


def result_text(result: WikiResult) -> str:
    parts = [result.title, result.text]
    if result.candidates:
        parts.append("候选条目：\n" + "\n".join(result.candidates))
        parts.append("请用准确名称重新查询，例如：zzz " + result.candidates[0])
    if result.stale:
        parts.append("上游暂时不可用，当前为过期缓存，可能不是最新数据。")
    if result.source_url:
        parts.append("来源：" + result.source_url)
    return "\n\n".join(p for p in parts if p)


class GameWikiPlugin(MaiBotPlugin):
    config_model = WikiConfig

    async def on_load(self):
        self.client = WikiClient(GAME, Path(self.ctx.paths.runtime_dir) / (GAME + "-pages"))
        self._apply_config()
        self.ctx.logger.info("%s Wiki 插件已加载（MaiBot SDK v2）", GAME_NAME)

    def _apply_config(self):
        cfg = self.config.plugin
        self.client.timeout = max(1.0, min(60.0, cfg.request_timeout))
        self.client.cache_ttl = max(0, cfg.cache_ttl)
        self.client.max_cache_mb = max(0, min(2048, cfg.max_cache_mb))
        self.client.max_chars = max(100, min(20000, cfg.max_text_chars))
        self.client.aliases = {**DEFAULT_ALIASES, **cfg.aliases}

    async def on_unload(self):
        if hasattr(self, "client"):
            await self.client.close()

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._apply_config()

    def help_text(self):
        return (
            GAME_NAME + " Wiki 查询\n"
            "zzz 艾莲 / zzz 深海访客\n"
            "zzz 艾莲 技能/养成/配装/影画/词条/大图/语音\n"
            "zzz 搜索 关键词：列出候选词条\n"
            "zzz clear：管理员清理本插件缓存\n"
            "支持 zzz艾莲技能，绝区零 艾莲；自定义别名在配置 aliases 中设置。\n"
            "章节由BWIKI实际内容决定；语音返回台词文字。每次结果附原文链接。"
        )

    async def dispatch(self, args: str, user_id: str = "") -> tuple[WikiResult, str]:
        if not self.config.plugin.enabled:
            return WikiResult(False, text=GAME_NAME + " Wiki 插件当前未启用。"), "帮助"
        if args.lower() in ("", "help", "帮助", "指令"):
            return WikiResult(True, text=self.help_text()), "帮助"
        if args.lower() == "clear":
            admins = [str(u) for u in self.config.plugin.admin_users]
            if not user_id or user_id not in admins:
                return WikiResult(False, text="该命令仅限配置 admin_users 中的管理员使用。"), "管理"
            count = await asyncio.to_thread(self.client.clear_cache)
            return WikiResult(True, text=f"已清理本插件 {count} 个页面缓存文件。"), "管理"
        if args.startswith("搜索 "):
            return await self.client.search(args[3:].strip()), "搜索"
        keyword, kind = parse_args(args)
        return await self.client.query(keyword, kind), kind

    async def fetch_art(self, url: str) -> bytes | None:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"patchwiki.biligame.com", "wiki.biligame.com"} or parts.username or parts.password or parts.port not in (None, 443):
            return None
        try:
            async def fetch():
                async with self.client._semaphore:
                    async with self.client._http.stream("GET", url, timeout=self.client.timeout) as response:
                        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                            return None
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 8 * 1024 * 1024:
                                return None
                        return bytes(data)
            return await asyncio.wait_for(fetch(), timeout=self.client.timeout)
        except Exception as exc:
            self.ctx.logger.warning("Wiki 图片获取失败：%s", type(exc).__name__)
            return None

    @Command("zzz_wiki", pattern=r"(?i)^(?:zzz(?:wiki)?|绝区零百科|绝区零(?:wiki)?)(?P<args>.*)$")
    async def handle_command(self, **kwargs) -> tuple:
        stream_id = kwargs.get("stream_id")
        try:
            result, kind = await self.dispatch(extract_args(kwargs), str(kwargs.get("user_id") or ""))
            reply = result_text(result)
            if result.success and kind not in {"帮助", "管理"} and self.config.plugin.image_reply:
                try:
                    art = await self.fetch_art(result.image_url) if result.image_url else None
                    if kind == "大图":
                        if art is None:
                            raise ValueError("图片下载失败")
                        payload = art
                    else:
                        renderer = CardRenderer(font_path=self.config.plugin.font_path, max_chars=max(100, min(3500, self.config.plugin.max_card_chars)))
                        payload = await asyncio.to_thread(renderer.render, result, GAME_NAME, kind, art)
                    await self.ctx.send.image(base64.b64encode(payload).decode("ascii"), stream_id)
                except Exception as exc:
                    self.ctx.logger.warning("Wiki 卡片或图片发送失败，回退文本：%s", type(exc).__name__)
                    await self.ctx.send.text(reply, stream_id)
                else:
                    caption = "来源：" + result.source_url
                    if result.stale:
                        caption += "\n上游不可用，当前显示过期缓存。"
                    if kind == "大图":
                        caption += "\n图片及游戏内容版权归原作者。"
                    await self.ctx.send.text(caption, stream_id)
            else:
                await self.ctx.send.text(reply, stream_id)
            return True, reply, 2
        except Exception:
            self.ctx.logger.exception("Wiki 查询处理失败")
            reply = "Wiki 查询处理失败，请稍后重试并检查插件日志。"
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

    @Tool(
        "zzz_wiki_query",
        brief_description="查询绝区零 BWIKI 角色、音擎、驱动盘、邦布信息",
        detailed_description="查询绝区零资料，返回原文内容和来源链接。keyword使用准确词条名；不存在则返回候选，禁止把候选或缺失章节当成查询成功。query_type支持属性、技能、养成、配装、影画、词条、语音、大图。语音为台词文字。",
        parameters=[
            ToolParameterInfo(name="keyword", param_type=ToolParamType.STRING, description="准确角色或装备名称", required=True),
            ToolParameterInfo(name="query_type", param_type=ToolParamType.STRING, description="属性/技能/养成/配装/影画/词条/语音/大图", required=False, default="属性"),
        ],
    )
    async def query_tool(self, keyword: str, query_type: str = "属性", **kwargs) -> dict:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "插件当前未启用。"}
        # 模型工具只查询资料，不调用管理指令，也不主动重复发送到聊天。
        result = await self.client.query(keyword, TYPES.get(query_type or "属性", query_type))
        return {"success": result.success, "content": result_text(result), "source_url": result.source_url, "image_url": result.image_url, "candidates": result.candidates, "stale": result.stale}


def create_plugin():
    return GameWikiPlugin()
