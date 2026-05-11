"""pytest 公共 fixture。

s5-data-views 引入 ``tmp_raw_db`` / ``populated_raw_db`` 给 read / stats / clean
三个命令测试共享。设计要点见 ai/sprints/active/s5-data-views/DECISIONS.md D3。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from news_collector.db import get_conn, init_db


# tmp_db 数据围绕这个 anchor 排布：60d 前 / 30d 前 / 24h 前等关键切边界都基于
# anchor 推算。测试 since/--before 过滤时建议直接用 ANCHOR + timedelta 构造
# datetime cutoff，避免与 utils.time.parse_since 真实"当前时刻"耦合。
ANCHOR = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


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
    source_type: str,
    source_id: str,
    source_tier: str,
    external_id: str,
    title: str,
    body: str,
    fetched_at: datetime,
    published_at: datetime | None = None,
    domain_tags: list[str] | None = None,
    url: str | None = None,
) -> None:
    """灌一行 articles_raw（test_sdk.py 同款 helper）。content_hash / url_canonical_hash 派生默认值。"""
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
            None,
        ),
    )


@pytest.fixture
def tmp_raw_db(tmp_path: Path):
    """空 raw.db：跑过 init_db 拿到 schema，conn 打开供测试灌数据。

    yield (db_path, conn)。conn 在 fixture teardown 时关闭。
    """
    db_path = tmp_path / "raw.db"
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        yield db_path, conn
    finally:
        conn.close()


@pytest.fixture
def populated_raw_db(tmp_raw_db):
    """tmp_raw_db + 20 行假数据。覆盖：

    - 2 source_type（rss × 16 / web × 4）
    - 3 source_tier（official_first_party / kol / secondary）
    - 多 domain：18 行 ['ai'] + 1 行 ['ai','finance'] + 1 行 ['finance']
    - 时间分布跨 60 天，关键边界点：60d / 30d / 7d / 24h / now-1h
    - published_at：~半数有值、半数 NULL
    """
    db_path, conn = tmp_raw_db
    _populate(conn)
    conn.commit()
    return db_path, conn


def _populate(conn: sqlite3.Connection) -> None:
    a = ANCHOR

    rows = [
        # ---- 60d 前（旧数据，clean --before=30d 应删） ----
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-old-1",
            title="Old Anthropic news 1",
            body="Old body 1",
            fetched_at=a - timedelta(days=60),
            published_at=a - timedelta(days=60, hours=2),
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-old-2",
            title="Old Anthropic news 2",
            body="Old body 2",
            fetched_at=a - timedelta(days=58),
            published_at=None,
        ),
        dict(
            source_type="web",
            source_id="claude_api_release_notes",
            source_tier="official_first_party",
            external_id="cl-old-1",
            title="Old Claude API release",
            body="Old changelog 1",
            fetched_at=a - timedelta(days=55),
            published_at=None,
        ),
        # ---- 30d 边界（含 31d 外 / 30d 整 / 29d 内） ----
        dict(
            source_type="rss",
            source_id="simonw_blog",
            source_tier="kol",
            external_id="simon-31d",
            title="Simon 31d ago",
            body="Body 31d",
            fetched_at=a - timedelta(days=31),
            published_at=a - timedelta(days=31, hours=3),
        ),
        dict(
            source_type="rss",
            source_id="simonw_blog",
            source_tier="kol",
            external_id="simon-30d",
            title="Simon exact 30d",
            body="Body 30d",
            fetched_at=a - timedelta(days=30),
            published_at=None,
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-29d",
            title="Anthropic 29d",
            body="Body 29d",
            fetched_at=a - timedelta(days=29),
            published_at=a - timedelta(days=29, hours=1),
        ),
        # ---- 14d 前（含多 domain ai+finance） ----
        dict(
            source_type="rss",
            source_id="dotey",
            source_tier="kol",
            external_id="dotey-14d",
            title="Dotey 14d ago",
            body="Body 14d",
            fetched_at=a - timedelta(days=14),
            published_at=None,
            domain_tags=["ai", "finance"],
        ),
        dict(
            source_type="web",
            source_id="claude_api_release_notes",
            source_tier="official_first_party",
            external_id="cl-14d",
            title="Claude API 14d release",
            body="Changelog 14d",
            fetched_at=a - timedelta(days=14, hours=5),
            published_at=a - timedelta(days=14, hours=5),
        ),
        # ---- 8d / 6d 前（last_7_days panel 边界附近） ----
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-8d",
            title="Anthropic 8d ago",
            body="Body 8d",
            fetched_at=a - timedelta(days=8),
            published_at=None,
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-6d",
            title="Anthropic 6d ago",
            body="Body 6d",
            fetched_at=a - timedelta(days=6),
            published_at=a - timedelta(days=6),
        ),
        dict(
            source_type="rss",
            source_id="simonw_blog",
            source_tier="kol",
            external_id="simon-6d",
            title="Simon 6d ago",
            body="Body simon 6d",
            fetched_at=a - timedelta(days=6, hours=3),
            published_at=None,
        ),
        # ---- 3d / 2d 前 ----
        dict(
            source_type="rss",
            source_id="dotey",
            source_tier="kol",
            external_id="dotey-3d",
            title="Dotey 3d ago",
            body="Body dotey 3d",
            fetched_at=a - timedelta(days=3),
            published_at=a - timedelta(days=3, hours=1),
        ),
        dict(
            source_type="web",
            source_id="claude_api_release_notes",
            source_tier="official_first_party",
            external_id="cl-3d",
            title="Claude API 3d release",
            body="Changelog 3d",
            fetched_at=a - timedelta(days=3, hours=8),
            published_at=None,
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-2d",
            title="Anthropic 2d ago",
            body="Body 2d",
            fetched_at=a - timedelta(days=2),
            published_at=a - timedelta(days=2),
        ),
        # ---- 24h 边界（read --since=24h 应过滤掉 25h 那条） ----
        dict(
            source_type="rss",
            source_id="simonw_blog",
            source_tier="kol",
            external_id="simon-25h",
            title="Simon 25h ago (boundary out)",
            body="Body simon 25h",
            fetched_at=a - timedelta(hours=25),
            published_at=None,
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-23h",
            title="Anthropic 23h ago (boundary in)",
            body="Body 23h",
            fetched_at=a - timedelta(hours=23),
            published_at=a - timedelta(hours=23),
        ),
        # ---- 12h / 1h 前 ----
        dict(
            source_type="rss",
            source_id="dotey",
            source_tier="kol",
            external_id="dotey-12h",
            title="Dotey 12h ago",
            body="Body dotey 12h",
            fetched_at=a - timedelta(hours=12),
            published_at=None,
        ),
        dict(
            source_type="rss",
            source_id="anthropic_news",
            source_tier="official_first_party",
            external_id="an-1h",
            title="Anthropic 1h ago",
            body="Body 1h",
            fetched_at=a - timedelta(hours=1),
            published_at=a - timedelta(hours=1),
        ),
        dict(
            source_type="web",
            source_id="claude_api_release_notes",
            source_tier="official_first_party",
            external_id="cl-1h",
            title="Claude API 1h release",
            body="Changelog 1h",
            fetched_at=a - timedelta(hours=1, minutes=30),
            published_at=None,
        ),
        # ---- finance 单 domain（read --domain=ai 应过滤掉） ----
        dict(
            source_type="rss",
            source_id="finance_demo",
            source_tier="secondary",
            external_id="fin-2d",
            title="Finance demo 2d",
            body="Body finance",
            fetched_at=a - timedelta(days=2, hours=4),
            published_at=None,
            domain_tags=["finance"],
        ),
    ]

    for r in rows:
        _insert(conn, **r)
