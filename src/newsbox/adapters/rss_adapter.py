"""RSS / Atom 信源适配器（设计 §3.2 / §3.7）。

直接用 httpx 拉 feed bytes，喂给 feedparser 解析。RSS 2.0 与 Atom 共用同一套
``entry`` 字典视图，feedparser 已抹平差异，所以本适配器对两种格式无感。

不接 sqlite，不去重 — 只把 feed entry 映射成 ``RawArticle``，由上层
``pipeline/fetch.py`` 负责入库 / canonical_hash / fetched_at / status。

s13: reddit URL 在 entry 解析后追加一次 `<url>.json` 富化请求拿回评论 + score / flair
等互动信号；body 被重写为 markdown 元信息块 + selftext；失败回落原 RSS body。
详见 ``adapters/reddit_enrich.py`` 与 ai/sprints/active/s13-reddit-comments-enrich/。
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from loguru import logger

from ..models import RawArticle
from .base import (
    TransientHTTPError,  # noqa: F401  re-export for callers; useful in tests
    raise_for_transient_status,
    with_retry,
)
from .reddit_enrich import (
    RedditEnrichError,
    enrich_reddit_post,
    format_body as format_reddit_body,
    is_reddit_url,
)

_USER_AGENT = "newsbox/0.1.0"
_TIMEOUT_SECONDS = 15.0


class RSSAdapter:
    """RSS / Atom 适配器。

    ``source`` 字典最少包含 ``id`` 与 ``url``；其他字段（如 ``tier``）由 pipeline 使用。

    s13 引入 reddit 富化集成；非 reddit URL 路径零侵入。可通过构造参数关闭富化用于测试。
    """

    source_type: str = "rss"

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        reddit_enrich_enabled: bool = True,
        reddit_enrich_rate_seconds: float = 6.0,
        reddit_top_comments: int = 5,
    ) -> None:
        # 允许调用方注入 client（便于测试 mock）；缺省时由调用方负责生命周期，
        # 这里用一个独立 client 实例并在 fetch 内复用。
        self._injected_client = http_client
        self._reddit_enrich_enabled = reddit_enrich_enabled
        self._reddit_enrich_rate_seconds = reddit_enrich_rate_seconds
        self._reddit_top_comments = reddit_top_comments

    # ---- HTTP --------------------------------------------------------------

    @with_retry()
    async def _http_get(self, client: httpx.AsyncClient, url: str) -> bytes:
        """拉取 feed 原始字节。命中 transient 状态由装饰器重试，其他 4xx/5xx 立刻抛。"""
        response = await client.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
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
        if self._injected_client is not None:
            return await self._fetch_with_client(source, since, self._injected_client)
        async with httpx.AsyncClient() as client:
            return await self._fetch_with_client(source, since, client)

    async def _fetch_with_client(
        self,
        source: dict[str, Any],
        since: datetime | None,
        client: httpx.AsyncClient,
    ) -> list[RawArticle]:
        source_id = source["id"]
        url = source["url"]

        content = await self._http_get(client, url)

        parsed = feedparser.parse(content)
        if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
            # 仅记录，不抛 — feedparser 对很多边角格式仍能解析出 entries
            logger.debug(
                f"feedparser bozo for source {source_id}: {parsed.bozo_exception!r}"
            )

        articles: list[RawArticle] = []
        for entry in parsed.entries:
            try:
                article = self._entry_to_article(source_id, entry)
            except ValueError as exc:
                logger.warning(f"skipping malformed entry from {source_id}: {exc}")
                continue

            if since is not None and article.published_at is not None:
                if article.published_at < since:
                    continue
            # published_at is None → 放行（由 pipeline 决定如何记账）

            # s13: reddit 富化分支（host 检测 + 失败兜底）
            if self._reddit_enrich_enabled and is_reddit_url(article.url):
                article = await self._try_reddit_enrich(article, client, source_id)

            articles.append(article)

        return articles

    # ---- reddit 富化 (s13) --------------------------------------------------

    async def _try_reddit_enrich(
        self,
        article: RawArticle,
        client: httpx.AsyncClient,
        source_id: str,
    ) -> RawArticle:
        """对 reddit permalink 调富化 API，成功 → 重写 body + 挂 enrichment；失败 → 回落原 article。"""
        try:
            enrichment = await enrich_reddit_post(
                article.url, client, top_n=self._reddit_top_comments
            )
        except RedditEnrichError as exc:
            logger.warning(
                f"reddit enrich failed for {source_id}:{article.external_id} "
                f"({article.url}): {exc}; falling back to RSS body"
            )
            return article

        new_body = format_reddit_body(enrichment)

        # 限速：富化成功后等待，再处理下一条 entry（D7：默认 6s ≈ 10 QPM 上限留 buffer）
        if self._reddit_enrich_rate_seconds > 0:
            await asyncio.sleep(self._reddit_enrich_rate_seconds)

        return dataclasses.replace(article, body=new_body, enrichment=enrichment)

    # ---- 字段映射 -----------------------------------------------------------

    def _entry_to_article(self, source_id: str, entry: Any) -> RawArticle:
        external_id = entry.get("id") or entry.get("link")
        if not external_id:
            raise ValueError("entry has neither id nor link")

        url = entry.get("link", "")
        title = entry.get("title", "")
        body = self._extract_body(entry)
        published_at = self._extract_published_at(entry)

        return RawArticle(
            source_type=self.source_type,
            source_id=source_id,
            external_id=external_id,
            url=url,
            title=title,
            body=body,
            published_at=published_at,
        )

    @staticmethod
    def _extract_body(entry: Any) -> str:
        # content[0].value（Atom）优先；否则 summary（RSS description / Atom summary）
        content = entry.get("content")
        if content:
            try:
                value = content[0].get("value", "")
            except (AttributeError, IndexError, TypeError):
                value = ""
            if value:
                return value
        return entry.get("summary", "") or ""

    @staticmethod
    def _extract_published_at(entry: Any) -> datetime | None:
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t is not None:
                # feedparser 给的是 time.struct_time；按 UTC 解读
                try:
                    return datetime(*t[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
        return None
