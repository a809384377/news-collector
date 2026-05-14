"""db 模块单元测试。

覆盖：
1. ``init_db`` 建出 articles_raw + source_state 两张表与全部索引
2. ``apply_migrations`` 幂等
3. ``schema_migrations`` 跟踪表正确记录
4. articles_raw 表 ``(source_type, external_id)`` UNIQUE 约束生效
5. articles_raw.domain_tags 缺省值为 '["ai"]'
6. 0002 migration 应用后：status / last_error 列已删；idx_status / idx_domain 已删
7. 0003 migration 应用后：reddit_comments 表 + idx_reddit_comments_article 索引就位
   UNIQUE(article_id, comment_id) + FK 生效
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from newsbox import db as db_module
from newsbox.db import apply_migrations, get_conn, init_db


# ---- helpers ---------------------------------------------------------------


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {row[0] for row in cur.fetchall()}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {row[0] for row in cur.fetchall()}


# ---- tests -----------------------------------------------------------------


def test_apply_migrations_creates_tables(tmp_path: Path) -> None:
    """采集层表：articles_raw + source_state；0003 起加 reddit_comments；不再有 clusters / reports / articles_vec。"""
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        tables = _table_names(conn)
        for required in ("articles_raw", "source_state", "reddit_comments"):
            assert required in tables, f"缺少表 {required}; got {tables}"
        # 已删除的 AI 加工层表
        for forbidden in ("articles", "clusters", "reports", "articles_vec"):
            assert forbidden not in tables, f"采集层不应有表 {forbidden}; got {tables}"

        indexes = _index_names(conn)
        # 0001 建 5 个 idx_*，0002 删除 idx_status + idx_domain → 剩 3 个；0003 加 idx_reddit_comments_article
        idx_prefix_count = sum(1 for name in indexes if name.startswith("idx_"))
        assert idx_prefix_count >= 4, f"idx_* 索引不足 4 个: {indexes}"
        assert "idx_reddit_comments_article" in indexes, (
            f"0003 应建 idx_reddit_comments_article: {indexes}"
        )
        # 0002 已删除的索引必须不在
        for forbidden_idx in ("idx_status", "idx_domain"):
            assert forbidden_idx not in indexes, (
                f"{forbidden_idx} 应被 0002_drop_dead_fields.sql 删除: {indexes}"
            )
    finally:
        conn.close()


def test_articles_raw_dead_columns_removed(tmp_path: Path) -> None:
    """0002 migration 应用后 articles_raw.status / last_error 列已删除。"""
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        cur = conn.execute("PRAGMA table_info(articles_raw)")
        col_names = {row[1] for row in cur.fetchall()}
        for forbidden_col in ("status", "last_error"):
            assert forbidden_col not in col_names, (
                f"articles_raw.{forbidden_col} 应被 0002 migration 删除: {col_names}"
            )
        # source_state.last_error 是另一张表，必须保留（按信源记账失败信息）
        cur = conn.execute("PRAGMA table_info(source_state)")
        ss_cols = {row[1] for row in cur.fetchall()}
        assert "last_error" in ss_cols, f"source_state.last_error 不应被删: {ss_cols}"
    finally:
        conn.close()


def test_apply_migrations_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    # 第二次调用 apply_migrations 应该返回空列表
    conn = get_conn(db_path)
    try:
        migrations_dir = Path(db_module.__file__).parent / "migrations"
        result = apply_migrations(conn, migrations_dir)
        assert result == [], f"第二次迁移不应有新应用文件，got {result}"
    finally:
        conn.close()


def test_schema_migrations_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        )
        names = [row[0] for row in cur.fetchall()]
        assert "0001_init.sql" in names, f"schema_migrations 缺少 0001_init.sql: {names}"
    finally:
        conn.close()


def test_articles_raw_unique_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        insert_sql = """
            INSERT INTO articles_raw (
                source_type, source_id, source_tier, external_id,
                url, url_canonical_hash, content_hash, title, body,
                fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            "rss", "x_test", "kol", "abc",
            "https://x.com/test/abc",
            "hash-url", "hash-content", "Title", "Body",
            "2026-05-08T00:00:00",
        )
        conn.execute(insert_sql, params)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            # 同样的 (source_type='rss', external_id='abc') 必须冲突
            conn.execute(insert_sql, params)
            conn.commit()
    finally:
        conn.close()


def test_reddit_comments_unique_and_fk(tmp_path: Path) -> None:
    """0003：reddit_comments UNIQUE(article_id, comment_id) + FK 行为。

    - 同 (article_id, comment_id) 重复 insert 必须冲突
    - FK 指向不存在的 articles_raw.id 时 PRAGMA foreign_keys=ON 应拦截
    """
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        # 先插一条 articles_raw 拿到 article_id
        conn.execute(
            """
            INSERT INTO articles_raw (
                source_type, source_id, source_tier, external_id,
                url, url_canonical_hash, content_hash, title, body,
                fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rss", "r_localllama", "secondary", "t3_abc",
                "https://www.reddit.com/r/LocalLLaMA/comments/abc/foo/",
                "hash-url", "hash-content", "Title", "Body",
                "2026-05-14T00:00:00",
            ),
        )
        article_id = conn.execute("SELECT id FROM articles_raw WHERE external_id='t3_abc'").fetchone()[0]
        conn.commit()

        insert_comment_sql = """
            INSERT INTO reddit_comments (
                article_id, comment_id, parent_id, author, score, body, created_utc, rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (article_id, "t1_xx1", "t3_abc", "alice", 10, "ok", None, 1)
        conn.execute(insert_comment_sql, params)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert_comment_sql, params)
            conn.commit()

        # FK 拦截：article_id 指向不存在
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert_comment_sql,
                (99999, "t1_yy1", "t3_xxx", "bob", 5, "x", None, 1),
            )
            conn.commit()
    finally:
        conn.close()


def test_articles_raw_domain_tags_default(tmp_path: Path) -> None:
    """未显式提供 domain_tags 时 schema 默认 '["ai"]'（D5）。"""
    db_path = tmp_path / "raw.db"
    init_db(db_path)

    conn = get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO articles_raw (
                source_type, source_id, source_tier, external_id,
                url, url_canonical_hash, content_hash, title, body,
                fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rss", "x_test", "kol", "no-tag",
                "https://x.com/test/no-tag",
                "h", "c", "t", "b",
                "2026-05-08T00:00:00",
            ),
        )
        conn.commit()

        cur = conn.execute(
            "SELECT domain_tags FROM articles_raw WHERE external_id='no-tag'"
        )
        raw = cur.fetchone()[0]
        assert json.loads(raw) == ["ai"]
    finally:
        conn.close()
