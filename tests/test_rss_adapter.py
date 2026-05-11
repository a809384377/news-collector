"""RSSAdapter 单元测试 — 全部使用本地 fixture，不联网。

覆盖：
1. 标准 RSS 2.0 fixture 解析
2. Atom fixture body 取 content[0].value
3. since 在未来 → 空列表
4. since 切分混合时间条目
5. 无 pubDate 的 entry → published_at=None 且不丢失
6. User-Agent 头部传递验证
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from newsbox.adapters.rss_adapter import RSSAdapter

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rss"


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
