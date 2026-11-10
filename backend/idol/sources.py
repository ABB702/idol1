"""Explicit source scopes and bounded, robots-aware public-web fetching."""

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

USER_AGENT = "IdolKnowledge/0.1 (+local personal knowledge reader)"
MAX_BYTES = 3 * 1024 * 1024
TOPICS = {"ai": (20, 200), "audio_video": (5, 50), "java": (5, 50), "frontend": (5, 50)}
TOPIC_WORDS = {
    "ai": (
        "agent",
        "rag",
        "llm",
        "langchain",
        "langgraph",
        "向量",
        "大模型",
        "deepseek",
        "评测",
        "embedding",
    ),
    "audio_video": ("webrtc", "h264", "h265", "h.264", "h.265", "hls", "flv", "音视频", "流媒体", "视频编码"),
    "java": ("spring", "java", "中间件", "后端架构", "kafka", "redis", "mysql"),
    "frontend": ("vue", "react", "electron", "uniapp", "uni-app", "前端", "reactnative"),
}
ACCOUNT_PATH = "/c/user/token/MS4wLjABAAAAVKlHQZBJGZmdsWK7YD2J35tCaIoSsZnMTKNkAzw6qEE/"


@dataclass(frozen=True)
class Source:
    key: str
    seed: str
    host: str
    paths: tuple[str, ...]
    kind: str
    topic: str = "frontend"

    def allows(self, url: str, *, discovered=False) -> bool:
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != self.host or parsed.username or parsed.password:
                return False
            if parsed.port not in (None, 443):
                return False
            path = unquote(parsed.path)
            if "\\" in path or any(part in (".", "..") for part in path.split("/")):
                return False
            if self.key == "toutiao":
                return path == ACCOUNT_PATH or (discovered and bool(re.fullmatch(r"/article/\d+/?", path)))
            return any(
                prefix == "/"
                or path == prefix.rstrip("/")
                or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
                for prefix in self.paths
            )
        except ValueError:
            return False


DOCUMENT_SOURCES = (
    Source(
        "vue",
        "https://cn.vuejs.org/guide/best-practices/production-deployment",
        "cn.vuejs.org",
        ("/guide/", "/api/"),
        "official_docs",
    ),
    Source(
        "react",
        "https://reactjs.p2hp.com/docs/faq-functions.html",
        "reactjs.p2hp.com",
        ("/docs/",),
        "official_docs",
    ),
    Source(
        "electron",
        "https://www.electronjs.org/zh/docs/latest",
        "www.electronjs.org",
        ("/zh/docs/latest",),
        "official_docs",
    ),
    Source("uniapp", "https://uniapp.dcloud.net.cn/", "uniapp.dcloud.net.cn", ("/",), "official_docs"),
    Source("langchain", "https://docs.langchain.com/", "docs.langchain.com", ("/",), "official_docs", "ai"),
)
INITIAL_SOURCES = tuple(source for source in DOCUMENT_SOURCES if source.key == "vue")

DAILY_SOURCES = (
    Source("juejin", "https://juejin.cn/", "juejin.cn", ("/",), "community"),
    Source("zhihu", "https://www.zhihu.com/", "www.zhihu.com", ("/",), "community"),
    Source("csdn", "https://blog.csdn.net/", "blog.csdn.net", ("/",), "community"),
    Source(
        "tencent", "https://cloud.tencent.com/developer", "cloud.tencent.com", ("/developer",), "community"
    ),
    Source("meituan", "https://tech.meituan.com/", "tech.meituan.com", ("/",), "technical_team"),
    Source(
        "toutiao",
        "https://web.toutiao.com" + ACCOUNT_PATH + "?wid=1792914114026&tab=all",
        "web.toutiao.com",
        (ACCOUNT_PATH,),
        "technical_team",
    ),
)
SOURCES = {source.key: source for source in DOCUMENT_SOURCES + DAILY_SOURCES}


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def topic_matches(text: str, topic: str) -> bool:
    text = text.lower()
    return any(word in text for word in TOPIC_WORDS[topic])


def is_article(source: Source, url: str) -> bool:
    path = urlsplit(url).path
    patterns = {
        "juejin": r"^/post/\d+/?$",
        "zhihu": r"^/question/\d+/answer/\d+/?$",
        "csdn": r"^/[^/]+/article/details/\d+/?$",
        "tencent": r"^/developer/article/\d+/?$",
        "meituan": r"^/\d{4}/\d{2}/\d{2}/.+\.html$",
        "toutiao": r"^/article/\d+/?$",
    }
    return bool(re.match(patterns.get(source.key, r"(?!)"), path))


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]]
    published_at: str | None = None
    author: str = ""
    extraction_error: str = ""


class DocumentMarkdown(MarkdownConverter):
    def convert_pre(self, el, text, parent_tags):
        code = el.find("code") or el
        raw = code.get_text("", strip=False).strip("\n")
        language = ""
        for node in (code, el, el.parent):
            for name in node.get("class", []) if node else []:
                match = re.fullmatch(r"(?:language|lang)-([\w+-]+)", name)
                if match:
                    language = match[1]
                    break
            if language:
                break
        fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", raw)), default=0))
        return f"\n\n{fence}{language}\n{raw}\n{fence}\n\n"


def parse_page(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("h1") or soup.find("title")
    title_text = title.get_text(" ", strip=True) if title else url
    links = [
        (canonical_url(urljoin(url, a["href"])), a.get_text(" ", strip=True)) for a in soup.select("a[href]")
    ]
    published = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find(
        "meta", attrs={"name": "publishdate"}
    )
    time_node = soup.find("time", attrs={"datetime": True})
    published_at = (
        published.get("content") if published else (time_node.get("datetime") if time_node else None)
    )
    author = soup.find("meta", attrs={"name": "author"})
    author_text = author.get("content", "") if author else ""
    for node in soup.select(
        "script, style, nav, footer, header, aside, form, noscript, button, "
        ".header-anchor, .line-numbers-wrapper, .vp-code-copy, .copy, .lang, .VPDocFooter, "
        ".recommend-box, .recommendations, .related-posts, .comment-box, .comments, "
        "#comments, .article-suspended-panel, .article-end, .blog-footer-bottom, "
        ".passport-login-container, .advertisement, .adsbygoogle"
    ):
        node.decompose()
    for anchor in soup.select("a[href]"):
        target = urljoin(url, anchor["href"])
        if urlsplit(target).scheme in {"http", "https"}:
            anchor["href"] = target
        else:
            anchor.unwrap()
    host = urlsplit(url).hostname
    selectors = {
        "cn.vuejs.org": (".vp-doc",),
        "blog.csdn.net": ("#content_views",),
        "juejin.cn": (".article-content",),
        "www.zhihu.com": (".AnswerItem .RichContent-inner .RichText",),
        "cloud.tencent.com": (".mod-content .J-articleContent", ".com-markdown-collpase-main"),
        "tech.meituan.com": (".post-content", ".content .post"),
        "web.toutiao.com": (".article-content",),
    }.get(host, (".vp-doc", ".theme-doc-markdown", "[itemprop='articleBody']"))
    converter = DocumentMarkdown(heading_style="ATX", bullets="-", wrap=False)
    text = ""
    for selector in selectors:
        main = soup.select_one(selector)
        if main is not None and len(main.get_text(strip=True)) >= 40:
            text = converter.convert_soup(main).strip()
            break
    if not text:
        import trafilatura

        # Keep code verbatim, but only restore blocks whose placeholders survived
        # main-content selection; never append all code from unrelated page regions.
        codes = {}
        for pre in soup.find_all("pre"):
            marker = "IDOLCODE" + uuid4().hex.upper()
            codes[marker] = converter.convert_pre(pre, "", set())
            replacement = soup.new_tag("p")
            replacement.string = marker
            pre.replace_with(replacement)
        text = (
            trafilatura.extract(
                str(soup),
                url=url,
                output_format="markdown",
                favor_precision=True,
                include_comments=False,
                include_tables=True,
                include_formatting=True,
                include_links=True,
                fast=True,
            )
            or ""
        )
        for marker, code in codes.items():
            text = text.replace(marker, code)
    error = "" if text.strip() else "未能可靠提取正文；已跳过，未使用整页内容兜底"
    return Page(
        url,
        title_text[:300],
        text,
        links[:3000],
        str(published_at) if published_at else None,
        author_text,
        error,
    )


def rejection_reason(page: Page, source: Source, topic: str) -> str | None:
    if page.extraction_error:
        return page.extraction_error
    if source.kind == "official_docs":
        return "官方文档正文为空或过短" if len(page.text.strip()) < 40 else None
    if len(page.text) < 500:
        return "正文不足500字符，可能为导航页、登录页或反爬响应"
    if re.search(
        r"训练营|扫码.{0,8}(加群|领取)|付费课程|面试.{0,8}(题库|八股)|刷题|限时优惠|课程优惠",
        page.title + "\n" + page.text[:2000],
    ):
        return "营销、课程引流或面试刷题内容"
    if source.kind == "community" and re.search(
        r"(阿里|腾讯|字节|美团|百度|京东|华为|网易).{0,12}(技术团队|研发团队|官方|技术公众号)", page.author
    ):
        return "作者元数据表明是大厂官方团队账号，不属于社区来源范围"
    if not topic_matches(page.title + "\n" + page.text[:5000], topic):
        return "与当前主题无关"
    if source.kind == "community" and not re.search(
        r"实践|踩坑|教程|实现|配置|代码|故障|优化|原理|问题|步骤|安装|部署|示例|benchmark|tutorial|implement|example",
        page.text,
        re.IGNORECASE,
    ):
        return "缺少实践、教程或问题分析内容"
    return None


class FetchError(RuntimeError):
    pass


class SafeFetcher:
    def __init__(self, *, client=None, resolver=None, interval=1.0):
        self.client = client or httpx.AsyncClient(
            timeout=20, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT}
        )
        self.resolver = resolver or self._resolve
        self.interval = interval
        self._last = {}
        self._robots = {}
        self._robots_errors = {}
        self._vue_reader = None

    async def _resolve(self, host):
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def _public(self, host):
        addresses = await self.resolver(host)
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise FetchError("来源解析到非公开地址")

    async def _request(self, url):
        host = urlsplit(url).hostname
        await self._public(host)
        delay = self.interval - (time.monotonic() - self._last.get(host, 0))
        if delay > 0:
            await asyncio.sleep(delay)
        self._last[host] = time.monotonic()
        async with self.client.stream("GET", url) as response:
            if response.is_redirect:
                return response.status_code, response.headers, ""
            if int(response.headers.get("content-length", 0)) > MAX_BYTES:
                raise FetchError("响应超过3MB")
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > MAX_BYTES:
                    raise FetchError("响应超过3MB")
            return (
                response.status_code,
                response.headers,
                body.decode(response.encoding or "utf-8", errors="replace"),
            )

    async def _check_robots(self, url):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots_errors.get(origin)
        if cached and time.monotonic() - cached[0] < 300:
            raise FetchError(cached[1])
        if origin in self._robots and time.monotonic() - self._robots[origin][0] >= 3600:
            del self._robots[origin]
        if origin not in self._robots:
            target = origin + "/robots.txt"
            try:
                for _ in range(5):
                    redirect = urlsplit(target)
                    if (
                        redirect.scheme != "https"
                        or redirect.hostname != parsed.hostname
                        or redirect.port not in (None, 443)
                        or redirect.username
                        or redirect.password
                    ):
                        raise FetchError("robots重定向超出同站HTTPS范围")
                    status, headers, text = await self._request(target)
                    if status not in (301, 302, 303, 307, 308):
                        break
                    if not headers.get("location"):
                        raise FetchError("robots重定向缺少目标")
                    target = urljoin(target, headers["location"])
                else:
                    raise FetchError("robots重定向次数超过上限")
                if status not in (200, 404):
                    raise FetchError(f"无法确认robots策略（HTTP {status}）")
                if status == 200 and re.search(r"<!doctype\s+html|<html\b", text[:1000], re.IGNORECASE):
                    raise FetchError("robots返回HTML页面而非抓取规则（可能是软404），无法确认策略")
            except (FetchError, httpx.HTTPError, ValueError) as exc:
                self._robots_errors[origin] = (time.monotonic(), str(exc))
                raise FetchError(str(exc)) from exc
            if status == 404:
                text = "User-agent: *\nAllow: /"
            robot = RobotFileParser()
            robot.parse(text.splitlines())
            self._robots[origin] = (time.monotonic(), robot)
            self._robots_errors.pop(origin, None)
        if not self._robots[origin][1].can_fetch(USER_AGENT, url):
            raise FetchError("robots.txt禁止抓取")

    async def fetch(self, url: str, source: Source, *, discovered=False) -> Page:
        if source.key == "vue":
            if not source.allows(url):
                raise FetchError("Vue文档URL超出范围")
            from .vue_docs import VueDocs

            if self._vue_reader is None:
                self._vue_reader = VueDocs(self._request)
            return await self._vue_reader.fetch(url)
        for _ in range(4):
            if not source.allows(url, discovered=discovered):
                raise FetchError("URL或重定向超出来源白名单")
            await self._check_robots(url)
            status, headers, html = await self._request(url)
            if status in (502, 503, 504, 521):
                await asyncio.sleep(2)
                status, headers, html = await self._request(url)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise FetchError("重定向缺少目标")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise FetchError(f"来源返回HTTP {status}")
            if "text/html" not in headers.get("content-type", "").lower():
                raise FetchError("来源不是HTML页面")
            return parse_page(url, html)
        raise FetchError("重定向次数超过上限")

    async def close(self):
        await self.client.aclose()
