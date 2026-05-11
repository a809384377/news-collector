"""pipeline.fetch 测试：用 stub adapter 注入，验证编排 + 去重 + source_state。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from news_collector import db as db_module
from news_collector.config import (
    AppConfig,
    FetchConfig,
    HttpRetryConfig,
    LoggingConfig,
    Secrets,
)
from news_collector.models import RawArticle
from news_collector.pipeline import fetch as fetch_module


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
