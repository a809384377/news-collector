"""验证 tests/conftest.py 提供的 tmp_raw_db / populated_raw_db fixture 工作正常。

这些 fixture 由 s5-data-views 引入，给 read / stats / clean 三个命令测试共享。
"""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import ANCHOR


def test_tmp_raw_db_is_empty(tmp_raw_db):
    db_path, conn = tmp_raw_db
    assert db_path.exists()
    cur = conn.execute("SELECT COUNT(*) FROM articles_raw")
    assert cur.fetchone()[0] == 0


def test_populated_raw_db_has_20_rows(populated_raw_db):
    _, conn = populated_raw_db
    cur = conn.execute("SELECT COUNT(*) FROM articles_raw")
    assert cur.fetchone()[0] == 20


def test_populated_raw_db_source_type_split(populated_raw_db):
    _, conn = populated_raw_db
    cur = conn.execute(
        "SELECT source_type, COUNT(*) FROM articles_raw GROUP BY source_type"
    )
    counts = dict(cur.fetchall())
    assert counts == {"rss": 16, "web": 4}


def test_populated_raw_db_30d_boundary(populated_raw_db):
    """clean --before=30d 应该删 4 条（60d/58d/55d/31d），保留 16 条（30d 整 + 更新）。"""
    _, conn = populated_raw_db
    cutoff = (ANCHOR - timedelta(days=30)).isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM articles_raw WHERE fetched_at < ?", (cutoff,)
    )
    assert cur.fetchone()[0] == 4


def test_populated_raw_db_24h_boundary(populated_raw_db):
    """read --since=24h 应该读 4 条（23h/12h/1h/1.5h）。"""
    _, conn = populated_raw_db
    cutoff = (ANCHOR - timedelta(hours=24)).isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM articles_raw WHERE fetched_at >= ?", (cutoff,)
    )
    assert cur.fetchone()[0] == 4


def test_populated_raw_db_finance_domain_isolated(populated_raw_db):
    """read --domain=ai 应该过滤掉单 finance 行（仅一条）。"""
    _, conn = populated_raw_db
    cur = conn.execute(
        "SELECT COUNT(*) FROM articles_raw "
        "WHERE EXISTS (SELECT 1 FROM json_each(domain_tags) WHERE value='ai')"
    )
    ai_count = cur.fetchone()[0]
    assert ai_count == 19  # 20 总 - 1 行 ['finance']


def test_populated_raw_db_published_at_mix(populated_raw_db):
    """published_at 应该有约一半 NULL（用于覆盖 NULL 处理路径）。"""
    _, conn = populated_raw_db
    cur = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END) AS null_cnt, "
        "  SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS not_null_cnt "
        "FROM articles_raw"
    )
    null_cnt, not_null_cnt = cur.fetchone()
    assert null_cnt + not_null_cnt == 20
    assert null_cnt >= 5  # 至少有几条 NULL
    assert not_null_cnt >= 5  # 至少有几条非 NULL


def test_populated_raw_db_tiers_all_present(populated_raw_db):
    """3 个 source_tier 都有数据。"""
    _, conn = populated_raw_db
    cur = conn.execute("SELECT DISTINCT source_tier FROM articles_raw")
    tiers = {row[0] for row in cur.fetchall()}
    assert tiers == {"official_first_party", "kol", "secondary"}
