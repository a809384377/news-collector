"""url 探测内核（s4-sources-management Step 3 产出）。

s4 决策 D2：probe 深度 = 最小可用版（HTTP 探测 + 类型猜测 + 1 条样本）。
更新频率估算 / 标题质量评级 / RSSHub 路由匹配 等高级能力留 ROADMAP「想法」段。

公开 API
========
- ``ProbeResult``：dataclass，7 字段
- ``async probe(url, *, client=None, timeout=12.0) -> ProbeResult``：主入口
- ``suggest_id(url) -> str | None``：暴露 id 推断逻辑（``add`` 命令直接复用）

设计契约
========
- ``probe`` 不抛异常：网络错误 / 超时 / 解析失败都映射到 ``ProbeResult.error``，
  ``reachable=False``。调用方按 ``ProbeResult`` 字段决策即可。
- 类型判定优先级：response content-type → body 头部 512 字节嗅探 → 默认 web。
- sample_title 提取尽力而为（feedparser / trafilatura.extract_metadata / HTML <title>
  兜底链）；任何一步异常静默归 None。
- ``client`` 参数允许调用方注入 ``httpx.AsyncClient(transport=MockTransport(...))``
  做离线测试；不传时函数内构造临时 client 并自动关闭。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import feedparser
import httpx
import trafilatura

# 历史上是 ``Literal["rss", "web"]``；解耦审查后退化为 ``str`` 让新 source_type
# （twikit / xhs 等）能复用 probe 数据结构。运行时校验改为查 prober 注册表。
SourceTypeHint = str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """``probe`` 的标准输出。

    字段
    ----
    - ``url``：探测的原始 url
    - ``reachable``：HTTP 200-399 视为 True；其余 / 网络错误 / 超时 False
    - ``status_code``：HTTP 状态码；网络层错误时 None
    - ``source_type``：``rss`` / ``web`` / None（None 表示 reachable=False）
    - ``suggested_id``：从 url 推断的 source id 候选；url 不合法时 None
    - ``sample_title``：第一条 entry / 页面 <title>；提取失败 None
    - ``error``：错误描述（reachable=False 时非空，否则 None）
    """

    url: str
    reachable: bool
    status_code: int | None
    source_type: SourceTypeHint | None
    suggested_id: str | None
    sample_title: str | None
    error: str | None


# id 推断时从 url path 中过滤的停用词（小写比较）
_PATH_STOPWORDS = frozenset(
    {"feed", "feeds", "rss", "rss2", "atom", "xml", "index", "everything"}
)
# id 推断时从 path 段去除的尾扩展名
_TRAILING_EXTS = ("xml", "atom", "rss", "html", "htm")
# 嗅探 body 头部字节数（content-type 不含 xml 时用）
_HEAD_SNIFF_BYTES = 512
# twikit prober 匹配的 host 集合（小写比较；去 www. 前缀后）
_TWIKIT_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})


def suggest_id(url: str) -> str | None:
    """从 url 推荐一个 source id。

    规则：``<domain 主体>_<path 关键字...>`` 全小写下划线连接，过滤停用词与扩展名。

    示例
    ----
    - ``https://www.anthropic.com/news`` → ``anthropic_news``
    - ``https://simonwillison.net/atom/everything/`` → ``simonwillison``
      （atom / everything 都是停用词）
    - ``https://github.com/anthropics/sdk/releases.atom`` → ``github_anthropics_sdk_releases``
    - ``https://example.com/`` → ``example``
    """
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return None

    # domain 主体：去 www / 取倒数第二段（example.com → example）
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host_parts = host.split(".")
    domain_main = host_parts[-2] if len(host_parts) >= 2 else host_parts[0]

    # path 关键字：按 / 切，去停用词，去尾扩展名
    cleaned: list[str] = []
    for raw in parsed.path.split("/"):
        seg = raw.lower().strip()
        if not seg:
            continue
        # 去尾扩展名
        for ext in _TRAILING_EXTS:
            if seg.endswith(f".{ext}"):
                seg = seg[: -(len(ext) + 1)]
                break
        if not seg or seg in _PATH_STOPWORDS:
            continue
        # 仅允许 [a-z0-9_-] 进入 id；其他字符替换为 _
        seg = re.sub(r"[^a-z0-9_-]+", "_", seg).strip("_-")
        if seg:
            cleaned.append(seg)

    pieces = [domain_main, *cleaned]
    sid = "_".join(pieces)
    # 多余下划线塌缩
    sid = re.sub(r"_+", "_", sid).strip("_")
    return sid or domain_main or None


# prober 注册表：``(predicate, source_type)`` 元组按优先级排列。
# predicate 签名：``(content_type: str, body_head: str, url: str) -> bool``
# 第一个返回 True 的 prober 赢；都未命中走 ``_DEFAULT_PROBE_TYPE`` fallback。
# 新增基于响应嗅探或 URL host 判类型的 source_type 时在此追加一行
# （twikit / xhs 等 host-based 类型适合走 URL 匹配 predicate）。
def _twikit_predicate(content_type: str, body_head: str, url: str) -> bool:
    """URL host ∈ ``_TWIKIT_HOSTS`` → ``twikit``。

    优先级排首位：host-based 判定比 content-type sniffing 更确定（X 反爬
    可能让 ``x.com/<handle>`` 返回 HTML 反爬页，content-type 嗅探会误判为 web；
    host 匹配先于嗅探让结果稳定）。
    """
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host in _TWIKIT_HOSTS


def _rss_predicate(content_type: str, body_head: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if "xml" in ct or "rss" in ct or "atom" in ct:
        return True
    head = body_head[:_HEAD_SNIFF_BYTES].lstrip().lower()
    return head.startswith(("<rss", "<feed", "<?xml"))


_PROBERS: list[tuple[Callable[[str, str, str], bool], SourceTypeHint]] = [
    (_twikit_predicate, "twikit"),  # host-based 确定性最强，排首位
    (_rss_predicate, "rss"),
]
_DEFAULT_PROBE_TYPE: SourceTypeHint = "web"


def _detect_type(
    content_type: str, body_head: str, url: str = ""
) -> SourceTypeHint:
    """遍历 ``_PROBERS`` 注册表判 source_type；都不命中走默认 fallback。

    Args:
        content_type: HTTP 响应 Content-Type
        body_head: 响应正文前 ``_HEAD_SNIFF_BYTES`` 字节
        url: 原始 URL（host-based prober 用，如 twikit）
    """
    for predicate, src_type in _PROBERS:
        try:
            if predicate(content_type, body_head, url):
                return src_type
        except Exception:
            continue
    return _DEFAULT_PROBE_TYPE


def _extract_title_rss(body: str) -> str | None:
    try:
        parsed = feedparser.parse(body)
        entries = getattr(parsed, "entries", None) or []
        if entries:
            title = entries[0].get("title")
            if title:
                return str(title).strip()
    except Exception:
        pass
    return None


def _extract_title_web(body: str) -> str | None:
    """HTML ``<title>`` 优先 → trafilatura.extract_metadata 兜底。

    顺序原因：probe 给用户判断"这是什么 url"用，HTML ``<title>`` 几乎总是页面级
    主题（``Anthropic - News``），更稳；trafilatura 倾向抽正文首个 H1，遇到
    单文章页会拿到具体标题（``Hi``），列表页可能拿到导航/广告位标题，不可控。
    """
    m = re.search(r"<title[^>]*>([^<]+)</title>", body, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        if title:
            return title
    try:
        meta = trafilatura.extract_metadata(body)
        if meta and getattr(meta, "title", None):
            t = meta.title.strip()
            if t:
                return t
    except Exception:
        pass
    return None


async def probe(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> ProbeResult:
    """探测 url，返回 ``ProbeResult``。详见模块 docstring。"""
    sid = suggest_id(url)
    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        own_client = True
    try:
        try:
            resp = await client.get(url, timeout=timeout)
        except httpx.HTTPError as e:
            return ProbeResult(
                url=url,
                reachable=False,
                status_code=None,
                source_type=None,
                suggested_id=sid,
                sample_title=None,
                error=f"{type(e).__name__}: {e}",
            )

        if resp.status_code >= 400:
            return ProbeResult(
                url=url,
                reachable=False,
                status_code=resp.status_code,
                source_type=None,
                suggested_id=sid,
                sample_title=None,
                error=f"HTTP {resp.status_code}",
            )

        ct = resp.headers.get("content-type", "")
        body = resp.text
        stype = _detect_type(ct, body[:_HEAD_SNIFF_BYTES], url)
        sample_title = (
            _extract_title_rss(body) if stype == "rss" else _extract_title_web(body)
        )

        return ProbeResult(
            url=url,
            reachable=True,
            status_code=resp.status_code,
            source_type=stype,
            suggested_id=sid,
            sample_title=sample_title,
            error=None,
        )
    finally:
        if own_client:
            await client.aclose()
