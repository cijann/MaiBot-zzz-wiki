"""离线回归；真实页面摘录只保留解析涉及的 DOM，不请求网络。"""
import asyncio
import importlib.util
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

CORE = Path(__file__).resolve().parents[1] / "zzz_wiki" / "core.py"
spec = importlib.util.spec_from_file_location("bwiki_core_under_test", CORE)
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
WikiClient, WikiResult = core.WikiClient, core.WikiResult

# 2026-10-28 实抓的四页结构摘录；省略中间表格行与无关的业务内容。
ELLEN = '''<div class="mw-parser-output">
<div id="bread-edit">编 刷 历</div><div class="toc">目录 属性数据 技能</div>
<center><div>如果是第一次来，按"Ctrl+D"可以收藏随时查看更新</div>
<img alt="角色信息-header.png" width="1920" height="300" src="https://patchwiki.biligame.com/images/zzz/b/b2/dm8urb6zsynq2ow8j5nv2apklgeamhm.png"></center>
<h2><span class="mw-headline" id="立绘">立绘</span></h2>
<div class="resp-tabs"><ul class="resp-tabs-list"><li>立绘</li><li>介绍立绘</li></ul>
<div class="resp-tabs-container"><div class="resp-tab-content"><img alt="角色立绘-艾莲.png" width="962" height="1246" class="img-kk" src="https://patchwiki.biligame.com/images/zzz/b/b1/0zf0pp4ougzv7n6rnvimn1bckp9ywdx.png"></div></div></div>
<h2><span class="mw-headline" id="艾莲">艾莲</span></h2>
<table class="wikitable"><tr><th>全名/本名</th><td>艾莲·乔（Ellen Joe）</td></tr><tr><th>阵营</th><td>维多利亚家政</td></tr><tr><th>属性</th><td>冰</td></tr></table>
<h2><span class="mw-headline" id="属性数据">属性数据</span><span class="mw-editsection">[编辑]</span></h2>
<table id="属性" class="wikitable"><tr><th>等级</th><th>生命值</th><th>攻击力</th><th>防御力</th></tr><tr><td>60</td><td>7673</td><td>863</td><td>606</td></tr></table>
<table id="属性" class="wikitable"><tr><th>暴击率</th><th>暴击伤害</th></tr><tr><td>5%</td><td>50%</td></tr><tr style="display:none"><td>不可见数值</td></tr></table>
<h2><span class="mw-headline" id="角色晋阶材料">角色晋阶材料</span></h2><table><tr><td>4 初阶强攻认证章</td><td>2.4万 丁尼</td></tr></table>
<h2><span class="mw-headline" id="意象影画">意象影画</span></h2><div class="zzz-mindscape">影画展示1</div><div class="poke-bg"><table class="wikitable"><tr><th>名称</th><th>效果</th></tr><tr><td>凛冬前兆</td><td>暴击率提升2%</td></tr></table></div>
<h2><span class="mw-headline" id="技能">技能</span></h2>
<div class="zzz-char-tab"><ul class="zzz-char-tab-nav"><li data-target=".zzz-char-tab1">未加强</li><li data-target=".zzz-char-tab2" class="active">加强</li></ul><div class="tab-content">
<div class="tab-pane zzz-char-tab1"><div class="main-line-wrap zzz-skill-tree"><div class="resp-tabs"><ul class="resp-tabs-list"><li>普通攻击</li><li>闪避</li></ul><div class="resp-tabs-container"><div class="resp-tab-content" style="display:block"><div class="r-skill-bg"><div class="r-skill-title-1">普通攻击：利齿修剪法</div><div class="r-skill-bg-2"><p>向前方进行至多三段的攻击，造成物理伤害</p></div></div></div><div class="resp-tab-content" style="display:none"><div class="r-skill-title-1">闪避：涡漩</div><p>快速的冲刺闪避</p></div></div></div></div></div>
<div class="tab-pane zzz-char-tab2 active"><div class="r-skill-title-1">普通攻击：急冻修剪法</div><p>消耗急冻充能</p></div></div></div>
<h2><span class="mw-headline" id="角色搭配推荐">角色搭配推荐</span></h2>
<h3><span class="mw-headline" id="配装推荐">配装推荐</span></h3><h4><span class="mw-headline" id="词条推荐">词条推荐</span></h4><table><tr><td>暴击率/暴击伤害</td></tr></table>
<h2><span class="mw-headline">角色相关</span></h2><div>不应混入技能或属性的角色故事</div></div>'''

DEEP_SEA = '''<div class="mw-parser-output"><div class="toc">目录 导航</div>
<h2><span class="mw-headline" id="基础属性">基础属性</span></h2>
<div class="resp-tabs"><ul class="resp-tabs-list"><li>音擎立绘</li></ul><div class="resp-tabs-container"><div class="resp-tab-content"><img alt="武器立绘-深海访客.png" width="200" height="200" src="https://patchwiki.biligame.com/images/zzz/thumb/4/4f/61j786an3boe39mbonjauiqk7ua0qsd.png/200px-武器立绘-深海访客.png"></div></div></div>
<table class="wikitable"><tr><th>深海访客</th></tr><tr><th>基础攻击力</th><td>48~713</td></tr><tr><th>副属性</th><td>暴击率 9.6~24%</td></tr><tr><th>音擎效果</th><td>诸洋之王 冰属性伤害提升25%/31.5%/38%/44.5%/50%</td></tr></table>
<h2><span class="mw-headline" id="详细面板">详细面板</span></h2><table><tr><th>等级</th><th>基础攻击力</th></tr><tr><td>60</td><td>713</td></tr></table>
<h2><span class="mw-headline" id="突破所需材料">突破所需材料</span></h2><table><tr><td>4 强攻组件</td><td>12000 丁尼</td></tr></table>
<h2><span class="mw-headline">音擎故事</span></h2><p>艾莲惯用的战术音擎</p></div>'''

HIMEKO = '''<div class="mw-parser-output"><div class="map-dh"><a title="姬子/语音" href="/sr/姬子/语音">角色语音</a></div>
<div class="main-line-wrap char-infobox"><div class="resp-tabs"><ul class="resp-tabs-list"><li>角色立绘</li><li>角色介绍立绘</li></ul>
<img class="char-infobox-back" alt="星穹列车-bg.jpg" width="1920" height="1080" src="https://patchwiki.biligame.com/images/sr/b/b4/pbpdgmnkddb1lv6sowu6j1v4ne8se81.jpg">
<div class="resp-tabs-container"><div class="resp-tab-content"><img class="char-painting" alt="姬子立绘.png" width="900" height="900" src="https://patchwiki.biligame.com/images/sr/thumb/e/ed/7fihsuzw7se69ls7p341128sbzg2vgj.png/900px-姬子立绘.png"></div><p><br></p><div class="resp-tab-content">角色介绍立绘</div></div></div>
<table><tr><th>姬子 Himeko</th></tr><tr><th>命途</th><td>智识</td></tr><tr><th>战斗属性</th><td>火</td></tr><tr><th>阵营</th><td>星穹列车</td></tr></table></div>
<div class="toc">目录</div><h2><span class="mw-headline" id="属性数据">属性数据</span></h2><table class="wikitable"><tr><th>等级</th><th>生命值</th><th>攻击力</th><th>防御力</th></tr><tr><td>80</td><td>1047</td><td>756</td><td>436</td></tr><tr><th>速度</th><td>96</td><th>能量上限</th><td>120</td></tr></table>
<h2><span class="mw-headline" id="角色晋阶材料">角色晋阶材料</span></h2><p>65 恒温晶壳</p>
<h2><span class="mw-headline" id="星魂">星魂</span></h2><div class="resp-tabs"><ul class="resp-tabs-list"><li>表格</li><li>星魂图</li></ul><div class="resp-tabs-container"><div class="resp-tab-content"><table class="wikitable"><tr><td>童年</td><td>速度提高20%</td></tr></table></div></div></div>
<h2><span class="mw-headline" id="行迹">行迹</span></h2><div style="overflow-x:overlay"><div class="xingji-container flex-row-s-a"><div class="xingji-box">图标导航重复</div><div class="flex-col-c xingji-more">
<div class="skill-more-box" data-skilltype="pugong" style="display:block"><div class="skill-mark">普攻</div><div class="skill-name img-tx2">武装调律</div><p>对指定敌方单体造成火属性伤害</p></div>
<div class="skill-more-box" data-skilltype="zhanji"><div class="skill-mark">战技</div><div class="skill-name img-tx2">熔核爆裂</div><p>对相邻目标造成伤害</p></div>
<div class="skill-more-box" data-skilltype="zhongjieji"><div class="skill-name">天坠之火</div></div><div class="skill-more-box" data-skilltype="tianfu"><div class="skill-name">乘胜追击</div></div>
</div></div></div><h2><span class="mw-headline" id="技能晋阶材料">技能晋阶材料</span></h2><p>信用点</p></div>'''

NIGHT = '''<div class="mw-parser-output"><div class="toc">目录 基本信息</div>
<h2><span class="mw-headline" id="基本信息">基本信息</span><span class="mw-editsection">[编辑]</span></h2>
<table class="wikitable"><tr><th colspan="4">银河铁道之夜</th></tr><tr><td colspan="4"><div class="center"><a class="image"><img alt="银河铁道之夜.png" width="150" height="150" src="https://patchwiki.biligame.com/images/sr/thumb/c/c8/960zvxe5n3hq5kjokhf5fhh9n1luh3s.png/150px-银河铁道之夜.png"></a></div></td></tr>
<tr><th>命途</th><td>智识</td><th>稀有度</th><td><img alt="5星.png" width="52" height="14" src="https://patchwiki.biligame.com/images/sr/c/c7/g380fo4o5accoa1rmlckz3e24vpx23e.png"></td></tr>
<tr><th>基础生命</th><td>52</td><th>满级生命</th><td>1164</td></tr><tr><th>基础攻击</th><td>26</td><th>满级攻击</th><td>582</td></tr><tr><th>基础防御</th><td>18</td><th>满级防御</th><td>396</td></tr><tr><th>技能名称</th><td colspan="3">流星群</td></tr></table>
<h2><span class="mw-headline" id="详细属性">详细属性</span></h2><table class="wikitable"><tr><th>等级</th><th>生命值</th><th>攻击力</th><th>防御力</th></tr><tr><td>20</td><td>203</td><td>101</td><td>69</td></tr></table>
<h2><span class="mw-headline" id="光锥故事">光锥故事</span></h2><p>故事不应进入属性</p>
<h2><span class="mw-headline" id="光锥立绘">光锥立绘</span></h2><img alt="光锥-立绘-银河铁道之夜.png" width="400" height="567" src="https://patchwiki.biligame.com/images/sr/thumb/c/ce/q5jshivsumfbdn8451mnf99t99lj4fs.png/400px-光锥-立绘-银河铁道之夜.png">
<h2><span class="mw-headline" id="晋阶材料">晋阶材料</span></h2><p>熄灭原核</p><h2><span class="mw-headline" id="搭配推荐">搭配推荐</span></h2><h3>光锥说明</h3><p>推荐角色</p></div>'''


def page(title="艾莲", html=ELLEN, **extra):
    return {"parse": {"title": title, "text": html, "sections": [
        {"line": "属性数据", "index": "T-1", "fromtitle": "模板:角色图鉴"}], **extra}}


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.temp.cleanup()

    async def client(self, handler, game="zzz", **kwargs):
        client = WikiClient(game, self.root, **kwargs)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.clients.append(client)
        return client

    async def test_four_real_page_fixtures(self):
        for game, title, html, expected in [
            ("zzz", "艾莲", ELLEN, "7673"), ("zzz", "深海访客", DEEP_SEA, "48~713"),
            ("sr", "姬子", HIMEKO, "1047"), ("sr", "银河铁道之夜", NIGHT, "1164"),
        ]:
            with self.subTest(game=game, title=title):
                def handler(request):
                    self.assertEqual(request.url.host, "wiki.biligame.com")
                    self.assertEqual(request.url.path, f"/{game}/api.php")
                    self.assertEqual(request.url.params["page"], title)
                    self.assertEqual(request.url.params["redirects"], "1")
                    return httpx.Response(200, json=page(title, html))
                client = await self.client(handler, game=game)
                result = await client.query(title)
                self.assertTrue(result.success, result.text)
                self.assertIn(expected, result.text)
                self.assertNotIn("目录", result.text)
                self.assertNotIn("编辑", result.text)
                self.assertNotIn("Ctrl+D", result.text)
                self.assertNotIn("故事不应", result.text)
                self.assertTrue(result.image_url.startswith("https://patchwiki.biligame.com/"))
                self.assertNotIn("/thumb/", result.image_url)
                self.assertNotIn("header", result.image_url)
                self.assertEqual(parse_qs(urlsplit(result.source_url).query)["title"], [title])

    async def test_tabs_and_game_specific_sections(self):
        for game, title, html, kind, words in [
            ("zzz", "艾莲", ELLEN, "技能", ["利齿修剪法", "涡漩", "未加强", "加强"]),
            ("sr", "姬子", HIMEKO, "技能", ["武装调律", "熔核爆裂", "天坠之火", "乘胜追击"]),
            ("zzz", "艾莲", ELLEN, "影画", ["凛冬前兆"]),
            ("sr", "姬子", HIMEKO, "星魂", ["童年"]),
            ("zzz", "艾莲", ELLEN, "词条", ["暴击率/暴击伤害"]),
            ("zzz", "深海访客", DEEP_SEA, "技能", ["诸洋之王"]),
            ("sr", "银河铁道之夜", NIGHT, "技能", ["流星群"]),
            ("sr", "银河铁道之夜", NIGHT, "养成", ["熄灭原核"]),
        ]:
            with self.subTest(game=game, kind=kind):
                client = await self.client(lambda r: httpx.Response(200, json=page(title, html)), game=game)
                result = await client.query(title, kind)
                self.assertTrue(result.success, result.text)
                for word in words:
                    self.assertIn(word, result.text)
                self.assertNotIn("角色故事", result.text)
                self.assertNotIn("图标导航重复", result.text)

    async def test_alias_and_exact_redirect(self):
        calls = []
        def handler(request):
            calls.append(request.url.params["page"])
            return httpx.Response(200, json=page())
        client = await self.client(handler, aliases={"鲨鱼妹": "艾莲"})
        self.assertTrue((await client.query("鲨鱼妹")).success)
        self.assertEqual(calls, ["艾莲"])
        redirected = await self.client(lambda r: httpx.Response(200, json=page(redirects=[{"from": "艾莲乔", "to": "艾莲"}])))
        self.assertTrue((await redirected.query("艾莲乔")).success)
        mismatch = await self.client(lambda r: httpx.Response(200, json=page()))
        result = await mismatch.query("相似的名字")
        self.assertFalse(result.success)
        self.assertEqual(result.candidates, ["艾莲"])

    async def test_only_missingtitle_searches_and_never_picks_candidate(self):
        calls = []
        def handler(request):
            action = request.url.params["action"]
            calls.append(action)
            if action == "parse":
                return httpx.Response(200, json={"error": {"code": "missingtitle"}})
            return httpx.Response(200, json={"query": {"search": [{"title": "艾莲"}, {"title": "艾莲·乔"}]}})
        client = await self.client(handler)
        result = await client.query("艾")
        self.assertFalse(result.success)
        self.assertEqual(result.candidates, ["艾莲", "艾莲·乔"])
        self.assertEqual(calls, ["parse", "query"])
        self.assertIn("精确", result.text)
        self.assertFalse((await client.search("艾")).success)

    async def test_absent_sections_and_voice_navigation_are_not_success(self):
        client = await self.client(lambda r: httpx.Response(200, json=page("姬子", HIMEKO)), game="sr")
        for kind in ["语音", "影画", "配装", "词条"]:
            result = await client.query("姬子", kind)
            self.assertFalse(result.success)
            self.assertIn(kind, result.text)
        self.assertFalse((await client.query("姬子", "随意章节")).success)

    async def test_voice_subpage_chinese_and_safe_api_boundary(self):
        voice_html = '''<div class="mw-parser-output"><h2><span class="mw-headline">正式语音</span></h2><table class="wikitable"><tr><th colspan="4">初次见面</th></tr><tr><td><div class="bikit-audio" data-src="https://patchwiki.biligame.com/images/sr/8/85/8hkveijf0tzcbdf68fbfg03j4ah1o1s.mp3"></div></td></tr><tr><td><div class="voice_text_chs_m vt_active_m">欢迎上车，开拓者，我是星穹列车的领航员，姬子。相信我们将会共度一段有趣的旅程。</div><div class="voice_text_en_m">Welcome aboard</div></td></tr></table></div>'''
        calls = []
        def handler(request):
            calls.append(request.url)
            title = request.url.params["page"]
            return httpx.Response(200, json=page(title, voice_html if title.endswith("/语音") else HIMEKO))
        client = await self.client(handler, game="sr")
        result = await client.query("姬子", "语音")
        self.assertTrue(result.success, result.text)
        self.assertIn("初次见面：欢迎上车", result.text)
        self.assertNotIn("Welcome", result.text)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(url.path == "/sr/api.php" for url in calls))
        self.assertEqual(parse_qs(urlsplit(result.source_url).query)["title"], ["姬子/语音"])
        calls.clear()
        client.clear_cache()
        hostile = HIMEKO.replace('href="/sr/姬子/语音"', 'href="https://evil.test/sr/姬子/语音"')
        bad = await self.client(lambda r: (calls.append(r.url), httpx.Response(200, json=page("姬子", hostile)))[1], game="sr")
        self.assertFalse((await bad.query("姬子", "语音")).success)
        self.assertEqual(len(calls), 1)

    async def test_image_allowlist_and_decoration_filter(self):
        html = ELLEN.replace("https://patchwiki.biligame.com/images/zzz/b/b1/0zf0pp4ougzv7n6rnvimn1bckp9ywdx.png", "https://patchwiki.biligame.com.evil.test/a.png")
        client = await self.client(lambda r: httpx.Response(200, json=page("危险图片", html)))
        result = await client.query("危险图片", "大图")
        self.assertFalse(result.success)
        self.assertEqual(result.image_url, "")
        for value in ["http://wiki.biligame.com/a.png", "https://wiki.biligame.com:444/a.png", "https://wiki.biligame.com@evil.test/a.png", "https://patchwiki.biligame.com/a.svg", "data:image/png;evil"]:
            self.assertEqual(core._safe_image(value, client._base), "")

    async def test_cache_stale_errors_and_live_settings(self):
        state = [200]
        calls = []
        def handler(request):
            calls.append(request)
            if state[0] == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if state[0] == "invalid":
                return httpx.Response(200, text="<html>上游维护</html>")
            if state[0] == "api":
                return httpx.Response(200, json={"error": {"code": "badtitle"}})
            return httpx.Response(state[0], json=page())
        client = await self.client(handler)
        first = await client.query("艾莲")
        self.assertTrue(first.success)
        self.assertFalse((await client.query("艾莲")).stale)
        self.assertEqual(len(calls), 1)
        client.cache_ttl = 0
        client.timeout = 1.25
        client.max_chars = 70
        for failure in [503, 429, "timeout", "invalid"]:
            state[0] = failure
            result = await client.query("艾莲")
            self.assertTrue(result.success, result.text)
            self.assertTrue(result.stale)
            self.assertLessEqual(len(result.text), 70)
            self.assertEqual(calls[-1].extensions["timeout"]["read"], 1.25)
        for failure in [400, 404, 302, "api"]:
            state[0] = failure
            result = await client.query("艾莲")
            self.assertFalse(result.success, result.text)
            self.assertFalse(result.stale)
        client.max_cache_mb = 0
        self.assertEqual(client._files(), [])
        state[0] = 200
        client.max_chars = 5000
        self.assertTrue((await client.query("艾莲")).success)
        self.assertEqual(client._files(), [])
        for invalid in [0, float("inf"), float("nan")]:
            with self.assertRaises(ValueError):
                client.timeout = invalid

    async def test_no_cache_errors_do_not_search(self):
        for failure in [400, 404, 500, 567, 302]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(failure, headers={"Location": "https://evil.test/private"})
            client = await self.client(handler)
            result = await client.query("不存在" + str(failure))
            self.assertFalse(result.success)
            self.assertIn(str(failure), result.text)
            self.assertEqual(len(calls), 1)
        client = await self.client(lambda r: httpx.Response(200, json={"error": {"code": "invalidtitle"}}))
        self.assertFalse((await client.query("非法标题")).success)

    async def test_hash_paths_clear_isolation_and_corrupt_cache(self):
        client = await self.client(lambda r: httpx.Response(200, json=page()))
        for keyword in ["../../外部", "/tmp/艾莲", "https://evil.test/private", "..\\..\\逃逸"]:
            await client.query(keyword)
        files = client._files()
        self.assertEqual(len(files), 4)
        for path in files:
            self.assertEqual(path.parent, self.root)
            self.assertRegex(path.name, r"^[a-f0-9]{64}\.zzz-bwiki\.json$")
        keep = self.root / "keep.json"
        keep.write_text("用户数据")
        foreign = self.root / ("a" * 64 + ".sr-bwiki.json")
        foreign.write_text("星铁缓存")
        lookalike = self.root / "user.zzz-bwiki.json"
        lookalike.write_text("其他数据")
        external = self.root / "external"
        external.write_text("不删除链接目标")
        link = self.root / ("f" * 64 + ".zzz-bwiki.json")
        link.symlink_to(external)
        self.assertEqual(client.clear_cache(), 4)
        self.assertTrue(all(p.exists() for p in [keep, foreign, lookalike, external, link]))
        self.assertEqual(client.clear_cache(), 0)
        await client.query("艾莲")
        client._files()[0].write_text("{broken")
        self.assertTrue((await client.query("艾莲")).success)
        self.assertFalse((await client.query("\x00bad")).success)
        self.assertFalse((await client.query(" ")).success)

    async def test_cache_capacity_atomic_and_ttl(self):
        client = await self.client(lambda r: httpx.Response(200, json=page()), max_cache_mb=1)
        for n in range(4):
            params = {"action": "parse", "n": n}
            client._write(client._cache_path(params), params, page(html="x" * 400000))
        self.assertLessEqual(sum(p.stat().st_size for p in client._files()), 1024 * 1024)
        self.assertEqual(len(client._files()), 2)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        huge = {"action": "parse", "n": "huge"}
        client._write(client._cache_path(huge), huge, page(html="中" * 600000))
        self.assertFalse(client._cache_path(huge).exists())
        client.clear_cache()
        await client.query("艾莲")
        path = client._files()[0]
        saved = json.loads(path.read_text())
        saved["time"] = time.time() - 3601
        path.write_text(json.dumps(saved))
        client._http = await self.replace_transport(client._http, lambda r: httpx.Response(503))
        self.assertTrue((await client.query("艾莲")).stale)
        client.max_cache_mb = 0
        self.assertEqual(client._files(), [])

    async def replace_transport(self, old, handler):
        await old.aclose()
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_network_concurrency_and_finite_deadline(self):
        active = peak = 0
        gate = asyncio.Event()
        four_started = asyncio.Event()
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                four_started.set()
            try:
                await gate.wait()
                return httpx.Response(200, json=page())
            finally:
                active -= 1
        client = await self.client(handler, timeout=2)
        pending = [asyncio.create_task(client.query(str(i))) for i in range(12)]
        await asyncio.wait_for(four_started.wait(), 1)
        self.assertEqual(peak, 4)
        gate.set()
        await asyncio.gather(*pending)
        self.assertEqual(peak, 4)
        never = asyncio.Event()
        async def blocked(request):
            await never.wait()
        client = await self.client(blocked, timeout=0.02)
        result = await asyncio.wait_for(client.query("艾莲"), 1)
        self.assertFalse(result.success)
        self.assertIn("超时", result.text)

    async def test_search_stale_empty_and_missing_does_not_use_old_page(self):
        mode = ["page"]
        def handler(request):
            if mode[0] == "down":
                return httpx.Response(503)
            if request.url.params["action"] == "parse":
                if mode[0] == "missing":
                    return httpx.Response(200, json={"error": {"code": "missingtitle"}})
                return httpx.Response(200, json=page())
            return httpx.Response(200, json={"query": {"search": [] if mode[0] == "empty" else [{"title": "艾莲"}]}})
        client = await self.client(handler, cache_ttl=0)
        self.assertTrue((await client.query("艾莲")).success)
        self.assertEqual((await client.search("艾")).candidates, ["艾莲"])
        mode[0] = "down"
        result = await client.search("艾")
        self.assertTrue(result.stale)
        self.assertFalse(result.success)
        mode[0] = "missing"
        result = await client.query("艾莲")
        self.assertFalse(result.success)
        self.assertFalse(result.stale)
        self.assertEqual(result.candidates, ["艾莲"])
        mode[0] = "empty"
        result = await client.search("不存在")
        self.assertEqual(result.candidates, [])
        self.assertIn("未找到", result.text)

    async def test_empty_and_nested_sections(self):
        html = '''<div class="mw-parser-output"><div class="toc">技能 语音</div><h2>技能</h2><h2>配装推荐</h2><p>装备推荐数据</p><h3>词条推荐</h3><p>暴击率建议</p><h2>故事</h2><p>排除内容</p></div>'''
        client = await self.client(lambda r: httpx.Response(200, json=page("测试", html)))
        self.assertFalse((await client.query("测试", "技能")).success)
        result = await client.query("测试", "配装")
        self.assertTrue(result.success)
        self.assertEqual(result.text.count("暴击率建议"), 1)
        self.assertNotIn("排除内容", result.text)
        self.assertFalse((await client.query("测试", "语音")).success)

    async def test_hidden_parent_with_styled_descendant(self):
        html = '<div class="mw-parser-output"><div hidden><span style="color:red">隐藏</span></div><h2>基础属性</h2><table><tr><th>生命值</th><td>7673</td></tr></table></div>'
        client = await self.client(lambda r: httpx.Response(200, json=page("测试", html)))
        result = await client.query("测试")
        self.assertTrue(result.success)
        self.assertIn("生命值 | 7673", result.text)
        self.assertNotIn("隐藏", result.text)

    async def test_result_defaults_and_independent_candidates(self):
        first, second = WikiResult(False), WikiResult(False)
        first.candidates.append("艾莲")
        self.assertEqual(second.candidates, [])
        self.assertFalse(second.stale)
        with self.assertRaises(ValueError):
            WikiClient("other", self.root)


if __name__ == "__main__":
    unittest.main()
