"""Read official Vue Markdown, keeping website URLs as document identities."""

import json
import posixpath
import re
from urllib.parse import urljoin, urlsplit

from .sources import FetchError, Page

REPOSITORY = "vuejs-translations/docs-zh-cn"
TREE_URL = f"https://api.github.com/repos/{REPOSITORY}/git/trees/main?recursive=1"


def clean_markdown(markdown, url):
    markdown = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, count=1, flags=re.DOTALL)
    output, prose, fence = [], [], None

    def flush():
        if not prose:
            return
        value = "".join(prose)
        prose.clear()
        inline = {}

        def protect(match):
            key = f"IDOLINLINECODE{len(inline)}END"
            inline[key] = match[0]
            return key

        value = re.sub(r"(`+)(.+?)\1", protect, value)
        value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", value, flags=re.DOTALL | re.IGNORECASE)
        value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
        value = re.sub(r"\s*\{#[\w-]+\}", "", value)
        value = re.sub(
            r"^:::\s*(?:tip|warning|danger|info|details|code-group)?\s*(.*)$",
            r"\1",
            value,
            flags=re.MULTILINE,
        )
        value = re.sub(
            r'<div\s+class="(options-api|composition-api)"[^>]*>',
            lambda m: "\n\n" + ("选项式 API" if m[1] == "options-api" else "组合式 API") + "：\n\n",
            value,
        )
        value = re.sub(r"<[A-Z][\w.-]*\b[^>]*?/>", "（交互示例请查看原文。）", value)
        # Remove presentation/component markup, preserving inline Markdown/code.
        value = re.sub(r"</?[A-Za-z][^>]*>", "", value)
        value = re.sub(r"\[[^\]]*\]\(https://(?:play|sfc)\.vuejs\.org/[^)]*\)", "", value)
        value = re.sub(r"(?m)^.*API 风格偏好.*$", "", value)
        value = re.sub(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)\)", lambda m: f"[{m[1]}]({urljoin(url, m[2])})", value)
        for key, code in inline.items():
            value = value.replace(key, code)
        output.append(value)

    for line in markdown.splitlines(keepends=True):
        if fence:
            output.append(line)
            if re.fullmatch(r"\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
            continue
        opening = re.match(r"^\s{0,3}(`{3,}|~{3,})([^\n]*)", line)
        if opening:
            flush()
            fence = opening[1]
            language = re.match(r"[\w+-]*", opening[2].strip())[0]
            output.append(f"{fence}{language}\n")
        else:
            prose.append(line)
    flush()
    return "".join(output).strip()


class VueDocs:
    def __init__(self, request):
        self.request = request
        self.paths = None
        self.revision = None
        self.files = set()

    async def read(self, url):
        status, _, text = await self.request(url)
        if status != 200:
            raise FetchError(f"Vue官方源码读取失败（HTTP {status}）；未降级为网页整页采集")
        return text

    async def manifest(self):
        if self.paths is not None:
            return
        data = json.loads(await self.read(TREE_URL))
        if data.get("truncated") or not re.fullmatch(r"[a-f0-9]{40}", data.get("sha", "")):
            raise FetchError("Vue源码目录不完整或版本标识无效")
        self.revision = data["sha"]
        self.files = {row["path"] for row in data["tree"] if row.get("type") == "blob"}
        self.paths = {
            "/" + path[4:-3]: path
            for path in self.files
            if re.fullmatch(r"src/(?:guide|api)/[\w/-]+\.md", path)
        }
        if not self.paths:
            raise FetchError("Vue源码中没有找到指南或API文档")

    async def fetch(self, url):
        await self.manifest()
        key = urlsplit(url).path.removesuffix(".html").removesuffix(".md").rstrip("/")
        path = self.paths.get(key)
        if path is None:
            raise FetchError("链接不对应Vue指南或API的Markdown源文件")
        canonical = "https://cn.vuejs.org" + key + ".html"
        raw = await self.read(f"https://raw.githubusercontent.com/{REPOSITORY}/{self.revision}/{path}")
        if re.search(r"<!--\s*@include:", raw):
            raise FetchError("Vue文档包含尚不支持的Markdown包含指令，已跳过以避免不完整入库")
        expanded, fence = [], None
        for line in raw.splitlines(keepends=True):
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                fence = (
                    None if fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) else marker[1]
                )
            include = re.match(r"^\s*<<<\s+([^\s{]+)", line) if not fence else None
            if not include:
                expanded.append(line)
                continue
            reference, _, region = include[1].partition("#")
            target = (
                ("src/" + reference[2:])
                if reference.startswith("@/")
                else posixpath.normpath(posixpath.join(posixpath.dirname(path), reference))
            )
            if target not in self.files or not re.fullmatch(
                r"src/[\w/.-]+\.(?:vue|js|ts|html|css|json|md)", target
            ):
                raise FetchError("Vue代码引用超出官方源码允许范围")
            code = await self.read(f"https://raw.githubusercontent.com/{REPOSITORY}/{self.revision}/{target}")
            if region:
                match = re.search(
                    r"^[^\n]*#region[ \t]+" + re.escape(region) + r"[ \t]*\r?\n(.*?)^[^\n]*#endregion[^\n]*$",
                    code,
                    re.DOTALL | re.MULTILINE,
                )
                if not match:
                    raise FetchError("Vue代码引用的region不存在")
                code = match[1]
            delimiter = "`" * max(3, max((len(x) for x in re.findall(r"`+", code)), default=0) + 1)
            expanded.append(f"\n{delimiter}{target.rsplit('.', 1)[1]}\n{code}\n{delimiter}\n")
        text = clean_markdown("".join(expanded), canonical)
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = heading[1] if heading else key.rsplit("/", 1)[-1]
        # Seed all documents from the repository directory, not just in-body links.
        links = [("https://cn.vuejs.org" + item + ".html", item) for item in sorted(self.paths)]
        return Page(canonical, title, text, links)
