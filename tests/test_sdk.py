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
from newsbox.sdk import ArticleRaw, read_raw


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
