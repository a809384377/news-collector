"""reddit_enrich 模块单测（s13-reddit-comments-enrich Step 3）。

测试策略：
- 离线 fixture：`tests/fixtures/reddit/` 4 个真实 .json 样本（4 sub 各 1 条）
- httpx Mock：用 `httpx.MockTransport` 让 client.get 直接返回 fixture 内容
- 不走真实网络
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from newsbox.adapters.reddit_enrich import (
    RedditEnrichError,
    _build_json_url,
    _select_top_comments,
    enrich_reddit_post,
    format_body,
    format_meta_block,
    is_reddit_url,
)
from newsbox.models import RedditEnrichment


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reddit"


# ---- _build_json_url -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
            "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo.json?limit=15&depth=2&sort=top",
        ),
        (
            "https://www.reddit.com/r/codex/comments/xyz/bar",  # 无尾 /
            "https://www.reddit.com/r/codex/comments/xyz/bar.json?limit=15&depth=2&sort=top",
        ),
        (
            "https://www.reddit.com/r/ClaudeAI/comments/qq/?utm_source=share",  # 有 query
            "https://www.reddit.com/r/ClaudeAI/comments/qq.json?limit=15&depth=2&sort=top",
        ),
        (
            "https://www.reddit.com/r/codex/comments/abc/foo.json",  # 已带 .json（兜底）
            "https://www.reddit.com/r/codex/comments/abc/foo.json?limit=15&depth=2&sort=top",
        ),
    ],
)
def test_build_json_url(raw: str, expected: str) -> None:
    assert _build_json_url(raw) == expected


# ---- _select_top_comments --------------------------------------------------


def test_select_top_comments_filters_bots_and_deleted() -> None:
    """三条过滤规则：kind=t1 / author 非 bot / body 非 [deleted]|[removed]。"""
    children = [
        {"kind": "t1", "data": {"name": "t1_a", "author": "alice", "score": 10, "body": "ok"}},
        {"kind": "t1", "data": {"name": "t1_b", "author": "AutoModerator", "score": 99, "body": "I am bot"}},
        {"kind": "t1", "data": {"name": "t1_c", "author": "bob", "score": 5, "body": "[deleted]"}},
        {"kind": "t1", "data": {"name": "t1_d", "author": "carol", "score": 8, "body": "[removed]"}},
        {"kind": "t1", "data": {"name": "t1_e", "author": "[deleted]", "score": 7, "body": "ghost"}},
        {"kind": "more", "data": {"children": ["t1_f"]}},  # more 占位
        {"kind": "t1", "data": {"name": "t1_g", "author": "dan", "score": 20, "body": "great"}},
    ]
    result = _select_top_comments(children, top_n=5)
    # 应只剩 alice / dan，按 score desc 排序
    assert [c.comment_id for c in result] == ["t1_g", "t1_a"]
    assert [c.rank for c in result] == [1, 2]
    assert [c.score for c in result] == [20, 10]


def test_select_top_comments_top_n_truncation() -> None:
    children = [
        {"kind": "t1", "data": {"name": f"t1_{i}", "author": "u", "score": i, "body": str(i)}}
        for i in range(10)
    ]
    result = _select_top_comments(children, top_n=3)
    assert len(result) == 3
    assert [c.score for c in result] == [9, 8, 7]
    assert [c.rank for c in result] == [1, 2, 3]


def test_select_top_comments_empty() -> None:
    assert _select_top_comments([], top_n=5) == ()
    # 全是 bot / deleted
    children = [
        {"kind": "t1", "data": {"name": "t1_a", "author": "AutoModerator", "score": 10, "body": "x"}},
    ]
    assert _select_top_comments(children, top_n=5) == ()


# ---- enrich_reddit_post (with fixtures) ------------------------------------


def _mock_client(fixture_path: Path, *, status_code: int = 200) -> httpx.AsyncClient:
    """构造 httpx.MockTransport 客户端，让所有请求返回 fixture 内容。"""
    body = fixture_path.read_bytes() if status_code == 200 else b'{"error":"x"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, headers={"content-type": "application/json"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_enrich_localllama_link_post() -> None:
    """r_localllama fixture: is_self=False 链接帖，score=165, num_comments=18, flair=New Model。"""
    fixture = FIXTURE_DIR / "r_localllama_post_with_comments.json"
    async with _mock_client(fixture) as client:
        result = await enrich_reddit_post(
            "https://www.reddit.com/r/LocalLLaMA/comments/1t7kgy4/new_moe_from_ai2_emo/",
            client,
            top_n=5,
        )

    assert result.name.startswith("t3_")
    assert result.subreddit == "LocalLLaMA"
    assert result.score >= 100  # fixture 当时是 165；用 ≥ 阈值避免快照式断言
    assert result.num_comments >= 10
    assert result.flair == "New Model"
    assert isinstance(result.upvote_ratio, float)
    assert 0.0 <= result.upvote_ratio <= 1.0
    assert isinstance(result.selftext, str)  # 链接帖通常为空
    assert 1 <= len(result.top_comments) <= 5
    # 评论按 score desc
    scores = [c.score for c in result.top_comments]
    assert scores == sorted(scores, reverse=True)
    # rank 1-indexed
    assert [c.rank for c in result.top_comments] == list(range(1, len(result.top_comments) + 1))
    # 无 bot
    for c in result.top_comments:
        assert c.author != "AutoModerator"
        assert c.body not in ("[deleted]", "[removed]")


@pytest.mark.anyio
async def test_enrich_claudecode_self_post() -> None:
    """r_claudecode fixture: is_self=True self post，selftext 长 578，flair=Help Needed。"""
    fixture = FIXTURE_DIR / "r_claudecode_post_with_comments.json"
    async with _mock_client(fixture) as client:
        result = await enrich_reddit_post(
            "https://www.reddit.com/r/ClaudeCode/comments/1t7v1p0/foo/",
            client,
            top_n=5,
        )

    assert result.subreddit == "ClaudeCode"
    assert result.flair == "Help Needed"
    assert len(result.selftext) > 100  # self post selftext 非空
    assert len(result.top_comments) >= 1


@pytest.mark.anyio
async def test_enrich_codex_with_more_placeholder() -> None:
    """r_codex fixture 含 'more' 占位评论，应被 _select_top_comments 过滤掉。"""
    fixture = FIXTURE_DIR / "r_codex_post_with_comments.json"
    async with _mock_client(fixture) as client:
        result = await enrich_reddit_post(
            "https://www.reddit.com/r/codex/comments/1t8tltf/foo/",
            client,
        )

    # 'more' 不进 top_comments
    for c in result.top_comments:
        assert c.comment_id.startswith("t1_")


@pytest.mark.anyio
async def test_enrich_claudeai_humor_flair() -> None:
    fixture = FIXTURE_DIR / "r_claudeai_post_with_comments.json"
    async with _mock_client(fixture) as client:
        result = await enrich_reddit_post(
            "https://www.reddit.com/r/ClaudeAI/comments/1taa7dl/foo/",
            client,
        )
    assert result.subreddit == "ClaudeAI"
    assert result.flair == "Humor"


# ---- error paths -----------------------------------------------------------


@pytest.mark.anyio
async def test_enrich_http_5xx_raises() -> None:
    """5xx 响应应抛 RedditEnrichError 让调用方降级。"""
    fixture = FIXTURE_DIR / "r_localllama_post_with_comments.json"
    async with _mock_client(fixture, status_code=503) as client:
        with pytest.raises(RedditEnrichError, match="http 503"):
            await enrich_reddit_post(
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                client,
            )


@pytest.mark.anyio
async def test_enrich_invalid_json_raises() -> None:
    """JSON 解析失败抛 RedditEnrichError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RedditEnrichError, match="json decode failed"):
            await enrich_reddit_post(
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                client,
            )


@pytest.mark.anyio
async def test_enrich_missing_fields_raises() -> None:
    """payload shape 不符合预期抛 RedditEnrichError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        # 单元素 list（缺评论 listing）
        return httpx.Response(
            200,
            content=json.dumps([{"data": {"children": []}}]).encode("utf-8"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RedditEnrichError, match="payload shape"):
            await enrich_reddit_post(
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                client,
            )


@pytest.mark.anyio
async def test_enrich_post_missing_name_raises() -> None:
    """帖子 data 缺 name → RedditEnrichError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps([
                {"data": {"children": [{"data": {"subreddit": "x"}}]}},
                {"data": {"children": []}},
            ]).encode("utf-8"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RedditEnrichError, match="missing 'name'"):
            await enrich_reddit_post(
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                client,
            )


@pytest.mark.anyio
async def test_enrich_http_transport_error_raises() -> None:
    """connect / read 错误也走 RedditEnrichError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RedditEnrichError, match="http error"):
            await enrich_reddit_post(
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                client,
            )


# ---- body 改写 helper -------------------------------------------------------


def _enrichment(
    *,
    subreddit: str = "LocalLLaMA",
    score: int = 100,
    upvote_ratio: float | None = 0.95,
    num_comments: int = 50,
    flair: str | None = "News",
    selftext: str = "",
) -> RedditEnrichment:
    return RedditEnrichment(
        name="t3_abc",
        subreddit=subreddit,
        score=score,
        upvote_ratio=upvote_ratio,
        num_comments=num_comments,
        flair=flair,
        selftext=selftext,
        top_comments=(),
    )


def test_format_meta_block_full() -> None:
    e = _enrichment()
    block = format_meta_block(e)
    assert block == "> **r/LocalLLaMA** · score=100 · 95% · 50 comments · flair: News"


def test_format_meta_block_no_upvote_ratio_no_flair() -> None:
    e = _enrichment(upvote_ratio=None, flair=None)
    block = format_meta_block(e)
    assert block == "> **r/LocalLLaMA** · score=100 · 50 comments"


def test_format_meta_block_rounds_ratio() -> None:
    # 0.875 → 88%
    e = _enrichment(upvote_ratio=0.875)
    block = format_meta_block(e)
    assert "· 88% ·" in block


def test_format_body_with_selftext() -> None:
    e = _enrichment(selftext="Hello world\nSecond line")
    out = format_body(e)
    lines = out.split("\n")
    assert lines[0].startswith("> **r/LocalLLaMA**")
    assert lines[1] == ""
    assert "Hello world" in out
    assert "Second line" in out


def test_format_body_empty_selftext_returns_meta_only() -> None:
    e = _enrichment(selftext="")
    out = format_body(e)
    assert "\n" not in out  # 单行元信息块
    assert out.startswith("> **r/LocalLLaMA**")


def test_format_body_whitespace_selftext_treated_empty() -> None:
    e = _enrichment(selftext="   \n  \n")
    out = format_body(e)
    assert "\n" not in out


# ---- is_reddit_url -------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.reddit.com/r/x/comments/a/b/", True),
        ("https://reddit.com/r/x/comments/a/", True),
        ("https://old.reddit.com/r/x/comments/a/", True),
        ("https://i.redd.it/foo.png", False),
        ("https://www.anthropic.com/news", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_reddit_url(url: str, expected: bool) -> None:
    assert is_reddit_url(url) is expected


# ---- anyio backend (pytest-anyio 默认会跑 asyncio + trio；我们只走 asyncio) -----


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
