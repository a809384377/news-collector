"""Reddit `.json` 富化模块（s13-reddit-comments-enrich）。

reddit 原生 RSS 缺关键互动信号（score / upvote_ratio / num_comments / flair / 评论），
且 ~40-60% 的链接帖 body 仅是 reddit 模板（缩略图 + 用户名 + 外链），对下游 LLM
零价值。本模块对单条 reddit permalink 调一次 `<url>.json?limit=15&depth=2&sort=top`
拿回完整帖子 + 评论树，由 rss_adapter 调用。

入库前评论过滤（D6）：
1. `kind == 't1'`（顶层评论，不收 'more' 占位）
2. author 不在 `{'AutoModerator', '[deleted]'}`
3. body 不在 `{'[deleted]', '[removed]'}`

限速（D7）：调用方负责（rss_adapter 在 fetch 循环中插入 sleep）；本模块只发请求。

失败兜底：HTTP 错误 / JSON 异常 / 字段缺失 → 抛 `RedditEnrichError`，由调用方决定
是否降级（参考 rss_adapter：失败保留原 RSS body，记 warning）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from ..models import RedditComment, RedditEnrichment


_USER_AGENT = "newsbox/1.0 by /u/anonymous"
"""reddit 推荐自定义 User-Agent；不带可能被 429。"""

_TIMEOUT_SECONDS = 15.0

_DEFAULT_TOP_N = 5

_BOT_AUTHORS = frozenset({"AutoModerator", "[deleted]"})
_DELETED_BODIES = frozenset({"[deleted]", "[removed]"})


class RedditEnrichError(Exception):
    """富化失败的统一异常；调用方应捕获并降级到原 RSS body。"""


def _build_json_url(post_url: str) -> str:
    """reddit permalink → `.json` 端点 URL。

    输入：``https://www.reddit.com/r/<sub>/comments/<id>/<slug>/`` 或末尾不带 /
    输出：``https://www.reddit.com/r/<sub>/comments/<id>/<slug>.json?limit=15&depth=2&sort=top``

    reddit 接受任意 permalink 末尾追加 .json。处理三种边界：
    - 末尾有 / 则去掉
    - 已含 query string 则替换
    - 已是 .json 则不重复加（理论不该有但兜底）
    """
    if "?" in post_url:
        post_url = post_url.split("?", 1)[0]
    post_url = post_url.rstrip("/")
    if not post_url.endswith(".json"):
        post_url = f"{post_url}.json"
    return f"{post_url}?limit=15&depth=2&sort=top"


def _parse_comment(raw: dict[str, Any], rank: int) -> RedditComment:
    """t1 评论 dict → RedditComment（rank 由调用方决定）。"""
    created = raw.get("created_utc")
    created_at: datetime | None
    if isinstance(created, (int, float)):
        created_at = datetime.fromtimestamp(created, tz=timezone.utc)
    else:
        created_at = None

    return RedditComment(
        comment_id=raw["name"],
        parent_id=raw.get("parent_id"),
        author=raw.get("author", "[unknown]"),
        score=int(raw.get("score", 0)),
        body=raw.get("body", ""),
        created_utc=created_at,
        rank=rank,
    )


def _select_top_comments(
    children: list[dict[str, Any]], *, top_n: int
) -> tuple[RedditComment, ...]:
    """评论列表过滤 + 排序 + 截断。

    入参是 listing children（含 't1' / 'more' 等多种 kind）。
    返回过滤后按 score desc 排序的前 top_n 条 RedditComment。
    """
    candidates: list[dict[str, Any]] = []
    for c in children:
        if c.get("kind") != "t1":
            continue
        data = c.get("data") or {}
        author = data.get("author") or ""
        body = data.get("body") or ""
        if author in _BOT_AUTHORS:
            continue
        if body in _DELETED_BODIES:
            continue
        candidates.append(data)

    # score desc；同分时保 reddit listing 原序
    candidates.sort(key=lambda d: int(d.get("score", 0)), reverse=True)
    selected = candidates[: max(0, top_n)]
    return tuple(_parse_comment(d, rank=i + 1) for i, d in enumerate(selected))


def _parse_payload(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """reddit `.json` 端点返回 [帖子 listing, 评论 listing] 两元素 list。

    返回 (post_data, comment_children)；任一字段缺失 → 抛 RedditEnrichError。
    """
    if not isinstance(payload, list) or len(payload) < 2:
        raise RedditEnrichError(
            f"payload shape unexpected: type={type(payload).__name__} "
            f"len={len(payload) if hasattr(payload, '__len__') else '?'}"
        )
    try:
        post_data = payload[0]["data"]["children"][0]["data"]
        comment_children = payload[1]["data"]["children"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RedditEnrichError(f"payload missing required fields: {exc!r}") from exc
    return post_data, comment_children


async def enrich_reddit_post(
    post_url: str,
    client: httpx.AsyncClient,
    *,
    top_n: int = _DEFAULT_TOP_N,
    timeout: float = _TIMEOUT_SECONDS,
) -> RedditEnrichment:
    """对单条 reddit permalink 拉富化数据。

    Args:
        post_url: reddit 帖子 permalink，形如 `https://www.reddit.com/r/<sub>/comments/<id>/<slug>/`
        client: 注入的 httpx 客户端（调用方负责生命周期 + User-Agent；本函数若 client 未设 UA 仍会自带 header）
        top_n: 评论截取数（按过滤后 score desc 顺序）
        timeout: 请求超时（秒）

    Returns:
        RedditEnrichment（含帖子级元信息 + top_n 评论元组）

    Raises:
        RedditEnrichError: 网络错误 / JSON 异常 / 字段缺失 / 非 reddit URL；调用方应捕获降级
    """
    json_url = _build_json_url(post_url)

    try:
        response = await client.get(
            json_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise RedditEnrichError(f"http error for {json_url}: {exc!r}") from exc

    if response.status_code >= 400:
        raise RedditEnrichError(
            f"http {response.status_code} for {json_url}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RedditEnrichError(f"json decode failed for {json_url}: {exc!r}") from exc

    post_data, comment_children = _parse_payload(payload)

    # 帖子级字段映射（缺失/类型异常时给安全默认）
    try:
        name = post_data["name"]  # t3_xxx
    except KeyError as exc:
        raise RedditEnrichError(f"post missing 'name' field: {post_data!r}") from exc

    subreddit = post_data.get("subreddit") or ""
    score = int(post_data.get("score", 0))
    num_comments = int(post_data.get("num_comments", 0))
    flair_raw = post_data.get("link_flair_text")
    flair: str | None = flair_raw.strip() if isinstance(flair_raw, str) and flair_raw.strip() else None
    upvote_ratio_raw = post_data.get("upvote_ratio")
    upvote_ratio: float | None
    if isinstance(upvote_ratio_raw, (int, float)):
        upvote_ratio = float(upvote_ratio_raw)
    else:
        upvote_ratio = None
    selftext = post_data.get("selftext") or ""

    top_comments = _select_top_comments(comment_children, top_n=top_n)

    logger.debug(
        f"reddit_enrich ok: {name} score={score} num_comments={num_comments} "
        f"top_kept={len(top_comments)} flair={flair!r}"
    )

    return RedditEnrichment(
        name=name,
        subreddit=subreddit,
        score=score,
        upvote_ratio=upvote_ratio,
        num_comments=num_comments,
        flair=flair,
        selftext=selftext,
        top_comments=top_comments,
    )


# ---- body 改写 helper (D9) -------------------------------------------------


_REDDIT_HOSTS = frozenset({"www.reddit.com", "reddit.com", "old.reddit.com"})


def is_reddit_url(url: str) -> bool:
    """是否为 reddit permalink。host 命中即可。"""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return False
    return host.lower() in _REDDIT_HOSTS


def format_meta_block(enrichment: RedditEnrichment) -> str:
    """帖子级元信息渲染为 markdown 引用块（D3 + D9）。

    格式: ``> **r/<sub>** · score=N · X% · M comments · flair: <text>``
    upvote_ratio / flair 缺省时省去对应 segment。
    """
    parts: list[str] = [f"**r/{enrichment.subreddit}**", f"score={enrichment.score}"]
    if enrichment.upvote_ratio is not None:
        parts.append(f"{int(round(enrichment.upvote_ratio * 100))}%")
    parts.append(f"{enrichment.num_comments} comments")
    if enrichment.flair:
        parts.append(f"flair: {enrichment.flair}")
    return f"> {' · '.join(parts)}"


def format_body(enrichment: RedditEnrichment) -> str:
    """统一 reddit body 格式: ``<元信息块>\\n\\n<selftext>``（D9）。

    selftext 为空（典型链接帖）时只返回元信息块；否则中间空行分隔。
    """
    meta = format_meta_block(enrichment)
    selftext = enrichment.selftext.strip()
    if not selftext:
        return meta
    return f"{meta}\n\n{selftext}"
