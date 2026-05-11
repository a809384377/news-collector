"""fetch 命令编排（设计 §6 去重三层 + §9 错误处理与限流）。

流程：
  1. 读 sources.yaml，按 ``source_filter`` 过滤
  2. 逐源调对应 adapter（2 类：rss / web）
  3. 对每条 RawArticle：先按 (source_type, external_id) 去重，再按 url_canonical_hash 去重，
     都不命中则 INSERT；写入失败（IntegrityError）也按"已存在"计入 dup_external
  4. 维护 source_state：成功置 consecutive_failures=0、last_success_external_id 为本批
     published_at 最新的那条；失败则 consecutive_failures += 1 并写 last_error
  5. ``--source=all`` 路径下，consecutive_failures ≥ ``config.fetch.consecutive_failure_skip``
     的源直接 skip（``--source=<id>`` 仍会尝试该具体源）
  6. 每源跑完按类型限流：``await asyncio.sleep(per_source_rate_limit_seconds[type])``

数据库写入采用即时 commit；单源失败不影响其他源继续。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from .. import db as db_module
from ..adapters.rss_adapter import RSSAdapter
from ..adapters.web_adapter import WebAdapter
from ..config import AppConfig, load_config
from ..models import RawArticle
from ..sources import iter_sources
from ..utils.url import canonicalize_url, content_hash, url_canonical_hash  # noqa: F401 — canonicalize 留给调用方诊断


# ---- 适配器路由 -------------------------------------------------------------


# 2 类适配器（s2-1-collector-extract Step 4 收敛后）：
#   rss : feedparser 兜底（含 X via RSSHub / Reddit .rss / GitHub releases.atom / 厂商 RSS）
#   web : trafilatura + Jina 兜底（默认列表两级模式；mode=changelog_page 走单页切段）
ADAPTER_REGISTRY: dict[str, Any] = {
    "rss": RSSAdapter,
    "web": WebAdapter,
}

# 保留常量便于以后扩展（如未来新增 source_type 临时占位）；当前为空。
DEFERRED_SOURCE_TYPES: frozenset[str] = frozenset()


# ---- 结果数据 ---------------------------------------------------------------


@dataclass
class SourceFetchResult:
    source_type: str
    source_id: str
    fetched: int = 0           # adapter 返回条数
    inserted: int = 0          # 实际写入条数
    deduped_url: int = 0       # 第二层 url_canonical_hash 命中跳过
    deduped_external: int = 0  # 第一层 (source_type, external_id) 命中跳过
    skipped: bool = False      # consecutive_failure_skip / 待实现源类型
    error: str | None = None


@dataclass
class FetchSummary:
    results: list[SourceFetchResult] = field(default_factory=list)
    total_inserted: int = 0


# ---- 数据库辅助 -------------------------------------------------------------


def _load_source_state(
    conn: sqlite3.Connection, source_type: str, source_id: str
) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT consecutive_failures, last_success_external_id "
        "FROM source_state WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _upsert_source_state(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    *,
    success_external_id: str | None,
    error: str | None,
    consecutive_failures: int,
    fetched_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO source_state (
            source_type, source_id, last_fetch_at,
            last_success_external_id, last_error, consecutive_failures
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            last_fetch_at = excluded.last_fetch_at,
            last_success_external_id = COALESCE(
                excluded.last_success_external_id,
                source_state.last_success_external_id
            ),
            last_error = excluded.last_error,
            consecutive_failures = excluded.consecutive_failures
        """,
        (
            source_type, source_id, fetched_at.isoformat(),
            success_external_id, error, consecutive_failures,
        ),
    )
    conn.commit()


def _insert_article(
    conn: sqlite3.Connection,
    article: RawArticle,
    *,
    source_tier: str,
    domain_tags: list[str],
    fetched_at: datetime,
) -> str:
    """写入一条 articles_raw 记录，返回 ``"inserted" / "dup_external" / "dup_url"``。

    实现去重前两层（设计 §6）：
      - 第一层：``(source_type, external_id)`` 命中 → ``dup_external``
      - 第二层：``url_canonical_hash`` 命中 → ``dup_url``
    """
    cur = conn.execute(
        "SELECT 1 FROM articles_raw WHERE source_type = ? AND external_id = ? LIMIT 1",
        (article.source_type, article.external_id),
    )
    if cur.fetchone():
        return "dup_external"

    canonical = url_canonical_hash(article.url)
    # 第二层 url_canonical_hash 去重：changelog_page 单页多 section 共享 base url
    # 时由 adapter 设 ``skip_url_dedup=True`` 跳过本层（D1）；其他 source 走默认路径。
    if not article.skip_url_dedup:
        cur = conn.execute(
            "SELECT 1 FROM articles_raw WHERE url_canonical_hash = ? LIMIT 1",
            (canonical,),
        )
        if cur.fetchone():
            return "dup_url"

    c_hash = content_hash(article.title, article.body)
    published = article.published_at.isoformat() if article.published_at else None

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO articles_raw (
            source_type, source_id, source_tier, external_id,
            url, url_canonical_hash, content_hash,
            title, body, is_long_form,
            published_at, fetched_at,
            domain_tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article.source_type, article.source_id, source_tier, article.external_id,
            article.url, canonical, c_hash,
            article.title, article.body,
            article.is_long_form,
            published, fetched_at.isoformat(),
            json.dumps(domain_tags),
        ),
    )
    conn.commit()
    if cur.rowcount > 0:
        return "inserted"
    # 走到这里说明同 batch 内重复 external_id（adapter 输出不洁），按 dup_external 计
    return "dup_external"


# ---- 单源抓取 ---------------------------------------------------------------


def _select_sources(sources: list[dict], source_filter: str) -> list[dict]:
    """``--source`` 过滤：``all`` / ``<source_type>`` / ``<source_id>``。"""
    if source_filter == "all":
        return list(sources)
    typed = [s for s in sources if s["source_type"] == source_filter]
    if typed:
        return typed
    return [s for s in sources if s.get("id") == source_filter]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _fetch_one_source(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    *,
    since: datetime | None,
    config: AppConfig,
    source_filter: str,
    adapter_registry: dict[str, Any],
) -> SourceFetchResult:
    source_type = source["source_type"]
    source_id = source["id"]
    tier = source.get("tier", "secondary")
    # domain_tags 来自 sources.yaml 信源条目（Step 4 后每条都有 domain: [ai]）；
    # 缺失或类型异常时回落到 ["ai"]（D5：一期统一 ai 领域）。
    raw_domain = source.get("domain", ["ai"])
    domain_tags = list(raw_domain) if isinstance(raw_domain, list) else ["ai"]
    res = SourceFetchResult(source_type=source_type, source_id=source_id)

    # S2 待实现的类型直接跳过
    if source_type in DEFERRED_SOURCE_TYPES:
        logger.warning(
            f"[skip] {source_type}:{source_id} — {source_type} 适配器留 S2 实现"
        )
        res.skipped = True
        return res

    # consecutive_failure_skip：仅 --source=all/类型 路径生效，--source=<id> 强制尝试
    state = _load_source_state(conn, source_type, source_id)
    if (
        state
        and source_filter != source_id
        and state["consecutive_failures"] >= config.fetch.consecutive_failure_skip
    ):
        logger.warning(
            f"[skip] {source_type}:{source_id} — 连续失败 "
            f"{state['consecutive_failures']} 次 ≥ {config.fetch.consecutive_failure_skip}"
        )
        res.skipped = True
        return res

    if source_type not in adapter_registry:
        logger.error(f"[fail] {source_type}:{source_id} — 未注册的 adapter 类型")
        res.error = f"unknown adapter: {source_type}"
        return res

    adapter = adapter_registry[source_type]()
    fetched_at = _now_utc()

    try:
        articles = await adapter.fetch(source, since)
    except NotImplementedError as exc:
        # 适配器明确声明"未实现"（用于占位的子模式或临时停用路径）
        # 与 DEFERRED_SOURCE_TYPES 语义一致：标记 skip，不污染 source_state 失败计数
        logger.warning(f"[skip] {source_type}:{source_id} — {exc}")
        res.skipped = True
        return res
    except Exception as exc:  # noqa: BLE001 — 单源失败不能拖垮全局（设计 §9）
        logger.error(f"[fail] {source_type}:{source_id} — {exc!r}")
        res.error = repr(exc)
        new_failures = (state["consecutive_failures"] if state else 0) + 1
        _upsert_source_state(
            conn, source_type, source_id,
            success_external_id=None,
            error=res.error,
            consecutive_failures=new_failures,
            fetched_at=fetched_at,
        )
        return res

    res.fetched = len(articles)
    last_external_id: str | None = None
    last_published: datetime | None = None

    for art in articles:
        try:
            status = _insert_article(
                conn, art,
                source_tier=tier,
                domain_tags=domain_tags,
                fetched_at=fetched_at,
            )
        except sqlite3.IntegrityError as exc:
            logger.warning(
                f"[soft-err] {source_type}:{source_id} "
                f"external_id={art.external_id}: {exc}"
            )
            res.deduped_external += 1
            continue

        if status == "inserted":
            res.inserted += 1
            if art.published_at and (
                last_published is None or art.published_at > last_published
            ):
                last_published = art.published_at
                last_external_id = art.external_id
            elif last_external_id is None and art.published_at is None:
                last_external_id = art.external_id
        elif status == "dup_url":
            res.deduped_url += 1
        else:  # dup_external
            res.deduped_external += 1

    _upsert_source_state(
        conn, source_type, source_id,
        success_external_id=last_external_id,
        error=None,
        consecutive_failures=0,
        fetched_at=fetched_at,
    )
    logger.info(
        f"[ok] {source_type}:{source_id} fetched={res.fetched} "
        f"inserted={res.inserted} dup_url={res.deduped_url} dup_ext={res.deduped_external}"
    )
    return res


# ---- 公共入口 ---------------------------------------------------------------


async def _run_bucket(
    bucket: list[dict[str, Any]],
    *,
    concurrency: int,
    rate_limit_seconds: int,
    conn: sqlite3.Connection,
    since: datetime | None,
    config: AppConfig,
    source_filter: str,
    adapter_registry: dict[str, Any],
) -> list[SourceFetchResult]:
    """跑同一类型（rss / web）的源桶。

    ``concurrency=1`` 等价串行；``>1`` 用 ``asyncio.Semaphore`` 限并发。
    每个源完成后按 ``rate_limit_seconds`` 等待，保护上游服务。
    """
    if not bucket:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(src: dict[str, Any]) -> SourceFetchResult:
        async with sem:
            res = await _fetch_one_source(
                conn, src,
                since=since,
                config=config,
                source_filter=source_filter,
                adapter_registry=adapter_registry,
            )
            if not res.skipped and not res.error and rate_limit_seconds > 0:
                await asyncio.sleep(rate_limit_seconds)
            return res

    return await asyncio.gather(*(_one(s) for s in bucket))


async def run_fetch(
    home: Path,
    *,
    db_path: Path | None = None,
    sources_yaml: Path | None = None,
    since: datetime | None = None,
    source_filter: str = "all",
    config: AppConfig | None = None,
    adapter_registry: dict[str, Any] | None = None,
    concurrency: int | None = None,
) -> FetchSummary:
    """编排入口。CLI 与测试都从这里进。

    Args:
        home: 运行时数据目录（如 ~/.newsbox）
        db_path: 缺省 ``home / 'raw.db'``
        sources_yaml: 缺省 ``home / 'sources.yaml'``
        since: ``None`` 表示不过滤
        source_filter: ``"all"`` / ``<source_type>`` / ``<source_id>``
        config: 注入的 AppConfig；缺省 ``load_config(home)``
        adapter_registry: 注入的 adapter 工厂表（测试用）；缺省 ``ADAPTER_REGISTRY``
        concurrency: rss 桶并发度覆盖（``None`` → 走 ``config.fetch.concurrency.rss``）。
                     web 桶始终串行（爬虫友好），不受此参数影响。
    """
    cfg = config or load_config(home)
    db_p = db_path or (home / "raw.db")
    sources_p = sources_yaml or (home / "sources.yaml")
    registry = adapter_registry if adapter_registry is not None else ADAPTER_REGISTRY

    all_sources = iter_sources(sources_p)
    target = _select_sources(all_sources, source_filter)
    if not target:
        logger.warning(f"[fetch] no sources match filter={source_filter!r}")
        return FetchSummary()

    # 幂等保证 schema 存在（schema_migrations 去重，不会重复应用）
    db_module.init_db(db_p)
    conn = db_module.get_conn(db_p)
    try:
        # 按 source_type 分桶；保留原 yaml 顺序便于 CLI 输出对齐
        bucket_rss = [s for s in target if s["source_type"] == "rss"]
        bucket_web = [s for s in target if s["source_type"] == "web"]

        rss_conc = concurrency if concurrency is not None else cfg.fetch.concurrency.get("rss", 8)
        web_conc = cfg.fetch.concurrency.get("web", 1)
        rss_rate = cfg.fetch.per_source_rate_limit_seconds.get("rss", 1)
        web_rate = cfg.fetch.per_source_rate_limit_seconds.get("web", 2)

        # 两桶外层并行启动；rss 内部 Semaphore 限并发，web 桶串行（concurrency=1）
        rss_task = _run_bucket(
            bucket_rss,
            concurrency=rss_conc, rate_limit_seconds=rss_rate,
            conn=conn, since=since, config=cfg,
            source_filter=source_filter, adapter_registry=registry,
        )
        web_task = _run_bucket(
            bucket_web,
            concurrency=web_conc, rate_limit_seconds=web_rate,
            conn=conn, since=since, config=cfg,
            source_filter=source_filter, adapter_registry=registry,
        )
        rss_results, web_results = await asyncio.gather(rss_task, web_task)
        # 输出顺序：rss 段在前，web 段在后（与原 yaml 顺序一致）
        results: list[SourceFetchResult] = list(rss_results) + list(web_results)
    finally:
        conn.close()

    return FetchSummary(
        results=results,
        total_inserted=sum(r.inserted for r in results),
    )
