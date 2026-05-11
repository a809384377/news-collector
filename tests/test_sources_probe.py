"""sources/_probe.py 测试：suggest_id 推断 + probe 主入口（离线 MockTransport）。"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx
import pytest

from news_collector.commands.sources._probe import (
    ProbeResult,
    probe,
    suggest_id,
)


def _run(coro):
    return asyncio.run(coro)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _route(routes: dict[str, tuple[int, str, str]]) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404, text="not found")
        status, ct, body = routes[url]
        return httpx.Response(status, text=body, headers={"content-type": ct})

    return handler


# ---------- fixtures ----------

ATOM_BODY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Simon Willison's Weblog</title>
  <link href="https://simonwillison.net/"/>
  <updated>2026-05-09T08:00:00Z</updated>
  <entry>
    <title>The first entry title</title>
    <link href="https://simonwillison.net/2026/May/9/post/"/>
    <id>tag:example.com,2026:1</id>
    <updated>2026-05-09T08:00:00Z</updated>
    <summary>summary text</summary>
  </entry>
</feed>
"""

RSS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample RSS</title>
    <item>
      <title>Hello RSS World</title>
      <link>https://example.com/post/1</link>
      <description>desc</description>
    </item>
  </channel>
</rss>
"""

HTML_BODY = """<!DOCTYPE html>
<html><head>
<title>Example Blog Homepage</title>
<meta name="description" content="ex"/>
</head><body>
<article><h1>Hi</h1><p>Lorem ipsum</p></article>
</body></html>
"""

HTML_BODY_NO_TITLE = """<!DOCTYPE html>
<html><body>
<article><h1>Hi</h1><p>some content</p></article>
</body></html>
"""


# ---------- suggest_id ----------


def test_suggest_id_anthropic_news() -> None:
    assert suggest_id("https://www.anthropic.com/news") == "anthropic_news"


def test_suggest_id_strips_atom_extension_and_stopwords() -> None:
    assert suggest_id("https://simonwillison.net/atom/everything/") == "simonwillison"


def test_suggest_id_github_releases_atom() -> None:
    sid = suggest_id("https://github.com/anthropics/sdk/releases.atom")
    assert sid == "github_anthropics_sdk_releases"


def test_suggest_id_simple_domain() -> None:
    assert suggest_id("https://example.com/") == "example"


def test_suggest_id_empty_returns_none() -> None:
    assert suggest_id("") is None


def test_suggest_id_no_scheme_returns_none() -> None:
    assert suggest_id("not-a-url") is None


def test_suggest_id_replaces_special_chars() -> None:
    sid = suggest_id("https://example.com/foo bar")
    assert sid == "example_foo_bar"


# ---------- probe（离线） ----------


def test_probe_atom_returns_type_rss_with_title() -> None:
    url = "https://example.com/feed.atom"
    client = _make_client(_route({url: (200, "application/atom+xml", ATOM_BODY)}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is True
    assert res.status_code == 200
    assert res.source_type == "rss"
    assert res.sample_title == "The first entry title"
    assert res.error is None
    assert res.suggested_id is not None  # 不强约束具体值


def test_probe_rss_xml_returns_type_rss_with_title() -> None:
    url = "https://example.com/feed.rss"
    client = _make_client(_route({url: (200, "application/rss+xml", RSS_BODY)}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is True
    assert res.source_type == "rss"
    assert res.sample_title == "Hello RSS World"


def test_probe_xml_content_type_falls_through_to_rss() -> None:
    """content-type 含 xml 但 body 不像 feed → 仍判 rss（嗅探优先级）。"""
    url = "https://example.com/feed.xml"
    client = _make_client(
        _route({url: (200, "text/xml", "<?xml version='1.0'?><root/>")})
    )

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.source_type == "rss"
    # feedparser 解不出 entries → sample_title 为 None
    assert res.sample_title is None


def test_probe_html_returns_type_web_with_title() -> None:
    url = "https://example.com/blog"
    client = _make_client(_route({url: (200, "text/html; charset=utf-8", HTML_BODY)}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is True
    assert res.source_type == "web"
    assert res.sample_title == "Example Blog Homepage"


def test_probe_html_no_title_falls_back_to_trafilatura() -> None:
    """无 ``<title>`` 时走 trafilatura 兜底；H1 ``Hi`` 会被抽出。"""
    url = "https://example.com/blog"
    client = _make_client(
        _route({url: (200, "text/html; charset=utf-8", HTML_BODY_NO_TITLE)})
    )

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is True
    assert res.source_type == "web"
    # trafilatura 兜底抽到 H1
    assert res.sample_title == "Hi"


def test_probe_404_marks_unreachable() -> None:
    url = "https://example.com/nope"
    client = _make_client(_route({url: (404, "text/html", "not found")}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is False
    assert res.status_code == 404
    assert res.source_type is None
    assert res.sample_title is None
    assert res.error and "404" in res.error


def test_probe_500_marks_unreachable() -> None:
    url = "https://example.com/err"
    client = _make_client(_route({url: (500, "text/html", "server error")}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is False
    assert res.status_code == 500


def test_probe_network_error_marks_unreachable() -> None:
    """MockTransport 抛 ConnectError → 走 httpx.HTTPError 分支。"""
    url = "https://example.com/x"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    client = _make_client(handler)

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert res.reachable is False
    assert res.status_code is None
    assert res.error and "ConnectError" in res.error


def test_probe_returns_dataclass_instance() -> None:
    url = "https://example.com/blog"
    client = _make_client(_route({url: (200, "text/html", HTML_BODY)}))

    async def go():
        try:
            return await probe(url, client=client)
        finally:
            await client.aclose()

    res = _run(go())
    assert isinstance(res, ProbeResult)
    # frozen dataclass：尝试改字段抛 FrozenInstanceError
    with pytest.raises(Exception):
        res.url = "other"  # type: ignore[misc]
