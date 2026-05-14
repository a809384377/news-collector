"""RSSAdapter 单元测试 — 全部使用本地 fixture，不联网。

覆盖：
1. 标准 RSS 2.0 fixture 解析
2. Atom fixture body 取 content[0].value
3. since 在未来 → 空列表
4. since 切分混合时间条目
5. 无 pubDate 的 entry → published_at=None 且不丢失
6. User-Agent 头部传递验证
7. s13 reddit 富化：URL host 检测 / body 改写 / 失败兜底 / disabled 时零侵入
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from newsbox.adapters.rss_adapter import RSSAdapter

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rss"
REDDIT_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reddit"


# ---- helpers ---------------------------------------------------------------


def _stub_client(content: bytes, status_code: int = 200) -> MagicMock:
    """造一个 AsyncClient mock，调用 .get(...) 返回伪 Response。"""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = content
    response.request = MagicMock()
    response.request.url = "https://example.com/feed"
    response.reason_phrase = "OK"
    response.raise_for_status = MagicMock()

    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response)
    return client


def _read_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _run(coro):
    return asyncio.run(coro)


# ---- tests -----------------------------------------------------------------


def test_parses_anthropic_blog_fixture() -> None:
    client = _stub_client(_read_fixture("anthropic_blog.xml"))
    adapter = RSSAdapter(http_client=client)

    source = {
        "id": "anthropic_blog",
        "url": "https://www.anthropic.com/rss.xml",
        "tier": "official_first_party",
    }

    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) >= 3, f"expected >= 3 articles, got {len(articles)}"
    for art in articles:
        assert art.source_type == "rss"
        assert art.source_id == "anthropic_blog"
        assert art.external_id, "external_id must be non-empty"
        assert art.url, "url must be non-empty"

    assert any(art.published_at is not None for art in articles), (
        "至少 1 条应该有 published_at"
    )


def test_parses_atom_format_fixture() -> None:
    client = _stub_client(_read_fixture("simon_atom.xml"))
    adapter = RSSAdapter(http_client=client)

    source = {
        "id": "simon_willison",
        "url": "https://simonwillison.net/atom/everything/",
    }

    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) >= 2
    # 第一条同时有 summary 和 content，body 应该来自 content[0].value
    first = articles[0]
    assert "Full content body" in first.body, (
        f"body 应来自 content[0].value，got: {first.body!r}"
    )
    assert "Short summary" not in first.body


def test_since_filter_in_future_returns_empty() -> None:
    client = _stub_client(_read_fixture("anthropic_blog.xml"))
    adapter = RSSAdapter(http_client=client)
    source = {"id": "anthropic_blog", "url": "https://example.com/feed"}

    far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
    articles = _run(adapter.fetch(source, since=far_future))

    assert articles == []


def test_since_filter_keeps_articles_after_cutoff() -> None:
    client = _stub_client(_read_fixture("anthropic_blog.xml"))
    adapter = RSSAdapter(http_client=client)
    source = {"id": "anthropic_blog", "url": "https://example.com/feed"}

    # fixture 中 2026-04-15 / 2026-04-20 / 2026-05-01 / 2025-10-15
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    articles = _run(adapter.fetch(source, since=cutoff))

    # 2025 那条应该被切掉
    assert len(articles) == 2, (
        f"expected 2 (after 2026-01-01), got {len(articles)}: "
        f"{[(a.title, a.published_at) for a in articles]}"
    )
    for art in articles:
        assert art.published_at is not None
        assert art.published_at >= cutoff


def test_entry_without_published_is_kept_with_none() -> None:
    feed_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>NoDate Feed</title>
    <link>https://example.com/</link>
    <description>desc</description>
    <item>
      <title>Entry without pubDate</title>
      <link>https://example.com/post-1</link>
      <guid>https://example.com/post-1</guid>
      <description>body without date.</description>
    </item>
  </channel>
</rss>
"""
    client = _stub_client(feed_bytes)
    adapter = RSSAdapter(http_client=client)
    source = {"id": "nodate", "url": "https://example.com/feed"}

    # 即使有 since，缺 published_at 的也应该被放行
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    articles = _run(adapter.fetch(source, since=since))

    assert len(articles) == 1
    assert articles[0].published_at is None
    assert articles[0].external_id == "https://example.com/post-1"
    assert articles[0].title == "Entry without pubDate"


# ---- s13 reddit 富化集成 ---------------------------------------------------


_REDDIT_RSS_TEMPLATE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>r/LocalLLaMA</title>
    <link>https://www.reddit.com/r/LocalLLaMA/</link>
    <description>desc</description>
    <item>
      <title>new MoE from ai2, EMO</title>
      <link>https://www.reddit.com/r/LocalLLaMA/comments/1t7kgy4/new_moe_from_ai2_emo/</link>
      <guid>t3_1t7kgy4</guid>
      <description>&lt;table&gt;&lt;tr&gt;&lt;td&gt;link post template&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</description>
      <pubDate>Sat, 10 May 2026 12:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""


def _mock_transport_client(feed_bytes: bytes, json_bytes: bytes | None, *, json_status: int = 200) -> httpx.AsyncClient:
    """同一 client 根据 path 路由：含 .json 走富化 fixture；否则走 feed bytes。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if ".json" in request.url.path:
            return httpx.Response(
                json_status,
                content=json_bytes or b'{"error":"x"}',
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=feed_bytes)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_reddit_url_enrich_success_rewrites_body() -> None:
    """reddit URL 富化成功：body 重写为元信息块+selftext，enrichment 挂载。"""
    json_bytes = (REDDIT_FIXTURE_DIR / "r_localllama_post_with_comments.json").read_bytes()

    async def _go() -> list:
        client = _mock_transport_client(_REDDIT_RSS_TEMPLATE, json_bytes)
        try:
            adapter = RSSAdapter(
                http_client=client,
                reddit_enrich_rate_seconds=0.0,  # 测试加速
            )
            return await adapter.fetch({"id": "r_localllama", "url": "https://example.com/feed"}, since=None)
        finally:
            await client.aclose()

    articles = _run(_go())
    assert len(articles) == 1
    art = articles[0]

    # body 重写：元信息引用块出现，原 RSS table 模板不再
    assert "**r/LocalLLaMA**" in art.body
    assert "score=" in art.body
    assert "comments" in art.body
    assert "flair: New Model" in art.body
    assert "<table>" not in art.body, f"原 RSS 模板应被丢弃，got body: {art.body[:200]}"

    # enrichment 挂载
    assert art.enrichment is not None
    assert art.enrichment.name.startswith("t3_")
    assert art.enrichment.subreddit == "LocalLLaMA"
    assert len(art.enrichment.top_comments) >= 1


def test_reddit_url_enrich_failure_falls_back_to_original_body() -> None:
    """富化 5xx 时回落原 RSS body，enrichment=None，不阻塞主流程。"""
    async def _go() -> list:
        client = _mock_transport_client(_REDDIT_RSS_TEMPLATE, b"err", json_status=503)
        try:
            adapter = RSSAdapter(
                http_client=client,
                reddit_enrich_rate_seconds=0.0,
            )
            return await adapter.fetch({"id": "r_localllama", "url": "https://example.com/feed"}, since=None)
        finally:
            await client.aclose()

    articles = _run(_go())
    assert len(articles) == 1
    art = articles[0]
    assert art.enrichment is None
    assert "<table>" in art.body, "富化失败应保留原 RSS body"


def test_reddit_url_with_enrich_disabled_zero_invasion() -> None:
    """reddit_enrich_enabled=False 时 reddit URL 也不调富化（兼容场景）。"""
    client = _stub_client(_REDDIT_RSS_TEMPLATE)
    adapter = RSSAdapter(http_client=client, reddit_enrich_enabled=False)

    articles = _run(adapter.fetch({"id": "r_localllama", "url": "https://example.com/feed"}, since=None))
    assert len(articles) == 1
    assert articles[0].enrichment is None
    assert "<table>" in articles[0].body
    # client.get 应只调一次（拉 feed，无富化请求）
    assert client.get.await_count == 1


def test_non_reddit_url_does_not_trigger_enrich() -> None:
    """非 reddit URL 即便 enrich_enabled=True 也不触发富化，client.get 只调一次。"""
    client = _stub_client(_read_fixture("anthropic_blog.xml"))
    adapter = RSSAdapter(http_client=client)  # 默认 enabled=True

    _run(adapter.fetch({"id": "anthropic_blog", "url": "https://www.anthropic.com/rss.xml"}, since=None))
    # anthropic_blog fixture 没有 reddit.com URL → 不应该有富化 GET
    assert client.get.await_count == 1, (
        f"非 reddit URL 应只拉 feed 一次，got {client.get.await_count} calls"
    )


def test_user_agent_is_set() -> None:
    client = _stub_client(_read_fixture("anthropic_blog.xml"))
    adapter = RSSAdapter(http_client=client)
    source = {"id": "anthropic_blog", "url": "https://example.com/feed"}

    _run(adapter.fetch(source, since=None))

    # client.get 应该被至少调用一次，且 headers 含 User-Agent
    assert client.get.await_count >= 1
    call = client.get.await_args
    # call.args 是位置参数 (url,)；call.kwargs 含 headers
    assert call is not None
    headers = call.kwargs.get("headers", {})
    assert headers.get("User-Agent") == "newsbox/0.1.0", (
        f"expected User-Agent=newsbox/0.1.0, got headers={headers!r}"
    )
    # 同时验证 url 与 follow_redirects
    assert call.kwargs.get("follow_redirects") is True
    # url 通过位置或关键字传都行
    url_passed = call.args[0] if call.args else call.kwargs.get("url")
    assert url_passed == "https://example.com/feed"
