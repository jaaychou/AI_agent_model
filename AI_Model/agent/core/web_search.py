"""Small, keyless web search adapter used for live facts by the local Agent."""
from __future__ import annotations

from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _TextOnly()
    try:
        parser.feed(unescape(value or ""))
    except Exception:
        parser.parts = [value or ""]
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _prepare_query(query: str) -> tuple[str, list[str]]:
    """Remove conversational wrappers and retain terms for a basic relevance gate."""
    original = str(query or "").strip()
    # Users often add context before the actual request. Prefer the clause after an
    # explicit search phrase so we do not send an entire conversation as a query.
    explicit = re.search(r"(?:联网搜索|上网搜索|网上搜索|在线搜索|网络搜索|网页搜索|搜一下网上|搜一下网页)([^。！？\n]{2,180})", original)
    if explicit:
        clean = explicit.group(1).strip()
    else:
        clauses = [part.strip() for part in re.split(r"[。！？\n]", original) if part.strip()]
        focused = [part for part in clauses if re.search(r"天气|气温|新闻|进展|最新|目前|现在|搜索", part)]
        clean = (focused[-1] if focused else original)
    for pattern in (
        r"^(?:请问|请|你)?(?:帮我|替我|给我)?(?:联网|上网|网上|在线|网页|网络)?(?:搜索|查找|查一下|查查|搜一下)(?:一下)?",
        r"^搜一下(?:网上|网页)?",
    ):
        clean = re.sub(pattern, "", clean, flags=re.I)
    clean = re.sub(r"^(?:你|请|帮我|替我|给我)+", "", clean)
    clean = re.sub(r"(?:联网查一下|联网搜索|联网查|上网搜索|上网查|网上搜索|在线搜索|网络搜索|搜一下|查一下|查查|搜索一下|搜索|一下|吧|好吗|好吧)+$", "", clean)
    clean = re.sub(r"^(?:今天|目前|当前|现在|最近|最新)", "", clean)
    clean = re.sub(r"(?:现在|目前|当前|今天|最近|最新)$", "", clean)
    clean = re.sub(r"(?:被谁解决了|解决了吗|是谁解决的)", " 证明 解决 ", clean)
    clean = re.sub(r"(?:怎么样|是什么|如何|好吗)[？?。！!]*$", "", clean)
    clean = re.sub(r"[？?。！!，,：:]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean and re.search(r"现在|目前|当前|今天|最近|最新", original):
        clean = f"{clean} {datetime.now().year}"
    cjk_terms = re.findall(r"[\u3400-\u9fff]{2,}", clean)
    if cjk_terms:
        # Require the meaningful Chinese phrase in a result, preventing single-character
        # matches such as Bing interpreting “黎曼” as a person's surname.
        return clean, [max(cjk_terms, key=len)]
    latin_terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9]{3,}", clean)
                   if term.casefold() not in {"the", "and", "for", "with", "latest", "news", "search"}]
    return clean, latin_terms


def search_topic_is_missing(query: str) -> bool:
    clean, terms = _prepare_query(query)
    return not clean or not terms


def search_web(query: str, limit: int = 6, timeout: int = 12) -> str:
    """Query Bing's public RSS endpoint and return only its actual result records."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("搜索关键词不能为空。")
    if len(query) > 400:
        raise ValueError("搜索关键词过长（最多 400 个字符）。")
    search_query, terms = _prepare_query(query)
    if not search_query or not terms:
        return "还不知道要搜索什么主题。请给出关键词或问题，例如“联网搜索今天武汉天气”。"
    def fetch(search_text):
        params = urllib.parse.urlencode({"q": search_text, "format": "rss", "setlang": "zh-CN"})
        request = urllib.request.Request(
            "https://www.bing.com/search?" + params,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalAgent/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            root = ET.fromstring(response.read())
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        matches = []
        for item in items[:max(1, min(int(limit), 8))]:
            title = _plain_text(item.findtext("title", ""))
            link = (item.findtext("link", "") or "").strip()
            snippet = _plain_text(item.findtext("description", ""))
            pub_date = _plain_text(item.findtext("pubDate", ""))
            if not title or not link.startswith(("https://", "http://")):
                continue
            haystack = f"{title} {snippet}".casefold()
            if terms and sum(term in haystack for term in terms) < (1 if len(terms) == 1 else 2):
                continue
            matches.append((title, link, snippet, pub_date))
        return matches

    results = fetch(search_query)
    if not results and terms and terms[0] != search_query:
        # Broad fallback can find fresh coverage, but the exact topic phrase still
        # must appear in every returned title/snippet.
        search_query = terms[0]
        results = fetch(search_query)

    checked = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
    if not results:
        return f"Bing 搜索在 {checked} 没有返回与关键词直接相关的可用结果。请明确告知用户当前无法核实，不要根据记忆补造搜索结果。"
    lines = [f"搜索引擎：Bing RSS；本机检索时间：{checked}；原始问题：{query}；实际查询：{search_query}"]
    for index, (title, link, snippet, pub_date) in enumerate(results, 1):
        lines.append(f"{index}. {title}\n   URL: {link}\n   发布日期: {pub_date or '搜索引擎未提供'}\n   摘要: {snippet[:900] or '无摘要'}")
    lines.append("以上是搜索引擎返回的网页元数据与摘要，不代表已独立核实全文；回答时按 URL 给来源链接，区分报道、当事人主张和独立验证结论。")
    return "\n".join(lines)


def is_live_search_request(text: str) -> bool:
    return bool(re.search(
        r"联网搜索|上网搜索|网上搜索|在线搜索|网络搜索|网页搜索|搜一下网上|搜一下网页|"
        r"(?:联网|上网|网上|在线|网络)(?:查一下|查查|搜索一下|查找)(?:吧|好吗)?$|"
        r"查(?:一下|一查|查查)?(?:一下)?(?:最新|近期|现在|目前|今天|天气|新闻|消息)|"
        r"(?:今天|现在|目前|当前).{0,10}(?:天气|气温|新闻|进展|谁解决|解决了吗)|"
        r"(?:天气|气温).{0,12}(?:今天|现在|武汉|北京|上海|广州|深圳)",
        str(text or ""), re.I,
    ))


def is_current_time_request(text: str) -> bool:
    value = str(text or "").strip()
    return bool(re.search(
        r"(?:现在|当前|此刻)(?:是)?(?:几点|几时|几号|几月几[日号]|日期|时间|星期几|周几)|"
        r"今天(?:是)?(?:几号|几月几[日号]|日期|星期几|周几|星期|周几)|"
        r"系统时间|本机时间|当前日期|现在日期|今天日期",
        value, re.I,
    ))


def is_open_vscode_request(text: str) -> bool:
    return bool(re.search(r"(?:打开|启动|运行|唤起).{0,12}(?:VS\s*Code|VSCode|Visual Studio Code)|(?:VS\s*Code|VSCode).{0,8}(?:打开|启动)", str(text or ""), re.I))


def is_network_location_request(text: str) -> bool:
    return bool(re.search(r"(?:我在哪|我在哪里|我现在在哪|定位我|查(?:一下)?(?:我|电脑|本机)(?:的)?(?:公网)?IP|本机公网IP|电脑公网IP|IP地址.*(?:位置|城市|哪里))", str(text or ""), re.I))
