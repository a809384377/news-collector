"""WebAdapter 单元测试 — 全部使用本地 fixture，不联网。

覆盖：
1. 列表页 → 子链接抽取（同域名 + path 段数 ≥ 2 启发式）
2. 子链接 canonicalize 后跨调用去重
3. trafilatura 主路径成功 → RawArticle 字段映射
4. trafilatura body 过短 → Jina 兜底取 markdown
5. 列表页 trafilatura 抽链接为 0 → Jina markdown 兜底
6. since 过滤（保留 published_at >= since 与 published_at = None 的）
7. max_articles 截断（默认 / 显式）
8. 同 listing 内重复链接去重后只产出 1 条 RawArticle

HTTP 走 httpx.MockTransport，全离线。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

from newsbox.adapters.web_adapter import (
    _PER_ARTICLE_TIMEOUT_SECONDS,
    _TIMEOUT_SECONDS,
    WebAdapter,
    _extract_article_links,
    _extract_article_links_from_markdown,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "web"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


# ---- transport helpers -----------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """构造一个 httpx.AsyncClient，所有请求走 MockTransport(handler)。"""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _route_table(routes: dict[str, tuple[int, str, str]]) -> Callable:
    """routes: {url -> (status, content_type, body)}。

    handler 按 request.url 字符串精确匹配；缺失时返回 404。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404, text=f"no route for {url}")
        status, content_type, body = routes[url]
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": content_type},
        )

    return handler


# ---- 1. 列表页解析子链接 ---------------------------------------------------


def test_extract_article_links_filters_nav_and_short_paths() -> None:
    html = _read("anthropic_news_listing.html")
    links = _extract_article_links(html, "https://www.anthropic.com/news")

    # nav/footer 中的 / /about /careers /legal 都被启发式（path 段数 ≥ 2）过滤
    # fragment-only #section-top 被过滤；twitter.com 跨域被过滤
    # 6 unique 文章链接：sonnet/haiku/computer-use/responsible-scaling/enterprise/safety-evals
    assert 5 <= len(links) <= 8, f"expected 5-8, got {len(links)}: {links}"
    for u in links:
        assert "anthropic.com" in u
        assert "/about" not in u
        assert "/careers" not in u
        assert "twitter" not in u

    # 所有链接 path 段数 >= 2
    from urllib.parse import urlparse

    for u in links:
        segs = [s for s in urlparse(u).path.split("/") if s]
        assert len(segs) >= 2, f"path too short: {u}"


# ---- 2. 子链接 canonicalize 后跨调用去重 -----------------------------------


def test_extract_article_links_dedupes_canonical() -> None:
    """anthropic_news_listing.html 中故意把 claude-3-5-sonnet 写了两次，应只出现一次。"""
    html = _read("anthropic_news_listing.html")
    links = _extract_article_links(html, "https://www.anthropic.com/news")

    sonnet_count = sum(1 for u in links if "claude-3-5-sonnet" in u)
    assert sonnet_count == 1, (
        f"expected 1 occurrence of claude-3-5-sonnet, got {sonnet_count}: "
        f"{links}"
    )

    computer_count = sum(1 for u in links if "computer-use-2026" in u)
    assert computer_count == 1, (
        f"expected 1 occurrence of computer-use-2026, got {computer_count}"
    )


# ---- 3. trafilatura 主路径成功 ---------------------------------------------


def test_trafilatura_primary_path_success() -> None:
    listing_html = _read("anthropic_news_listing.html")
    art1_html = _read("anthropic_article_1.html")

    routes = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    # 所有 anthropic.com/news/* 子页都用 article_1 fixture（body > 200 chars）
    listing_links = _extract_article_links(
        listing_html, "https://www.anthropic.com/news"
    )
    for link in listing_links:
        routes[link] = (200, "text/html", art1_html)

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
        "selector": "auto",
        "tier": "official_first_party",
    }
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == len(listing_links)
    for art in articles:
        assert art.source_type == "web"
        assert art.source_id == "anthropic_news"
        assert art.title  # trafilatura.metadata.title 非空
        assert art.body and len(art.body) >= 200
        assert art.published_at is not None
        assert art.published_at.tzinfo is not None
        # external_id 是 canonicalize 过的 url
        assert art.external_id.startswith("https://www.anthropic.com/news/")
        # url 保留原样未规范化
        assert art.url.startswith("https://www.anthropic.com/news/")


# ---- 4. trafilatura body 过短 → Jina 兜底 ---------------------------------


def test_trafilatura_short_body_falls_back_to_jina() -> None:
    listing_html = _read("anthropic_news_listing.html")
    short_html = _read("anthropic_article_short.html")
    jina_md = _read("jina_article.md")

    listing_links = _extract_article_links(
        listing_html, "https://www.anthropic.com/news"
    )

    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    for link in listing_links:
        # 子页第一跳：HTML 全是 nav/SPA 占位 → trafilatura 返回 short body
        routes[link] = (200, "text/html", short_html)
        # 子页第二跳：r.jina.ai/{url}
        routes[f"https://r.jina.ai/{link}"] = (200, "text/markdown", jina_md)

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
        "selector": "auto",
    }
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == len(listing_links)
    for art in articles:
        # body 来自 Jina markdown 而非 trafilatura（含 markdown 标记 ``# Introducing``）
        assert "# Introducing Claude 3.5 Sonnet" in art.body
        # title 取自 markdown 首个 H1
        assert art.title == "Introducing Claude 3.5 Sonnet"
        # Jina 路径 published_at = None
        assert art.published_at is None


# ---- 5. listing 走 Jina 兜底 -----------------------------------------------


def test_listing_fallback_to_jina_when_html_yields_zero_links() -> None:
    """列表页 HTML 中没有任何 path 段数 ≥ 2 的同域名链接 → 走 Jina markdown。"""
    spa_listing_html = """<html><body>
        <nav><a href="/">Home</a><a href="/about">About</a></nav>
        <div id="app"></div>
    </body></html>"""
    jina_md = _read("jina_listing.md")
    art1_html = _read("anthropic_article_1.html")

    # Jina markdown 中提到的子文章 URL
    jina_links = _extract_article_links_from_markdown(
        jina_md, "https://www.anthropic.com/news"
    )
    assert len(jina_links) >= 5, f"jina fixture should have >= 5 links: {jina_links}"

    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", spa_listing_html),
        "https://r.jina.ai/https://www.anthropic.com/news": (
            200,
            "text/markdown",
            jina_md,
        ),
    }
    for link in jina_links:
        routes[link] = (200, "text/html", art1_html)

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
        "selector": "auto",
    }
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == len(jina_links)
    assert all(a.source_type == "web" for a in articles)


# ---- 6. since 过滤 ---------------------------------------------------------


def test_since_filter_keeps_recent_and_drops_older() -> None:
    """anthropic_article_1.html date=2026-05-01；article_2.html date=2025-11-15。"""
    listing_html = """<html><body>
        <a href="/news/recent-a">A</a>
        <a href="/news/recent-b">B</a>
        <a href="/news/older-c">C</a>
    </body></html>"""
    art1 = _read("anthropic_article_1.html")  # 2026-05-01
    art2 = _read("anthropic_article_2.html")  # 2025-11-15

    routes = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
        "https://www.anthropic.com/news/recent-a": (200, "text/html", art1),
        "https://www.anthropic.com/news/recent-b": (200, "text/html", art1),
        "https://www.anthropic.com/news/older-c": (200, "text/html", art2),
    }
    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    articles = _run(adapter.fetch(source, since=since))

    # recent-a / recent-b 通过；older-c 被过滤
    assert len(articles) == 2
    urls = {a.url for a in articles}
    assert urls == {
        "https://www.anthropic.com/news/recent-a",
        "https://www.anthropic.com/news/recent-b",
    }


# ---- 7. max_articles 截断 --------------------------------------------------


def test_max_articles_truncates_listing() -> None:
    """列表页含 50 个链接，max_articles=10 → 只抓前 10 个 RawArticle。"""
    items = "\n".join(
        f'<a href="/news/post-{i:02d}">post {i}</a>' for i in range(50)
    )
    listing_html = f"<html><body>{items}</body></html>"
    art1 = _read("anthropic_article_1.html")

    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    for i in range(50):
        routes[f"https://www.anthropic.com/news/post-{i:02d}"] = (
            200,
            "text/html",
            art1,
        )

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
        "max_articles": 10,
    }
    articles = _run(adapter.fetch(source, since=None))
    assert len(articles) == 10
    # 顺序保留
    seen_indices = [int(a.url.rsplit("-", 1)[-1]) for a in articles]
    assert seen_indices == list(range(10))


def test_default_max_articles_is_30() -> None:
    items = "\n".join(
        f'<a href="/news/p{i:03d}">post {i}</a>' for i in range(50)
    )
    listing_html = f"<html><body>{items}</body></html>"
    art1 = _read("anthropic_article_1.html")
    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    for i in range(50):
        routes[f"https://www.anthropic.com/news/p{i:03d}"] = (
            200,
            "text/html",
            art1,
        )

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    articles = _run(adapter.fetch(source, since=None))
    assert len(articles) == 30


# ---- 8. 跨调用去重（listing 含重复链接） -----------------------------------


def test_duplicate_links_in_listing_produce_single_article() -> None:
    """listing 含 5 个链接其中 2 对重复（实质 4 unique）→ 4 条 RawArticle。"""
    listing_html = """<html><body>
        <a href="/news/post-a">A</a>
        <a href="/news/post-b">B</a>
        <a href="/news/post-a">A again</a>
        <a href="/news/post-c">C</a>
        <a href="/news/post-d">D</a>
        <a href="/news/post-d">D again</a>
    </body></html>"""
    art1 = _read("anthropic_article_1.html")
    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    for slug in ("post-a", "post-b", "post-c", "post-d"):
        routes[f"https://www.anthropic.com/news/{slug}"] = (
            200,
            "text/html",
            art1,
        )

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 4
    slugs = {a.url.rsplit("/", 1)[-1] for a in articles}
    assert slugs == {"post-a", "post-b", "post-c", "post-d"}


# ---- 额外：UA / selector 容错 ----------------------------------------------


def test_unknown_selector_value_does_not_raise() -> None:
    """selector 字段不为 ``auto`` 时静默走默认路径，不报错。"""
    listing_html = """<html><body>
        <a href="/news/x-1">x1</a>
        <a href="/news/x-2">x2</a>
    </body></html>"""
    art1 = _read("anthropic_article_1.html")
    routes = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
        "https://www.anthropic.com/news/x-1": (200, "text/html", art1),
        "https://www.anthropic.com/news/x-2": (200, "text/html", art1),
    }
    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
        "selector": "css:.post-card",  # 故意非 auto
    }
    articles = _run(adapter.fetch(source, since=None))
    assert len(articles) == 2


def test_published_none_passes_through_since_filter() -> None:
    """trafilatura 解析不出 date 时，published_at=None；since 过滤应放行。"""
    # 自造一个无 date 的 article HTML，但正文足够长
    long_para = "Detailed coverage of new model capabilities. " * 30
    no_date_html = f"""<html><body><article>
        <h1>Untitled but long</h1>
        <p>{long_para}</p>
    </article></body></html>"""
    listing_html = """<html><body>
        <a href="/news/x-1">x1</a>
    </body></html>"""
    routes = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
        "https://www.anthropic.com/news/x-1": (200, "text/html", no_date_html),
    }
    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)
    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    articles = _run(adapter.fetch(source, since=since))
    # published_at = None 应放行
    assert len(articles) == 1
    assert articles[0].published_at is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.anthropic.com/news/post", True),
        ("https://www.anthropic.com/", False),
        ("https://www.anthropic.com/about", False),
        ("https://other.com/news/post", False),
    ],
)
def test_link_filter_predicates(url: str, expected: bool) -> None:
    html = f'<html><body><a href="{url}">x</a></body></html>'
    out = _extract_article_links(html, "https://www.anthropic.com/news")
    assert (len(out) == 1) is expected


# ---- 9. 超时常量值（s2 Step 5 防御性加固）-----------------------------------


def test_timeout_constants_have_expected_values() -> None:
    """锁住 s2 Step 5 引入的两个超时常量值。

    - `_TIMEOUT_SECONDS=12.0`：列表两级模式单次 HTTP GET timeout（从 20s 降下来）
    - `_PER_ARTICLE_TIMEOUT_SECONDS=24.0`：单篇子文章端到端时间上限
    """
    assert _TIMEOUT_SECONDS == 12.0
    assert _PER_ARTICLE_TIMEOUT_SECONDS == 24.0


# ---- 10. per-article timeout 触发后 skip 不传播 -----------------------------


def test_per_article_timeout_skips_one_does_not_kill_listing() -> None:
    """列表 3 篇：第 1/3 正常返回，第 2 触发 per-article timeout → 应得 2 条。

    实现细节：
    - mock 三个子页面的 `_fetch_article`（直接 monkey-patch 实例方法），
      第 2 个 URL 抛 `asyncio.TimeoutError`（asyncio.wait_for 的等价异常），
      第 1 / 3 正常返回 RawArticle
    - 不真 sleep；通过抛异常模拟 timeout 触发，避免测试本身耗时 24s
    - 验证：2 条 RawArticle（不是 0 也不是 3），第 2 篇 URL 缺席
    """
    listing_html = """<html><body>
        <a href="/news/post-1">1</a>
        <a href="/news/post-2">2</a>
        <a href="/news/post-3">3</a>
    </body></html>"""
    art1 = _read("anthropic_article_1.html")
    routes: dict[str, tuple[int, str, str]] = {
        "https://www.anthropic.com/news": (200, "text/html", listing_html),
    }
    for slug in ("post-1", "post-2", "post-3"):
        routes[f"https://www.anthropic.com/news/{slug}"] = (
            200,
            "text/html",
            art1,
        )

    client = _make_client(_route_table(routes))
    adapter = WebAdapter(http_client=client)

    # 用 monkey-patch 替换 _fetch_article：第 2 个 URL 抛 asyncio.TimeoutError
    # （asyncio.wait_for 超时时抛此异常；用直接抛而非 sleep 避免真等 24s）
    original_fetch_article = adapter._fetch_article

    async def fake_fetch_article(client_, source_id, url):  # type: ignore[no-untyped-def]
        if url.endswith("/post-2"):
            raise asyncio.TimeoutError("simulated per-article timeout")
        return await original_fetch_article(client_, source_id, url)

    adapter._fetch_article = fake_fetch_article  # type: ignore[assignment,method-assign]

    source = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 2, (
        f"expected 2 articles (post-2 skipped), got {len(articles)}: "
        f"{[a.url for a in articles]}"
    )
    urls = {a.url for a in articles}
    assert urls == {
        "https://www.anthropic.com/news/post-1",
        "https://www.anthropic.com/news/post-3",
    }
    # post-2 没有出现
    assert all("post-2" not in a.url for a in articles)
