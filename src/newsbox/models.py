"""dataclass / pydantic 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RedditComment:
    """reddit 评论一条（s13-reddit-comments-enrich）。

    入库前已过滤：kind == 't1' / author 非 AutoModerator|[deleted] / body 非 [deleted]|[removed]。
    rank 由富化模块按过滤后 score desc 顺序赋值，1-indexed。
    """

    comment_id: str
    """reddit t1_xxx。"""

    parent_id: str | None
    """父评论 id（顶层评论时 = 帖子 t3_xxx）。"""

    author: str
    score: int
    body: str
    created_utc: datetime | None
    """reddit 偶有未返回；缺失允许 None。"""

    rank: int
    """过滤后按 score 倒序的位置（1-indexed）。"""


@dataclass(frozen=True, slots=True)
class RedditEnrichment:
    """reddit `.json` 富化产出（s13-reddit-comments-enrich）。

    由 ``adapters/reddit_enrich.py`` 生产，挂在 ``RawArticle.enrichment`` 上；
    pipeline/fetch.py 写完 articles_raw 后取出 ``top_comments`` 写入 reddit_comments 表。
    帖子级元信息（score / upvote_ratio / num_comments / flair）由 rss_adapter
    拼进 articles_raw.body 头部 markdown 引用块（D3），本结构保留原值便于测试断言。
    """

    name: str
    """reddit t3_xxx，可作稳定 external_id。"""

    subreddit: str
    score: int
    upvote_ratio: float | None
    num_comments: int
    flair: str | None
    selftext: str

    top_comments: tuple[RedditComment, ...] = ()


@dataclass(frozen=True, slots=True)
class RawArticle:
    """信源适配器的标准化输出（设计 §3.7）。

    各 adapter 把异构源（RSS-like / Web-like）映射到本结构。
    pipeline/fetch.py 负责把 RawArticle 写入 articles_raw 表，并补足
    url_canonical_hash / content_hash / fetched_at / status / domain_tags 等下游字段。
    domain_tags 是 source-level 标签（来自 sources.yaml 信源条目），不在本 dataclass。
    """

    source_type: str
    """rss / web，对应 sources.yaml 顶层 key（s2-1 Step 4 后两类）。"""

    source_id: str
    """sources.yaml 中条目的 id。"""

    external_id: str
    """信源给的唯一 ID（feed entry.id / reddit post id / FxTwitter status id ...）。"""

    url: str
    title: str
    body: str
    published_at: datetime | None
    """来源未提供发布时间时为 None；下游可回落到 fetched_at。"""

    is_long_form: str | None = None
    """长文标记：``normal`` / ``note_tweet`` / ``article``；普通短文/未识别为 None。
    一期 RSSHub Twitter 路由暂不下发该信号；未来路由若识别 note_tweet 时填充。"""

    skip_url_dedup: bool = False
    """changelog_page 单页多 section 共享 base url 时置 True，pipeline 第二层
    url_canonical_hash 检查跳过；仍走第一层 (source_type, external_id) 去重（D1）。
    """

    enrichment: RedditEnrichment | None = None
    """reddit 富化产出（s13）；非 reddit 帖子或富化失败时为 None（fallback 原 body）。"""
