"""消费方 SDK：从 raw.db 流式读取 articles_raw 行。

外部消费方典型用法（D2 决策：Python SDK + 共享 SQLite 文件）::

    from newsbox.sdk import read_raw
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    for art in read_raw(domain="ai", since=since, source_types=["rss"]):
        print(art.title)

设计取舍：

- 暴露字段为消费方需要的"业务字段"；内部字段（url_canonical_hash / is_long_form）
  不进 SDK 投影。``content_hash``（内容指纹）作为暴露字段提供给消费方按需做
  去重 / 热度聚合，算法定义：``sha256(title + body[:500])``，输出 64 字符 hex。
  采集层自身不替消费方按 content_hash 去重（s1-schema-cleanup §5：原料型路线）。
- 失败信息不在 articles_raw 表（s1-schema-cleanup 通过 0002_drop_dead_fields.sql
  删除了原 status / last_error 两列）。失败按信源记账于 source_state 表
  （last_error / consecutive_failures / last_fetch_at），由 CLI status / state /
  doctor 命令读取，不进 SDK。
- 流式游标（sqlite3 cursor 迭代）—— 不一次性加载到内存，便于跑大批量。
  generator + try/finally 关连接：游标存活期间连接保持打开，迭代结束/异常时关闭。
- 默认 db_path = ~/.newsbox/raw.db；调用方可覆盖（测试 / 多 db 场景）。
- since 过滤按 ``fetched_at`` 而非 ``published_at``：published_at 可能为空，
  且消费方真实语义是"我上次消费到这里"——按 fetched_at 单调可恢复。
- 不在 SDK 内部跑 init_db / migrations；SDK 只读，假设 raw.db 已存在。
  不存在时抛 FileNotFoundError 友好提示。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


# ---- 公开数据类 -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedditCommentRow:
    """消费方视角的一条 reddit 评论（s13-reddit-comments-enrich）。

    采集层 ``reddit_comments`` 表的对外投影。入库前已按
    ``kind == 't1'`` + author 非 AutoModerator|[deleted] + body 非
    [deleted]|[removed] 过滤；``rank`` 由富化模块按过滤后 score desc 顺序赋值
    （1-indexed），消费方按 rank ASC 拿到的就是热度从高到低的评论。
    """

    article_id: int
    """关联的 ``articles_raw.id``。"""

    comment_id: str
    """reddit ``t1_xxx``。"""

    parent_id: str | None
    """父评论 id（顶层评论时 = 帖子 ``t3_xxx``）。"""

    author: str
    score: int
    body: str

    created_utc: datetime | None
    """reddit 偶有未返回；缺失允许 None。"""

    rank: int
    """过滤后按 score 倒序的位置（1-indexed）。"""


@dataclass(frozen=True, slots=True)
class ArticleRaw:
    """消费方视角的一条原始文章。

    字段为采集层暴露给消费方的"业务字段"子集，不含 url_canonical_hash /
    is_long_form 等内部实现字段。``content_hash`` 暴露给消费方做内容指纹
    去重 / 热度聚合（采集层自身不去重）。
    """

    id: int
    source_type: str
    """rss / web。"""

    source_id: str
    """sources.yaml 中条目的 id。"""

    source_tier: str
    """official_first_party / kol / secondary。"""

    external_id: str
    """信源给的唯一 ID。"""

    url: str
    title: str
    body: str

    content_hash: str
    """内容指纹：``sha256(title + body[:500])`` 输出的 64 字符 hex。
    消费方可用作"同内容多源转发"的去重键或热度聚合维度。
    算法稳定：标题 + 正文前 500 字的 sha256（s1-schema-cleanup §5 / §9-D8）。"""

    published_at: datetime | None
    """信源未提供发布时间时为 None；消费方可回落到 fetched_at。"""

    fetched_at: datetime
    """采集时刻；read_raw 的 since 参数即按此字段过滤。"""

    domain_tags: list[str]
    """领域标签（D5：一期均为 ["ai"]，未来支持多领域）。"""


# ---- 读 API -----------------------------------------------------------------


_DEFAULT_DB_PATH = Path.home() / ".newsbox" / "raw.db"

# SDK SELECT 12 个业务字段；内部字段（url_canonical_hash / is_long_form）
# 不出现在投影中。content_hash 作为内容指纹暴露给消费方。
_SELECT_COLS = (
    "id, source_type, source_id, source_tier, external_id, "
    "url, title, body, content_hash, published_at, fetched_at, domain_tags"
)


def read_raw(
    domain: str = "ai",
    since: datetime | None = None,
    source_types: list[str] | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> Iterator[ArticleRaw]:
    """流式读取 articles_raw 行。

    参数:
        domain: 领域过滤；任一 domain_tag 命中即匹配（一期数据都是 ["ai"]）。
        since: 仅返回 fetched_at >= since 的行；None 表示不过滤。
        source_types: 限定 source_type 列表；None 表示不过滤。
        limit: 限制返回条数；None 表示无限。
        db_path: 覆盖默认 ~/.newsbox/raw.db；测试 / 多 db 场景使用。

    生成:
        ``ArticleRaw`` 实例，按 (fetched_at ASC, id ASC) 排序。

    Raises:
        FileNotFoundError: db_path 指向的文件不存在。
    """
    db = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
    if not db.exists():
        raise FileNotFoundError(
            f"raw.db 未找到：{db}. 请先运行 newsbox fetch 落库，"
            f"或通过 db_path 参数指向已存在的 raw.db。"
        )

    where: list[str] = [
        "EXISTS (SELECT 1 FROM json_each(domain_tags) WHERE value = :domain)"
    ]
    params: dict[str, object] = {"domain": domain}

    if since is not None:
        where.append("fetched_at >= :since")
        params["since"] = since.isoformat()

    if source_types:
        # 用命名参数 :st0, :st1 ... 拼 IN 子句（避免 sqlite3 不支持序列展开）
        placeholders = []
        for i, st in enumerate(source_types):
            key = f"st{i}"
            placeholders.append(f":{key}")
            params[key] = st
        where.append(f"source_type IN ({', '.join(placeholders)})")

    sql = (
        f"SELECT {_SELECT_COLS} FROM articles_raw "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY fetched_at ASC, id ASC"
    )
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    # 用 generator + try/finally 管理连接生命周期：
    # 游标存活期间连接必须打开（流式 yield 不能提前关）；
    # 迭代结束（消费方 break / 异常 / 正常耗尽）时由 GeneratorExit / finally 收尾。
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        with closing(conn.execute(sql, params)) as cur:
            for row in cur:
                yield _row_to_article(row)
    finally:
        conn.close()


# ---- reddit 评论查询 --------------------------------------------------------


def get_reddit_comments(
    article_id: int,
    db_path: Path | None = None,
) -> list[RedditCommentRow]:
    """读取一篇 reddit 文章的全部评论（s13-reddit-comments-enrich）。

    参数:
        article_id: ``articles_raw.id``；非 reddit 文章会返回空 list（外键侧
            不存在对应行）。
        db_path: 覆盖默认 ``~/.newsbox/raw.db``；测试 / 多 db 场景使用。

    返回:
        ``RedditCommentRow`` 列表，按 ``rank ASC`` 排序（rank=1 是过滤后
        score 最高的评论；默认 top 5）。无评论时返回空 list。

    Raises:
        FileNotFoundError: db_path 指向的文件不存在。

    设计取舍：
        - 返回 list 而非 generator——单篇帖子评论数量极小（默认 top 5），
          一次性 fetchall 比游标迭代更清晰，调用方可直接 ``[0].body`` 取头条。
        - 不接受 since / limit 等过滤——评论按 article_id 强关联，过滤场景
          应在 ``read_raw`` 层完成后逐 article 调用本函数。
        - 与 ``read_raw`` 共享 ``FileNotFoundError`` 兜底语义。
    """
    db = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
    if not db.exists():
        raise FileNotFoundError(
            f"raw.db 未找到：{db}. 请先运行 newsbox fetch 落库，"
            f"或通过 db_path 参数指向已存在的 raw.db。"
        )

    sql = (
        "SELECT article_id, comment_id, parent_id, author, score, body, "
        "created_utc, rank "
        "FROM reddit_comments WHERE article_id = :article_id "
        "ORDER BY rank ASC"
    )
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        with closing(conn.execute(sql, {"article_id": article_id})) as cur:
            return [_row_to_comment(row) for row in cur]
    finally:
        conn.close()


# ---- 内部 helpers -----------------------------------------------------------


def _row_to_comment(row: sqlite3.Row) -> RedditCommentRow:
    """sqlite Row → RedditCommentRow。

    - created_utc 列允许 NULL；NULL 时返回 None（reddit 偶有未返回）。
    - parent_id 列允许 NULL；NULL 时返回 None。
    """
    created_raw = row["created_utc"]
    created_utc = datetime.fromisoformat(created_raw) if created_raw else None

    return RedditCommentRow(
        article_id=row["article_id"],
        comment_id=row["comment_id"],
        parent_id=row["parent_id"],
        author=row["author"],
        score=row["score"],
        body=row["body"],
        created_utc=created_utc,
        rank=row["rank"],
    )


def _row_to_article(row: sqlite3.Row) -> ArticleRaw:
    """sqlite Row → ArticleRaw。

    - published_at 列允许 NULL；NULL 时返回 None。
    - fetched_at 列 NOT NULL；DB 里存 ISO 字符串，直接 fromisoformat。
    - domain_tags 列 JSON 字符串 → 反序列化为 list[str]。
    """
    pub_raw = row["published_at"]
    published_at = datetime.fromisoformat(pub_raw) if pub_raw else None

    fetched_at = datetime.fromisoformat(row["fetched_at"])
    domain_tags = json.loads(row["domain_tags"])

    return ArticleRaw(
        id=row["id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        source_tier=row["source_tier"],
        external_id=row["external_id"],
        url=row["url"],
        title=row["title"],
        body=row["body"],
        content_hash=row["content_hash"],
        published_at=published_at,
        fetched_at=fetched_at,
        domain_tags=domain_tags,
    )
