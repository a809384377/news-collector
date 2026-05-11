"""通用网页适配器（设计 §3.5 / §3.6 / KNOWLEDGE-LOG #5）。

适用于无 RSS / atom feed 的官方博客（如 anthropic.com/news）以及单页持续追加的
官方 changelog（如 Claude / ChatGPT release notes）。

按 ``source["mode"]`` 分派两条子路径：

1. **缺省（列表两级模式）** — 适用于 ``anthropic.com/news`` 这类列表页：
   - 第一级：列表页 → 子文章 URL 列表
     - 主路径：HTTP GET HTML，自写启发式从 ``<a href>`` 中筛同域名 + path 段数 ≥ 2
       的链接，canonicalize 后去重。
     - 兜底：``r.jina.ai/{url}`` 取 markdown，正则抽 ``[title](url)`` 同样过滤同域名。
   - 第二级：子文章 URL → RawArticle
     - 主路径：trafilatura.extract 取正文 + extract_metadata 取 title / date。
       失败标志：返回 None 或 body 长度 < 200 字符（Anthropic 子页面正文一般 > 1000）。
     - 兜底：``r.jina.ai/{url}`` markdown，title 取首个 H1，body 用整段 markdown。

2. **mode: changelog_page** — 适用于 Claude / ChatGPT release notes 这类单页持续追加：
   - 三层兜底链：``markdown_url`` → ``r.jina.ai/{url}`` → fail-skip
   - 拿到 markdown 后按 H2/H3 级日期标题切段，每段映射成一条 RawArticle
   - 共享 base url，``skip_url_dedup=True``（D1）

不接 sqlite，不去重 — 只把列表页 + 子文章 / 单页切段映射成 ``RawArticle``，由上层
``pipeline/fetch.py`` 负责入库 / canonical_hash / fetched_at / status。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

import trafilatura

from ..models import RawArticle
from ..utils.url import canonicalize_url
from .base import (
    TransientHTTPError,  # noqa: F401  re-export for callers; useful in tests
    raise_for_transient_status,
    with_retry,
)

_USER_AGENT = "newsbox/0.1.0"
# 列表两级模式单次 HTTP GET timeout（trafilatura / jina 各自 GET 都用此值）
# 从 20s 降到 12s：普通页面 1-3s 即返；20s 是 SPA 异常源 144s 卡死现象的放大器
# 配合 _PER_ARTICLE_TIMEOUT_SECONDS 形成双重保护（s2 Step 5）
_TIMEOUT_SECONDS = 12.0
# 每条子文章端到端时间上限（trafilatura 主 + jina 兜底各最多 _TIMEOUT_SECONDS）
# 触发后 skip 当前文章，不让一篇卡死整个列表（s2 Step 5 防御性加固）
_PER_ARTICLE_TIMEOUT_SECONDS = 24.0
_DEFAULT_MAX_ARTICLES = 30
_TRAFILATURA_MIN_BODY_CHARS = 200
_JINA_PREFIX = "https://r.jina.ai/"
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MARKDOWN_H1_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)

# changelog_page 兜底链阈值（设计 §3.6.2）
_CHANGELOG_MIN_BYTES = 500
_CHANGELOG_PLACEHOLDER_TOKEN = "loading..."
_CHANGELOG_TIMEOUT_SECONDS = 15.0

# H2/H3 级日期标题正则（设计 §3.6.3 抄写为可执行 Python）
_DATE_HEADING_RE = re.compile(
    r"^(?P<level>#{2,3})\s*\*?\*?\s*"
    r"(?P<date>"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")\s*\*?\*?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# 段内首个子标题：## 或 ### （日期标题已被切走）
_SUBHEADING_RE = re.compile(r"^(?P<level>#{2,3})\s+(?P<text>.+?)\s*$", re.MULTILINE)

# 解析"May 7, 2026" / "May 7th, 2026" 的 strptime 格式（去 ordinal 后）
_ENGLISH_DATE_FORMATS = (
    "%B %d, %Y",   # May 7, 2026
    "%b %d, %Y",   # May 7, 2026（短月名）
    "%B %d %Y",    # May 7 2026（无逗号）
    "%b %d %Y",    # May 7 2026（短月名 + 无逗号）
)
_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)


# ---- 简易 anchor 抽取器 ------------------------------------------------------


class _AnchorCollector(HTMLParser):
    """从 HTML 收集所有 ``<a href>`` 的 href 与可见文本。

    标准库 html.parser 足够 — 不引入额外依赖，避免与 trafilatura 内部 lxml 冲突。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)
                break


# ---- 适配器 -----------------------------------------------------------------


class WebAdapter:
    """通用网页适配器（list page → article 或 changelog_page → 切段）。

    ``source`` 字典字段：
    - ``id``           sources.yaml 中的条目 id
    - ``url``          列表页 URL（默认模式）或 changelog 单页 URL
    - ``mode``         可选 ``"changelog_page"``；缺省走列表两级模式
    - ``markdown_url`` 仅 changelog_page 用，原生 markdown 端点（设计 §3.6 层 1）
    - ``selector``     列表两级模式下，现仅识别 ``"auto"``，其他值忽略不报错（前向兼容）
    - ``tier``         pipeline 使用，本适配器不读
    - ``max_articles`` 列表两级模式下，列表页一次最多抓多少条子文章（缺省 30）
    """

    source_type: str = "web"

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._injected_client = http_client

    # ---- HTTP --------------------------------------------------------------

    @with_retry()
    async def _http_get(self, client: httpx.AsyncClient, url: str) -> str:
        """拉文本。命中 transient 状态由装饰器重试，其他 4xx/5xx 立刻抛。"""
        response = await client.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        raise_for_transient_status(response)
        return response.text

    @with_retry()
    async def _http_get_bytes(self, client: httpx.AsyncClient, url: str) -> bytes:
        """changelog_page 路径用：拉原始字节。命中 transient 状态由装饰器重试，
        其他 4xx/5xx 立刻抛。changelog 模式 timeout 沿用 15s（与 atom 一致）。"""
        response = await client.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_CHANGELOG_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        raise_for_transient_status(response)
        return response.content

    # ---- 主入口 -------------------------------------------------------------

    async def fetch(
        self,
        source: dict[str, Any],
        since: datetime | None,
    ) -> list[RawArticle]:
        mode = source.get("mode")

        if mode == "changelog_page":
            return await self._fetch_changelog_page(source, since)
        # 缺省路径：列表两级模式
        return await self._fetch_listing_two_level(source, since)

    # ---- 列表两级模式（缺省）---------------------------------------------

    async def _fetch_listing_two_level(
        self,
        source: dict[str, Any],
        since: datetime | None,
    ) -> list[RawArticle]:
        source_id = source["id"]
        listing_url = source["url"]
        max_articles = int(source.get("max_articles", _DEFAULT_MAX_ARTICLES))

        # selector 字段：仅认 "auto"，其他值（含缺省）静默走默认路径
        _ = source.get("selector", "auto")

        if self._injected_client is not None:
            articles = await self._fetch_with_client(
                self._injected_client,
                source_id,
                listing_url,
                max_articles,
                since,
            )
        else:
            async with httpx.AsyncClient() as client:
                articles = await self._fetch_with_client(
                    client,
                    source_id,
                    listing_url,
                    max_articles,
                    since,
                )

        return articles

    async def _fetch_with_client(
        self,
        client: httpx.AsyncClient,
        source_id: str,
        listing_url: str,
        max_articles: int,
        since: datetime | None,
    ) -> list[RawArticle]:
        # ---- 第一级：列表页 → 子链接 ----
        article_urls = await self._fetch_listing(client, listing_url)
        if not article_urls:
            logger.debug(
                f"web adapter listing returned 0 links via trafilatura, "
                f"falling back to jina: {listing_url}"
            )
            article_urls = await self._fetch_listing_via_jina(
                client, listing_url
            )

        # 同一列表页内可能 nav + 内容区都有同一文章链接，canonicalize 后 set 去重
        article_urls = _dedupe_canonical(article_urls)

        # 截断到 max_articles，防首跑炸量
        article_urls = article_urls[:max_articles]

        # ---- 第二级：子文章 → RawArticle ----
        articles: list[RawArticle] = []
        for url in article_urls:
            try:
                # per-article timeout（s2 Step 5）：单篇文章最多 _PER_ARTICLE_TIMEOUT_SECONDS
                # 触发 asyncio.TimeoutError 后 skip 不传播，与 HTTP 异常 skip 行为一致
                article = await asyncio.wait_for(
                    self._fetch_article(client, source_id, url),
                    timeout=_PER_ARTICLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"web adapter skipping article {url}: "
                    f"per-article timeout > {_PER_ARTICLE_TIMEOUT_SECONDS}s"
                )
                continue
            except (
                httpx.HTTPStatusError,
                TransientHTTPError,
                httpx.TimeoutException,
                httpx.NetworkError,
            ) as exc:
                logger.warning(
                    f"web adapter skipping article {url}: {exc!r}"
                )
                continue
            if article is None:
                continue

            if since is not None and article.published_at is not None:
                if article.published_at < since:
                    continue
            # published_at is None → 放行（与其他 adapter 一致）

            articles.append(article)

        return articles

    # ---- 第一级：列表页 ----------------------------------------------------

    async def _fetch_listing(
        self, client: httpx.AsyncClient, listing_url: str
    ) -> list[str]:
        """主路径：HTTP GET 列表页 HTML，启发式抽同域名子文章链接。"""
        try:
            html = await self._http_get(client, listing_url)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning(
                f"web adapter listing GET failed: {listing_url}: {exc!r}"
            )
            return []
        return _extract_article_links(html, listing_url)

    async def _fetch_listing_via_jina(
        self, client: httpx.AsyncClient, listing_url: str
    ) -> list[str]:
        """兜底：通过 r.jina.ai 取 markdown，正则抽链接。"""
        try:
            md = await self._http_get(client, _JINA_PREFIX + listing_url)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning(
                f"web adapter jina listing fallback failed: {listing_url}: "
                f"{exc!r}"
            )
            return []
        return _extract_article_links_from_markdown(md, listing_url)

    # ---- 第二级：子文章 ----------------------------------------------------

    async def _fetch_article(
        self,
        client: httpx.AsyncClient,
        source_id: str,
        article_url: str,
    ) -> RawArticle | None:
        """主路径 trafilatura；失败兜底 Jina。两路径都失败返回 None。"""
        result = await self._fetch_article_via_trafilatura(client, article_url)
        if result is None:
            result = await self._fetch_article_via_jina(client, article_url)
        if result is None:
            logger.warning(
                f"web adapter both trafilatura and jina failed: {article_url}"
            )
            return None

        title, body, published_at = result
        return RawArticle(
            source_type=self.source_type,
            source_id=source_id,
            external_id=canonicalize_url(article_url),
            url=article_url,
            title=title,
            body=body,
            published_at=published_at,
        )

    async def _fetch_article_via_trafilatura(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str, datetime | None] | None:
        """主路径：HTTP GET → trafilatura.extract / extract_metadata。

        失败判定（任一命中视为失败 → 返回 None 由上层兜底 Jina）：
        - HTTP 抓取异常
        - trafilatura.extract 返回 None
        - body 长度 < 200 字符（Anthropic 子页面正文一般 > 1000）
        """
        try:
            html = await self._http_get(client, url)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.debug(
                f"web adapter trafilatura GET failed for {url}: {exc!r}"
            )
            return None

        body = trafilatura.extract(
            html,
            output_format="txt",
            include_comments=False,
            include_tables=False,
        )
        if not body or len(body) < _TRAFILATURA_MIN_BODY_CHARS:
            return None

        metadata = trafilatura.extract_metadata(html)
        title = ""
        published_at: datetime | None = None
        if metadata is not None:
            title = (metadata.title or "") or ""
            published_at = _parse_iso_date(metadata.date)

        # 兜底：metadata 没给 title 时用 URL 末段
        if not title:
            title = url

        return title, body, published_at

    async def _fetch_article_via_jina(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str, datetime | None] | None:
        """兜底：r.jina.ai/{url} → markdown，标题取首个 H1，date 一般取不到。"""
        try:
            md = await self._http_get(client, _JINA_PREFIX + url)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.debug(f"web adapter jina GET failed for {url}: {exc!r}")
            return None
        if not md:
            return None

        match = _MARKDOWN_H1_RE.search(md)
        title = match.group(1).strip() if match else url
        return title, md, None

    # ---- changelog_page 模式（设计 §3.6） --------------------------------

    async def _fetch_changelog_page(
        self,
        source: dict[str, Any],
        since: datetime | None,
    ) -> list[RawArticle]:
        """三层兜底链 + 日期切段（设计 §3.6.2 / §3.6.3）。

        层 1：``markdown_url`` 配置 → 直拉原生 markdown
        层 2：层 1 失败或内容空 → ``r.jina.ai/{url}`` 兜底
        层 3：层 2 也空 → 抛 ``ValueError``，由 pipeline fail 分支兜住
        """
        source_id = source["id"]
        url = source["url"]
        markdown_url = source.get("markdown_url")

        markdown: str | None = None

        # 层 1：markdown_url
        if markdown_url:
            try:
                markdown_bytes = await self._changelog_get_bytes(markdown_url)
                candidate = markdown_bytes.decode("utf-8", errors="replace")
                if self._is_changelog_content_valid(candidate):
                    markdown = candidate
                else:
                    logger.info(
                        f"[changelog_page] {source_id} markdown_url 内容空/占位，落到 Jina"
                    )
            except (
                TransientHTTPError,
                httpx.NetworkError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                httpx.HTTPStatusError,
            ) as exc:
                logger.info(
                    f"[changelog_page] {source_id} markdown_url 失败，落到 Jina：{exc!r}"
                )

        # 层 2：Jina Reader
        if markdown is None:
            jina_url = f"{_JINA_PREFIX}{url}"
            try:
                jina_bytes = await self._changelog_get_bytes(jina_url)
            except (
                TransientHTTPError,
                httpx.NetworkError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                httpx.HTTPStatusError,
            ) as exc:
                # 层 2 失败 → 整体抛 ValueError 走 pipeline fail 分支
                raise ValueError(
                    f"changelog_page empty/SPA placeholder: {source_id} "
                    f"(jina fetch failed: {exc!r})"
                ) from exc

            candidate = jina_bytes.decode("utf-8", errors="replace")
            if not self._is_changelog_content_valid(candidate):
                # 层 3：两层都返回占位/过短
                raise ValueError(
                    f"changelog_page empty/SPA placeholder: {source_id} "
                    f"(content<{_CHANGELOG_MIN_BYTES}B or 'Loading...' only)"
                )
            markdown = candidate

        # 切段
        sections = self._split_changelog(markdown)
        if not sections:
            # 兜底：抓到了正文但没有任何符合日期标题格式的段 — 视为页面尚未发布或结构变更
            raise ValueError(
                f"changelog_page empty/no date headings matched: {source_id}"
            )

        articles: list[RawArticle] = []
        for date_str, published_at, body in sections:
            if since is not None and published_at is not None and published_at < since:
                continue

            title = self._extract_first_subheading(body) or f"Update {date_str}"

            if published_at is not None:
                external_id = f"{source_id}#{published_at:%Y-%m-%d}"
            else:
                # 解析失败兜底：按 body MD5 前 8 位区分，避免同源多个 unknown 互相覆盖
                digest = hashlib.md5(body.encode("utf-8")).hexdigest()[:8]
                external_id = f"{source_id}#unknown-{digest}"

            articles.append(
                RawArticle(
                    source_type=self.source_type,
                    source_id=source_id,
                    external_id=external_id,
                    url=url,  # 共享 base url，不拼 fragment（D1）
                    title=title,
                    body=body,
                    published_at=published_at,
                    is_long_form=None,
                    skip_url_dedup=True,  # D1：绕开第二层 url_canonical_hash 去重
                )
            )

        return articles

    async def _changelog_get_bytes(self, url: str) -> bytes:
        """changelog_page 路径下拉原始字节，与列表两级模式 client 生命周期一致。"""
        if self._injected_client is not None:
            return await self._http_get_bytes(self._injected_client, url)
        async with httpx.AsyncClient() as client:
            return await self._http_get_bytes(client, url)

    @staticmethod
    def _is_changelog_content_valid(text: str) -> bool:
        """长度 ≥ 500 字节 且非纯 ``Loading...`` 占位（设计 §3.6.2）。"""
        encoded = text.encode("utf-8")
        if len(encoded) < _CHANGELOG_MIN_BYTES:
            return False
        # 仅含 "Loading..." 占位（含大小写 / 周边空白）— 视为 SPA 未渲染
        if text.strip().lower() == _CHANGELOG_PLACEHOLDER_TOKEN:
            return False
        return True

    @staticmethod
    def _split_changelog(
        markdown: str,
    ) -> list[tuple[str, datetime | None, str]]:
        """按 H2/H3 级日期标题切段。返回 ``[(date_str, parsed_date, body), ...]``。"""
        matches = list(_DATE_HEADING_RE.finditer(markdown))
        sections: list[tuple[str, datetime | None, str]] = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            body = markdown[m.end():end].strip()
            date_str = m.group("date")
            published_at = WebAdapter._parse_section_date(date_str)
            sections.append((date_str, published_at, body))
        return sections

    @staticmethod
    def _parse_section_date(date_str: str) -> datetime | None:
        """三种格式：``2026-05-07`` / ``May 7, 2026`` / ``5/7/2026``。失败返 None。"""
        s = date_str.strip()

        # ISO
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        # M/D/Y
        try:
            dt = datetime.strptime(s, "%m/%d/%Y")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        # English long-form（含 ordinal 处理）
        cleaned = _ORDINAL_RE.sub(r"\1", s)
        for fmt in _ENGLISH_DATE_FORMATS:
            try:
                dt = datetime.strptime(cleaned, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_first_subheading(body: str) -> str | None:
        """段内第一个 ``##`` 或 ``###`` 标题；没有则回落到第一个整行 ``**Bold**``。

        Help Center 类信源（claude_product / claude_api / chatgpt）段内子标题
        全是单行 bold 而非 H2/H3，会让标题退化成 ``Update <date>``。
        优先级：H2/H3 > 整行 bold > None。
        """
        bold_fallback: str | None = None
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("### ") or stripped.startswith("## "):
                text = stripped.lstrip("#").strip().strip("*").strip()
                if text:
                    return text
            if (
                bold_fallback is None
                and stripped.startswith("**")
                and stripped.endswith("**")
                and len(stripped) >= 5
            ):
                inner = stripped.strip("*").strip()
                # 拒绝行内多 bold 段（如 ``**A** **B**``）；只接整行单一 bold
                if inner and "**" not in inner:
                    bold_fallback = inner
        return bold_fallback


# ---- helpers ---------------------------------------------------------------


def _extract_article_links(html: str, base_url: str) -> list[str]:
    """从 HTML 抽同域名 + path 段数 ≥ 2 的 ``<a href>``，按出现顺序保序去重。

    启发式策略（与设计 §3.5 / KNOWLEDGE-LOG #5 配套）：
    - 同域名（urlparse 后 netloc 相同）才保留
    - 去掉 fragment-only 链接（href 以 ``#`` 开头）
    - 相对路径用 urljoin 解析为绝对 URL
    - path 段数 ≥ 2 才算文章链接（``/news/claude-3-5`` ✓ / ``/news`` ✗ / ``/`` ✗）
    - canonicalize 后用 dict 保序去重
    """
    parser = _AnchorCollector()
    try:
        parser.feed(html)
    except Exception as exc:  # html.parser 在极端 malformed 输入上偶尔抛
        logger.debug(f"_AnchorCollector parse error: {exc!r}")
        return []

    base_netloc = urlparse(base_url).netloc.lower()
    seen: dict[str, str] = {}  # canonical -> raw absolute
    for href in parser.links:
        href = href.strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        parts = urlparse(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        if parts.netloc.lower() != base_netloc:
            continue
        # path 段数 ≥ 2
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            continue
        try:
            canonical = canonicalize_url(absolute)
        except Exception:  # pragma: no cover - canonicalize 极少失败
            continue
        if canonical in seen:
            continue
        seen[canonical] = absolute

    return list(seen.values())


def _extract_article_links_from_markdown(md: str, base_url: str) -> list[str]:
    """同 ``_extract_article_links`` 的 markdown 版本，过滤规则一致。"""
    base_netloc = urlparse(base_url).netloc.lower()
    seen: dict[str, str] = {}
    for _text, raw_href in _MARKDOWN_LINK_RE.findall(md):
        href = raw_href.strip().split()[0]  # 去掉 markdown title 段如 (url "t")
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        parts = urlparse(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        if parts.netloc.lower() != base_netloc:
            continue
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            continue
        try:
            canonical = canonicalize_url(absolute)
        except Exception:  # pragma: no cover
            continue
        if canonical in seen:
            continue
        seen[canonical] = absolute
    return list(seen.values())


def _dedupe_canonical(urls: list[str]) -> list[str]:
    """canonicalize 后保序去重。"""
    seen: dict[str, str] = {}
    for u in urls:
        try:
            c = canonicalize_url(u)
        except Exception:  # pragma: no cover
            continue
        if c in seen:
            continue
        seen[c] = u
    return list(seen.values())


def _parse_iso_date(value: str | None) -> datetime | None:
    """trafilatura.metadata.date 一般是 ``YYYY-MM-DD`` 或 ISO 全格式。

    解析失败返回 None；成功时按 UTC 解读（trafilatura 不返回时区信息）。
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    # 优先全 ISO；失败回落 date-only
    candidates = [raw, raw.replace("Z", "+00:00")]
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    # date-only YYYY-MM-DD
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
