"""sdk 模块单元测试。

覆盖（D2 决策对应行为）：
1. 空库 → 迭代 0 条
2. domain 过滤命中 / 不命中
3. 多 domain 文章在多个 domain 下都能读到
4. since 过滤按 fetched_at（中位划分）
5. source_types 过滤（rss / web / 不传）
6. limit 截断
7. ORDER BY fetched_at ASC 稳定
8. ArticleRaw frozen 不可变
9. published_at NULL → None
10. domain_tags JSON 反序列化为 list
11. ArticleRaw 不暴露内部字段
12. db_path 覆盖默认路径
13. db_path 不存在 → FileNotFoundError
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsbox.db import get_conn, init_db
from newsbox.sdk import ArticleRaw, RedditCommentRow, get_reddit_comments, read_raw


# ---- helpers ---------------------------------------------------------------


_INSERT_SQL = """
    INSERT INTO articles_raw (
        source_type, source_id, source_tier, external_id,
        url, url_canonical_hash, content_hash, title, body,
        published_at, fetched_at, domain_tags, is_long_form
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert(
    conn: sqlite3.Connection,
    *,
    source_type: str = "rss",
    source_id: str = "src1",
    source_tier: str = "kol",
    external_id: str,
    url: str | None = None,
    title: str = "title",
    body: str = "body",
    content_hash: str | None = None,
    published_at: datetime | None = None,
    fetched_at: datetime,
    domain_tags: list[str] | None = None,
    is_long_form: str | None = None,
) -> None:
    # content_hash：默认与生产路径一致的 sha256(title + body[:500]) hex（64 字符）
    if content_hash is None:
        content_hash = hashlib.sha256((title + body[:500]).encode("utf-8")).hexdigest()
    conn.execute(
        _INSERT_SQL,
        (
            source_type,
            source_id,
            source_tier,
            external_id,
            url or f"https://example.com/{external_id}",
            f"hash-url-{external_id}",
            content_hash,
            title,
            body,
            published_at.isoformat() if published_at else None,
            fetched_at.isoformat(),
            json.dumps(domain_tags if domain_tags is not None else ["ai"]),
            is_long_form,
        ),
    )


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "raw.db"
    init_db(db_path)
    return db_path


# ---- tests -----------------------------------------------------------------


def test_empty_db_yields_nothing(empty_db: Path) -> None:
    assert list(read_raw(db_path=empty_db)) == []


def test_domain_filter(empty_db: Path) -> None:
    """3 条 ai + 1 条 finance → ai 返 3，finance 返 1。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        for i in range(3):
            _insert(
                conn,
                external_id=f"ai-{i}",
                fetched_at=base + timedelta(minutes=i),
                domain_tags=["ai"],
            )
        _insert(
            conn,
            external_id="fin-1",
            fetched_at=base + timedelta(minutes=10),
            domain_tags=["finance"],
        )
        conn.commit()
    finally:
        conn.close()

    ai = list(read_raw(domain="ai", db_path=empty_db))
    fin = list(read_raw(domain="finance", db_path=empty_db))
    assert len(ai) == 3
    assert len(fin) == 1
    assert {a.external_id for a in ai} == {"ai-0", "ai-1", "ai-2"}
    assert fin[0].external_id == "fin-1"


def test_multi_domain_article_visible_in_each(empty_db: Path) -> None:
    """domain_tags=['ai','finance'] 的文章在两个 domain 下都能读到。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(
            conn,
            external_id="multi-1",
            fetched_at=base,
            domain_tags=["ai", "finance"],
        )
        conn.commit()
    finally:
        conn.close()

    assert [a.external_id for a in read_raw(domain="ai", db_path=empty_db)] == ["multi-1"]
    assert [a.external_id for a in read_raw(domain="finance", db_path=empty_db)] == ["multi-1"]
    # 不存在的 domain
    assert list(read_raw(domain="crypto", db_path=empty_db)) == []


def test_since_filter_by_fetched_at(empty_db: Path) -> None:
    """5 条不同 fetched_at；since=中位 → 返 ≥ 中位的 3 条。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(5)]  # 0,1,2,3,4
    try:
        for i, t in enumerate(times):
            _insert(conn, external_id=f"e{i}", fetched_at=t)
        conn.commit()
    finally:
        conn.close()

    median = times[2]  # base + 2h
    got = list(read_raw(since=median, db_path=empty_db))
    assert [a.external_id for a in got] == ["e2", "e3", "e4"]


def test_source_types_filter(empty_db: Path) -> None:
    """rss×2 + web×1 → ['rss']=2; ['web']=1; 不传=3。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(conn, source_type="rss", external_id="r1", fetched_at=base)
        _insert(conn, source_type="rss", external_id="r2", fetched_at=base + timedelta(minutes=1))
        _insert(conn, source_type="web", external_id="w1", fetched_at=base + timedelta(minutes=2))
        conn.commit()
    finally:
        conn.close()

    rss_only = list(read_raw(source_types=["rss"], db_path=empty_db))
    web_only = list(read_raw(source_types=["web"], db_path=empty_db))
    all_types = list(read_raw(db_path=empty_db))
    assert len(rss_only) == 2 and {a.source_type for a in rss_only} == {"rss"}
    assert len(web_only) == 1 and web_only[0].source_type == "web"
    assert len(all_types) == 3


def test_limit(empty_db: Path) -> None:
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        for i in range(5):
            _insert(conn, external_id=f"e{i}", fetched_at=base + timedelta(minutes=i))
        conn.commit()
    finally:
        conn.close()

    got = list(read_raw(limit=2, db_path=empty_db))
    assert len(got) == 2
    assert [a.external_id for a in got] == ["e0", "e1"]


def test_order_by_fetched_at_asc_stable(empty_db: Path) -> None:
    """乱序插入 → 输出严格 ASC。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    # 故意乱序
    minutes = [3, 0, 4, 1, 2]
    try:
        for m in minutes:
            _insert(conn, external_id=f"m{m}", fetched_at=base + timedelta(minutes=m))
        conn.commit()
    finally:
        conn.close()

    got = list(read_raw(db_path=empty_db))
    assert [a.external_id for a in got] == ["m0", "m1", "m2", "m3", "m4"]
    # 单调
    fetched = [a.fetched_at for a in got]
    assert fetched == sorted(fetched)


def test_article_raw_is_frozen(empty_db: Path) -> None:
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(conn, external_id="e1", fetched_at=base)
        conn.commit()
    finally:
        conn.close()

    art = next(iter(read_raw(db_path=empty_db)))
    with pytest.raises((FrozenInstanceError, AttributeError)):
        art.title = "mutated"  # type: ignore[misc]


def test_published_at_null_yields_none(empty_db: Path) -> None:
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(conn, external_id="no-pub", fetched_at=base, published_at=None)
        conn.commit()
    finally:
        conn.close()

    art = next(iter(read_raw(db_path=empty_db)))
    assert art.published_at is None
    assert isinstance(art.fetched_at, datetime)


def test_domain_tags_deserialized_to_list(empty_db: Path) -> None:
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(
            conn,
            external_id="multi",
            fetched_at=base,
            domain_tags=["ai", "finance"],
        )
        conn.commit()
    finally:
        conn.close()

    art = next(iter(read_raw(domain="ai", db_path=empty_db)))
    assert art.domain_tags == ["ai", "finance"]
    assert isinstance(art.domain_tags, list)


def test_internal_fields_not_exposed() -> None:
    """ArticleRaw 不应暴露采集层内部字段；content_hash 作为内容指纹暴露给消费方。"""
    fields = set(ArticleRaw.__dataclass_fields__.keys())
    forbidden = {
        "url_canonical_hash",
        "status",
        "last_error",
        "is_long_form",
    }
    leaked = fields & forbidden
    assert not leaked, f"ArticleRaw 暴露了内部字段: {leaked}"
    # 同时确认 12 个业务字段全在（s1-schema-cleanup §9-D8：content_hash 暴露）
    expected = {
        "id",
        "source_type",
        "source_id",
        "source_tier",
        "external_id",
        "url",
        "title",
        "body",
        "content_hash",
        "published_at",
        "fetched_at",
        "domain_tags",
    }
    assert fields == expected, f"ArticleRaw 字段不符合契约: {fields}"


def test_content_hash_exposed_and_propagated(empty_db: Path) -> None:
    """content_hash 透传给消费方：64 字符 sha256 hex，与原始内容一致。"""
    conn = get_conn(empty_db)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    title = "Hello World"
    body = "A" * 600  # 故意超 500 字，验证算法只取前 500 字
    expected_hash = hashlib.sha256((title + body[:500]).encode("utf-8")).hexdigest()
    try:
        _insert(
            conn,
            external_id="ch-1",
            title=title,
            body=body,
            fetched_at=base,
        )
        conn.commit()
    finally:
        conn.close()

    art = next(iter(read_raw(db_path=empty_db)))
    assert art.content_hash == expected_hash
    assert len(art.content_hash) == 64
    # sha256 hex 仅含 0-9a-f
    assert all(c in "0123456789abcdef" for c in art.content_hash)


def test_db_path_override_does_not_touch_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传入 db_path 时不应去碰 ~/.newsbox/raw.db。"""
    # 把 home 重定向到 tmp，确保即使去查也不会命中真实数据
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    db_path = tmp_path / "custom.db"
    init_db(db_path)
    conn = get_conn(db_path)
    base = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    try:
        _insert(conn, external_id="custom-1", fetched_at=base)
        conn.commit()
    finally:
        conn.close()

    got = list(read_raw(db_path=db_path))
    assert [a.external_id for a in got] == ["custom-1"]

    # 默认路径 ~/.newsbox/raw.db 不应存在（因为我们没创建）
    default_db = fake_home / ".newsbox" / "raw.db"
    assert not default_db.exists()


def test_missing_db_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(FileNotFoundError):
        list(read_raw(db_path=missing))


# ---- get_reddit_comments / RedditCommentRow (s13-reddit-comments-enrich) ----


_INSERT_COMMENT_SQL = """
    INSERT INTO reddit_comments (
        article_id, comment_id, parent_id, author, score, body,
        created_utc, rank
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_comment(
    conn: sqlite3.Connection,
    *,
    article_id: int,
    comment_id: str,
    rank: int,
    score: int = 10,
    author: str = "alice",
    body: str = "comment body",
    parent_id: str | None = "t3_post",
    created_utc: datetime | None = None,
) -> None:
    conn.execute(
        _INSERT_COMMENT_SQL,
        (
            article_id,
            comment_id,
            parent_id,
            author,
            score,
            body,
            created_utc.isoformat() if created_utc else None,
            rank,
        ),
    )


def _seed_article_and_comments(
    db_path: Path,
    *,
    article_external_id: str = "t3_post",
    comments: list[tuple[str, int, int]] | None = None,
) -> int:
    """灌一条 reddit 帖子 + 指定评论行；返回 article_id。

    comments 元组: (comment_id, rank, score)。
    """
    base = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    conn = get_conn(db_path)
    try:
        _insert(
            conn,
            source_type="rss",
            source_id="r_codex",
            external_id=article_external_id,
            url=f"https://www.reddit.com/r/codex/comments/{article_external_id}/x/",
            fetched_at=base,
        )
        article_id = conn.execute(
            "SELECT id FROM articles_raw WHERE external_id = ?",
            (article_external_id,),
        ).fetchone()[0]
        for cid, rank, score in comments or []:
            _insert_comment(
                conn,
                article_id=article_id,
                comment_id=cid,
                rank=rank,
                score=score,
            )
        conn.commit()
        return article_id
    finally:
        conn.close()


def test_get_reddit_comments_empty_when_no_rows(empty_db: Path) -> None:
    """article 存在但无评论 → 返回空 list（非 reddit 帖子同理）。"""
    article_id = _seed_article_and_comments(empty_db, comments=[])
    assert get_reddit_comments(article_id, db_path=empty_db) == []
    # 不存在的 article_id 也应返回空 list（外键侧无对应行）
    assert get_reddit_comments(99999, db_path=empty_db) == []


def test_get_reddit_comments_ordered_by_rank_asc(empty_db: Path) -> None:
    """5 条评论故意乱序插入，输出严格按 rank ASC（rank=1 在前）。"""
    article_id = _seed_article_and_comments(
        empty_db,
        comments=[
            ("t1_e", 5, 1),   # rank 5
            ("t1_a", 1, 100), # rank 1
            ("t1_c", 3, 30),  # rank 3
            ("t1_b", 2, 80),  # rank 2
            ("t1_d", 4, 10),  # rank 4
        ],
    )
    rows = get_reddit_comments(article_id, db_path=empty_db)
    assert [r.comment_id for r in rows] == ["t1_a", "t1_b", "t1_c", "t1_d", "t1_e"]
    assert [r.rank for r in rows] == [1, 2, 3, 4, 5]
    # 同时确认 article_id 透传正确
    assert all(r.article_id == article_id for r in rows)


def test_get_reddit_comments_isolates_by_article_id(empty_db: Path) -> None:
    """两个 article 各自评论不互窜。"""
    art_a = _seed_article_and_comments(
        empty_db,
        article_external_id="t3_postA",
        comments=[("t1_a1", 1, 50), ("t1_a2", 2, 30)],
    )
    art_b = _seed_article_and_comments(
        empty_db,
        article_external_id="t3_postB",
        comments=[("t1_b1", 1, 70)],
    )
    rows_a = get_reddit_comments(art_a, db_path=empty_db)
    rows_b = get_reddit_comments(art_b, db_path=empty_db)
    assert {r.comment_id for r in rows_a} == {"t1_a1", "t1_a2"}
    assert {r.comment_id for r in rows_b} == {"t1_b1"}


def test_reddit_comment_row_is_frozen(empty_db: Path) -> None:
    article_id = _seed_article_and_comments(
        empty_db, comments=[("t1_x", 1, 50)]
    )
    row = get_reddit_comments(article_id, db_path=empty_db)[0]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        row.score = 999  # type: ignore[misc]


def test_reddit_comment_row_handles_null_optional_fields(empty_db: Path) -> None:
    """created_utc 与 parent_id 允许 NULL；列回 None 不报错。"""
    base = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    conn = get_conn(empty_db)
    try:
        _insert(
            conn,
            source_type="rss",
            source_id="r_codex",
            external_id="t3_null",
            url="https://www.reddit.com/r/codex/comments/t3_null/x/",
            fetched_at=base,
        )
        article_id = conn.execute(
            "SELECT id FROM articles_raw WHERE external_id = 't3_null'"
        ).fetchone()[0]
        # created_utc=NULL + parent_id=NULL（顶层评论无父；reddit 偶有缺时间）
        _insert_comment(
            conn,
            article_id=article_id,
            comment_id="t1_naked",
            rank=1,
            parent_id=None,
            created_utc=None,
        )
        conn.commit()
    finally:
        conn.close()

    row = get_reddit_comments(article_id, db_path=empty_db)[0]
    assert row.parent_id is None
    assert row.created_utc is None
    assert row.comment_id == "t1_naked"


def test_get_reddit_comments_missing_db_raises_file_not_found(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(FileNotFoundError):
        get_reddit_comments(1, db_path=missing)


def test_reddit_comment_row_field_contract() -> None:
    """RedditCommentRow 字段集合锁定（防 schema 漂移）。"""
    fields = set(RedditCommentRow.__dataclass_fields__.keys())
    expected = {
        "article_id",
        "comment_id",
        "parent_id",
        "author",
        "score",
        "body",
        "created_utc",
        "rank",
    }
    assert fields == expected, f"RedditCommentRow 字段不符合契约: {fields}"
