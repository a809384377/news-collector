"""WebAdapter changelog_page 子模式单元测试（设计 §3.6）。

覆盖三层兜底链 / 日期切段 / 字段填充 / since 过滤；不联网，HTTP 走 mock。

历史：S2-1 Step 4 之前为 OfficialAdapter 的 changelog_page 子模式，
现合并进 WebAdapter（按 ``mode: changelog_page`` 分派）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from news_collector.adapters.web_adapter import WebAdapter

CHANGELOG_DIR = Path(__file__).parent / "fixtures" / "web" / "changelog"


# ---- 测试辅助 ---------------------------------------------------------------


def _make_response(content: bytes, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = content
    response.request = MagicMock()
    response.request.url = "https://example.invalid/page"
    response.reason_phrase = "OK" if status_code < 400 else "ERR"

    def _raise_for_status() -> None:
        if 400 <= status_code < 600:
            raise httpx.HTTPStatusError(
                f"{status_code}",
                request=response.request,
                response=response,
            )

    response.raise_for_status = MagicMock(side_effect=_raise_for_status)
    return response


def _client_with_url_map(url_to_response: dict[str, MagicMock]) -> httpx.AsyncClient:
    """根据 URL 路由到不同 mock response。未配置的 URL 抛 AssertionError。"""
    client = MagicMock(spec=httpx.AsyncClient)

    async def _get(url: str, **_kwargs: object) -> MagicMock:
        for key, resp in url_to_response.items():
            if url == key:
                return resp
        raise AssertionError(f"unexpected GET to {url!r}; configured: {list(url_to_response)}")

    client.get = AsyncMock(side_effect=_get)
    return client


def _read(name: str) -> bytes:
    return (CHANGELOG_DIR / name).read_bytes()


def _claude_source() -> dict[str, object]:
    return {
        "id": "claude_api_release_notes",
        "mode": "changelog_page",
        "url": "https://platform.claude.com/docs/en/release-notes/overview",
        "markdown_url": "https://platform.claude.com/docs/en/release-notes/overview.md",
    }


def _chatgpt_source() -> dict[str, object]:
    """无 markdown_url — 直接走 Jina 路径。"""
    return {
        "id": "chatgpt_release_notes",
        "mode": "changelog_page",
        "url": "https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
    }


# ---- 1. markdown_url 主路径成功 ---------------------------------------------


def test_changelog_markdown_url_primary_success() -> None:
    """source 含 markdown_url，第一层 200 + 正文 → 切段、url 不带 fragment、skip_url_dedup=True。"""
    source = _claude_source()
    md = _read("claude_api_release.md")
    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})

    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    # fixture 含 3 个日期段
    assert len(articles) == 3
    assert all(a.source_type == "web" for a in articles)
    assert all(a.source_id == source["id"] for a in articles)

    # ① 切出多段
    assert {a.published_at for a in articles} == {
        datetime(2026, 2, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 22, tzinfo=timezone.utc),
        datetime(2026, 1, 8, tzinfo=timezone.utc),
    }

    # ② base url，不拼 fragment（D1）
    assert all(a.url == source["url"] for a in articles)

    # ③ skip_url_dedup=True
    assert all(a.skip_url_dedup is True for a in articles)

    # ④ external_id 形如 {source_id}#YYYY-MM-DD
    ext_ids = {a.external_id for a in articles}
    assert ext_ids == {
        f"{source['id']}#2026-02-05",
        f"{source['id']}#2026-01-22",
        f"{source['id']}#2026-01-08",
    }

    # is_long_form 永远 None
    assert all(a.is_long_form is None for a in articles)


# ---- 2. markdown_url 404 → Jina 兜底成功 -------------------------------------


def test_changelog_markdown_url_404_falls_back_to_jina() -> None:
    """第一层 404 → 走 Jina Reader，仍能正确解析。"""
    source = _claude_source()
    jina_url = f"https://r.jina.ai/{source['url']}"
    md = _read("claude_api_release.md")

    client = _client_with_url_map({
        source["markdown_url"]: _make_response(b"<html>not found</html>", status_code=404),
        jina_url: _make_response(md),
    })

    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    assert len(articles) == 3
    # 应当对两个 URL 都发了请求
    awaited_urls = [c.args[0] for c in client.get.await_args_list]
    assert source["markdown_url"] in awaited_urls
    assert jina_url in awaited_urls


# ---- 3. markdown_url 配置但内容是 "Loading..." → Jina 兜底 ------------------


def test_changelog_markdown_url_placeholder_falls_back_to_jina() -> None:
    """第一层返回 'Loading...' / 过短 → 视为空内容，落到 Jina。"""
    source = _claude_source()
    jina_url = f"https://r.jina.ai/{source['url']}"
    md = _read("claude_api_release.md")

    client = _client_with_url_map({
        source["markdown_url"]: _make_response(b"Loading..."),
        jina_url: _make_response(md),
    })

    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    assert len(articles) == 3
    # 验证确实落到了 Jina（jina_url 被请求过）
    awaited_urls = [c.args[0] for c in client.get.await_args_list]
    assert jina_url in awaited_urls


# ---- 4. 两层都返回占位 → ValueError -----------------------------------------


def test_changelog_both_layers_placeholder_raises_value_error() -> None:
    """markdown_url + Jina 都返回 'Loading...' → 抛 ValueError 含 empty/placeholder。"""
    source = _claude_source()
    jina_url = f"https://r.jina.ai/{source['url']}"

    client = _client_with_url_map({
        source["markdown_url"]: _make_response(b"Loading..."),
        jina_url: _make_response(b"Loading..."),
    })

    adapter = WebAdapter(http_client=client)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(adapter.fetch(source, since=None))

    msg = str(excinfo.value).lower()
    assert "empty" in msg or "placeholder" in msg
    assert source["id"] in str(excinfo.value)


def test_changelog_both_layers_under_500_bytes_raises_value_error() -> None:
    """两层都返回 < 500 字节内容 → 抛 ValueError。"""
    source = _claude_source()
    jina_url = f"https://r.jina.ai/{source['url']}"

    client = _client_with_url_map({
        source["markdown_url"]: _make_response(b"## tiny\n\nshort body"),
        jina_url: _make_response(b"<html>nav only</html>"),
    })

    adapter = WebAdapter(http_client=client)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(adapter.fetch(source, since=None))
    assert "empty" in str(excinfo.value).lower() or "placeholder" in str(excinfo.value).lower()


def test_changelog_jina_only_no_markdown_url_failure() -> None:
    """无 markdown_url 配置 + Jina 返回占位 → 抛 ValueError。"""
    source = _chatgpt_source()
    jina_url = f"https://r.jina.ai/{source['url']}"

    client = _client_with_url_map({jina_url: _make_response(b"Loading...")})

    adapter = WebAdapter(http_client=client)
    with pytest.raises(ValueError):
        asyncio.run(adapter.fetch(source, since=None))


# ---- 5. DATE_HEADING_RE 三种日期格式都能切 ----------------------------------


def test_changelog_date_heading_re_matches_all_formats() -> None:
    """mixed_dates.md 含 ISO / English / English ordinal / M/D/Y 4 段，全部切出。"""
    source = _claude_source()
    md = _read("mixed_dates.md")

    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    # 5 段：ISO / "May 7, 2026" / "May 7th, 2026" / "**May 7, 2026**" / "5/7/2026"
    # 注意都解析为 2026-05-07
    assert len(articles) == 5

    # 所有段都解析到同一天
    for a in articles:
        assert a.published_at == datetime(2026, 5, 7, tzinfo=timezone.utc), (
            f"{a.title!r} parsed to {a.published_at!r}"
        )

    # external_id 因都同日 → 5 条都是同一个 ext_id（D1：靠 url 共享时, pipeline 第一层
    # (source_type, external_id) 仍会去重；这是预期的，多 section 同日只保留一条）
    # 我们只断言切段确实切出了 5 段（ext_id 重复在 pipeline 层处理）
    titles = [a.title for a in articles]
    # 第 4 段（**May 7, 2026** 包裹 + 无 H3 子标题）应回落到 "Update May 7, 2026"
    assert any(t.startswith("Update ") for t in titles)


# ---- 6. 段内首个 H3 抓为 title ----------------------------------------------


def test_changelog_first_subheading_becomes_title() -> None:
    """有 H3 子标题 → title 取 H3；没有则 fallback 到 'Update {date_str}'。"""
    source = _claude_source()
    md = _read("claude_api_release.md")

    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    by_date = {a.published_at: a for a in articles}

    feb5 = by_date[datetime(2026, 2, 5, tzinfo=timezone.utc)]
    assert feb5.title == "Tool use improvements"

    jan22 = by_date[datetime(2026, 1, 22, tzinfo=timezone.utc)]
    assert jan22.title == "New model: claude-opus-4-7"

    # ChatGPT fixture：所有段都没有 H3，title fallback
    chatgpt = _chatgpt_source()
    jina_url = f"https://r.jina.ai/{chatgpt['url']}"
    client2 = _client_with_url_map({jina_url: _make_response(_read("chatgpt_release_notes.md"))})
    adapter2 = WebAdapter(http_client=client2)
    arts2 = asyncio.run(adapter2.fetch(chatgpt, since=None))

    assert len(arts2) == 3
    for a in arts2:
        assert a.title.startswith("Update "), f"expected fallback title, got {a.title!r}"


# ---- 7. 同一页面切出 N 段，所有 url 相同 ------------------------------------


def test_changelog_all_sections_share_base_url() -> None:
    """N 段共享同一 base url（D1），且 skip_url_dedup=True。"""
    source = _claude_source()
    md = _read("gemini_api_changelog.md")

    # 复用 claude_source 但用 gemini fixture 内容（测试只关心 base url 的行为）
    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    assert len(articles) == 3
    assert all(a.url == source["url"] for a in articles)
    assert all(a.skip_url_dedup is True for a in articles)
    # url 不带 fragment
    assert all("#" not in a.url for a in articles)


# ---- 8. since 过滤 ----------------------------------------------------------


def test_changelog_since_filter() -> None:
    """fixture 含 3 段日期段，since=中间日期 → 返回 2 段。"""
    source = _claude_source()
    md = _read("claude_api_release.md")  # 2/5, 1/22, 1/8

    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})
    adapter = WebAdapter(http_client=client)

    since = datetime(2026, 1, 22, tzinfo=timezone.utc)  # 含 1/22 与 2/5，丢弃 1/8
    articles = asyncio.run(adapter.fetch(source, since=since))

    assert len(articles) == 2
    dates = {a.published_at for a in articles}
    assert datetime(2026, 1, 8, tzinfo=timezone.utc) not in dates
    assert datetime(2026, 2, 5, tzinfo=timezone.utc) in dates
    assert datetime(2026, 1, 22, tzinfo=timezone.utc) in dates


# ---- 9. external_id unknown 兜底 -------------------------------------------


def test_changelog_unknown_date_external_id_fallback() -> None:
    """构造一个 markdown：有日期标题但格式异常 → 走 unknown-{md5} 路径。

    我们直接调静态方法验证日期解析失败时的兜底；不依赖网络。
    """
    # 构造一个能匹配正则但 strptime 失败的边角情况其实极难 — 因为正则限定了格式。
    # 改而验证 _parse_section_date 对纯异常输入返 None。
    assert WebAdapter._parse_section_date("not-a-date") is None
    # 边角：月份名拼错（正则会拒绝，所以走不到这一层；这里仅做单元覆盖）
    assert WebAdapter._parse_section_date("Mayyy 7, 2026") is None

    # ISO 边界
    assert WebAdapter._parse_section_date("2026-05-07") == datetime(
        2026, 5, 7, tzinfo=timezone.utc
    )
    # M/D/Y
    assert WebAdapter._parse_section_date("5/7/2026") == datetime(
        2026, 5, 7, tzinfo=timezone.utc
    )
    # English
    assert WebAdapter._parse_section_date("May 7, 2026") == datetime(
        2026, 5, 7, tzinfo=timezone.utc
    )
    # English ordinal
    assert WebAdapter._parse_section_date("May 7th, 2026") == datetime(
        2026, 5, 7, tzinfo=timezone.utc
    )


# ---- 10. Jina URL 拼接正确 --------------------------------------------------


def test_changelog_jina_url_format() -> None:
    """无 markdown_url → 走 Jina，URL 拼接为 https://r.jina.ai/{url}。"""
    source = _chatgpt_source()
    jina_url = f"https://r.jina.ai/{source['url']}"
    md = _read("chatgpt_release_notes.md")

    client = _client_with_url_map({jina_url: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    assert len(articles) == 3
    awaited_urls = [c.args[0] for c in client.get.await_args_list]
    assert jina_url in awaited_urls
    # 应当只调一次（无 markdown_url 不会做层 1）
    assert len(awaited_urls) == 1


# ---- 11. mode dispatch：缺省路径走列表两级模式（非 changelog_page）----------


def test_no_mode_dispatches_to_listing_two_level() -> None:
    """source 缺 mode 字段 → 走 _fetch_listing_two_level（不调 _fetch_changelog_page）。

    用 monkeypatch 拦截两个分支方法，断言只调了 listing 那条。
    """
    listing_called: list[bool] = []
    changelog_called: list[bool] = []

    async def fake_listing(self, source, since):  # type: ignore[no-redef]
        listing_called.append(True)
        return []

    async def fake_changelog(self, source, since):  # type: ignore[no-redef]
        changelog_called.append(True)
        return []

    adapter = WebAdapter()
    adapter._fetch_listing_two_level = fake_listing.__get__(adapter, WebAdapter)
    adapter._fetch_changelog_page = fake_changelog.__get__(adapter, WebAdapter)

    source_no_mode = {
        "id": "anthropic_news",
        "url": "https://www.anthropic.com/news",
    }
    asyncio.run(adapter.fetch(source_no_mode, since=None))
    assert listing_called == [True]
    assert changelog_called == []


def test_changelog_real_claude_api_fixture_split_count() -> None:
    """真实 claude_api markdown_url 端点快照（s2-collector-resilience Step 3 抓取）。

    防回归：锁定当前切段数量与关键属性，防止后续页面格式漂移导致 split 失效。
    """
    source = _claude_source()
    md = _read("claude_api_release_notes.md")
    client = _client_with_url_map({source["markdown_url"]: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    # 真实 fixture 含 93 段，留容差防止页面收缩
    assert len(articles) >= 80
    # 段间共享 base url + skip_url_dedup（D1）
    assert all(a.url == source["url"] for a in articles)
    assert all(a.skip_url_dedup is True for a in articles)
    # 顶部最新条目（fixture 抓取时为 May 6, 2026）
    assert articles[0].external_id == f"{source['id']}#2026-05-06"
    # 覆盖 ordinal 后缀解析（如 ``April 9th, 2025``）也能成功落到 #2025-04-09
    assert any(
        a.external_id == f"{source['id']}#2025-04-09" for a in articles
    ), "ordinal 后缀（April 9th, 2025）应被解析"


def test_changelog_real_claude_product_fixture_split_count() -> None:
    """真实 claude_product Jina Reader 输出快照（s2-collector-resilience Step 3 抓取）。

    防回归：support.claude.com 是 Intercom Help Center，37 段 ``### Month D, YYYY``。
    """
    # 用 chatgpt_source 模拟"无 markdown_url 走 Jina"路径
    source = {
        "id": "claude_product_release_notes",
        "mode": "changelog_page",
        "url": "https://support.claude.com/en/articles/12138966-release-notes",
    }
    jina_url = f"https://r.jina.ai/{source['url']}"
    md = _read("claude_product_release_notes.jina.md")

    client = _client_with_url_map({jina_url: _make_response(md)})
    adapter = WebAdapter(http_client=client)
    articles = asyncio.run(adapter.fetch(source, since=None))

    assert len(articles) >= 30  # 实测 37
    # 顶部最新条目应在 2026-04 月份
    assert articles[0].published_at is not None
    assert articles[0].published_at.year == 2026
    assert articles[0].published_at.month == 4
    # 段间共享 base url + skip_url_dedup
    assert all(a.url == source["url"] for a in articles)
    assert all(a.skip_url_dedup is True for a in articles)


def test_extract_first_subheading_falls_back_to_bold() -> None:
    """段内无 H2/H3 但有整行 ``**Bold**`` → 取 bold 内容作 title。

    覆盖 D5：claude_product / claude_api / chatgpt 段内子标题全是 bold。
    """
    # H2/H3 优先：即使下方有 bold，仍取 H2/H3
    body_h3 = "### Real Heading\n\nbody text\n\n**Bold Below**\n"
    assert WebAdapter._extract_first_subheading(body_h3) == "Real Heading"

    # 无 H2/H3，单行 bold 作兜底
    body_bold = "**Claude Design by Anthropic Labs**\n\nWith Opus 4.7..."
    assert WebAdapter._extract_first_subheading(body_bold) == "Claude Design by Anthropic Labs"

    # 多个 bold 行：取第一个
    body_multi = "**First Feature**\n\nbody\n\n**Second Feature**\n"
    assert WebAdapter._extract_first_subheading(body_multi) == "First Feature"

    # 行内嵌入 bold（不是整行 bold）— 不识别
    body_inline = "正文中提到 **Claude** 是新模型\n\n更多内容"
    assert WebAdapter._extract_first_subheading(body_inline) is None

    # 行内多个独立 bold 段 — 不识别
    body_multi_bold = "**A** **B** **C**\n"
    assert WebAdapter._extract_first_subheading(body_multi_bold) is None

    # 完全没有标题 — 返回 None
    body_plain = "just bullets:\n- item 1\n- item 2\n"
    assert WebAdapter._extract_first_subheading(body_plain) is None


def test_mode_changelog_page_dispatches_to_changelog_branch() -> None:
    """source 有 mode=changelog_page → 走 _fetch_changelog_page。"""
    listing_called: list[bool] = []
    changelog_called: list[bool] = []

    async def fake_listing(self, source, since):  # type: ignore[no-redef]
        listing_called.append(True)
        return []

    async def fake_changelog(self, source, since):  # type: ignore[no-redef]
        changelog_called.append(True)
        return []

    adapter = WebAdapter()
    adapter._fetch_listing_two_level = fake_listing.__get__(adapter, WebAdapter)
    adapter._fetch_changelog_page = fake_changelog.__get__(adapter, WebAdapter)

    source_changelog = {
        "id": "claude_api_release_notes",
        "mode": "changelog_page",
        "url": "https://platform.claude.com/docs/release-notes/overview",
    }
    asyncio.run(adapter.fetch(source_changelog, since=None))
    assert changelog_called == [True]
    assert listing_called == []
