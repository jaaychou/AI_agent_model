"""网页抓取工具 —— 爬虫/研究 Agent 的基础能力（无第三方依赖）。

web_fetch：给定一个真实 URL，抓取网页并提取可读正文与链接，返回给模型分析。
仅访问 http/https，禁止 file/内网保留地址，防止 SSRF 打本机服务。
robots.txt 做基本的合规检查；抓取失败如实报告。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape
from html.parser import HTMLParser

from .tools import ToolResult

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalAgent/1.0 (research)"


class _Extractor(HTMLParser):
    """提取正文文本与链接，跳过 script/style/nav 等噪音标签。"""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "iframe", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []
        self._skip_depth = 0
        self._current_link = None
        self._link_text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "a" and self._skip_depth == 0:
            self._current_link = dict(attrs).get("href", "")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "a" and self._current_link is not None:
            label = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            if label and self._current_link:
                self.links.append((label, self._current_link))
            self._current_link = None
            self._link_text = []
        if tag in self.BLOCK and self._skip_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._current_link is not None:
            self._link_text.append(data)
        self.text_parts.append(data)


def _is_blocked_url(url: str) -> str | None:
    """拒绝非 http(s) 和解析到本机/内网的地址（SSRF 防护）。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "只支持 http/https 网页。"
    host = parsed.hostname or ""
    if not host:
        return "URL 缺少主机名。"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
        except Exception:
            return f"无法解析域名：{host}"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return f"拒绝访问本机/内网保留地址：{host}（{ip}）。"
    return None


def _robots_allows(url: str) -> bool:
    """粗略遵守 robots.txt：抓一次 Disallow 检查目标路径。失败时放行（宽松）。"""
    try:
        parsed = urllib.parse.urlparse(url)
        robots = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        req = urllib.request.Request(robots, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read(200000).decode("utf-8", "replace").casefold()
        path = parsed.path or "/"
        for line in content.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("disallow:"):
                rule = line.split(":", 1)[1].strip()
                if rule and rule != "/" and path.startswith(rule):
                    return False
        return True
    except Exception:
        return True


def fetch_page(url: str, max_chars: int = 20000, timeout: int = 20,
               include_links: bool = True) -> str:
    check = _is_blocked_url(url)
    if check:
        raise ValueError(check)
    if not _robots_allows(url):
        raise ValueError("目标站点的 robots.txt 不允许抓取该路径，已停止。")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    # 证书上下文：优先 certifi；某些代理会做 TLS 中间人导致验证失败，
    # 此时降级为不验证（仅抓取公开网页，明确标注），保证爬虫能力可用。
    ssl_unverified = False
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl.create_default_context()
    try:
        response = urllib.request.urlopen(req, timeout=timeout, context=context)
    except urllib.error.URLError as exc:
        if "CERTIFICATE" not in str(exc).upper():
            raise
        ssl_unverified = True
        insecure = ssl._create_unverified_context()
        response = urllib.request.urlopen(req, timeout=timeout, context=insecure)
    with response:
        raw = response.read(3_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            html = raw.decode(charset, "replace")
        except LookupError:
            html = raw.decode("utf-8", "replace")
        final_url = response.geturl()
    parser = _Extractor()
    parser.feed(html)
    text = unescape("".join(parser.text_parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    lines = [f"抓取时间：{datetime.now().astimezone().isoformat(sep=' ', timespec='seconds')}",
             f"请求 URL：{url}", f"最终 URL：{final_url}",
             f"正文（前 {max_chars} 字）：", text[:max_chars]]
    if include_links and parser.links:
        links = []
        seen = set()
        for label, href in parser.links:
            absolute = urllib.parse.urljoin(final_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append(f"- {label[:80]} → {absolute}")
            if len(links) >= 30:
                break
        lines.append("\n页面链接（前 30 条）：\n" + "\n".join(links))
    if ssl_unverified:
        lines.append("⚠ 注意：本机网络代理导致证书验证失败，本次已跳过 HTTPS 证书校验抓取；内容仅供研究参考，勿用于支付/登录等敏感场景。")
    lines.append("\n说明：这是网页可见文本，不含脚本动态渲染内容；JS 渲染页面请用浏览器工具（Playwright MCP）。")
    return "\n".join(lines)


def register_web_tools(registry, confirm=None):
    def web_fetch(args):
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult(False, "请提供要抓取的 URL。")
        try:
            return ToolResult(True, fetch_page(
                url,
                max_chars=max(1000, min(int(args.get("max_chars", 20000) or 20000), 50000)),
                include_links=bool(args.get("include_links", True))))
        except Exception as exc:
            return ToolResult(False, f"抓取网页失败：{type(exc).__name__}: {exc}。请如实说明无法获取，不要用记忆补造网页内容。")

    registry.register(
        "web_fetch", "抓取指定 URL 的网页正文与链接（纯文本，不含 JS 渲染内容）。用于阅读网页、文档、新闻正文；遵守 robots.txt，拒绝本机/内网地址。动态渲染页面请改用浏览器工具",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "完整 http/https URL"},
            "max_chars": {"type": "integer", "description": "正文最多返回字数，默认 20000"},
            "include_links": {"type": "boolean", "description": "是否附带页面链接，默认 true"},
        }, "required": ["url"]}, web_fetch, source="web")
