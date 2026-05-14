"""pipeline.fetch 测试：用 stub adapter 注入，验证编排 + 去重 + source_state。

末尾追加了 ``newsbox fetch`` CLI 命令的 ``--json`` 测试（s9 Step 2）。
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from newsbox import db as db_module
from newsbox.commands import fetch as fetch_cmd_mod
from newsbox.config import (
    AppConfig,
    FetchConfig,
    HttpRetryConfig,
    LoggingConfig,
    Secrets,
)
from newsbox.models import RawArticle
from newsbox.pipeline import fetch as fetch_module


# ---- 测试辅助 ---------------------------------------------------------------


def _make_config() -> AppConfig:
    """构造最小可用 AppConfig；限流统一 0 秒以加速测试。"""
    return AppConfig(
        fetch=FetchConfig(
            default_since="24h",
            per_source_rate_limit_seconds={
                "rss": 0,
                "web": 0,
            },
            http_retry=HttpRetryConfig(max_attempts=4, backoff_base_seconds=1),
            consecutive_failure_skip=3,
        ),
        logging=LoggingConfig(
            level="info", file="/tmp/log", rotation="daily", retention_days=30
        ),
        secrets=Secrets(),
    )


def _make_article(
    source_id: str,
    ext_id: str,
    url: str,
    *,
    source_type: str = "rss",
    title: str | None = None,
    body: str | None = None,
) -> RawArticle:
    return RawArticle(
        source_type=source_type,
        source_id=source_id,
        external_id=ext_id,
        url=url,
        title=title or f"Title-{ext_id}",
        body=body or f"Body-{ext_id}",
        published_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


class StubAdapter:
    """注入用 stub adapter；构造时给定 articles 列表，fetch 直接返回。"""

    def __init__(
        self,
        articles: list[RawArticle],
        *,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._articles = articles
        self._raise = raise_on_fetch

    @classmethod
    def factory(
        cls,
        articles: list[RawArticle],
        *,
        raise_on_fetch: Exception | None = None,
    ):
        """返回 callable，run_fetch 用 ``registry[type]()`` 实例化。"""

        def make() -> "StubAdapter":
            return cls(articles, raise_on_fetch=raise_on_fetch)

        return make

    async def fetch(
        self, source: dict[str, Any], since: datetime | None
    ) -> list[RawArticle]:
        if self._raise:
            raise self._raise
        return list(self._articles)


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    db_module.init_db(db_path)
    return db_path


def _setup_sources_yaml(tmp_path: Path, content: str | None = None) -> Path:
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        content
        or (
            "rss:\n"
            "  - id: anthropic_blog\n"
            "    url: https://www.anthropic.com/rss.xml\n"
            "    tier: official_first_party\n"
        ),
        encoding="utf-8",
    )
    return yaml_path


def _run(coro):
    return asyncio.run(coro)


# ---- _select_sources --------------------------------------------------------


def test_select_sources_all_returns_all() -> None:
    sources = [
        {"source_type": "rss", "id": "a"},
        {"source_type": "web", "id": "w1"},
    ]
    assert fetch_module._select_sources(sources, "all") == sources


def test_select_sources_by_type() -> None:
    sources = [
        {"source_type": "rss", "id": "a"},
        {"source_type": "web", "id": "w1"},
        {"source_type": "rss", "id": "b"},
    ]
    res = fetch_module._select_sources(sources, "rss")
    assert [s["id"] for s in res] == ["a", "b"]


def test_select_sources_by_id() -> None:
    sources = [
        {"source_type": "rss", "id": "a"},
        {"source_type": "web", "id": "w1"},
    ]
    assert [s["id"] for s in fetch_module._select_sources(sources, "w1")] == ["w1"]


def test_select_sources_no_match_returns_empty() -> None:
    sources = [{"source_type": "rss", "id": "a"}]
    assert fetch_module._select_sources(sources, "nonexistent") == []


# ---- run_fetch happy path ---------------------------------------------------


def test_run_fetch_inserts_articles(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    articles = [
        _make_article("anthropic_blog", "ext-1", "https://www.anthropic.com/news/a"),
        _make_article("anthropic_blog", "ext-2", "https://www.anthropic.com/news/b"),
        _make_article("anthropic_blog", "ext-3", "https://www.anthropic.com/news/c"),
    ]
    registry = {"rss": StubAdapter.factory(articles)}

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )

    assert len(summary.results) == 1
    r = summary.results[0]
    assert r.fetched == 3
    assert r.inserted == 3
    assert r.deduped_url == 0
    assert r.deduped_external == 0
    assert r.error is None
    assert summary.total_inserted == 3

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute("SELECT count(*) FROM articles_raw")
        assert cur.fetchone()[0] == 3
        cur = conn.execute(
            "SELECT source_type, source_id, source_tier, is_long_form "
            "FROM articles_raw ORDER BY external_id"
        )
        rows = cur.fetchall()
        # tier / is_long_form 字段写入正确（status 列已被 0002 migration 删除）
        for row in rows:
            assert row[0] == "rss"
            assert row[1] == "anthropic_blog"
            assert row[2] == "official_first_party"
            assert row[3] is None  # is_long_form 仅 X 用
    finally:
        conn.close()


def test_run_fetch_dedup_external_on_second_run(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    articles = [_make_article("anthropic_blog", "ext-1", "https://x.com/a")]
    registry = {"rss": StubAdapter.factory(articles)}

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )
    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )

    r = summary.results[0]
    assert r.fetched == 1
    assert r.inserted == 0
    assert r.deduped_external == 1
    assert r.deduped_url == 0


def test_run_fetch_dedup_url_canonical(tmp_path: Path) -> None:
    """同一 URL 加 utm 参数后 canonical 相同 — 不同 external_id 命中第二层去重。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    first = [_make_article("anthropic_blog", "ext-1", "https://x.com/post")]
    second = [
        _make_article(
            "anthropic_blog", "ext-2", "https://x.com/post?utm_source=newsletter"
        )
    ]

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={"rss": StubAdapter.factory(first)},
        )
    )
    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={"rss": StubAdapter.factory(second)},
        )
    )

    r = summary.results[0]
    assert r.fetched == 1
    assert r.inserted == 0
    assert r.deduped_url == 1
    assert r.deduped_external == 0


# ---- source_state -----------------------------------------------------------


def test_run_fetch_writes_source_state_on_success(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    articles = [_make_article("anthropic_blog", "ext-1", "https://x.com/a")]
    registry = {"rss": StubAdapter.factory(articles)}

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT consecutive_failures, last_success_external_id, last_error, "
            "last_fetch_at FROM source_state "
            "WHERE source_type='rss' AND source_id='anthropic_blog'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0                  # consecutive_failures
        assert row[1] == "ext-1"            # last_success_external_id
        assert row[2] is None               # last_error
        assert row[3] is not None           # last_fetch_at
    finally:
        conn.close()


def test_run_fetch_failure_increments_consecutive(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    registry = {"rss": StubAdapter.factory([], raise_on_fetch=RuntimeError("boom"))}

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )

    r = summary.results[0]
    assert r.error is not None
    assert "boom" in r.error
    assert r.fetched == 0
    assert r.inserted == 0

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT consecutive_failures, last_error FROM source_state "
            "WHERE source_type='rss' AND source_id='anthropic_blog'"
        )
        row = cur.fetchone()
        assert row[0] == 1
        assert "boom" in row[1]
    finally:
        conn.close()


def test_run_fetch_consecutive_failure_skip_in_all_mode(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()  # consecutive_failure_skip = 3

    conn = db_module.get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO source_state "
            "(source_type, source_id, last_fetch_at, consecutive_failures, last_error) "
            "VALUES (?, ?, ?, ?, ?)",
            ("rss", "anthropic_blog", "2026-05-01T00:00:00+00:00", 3, "old fail"),
        )
        conn.commit()
    finally:
        conn.close()

    articles = [_make_article("anthropic_blog", "ext-1", "https://x.com/a")]
    registry = {"rss": StubAdapter.factory(articles)}

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            source_filter="all",
            config=cfg,
            adapter_registry=registry,
        )
    )

    r = summary.results[0]
    assert r.skipped is True
    assert r.fetched == 0
    assert r.inserted == 0


def test_run_fetch_explicit_id_overrides_consecutive_skip(tmp_path: Path) -> None:
    """显式 ``--source=<id>`` 即使 consecutive_failures 超阈值也仍尝试。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    conn = db_module.get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO source_state "
            "(source_type, source_id, last_fetch_at, consecutive_failures) "
            "VALUES (?, ?, ?, ?)",
            ("rss", "anthropic_blog", "2026-05-01T00:00:00+00:00", 5),
        )
        conn.commit()
    finally:
        conn.close()

    articles = [_make_article("anthropic_blog", "ext-1", "https://x.com/a")]
    registry = {"rss": StubAdapter.factory(articles)}

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            source_filter="anthropic_blog",
            config=cfg,
            adapter_registry=registry,
        )
    )

    r = summary.results[0]
    assert r.skipped is False
    assert r.inserted == 1

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT consecutive_failures FROM source_state "
            "WHERE source_type='rss' AND source_id='anthropic_blog'"
        )
        # 成功后归零
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ---- filter & deferred types -------------------------------------------------


def test_run_fetch_not_implemented_treated_as_skip(tmp_path: Path) -> None:
    """适配器抛 NotImplementedError（如 changelog_page 模式留 S2）应记为 skip 而非 fail。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    registry = {
        "rss": StubAdapter.factory(
            [], raise_on_fetch=NotImplementedError("留 S2 实现")
        )
    }

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry=registry,
        )
    )

    r = summary.results[0]
    assert r.skipped is True
    assert r.error is None
    assert r.fetched == 0

    # source_state 不应有该源记录（与 DEFERRED 一致，不污染失败计数）
    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT count(*) FROM source_state "
            "WHERE source_type='rss' AND source_id='anthropic_blog'"
        )
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_run_fetch_rss_and_web_routed_to_registry(tmp_path: Path) -> None:
    """两个不同 source_type 都走正常 adapter 路径（DEFERRED_SOURCE_TYPES 为空）。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        content=(
            "rss:\n"
            "  - id: feed_a\n"
            "    url: https://a.com/feed\n"
            "    tier: kol\n"
            "web:\n"
            "  - id: anthropic_news\n"
            "    url: https://www.anthropic.com/news\n"
            "    tier: official_first_party\n"
        ),
    )
    cfg = _make_config()

    rss_articles = [
        _make_article(
            "feed_a",
            "rss-1",
            "https://a.com/p1",
            source_type="rss",
        )
    ]
    web_articles = [
        _make_article(
            "anthropic_news",
            "https://www.anthropic.com/news/post-1",
            "https://www.anthropic.com/news/post-1",
            source_type="web",
        )
    ]

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={
                "rss": StubAdapter.factory(rss_articles),
                "web": StubAdapter.factory(web_articles),
            },
        )
    )

    assert len(summary.results) == 2
    for r in summary.results:
        assert r.skipped is False
        assert r.error is None
        assert r.fetched == 1
        assert r.inserted == 1
    assert summary.total_inserted == 2


def test_deferred_source_types_is_empty_after_s2() -> None:
    """S2 完成后 DEFERRED_SOURCE_TYPES 应为空集，避免任何 source 被错误 skip。"""
    assert fetch_module.DEFERRED_SOURCE_TYPES == frozenset()


def test_run_fetch_no_match_returns_empty_summary(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            source_filter="nonexistent",
            config=cfg,
            adapter_registry={"rss": StubAdapter.factory([])},
        )
    )

    assert summary.results == []
    assert summary.total_inserted == 0


def test_run_fetch_propagates_is_long_form(tmp_path: Path) -> None:
    """adapter 设的 is_long_form 字段应原样写入 articles 表（X 用，但 pipeline 透传）。

    用 rss 源类型只是为了避开 DEFERRED_SOURCE_TYPES — 字段透传逻辑与 source_type 无关。
    """
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(tmp_path)
    cfg = _make_config()

    art = RawArticle(
        source_type="rss",
        source_id="anthropic_blog",
        external_id="ext-lf",
        url="https://www.anthropic.com/news/lf",
        title="t1",
        body="b1",
        published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        is_long_form="article",
    )

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={"rss": StubAdapter.factory([art])},
        )
    )
    assert summary.results[0].inserted == 1

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT is_long_form FROM articles_raw WHERE external_id='ext-lf'"
        )
        assert cur.fetchone()[0] == "article"
    finally:
        conn.close()


def test_run_fetch_skip_url_dedup_allows_same_canonical(tmp_path: Path) -> None:
    """changelog_page 多 section 共享 base url 时 skip_url_dedup=True 应允许多条入库。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        content=(
            "web:\n"
            "  - id: chatgpt_release_notes\n"
            "    mode: changelog_page\n"
            "    url: https://help.openai.com/x\n"
            "    tier: official_first_party\n"
        ),
    )
    cfg = _make_config()

    base_url = "https://help.openai.com/articles/chatgpt-release-notes"
    sections = [
        RawArticle(
            source_type="web",
            source_id="chatgpt_release_notes",
            external_id=f"chatgpt_release_notes#2026-05-{day:02d}",
            url=base_url,
            title=f"Update May {day}",
            body=f"Body for May {day}",
            published_at=datetime(2026, 5, day, tzinfo=timezone.utc),
            skip_url_dedup=True,
        )
        for day in (3, 5, 7)
    ]

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={"web": StubAdapter.factory(sections)},
        )
    )

    r = summary.results[0]
    assert r.inserted == 3
    assert r.deduped_url == 0
    assert r.deduped_external == 0

    # 验证三条都进库 + url_canonical_hash 相同（同 base url）
    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT count(*), count(DISTINCT url_canonical_hash), count(DISTINCT external_id) "
            "FROM articles_raw WHERE source_id='chatgpt_release_notes'"
        )
        total, distinct_hash, distinct_ext = cur.fetchone()
        assert total == 3
        assert distinct_hash == 1       # 共享 base url
        assert distinct_ext == 3        # 靠 external_id 区分
    finally:
        conn.close()


# ---- twikit 桶派生（s10 Step 1）---------------------------------------------


def test_run_fetch_three_type_buckets_routed_by_registry(tmp_path: Path) -> None:
    """sources.yaml 含 rss + web + twikit 三段时，pipeline 通过 ADAPTER_REGISTRY
    派生分桶：三类 adapter 都被调过，各自的 RawArticle 都按 source_type 落库。

    本用例锁住 s10 Step 0.5 重构：pipeline 不再硬编码 rss / web，
    新增第 3 类（twikit）通过注入的 adapter_registry 即被自动识别 & 分桶。
    """
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        content=(
            "rss:\n"
            "  - id: feed_a\n"
            "    url: https://a.com/feed\n"
            "    tier: kol\n"
            "  - id: feed_b\n"
            "    url: https://b.com/feed\n"
            "    tier: secondary\n"
            "web:\n"
            "  - id: anthropic_news\n"
            "    url: https://www.anthropic.com/news\n"
            "    tier: official_first_party\n"
            "twikit:\n"
            "  - id: x_karpathy\n"
            "    handle: karpathy\n"
            "    tier: kol\n"
            "  - id: x_simonw\n"
            "    handle: simonw\n"
            "    tier: kol\n"
        ),
    )
    cfg = _make_config()
    # twikit 桶限速 / 并发用兜底（registry 未在 _DEFAULT 显式枚举的 source_type
    # 落到 _bucket_params 兜底分支：conc=1 / rate=1）；显式设为 0 加速测试。
    cfg.fetch.per_source_rate_limit_seconds = {"rss": 0, "web": 0, "twikit": 0}

    # 用按 source_id 路由的 stub：每个源得到自己专属的 article 列表（避免
    # 同桶内多源共享一份 list → external_id 撞库 dup_external）。
    rss_per_source = {
        "feed_a": [
            _make_article("feed_a", "rss-a-1", "https://a.com/p1", source_type="rss"),
        ],
        "feed_b": [
            _make_article("feed_b", "rss-b-1", "https://b.com/p1", source_type="rss"),
        ],
    }
    web_per_source = {
        "anthropic_news": [
            _make_article(
                "anthropic_news",
                "anthropic-news-1",
                "https://www.anthropic.com/news/post-1",
                source_type="web",
            ),
        ],
    }
    twikit_per_source = {
        "x_karpathy": [
            _make_article(
                "x_karpathy",
                "1791234567890123456",
                "https://x.com/karpathy/status/1791234567890123456",
                source_type="twikit",
            ),
            _make_article(
                "x_karpathy",
                "1791234567890123457",
                "https://x.com/karpathy/status/1791234567890123457",
                source_type="twikit",
            ),
        ],
        "x_simonw": [
            _make_article(
                "x_simonw",
                "1791234567890123999",
                "https://x.com/simonw/status/1791234567890123999",
                source_type="twikit",
            ),
            _make_article(
                "x_simonw",
                "1791234567890124000",
                "https://x.com/simonw/status/1791234567890124000",
                source_type="twikit",
            ),
        ],
    }

    class _RoutingStub:
        """按 source_id 路由的 adapter stub（避免同桶多源共享 article 列表）。"""

        def __init__(self, mapping: dict[str, list[RawArticle]]) -> None:
            self._mapping = mapping

        @classmethod
        def factory(cls, mapping: dict[str, list[RawArticle]]):
            def make() -> "_RoutingStub":
                return cls(mapping)
            return make

        async def fetch(
            self, source: dict[str, Any], since: datetime | None
        ) -> list[RawArticle]:
            return list(self._mapping.get(source["id"], []))

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={
                "rss": _RoutingStub.factory(rss_per_source),
                "web": _RoutingStub.factory(web_per_source),
                "twikit": _RoutingStub.factory(twikit_per_source),
            },
        )
    )

    # 4 个源（2 rss + 1 web + 2 twikit）都跑过，没人 skip / error
    assert len(summary.results) == 5
    for r in summary.results:
        assert r.skipped is False, f"{r.source_type}:{r.source_id} 不应 skip"
        assert r.error is None, f"{r.source_type}:{r.source_id} error={r.error}"

    # 各桶被分组正确：通过结果中 source_type 计数 + DB 行数双重验证
    by_type: dict[str, int] = {}
    for r in summary.results:
        by_type[r.source_type] = by_type.get(r.source_type, 0) + 1
    assert by_type == {"rss": 2, "web": 1, "twikit": 2}

    conn = db_module.get_conn(db_path)
    try:
        cur = conn.execute(
            "SELECT source_type, count(*) FROM articles_raw GROUP BY source_type"
        )
        counts = dict(cur.fetchall())
        # 每个 source_type 写入条数 = 对应 adapter 返回数量
        # rss: 2 源 × 1 条 = 2；web: 1 源 × 1 条 = 1；twikit: 2 源 × 2 条 = 4
        assert counts.get("rss") == 2
        assert counts.get("web") == 1
        assert counts.get("twikit") == 4
    finally:
        conn.close()


def test_run_fetch_twikit_end_to_end_persists_articles_and_state(
    tmp_path: Path,
) -> None:
    """mock TwikitAdapter 返回 2 条 RawArticle，跑完后 articles_raw + source_state
    双写正确：articles 2 行 source_type='twikit'；source_state 1 行 consecutive_failures=0。
    """
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        content=(
            "twikit:\n"
            "  - id: x_test\n"
            "    handle: test_user\n"
            "    tier: kol\n"
            "    domain: [ai]\n"
        ),
    )
    cfg = _make_config()
    cfg.fetch.per_source_rate_limit_seconds = {"rss": 0, "web": 0, "twikit": 0}

    tweets = [
        RawArticle(
            source_type="twikit",
            source_id="x_test",
            external_id="1791000000000000001",
            url="https://x.com/test_user/status/1791000000000000001",
            title="First tweet",
            body="First tweet body",
            published_at=datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc),
        ),
        RawArticle(
            source_type="twikit",
            source_id="x_test",
            external_id="1791000000000000002",
            url="https://x.com/test_user/status/1791000000000000002",
            title="Second tweet",
            body="Second tweet body",
            published_at=datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ]

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={"twikit": StubAdapter.factory(tweets)},
        )
    )

    assert len(summary.results) == 1
    r = summary.results[0]
    assert r.source_type == "twikit"
    assert r.source_id == "x_test"
    assert r.fetched == 2
    assert r.inserted == 2
    assert r.error is None
    assert r.skipped is False
    assert summary.total_inserted == 2

    conn = db_module.get_conn(db_path)
    try:
        # articles_raw: 2 行 twikit
        cur = conn.execute(
            "SELECT count(*) FROM articles_raw WHERE source_type='twikit'"
        )
        assert cur.fetchone()[0] == 2

        # source_state: 1 行 twikit / x_test，consecutive_failures=0，
        # last_success_external_id 是 published_at 最新的那条
        cur = conn.execute(
            "SELECT consecutive_failures, last_success_external_id, last_error "
            "FROM source_state WHERE source_type='twikit' AND source_id='x_test'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "1791000000000000002"  # 最新 published_at 那条
        assert row[2] is None
    finally:
        conn.close()


def test_run_fetch_filters_by_type(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        content=(
            "rss:\n"
            "  - id: feed_a\n"
            "    url: https://a.com/feed\n"
            "web:\n"
            "  - id: web_a\n"
            "    url: https://a.com/news\n"
        ),
    )
    cfg = _make_config()

    rss_articles = [_make_article("feed_a", "ext-rss-1", "https://a.com/p1")]
    web_articles = [
        _make_article("web_a", "ext-web-1", "https://a.com/news/p1", source_type="web")
    ]

    summary = _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            source_filter="rss",
            config=cfg,
            adapter_registry={
                "rss": StubAdapter.factory(rss_articles),
                "web": StubAdapter.factory(web_articles),
            },
        )
    )

    # 仅 rss 被跑
    assert len(summary.results) == 1
    assert summary.results[0].source_type == "rss"
    assert summary.results[0].source_id == "feed_a"
    assert summary.results[0].inserted == 1


# ---- 并发架构（s2-collector-resilience Step 4）------------------------------


class _ConcurrencyTracker:
    """共享统计：``active`` 当前并发数；``max`` 历史峰值；``enter_log`` 进入次数。"""

    def __init__(self) -> None:
        self.active = 0
        self.max = 0
        self.enter_log: list[float] = []
        self.exit_log: list[float] = []


class _TrackingAdapter:
    """每次 fetch sleep 一段时间、记录并发峰值；用 tracker 共享类间数据。"""

    def __init__(
        self,
        tracker: _ConcurrencyTracker,
        articles: list[RawArticle],
        sleep_seconds: float = 0.05,
    ) -> None:
        self._tracker = tracker
        self._articles = articles
        self._sleep = sleep_seconds

    @classmethod
    def factory(
        cls,
        tracker: _ConcurrencyTracker,
        articles: list[RawArticle],
        sleep_seconds: float = 0.05,
    ):
        def make() -> "_TrackingAdapter":
            return cls(tracker, articles, sleep_seconds=sleep_seconds)
        return make

    async def fetch(
        self, source: dict[str, Any], since: datetime | None
    ) -> list[RawArticle]:
        self._tracker.active += 1
        self._tracker.max = max(self._tracker.max, self._tracker.active)
        loop = asyncio.get_event_loop()
        self._tracker.enter_log.append(loop.time())
        try:
            await asyncio.sleep(self._sleep)
        finally:
            self._tracker.active -= 1
            self._tracker.exit_log.append(loop.time())
        return list(self._articles)


def _setup_n_rss_sources(tmp_path: Path, n: int) -> Path:
    """生成含 N 个 rss 源的 sources.yaml，用于并发桶测试。"""
    lines = ["rss:"]
    for i in range(n):
        lines.extend([
            f"  - id: feed_{i}",
            f"    url: https://example.com/feed_{i}.xml",
            "    tier: secondary",
        ])
    return _setup_sources_yaml(tmp_path, "\n".join(lines) + "\n")


def test_concurrency_param_caps_rss_bucket_max_active(tmp_path: Path) -> None:
    """rss 桶并发上限由 concurrency 参数控制；max_active ≤ concurrency。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_n_rss_sources(tmp_path, n=10)
    cfg = _make_config()
    tracker = _ConcurrencyTracker()

    article = _make_article("feed_x", "ext", "https://example.com/p")

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={
                "rss": _TrackingAdapter.factory(tracker, [article], sleep_seconds=0.05),
            },
            concurrency=3,
        )
    )

    assert tracker.max <= 3, f"max_active={tracker.max} 超过 concurrency=3"
    assert tracker.max >= 2, f"max_active={tracker.max}：并发未生效，仍像串行"
    assert len(tracker.enter_log) == 10, "10 个源都应被处理一次"


def test_concurrency_default_uses_config_value(tmp_path: Path) -> None:
    """不传 concurrency → 读 config.fetch.concurrency.rss。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_n_rss_sources(tmp_path, n=8)
    cfg = _make_config()
    cfg.fetch.concurrency = {"rss": 4, "web": 1}
    tracker = _ConcurrencyTracker()

    article = _make_article("feed_x", "ext", "https://example.com/p")

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={
                "rss": _TrackingAdapter.factory(tracker, [article], sleep_seconds=0.05),
            },
            # 不传 concurrency
        )
    )

    assert tracker.max <= 4
    assert tracker.max >= 2  # 并发实际生效


def test_web_bucket_always_serial_regardless_of_concurrency(tmp_path: Path) -> None:
    """web 桶并发度由 config.fetch.concurrency.web 决定；--concurrency 仅影响 rss。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        "web:\n"
        "  - id: w1\n"
        "    url: https://example.com/w1\n"
        "    tier: secondary\n"
        "  - id: w2\n"
        "    url: https://example.com/w2\n"
        "    tier: secondary\n"
        "  - id: w3\n"
        "    url: https://example.com/w3\n"
        "    tier: secondary\n",
    )
    cfg = _make_config()
    cfg.fetch.concurrency = {"rss": 10, "web": 1}
    tracker = _ConcurrencyTracker()

    article = _make_article("w_any", "ext", "https://example.com/p", source_type="web")

    _run(
        fetch_module.run_fetch(
            tmp_path,
            db_path=db_path,
            sources_yaml=yaml_path,
            config=cfg,
            adapter_registry={
                "web": _TrackingAdapter.factory(tracker, [article], sleep_seconds=0.05),
            },
            concurrency=10,  # 命令行尝试拉到 10 — 但 web 不应被影响
        )
    )

    assert tracker.max == 1, f"web 桶应严格串行；max_active={tracker.max}"


def test_rss_and_web_buckets_run_in_parallel(tmp_path: Path) -> None:
    """两个桶外层并行启动：1 个慢 rss + 1 个慢 web，总耗时 ≈ max(单桶) 而非和。"""
    db_path = _setup_db(tmp_path)
    yaml_path = _setup_sources_yaml(
        tmp_path,
        "rss:\n"
        "  - id: r1\n"
        "    url: https://example.com/r1\n"
        "    tier: secondary\n"
        "web:\n"
        "  - id: w1\n"
        "    url: https://example.com/w1\n"
        "    tier: secondary\n",
    )
    cfg = _make_config()
    rss_tracker = _ConcurrencyTracker()
    web_tracker = _ConcurrencyTracker()

    rss_art = _make_article("r1", "ext-r", "https://example.com/r/p")
    web_art = _make_article(
        "w1", "ext-w", "https://example.com/w/p", source_type="web"
    )

    start = asyncio.new_event_loop().time()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            fetch_module.run_fetch(
                tmp_path,
                db_path=db_path,
                sources_yaml=yaml_path,
                config=cfg,
                adapter_registry={
                    "rss": _TrackingAdapter.factory(
                        rss_tracker, [rss_art], sleep_seconds=0.10
                    ),
                    "web": _TrackingAdapter.factory(
                        web_tracker, [web_art], sleep_seconds=0.10
                    ),
                },
            )
        )
    finally:
        loop.close()
    elapsed = asyncio.new_event_loop().time() - start

    # rss 与 web 桶外层并行：总耗时应远小于 0.20s（两桶串行的和），
    # 容差 0.18s 防 CI 抖动 / 事件循环开销。
    assert elapsed < 0.18, (
        f"两桶应外层并行，elapsed={elapsed:.3f}s 太长，疑似串行"
    )


# ---- CLI `--json` 测试（s9 Step 2）------------------------------------------


def _build_fetch_app() -> typer.Typer:
    app = typer.Typer()
    app.command("fetch")(fetch_cmd_mod.fetch_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def test_fetch_cli_json_happy(tmp_path: Path, monkeypatch: Any) -> None:
    """CLI fetch --json 成功：emit_ok 一块 JSON，含 total_inserted + results 数组。"""
    home = tmp_path / "home"
    home.mkdir()

    # mock load_app_config 返回最小 cfg（避免读 home/config.yaml + init_logging）
    monkeypatch.setattr(fetch_cmd_mod, "load_app_config", lambda *a, **kw: _make_config())

    # mock pipeline.run_fetch 返回一个固定 summary
    summary = fetch_module.FetchSummary(
        results=[
            fetch_module.SourceFetchResult(
                source_type="rss",
                source_id="anthropic_blog",
                fetched=3,
                inserted=2,
                deduped_url=0,
                deduped_external=1,
            ),
            fetch_module.SourceFetchResult(
                source_type="web",
                source_id="some_web",
                fetched=0,
                inserted=0,
                skipped=True,
            ),
        ],
        total_inserted=2,
    )

    async def _fake_run_fetch(*args: Any, **kwargs: Any):
        return summary

    monkeypatch.setattr(fetch_module, "run_fetch", _fake_run_fetch)

    runner = CliRunner()
    result = runner.invoke(_build_fetch_app(), ["fetch", "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["ok"] is True
    assert payload["message"] == "fetch complete"
    d = payload["details"]
    assert d["total_inserted"] == 2
    assert len(d["results"]) == 2
    r0 = d["results"][0]
    assert r0["source_type"] == "rss"
    assert r0["source_id"] == "anthropic_blog"
    assert r0["fetched"] == 3
    assert r0["inserted"] == 2
    assert r0["deduped_external"] == 1
    assert r0["skipped"] is False
    assert r0["error"] is None
    r1 = d["results"][1]
    assert r1["skipped"] is True


def test_fetch_cli_json_empty_results(tmp_path: Path, monkeypatch: Any) -> None:
    """CLI fetch --json 无匹配源：emit_ok 但 results=[]，不输出 '(no sources matched)' 文本。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(fetch_cmd_mod, "load_app_config", lambda *a, **kw: _make_config())

    async def _fake_run_fetch(*args: Any, **kwargs: Any):
        return fetch_module.FetchSummary()

    monkeypatch.setattr(fetch_module, "run_fetch", _fake_run_fetch)

    runner = CliRunner()
    result = runner.invoke(_build_fetch_app(), ["fetch", "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["ok"] is True
    assert payload["details"]["total_inserted"] == 0
    assert payload["details"]["results"] == []
    # 人类视图的提示不应混入 JSON 输出
    assert "no sources matched" not in result.output


def test_fetch_cli_json_invalid_since(tmp_path: Path, monkeypatch: Any) -> None:
    """CLI fetch --json --since=<invalid>：emit_err + exit 2，不抛 traceback（codex P1 修）。"""
    home = tmp_path / "home"
    home.mkdir()
    # 不需 mock load_app_config / run_fetch —— --since 解析在它们之前
    runner = CliRunner()
    result = runner.invoke(
        _build_fetch_app(),
        ["fetch", "--home", str(home), "--json", "--since", "not-a-time"],
    )

    assert result.exit_code == 2, result.output
    payload = _json.loads(result.output)
    assert payload["ok"] is False
    assert "invalid --since" in payload["message"]
    assert payload["details"]["since"] == "not-a-time"
