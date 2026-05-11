"""``news-collector doctor`` — 健康自检。

四组检查，每条输出 ``[OK]`` / ``[WARN]`` / ``[FAIL]`` + 简要"如何修"：

1. **Docker**：daemon 在跑 + rsshub Up + redis Up
2. **Config**：``.env`` + ``TWITTER_AUTH_TOKEN`` 非空 + ``sources.yaml`` 存在
3. **Database**：``raw.db`` + ``schema_migrations`` 含全部迁移
4. **Sample fetch**：随机抽 1 rss + 1 web，跑 adapter.fetch（不入库），30s 超时

退出码：
- 任一检查项 FAIL → exit 1
- 仅 WARN（含抽样网络抖动）→ exit 0
- 全 OK → exit 0

设计取舍：
- 抽样信源失败标 ``[WARN]`` 而非 ``[FAIL]``（KNOWLEDGE-LOG #7：网络/上游波动可能误报；
  doctor 的目的是诊断系统配置而非证明信源 24h 内有更新）
- 抽样的成功标准 = adapter 能跑通（不抛错且不超时），即使 fetched=0 也算 OK
- ``schema_migrations`` 期望数从扫描包内 migrations 目录得出，不硬编码
"""
from __future__ import annotations

import asyncio
import random
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import typer

from ..adapters.rss_adapter import RSSAdapter
from ..adapters.web_adapter import WebAdapter
from ..sources import iter_sources
from . import docker_helpers
from ._helpers import home_option

_TOKEN_KEY = "TWITTER_AUTH_TOKEN"
_SAMPLE_TIMEOUT_SECONDS = 30.0
_ADAPTER_REGISTRY: dict[str, Any] = {"rss": RSSAdapter, "web": WebAdapter}


# ---- 输出工具 ---------------------------------------------------------------


class _Status:
    """收集每条检查的结果，决定最终 exit code。"""

    def __init__(self) -> None:
        self.has_fail = False

    def ok(self, msg: str) -> None:
        typer.echo(f"  [OK]   {msg}")

    def warn(self, msg: str, fix: str | None = None) -> None:
        suffix = f" — fix: {fix}" if fix else ""
        typer.echo(f"  [WARN] {msg}{suffix}")

    def fail(self, msg: str, fix: str | None = None) -> None:
        suffix = f" — fix: {fix}" if fix else ""
        typer.echo(f"  [FAIL] {msg}{suffix}")
        self.has_fail = True


# ---- 各组检查 ---------------------------------------------------------------


def _check_docker(s: _Status, home: Path) -> None:
    typer.echo("[Docker]")
    if not docker_helpers.docker_available():
        s.fail("docker CLI 未安装", fix="安装 Docker Desktop 或 docker engine")
        return
    if not docker_helpers.docker_daemon_alive():
        s.fail("docker daemon 未运行", fix="启动 Docker Desktop（或 dockerd）")
        return
    s.ok("docker daemon running")
    compose_file = home / "docker-compose.yml"
    try:
        statuses = docker_helpers.container_status(compose_file)
    except docker_helpers.DockerError as exc:
        s.fail(f"docker compose ps 失败: {exc}", fix="news-collector setup")
        return
    for svc in ("rsshub", "redis"):
        st = statuses.get(svc, "Missing")
        if st == "Up":
            s.ok(f"container {svc} = Up")
        else:
            s.fail(
                f"container {svc} = {st}",
                fix=f"news-collector restart 或 setup",
            )


def _read_token(env_path: Path) -> str:
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{_TOKEN_KEY}="):
            return line[len(_TOKEN_KEY) + 1 :].strip()
    return ""


def _check_config(s: _Status, home: Path) -> None:
    typer.echo("\n[Config]")
    env_path = home / ".env"
    if not env_path.exists():
        s.fail(f".env not found ({env_path})", fix="news-collector setup")
    else:
        s.ok(f".env exists ({env_path})")
        token = _read_token(env_path)
        if token:
            s.ok(f"{_TOKEN_KEY} set ({len(token)} chars)")
        else:
            s.warn(
                f"{_TOKEN_KEY} empty",
                fix=f"填 {env_path}（X 信源会失败但其他类型不受影响）",
            )

    sources_yaml = home / "sources.yaml"
    if sources_yaml.exists():
        s.ok(f"sources.yaml exists ({sources_yaml})")
    else:
        s.fail(
            f"sources.yaml not found ({sources_yaml})",
            fix="news-collector setup",
        )


def _expected_migration_count() -> int:
    """扫包内 migrations 目录数 .sql 文件数（实时反映新增 migration）。"""
    pkg_root = Path(__file__).resolve().parents[1]
    return len(list((pkg_root / "migrations").glob("*.sql")))


def _check_database(s: _Status, home: Path) -> None:
    typer.echo("\n[Database]")
    db_path = home / "raw.db"
    if not db_path.exists():
        s.fail(f"raw.db not found ({db_path})", fix="news-collector setup")
        return

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            with closing(
                conn.execute("SELECT COUNT(*) FROM schema_migrations")
            ) as cur:
                applied = cur.fetchone()[0]
            with closing(conn.execute("SELECT COUNT(*) FROM articles_raw")) as cur:
                article_count = cur.fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        s.fail(f"raw.db query failed: {exc}", fix="news-collector setup（重新应用迁移）")
        return

    expected = _expected_migration_count()
    if applied >= expected:
        s.ok(f"raw.db migrations applied {applied}/{expected}")
    else:
        s.fail(
            f"raw.db migrations only {applied}/{expected}",
            fix="news-collector setup（应用未跑的迁移）",
        )
    s.ok(f"raw.db articles_raw rows = {article_count}")


def _check_sample_fetch(s: _Status, home: Path) -> None:
    typer.echo("\n[Sample fetch]")
    sources_yaml = home / "sources.yaml"
    if not sources_yaml.exists():
        s.warn("skip: sources.yaml 不存在", fix="先 news-collector setup")
        return

    sources = iter_sources(sources_yaml)
    if not sources:
        s.warn("skip: sources.yaml 中无 enabled 信源", fix="检查 sources.yaml")
        return

    by_type: dict[str, list[dict[str, Any]]] = {"rss": [], "web": []}
    for src in sources:
        by_type.setdefault(src["source_type"], []).append(src)

    samples: list[dict[str, Any]] = []
    for kind in ("rss", "web"):
        bucket = by_type.get(kind) or []
        if bucket:
            samples.append(random.choice(bucket))

    if not samples:
        s.warn("skip: 无可抽样的 rss / web 信源")
        return

    for src in samples:
        kind = src["source_type"]
        sid = src.get("id", "<no-id>")
        adapter_cls = _ADAPTER_REGISTRY.get(kind)
        if adapter_cls is None:
            s.warn(f"sample {kind}:{sid} — adapter 未注册")
            continue

        try:
            articles = asyncio.run(
                asyncio.wait_for(
                    adapter_cls().fetch(src, None),
                    timeout=_SAMPLE_TIMEOUT_SECONDS,
                )
            )
            count = len(articles)
            if count > 0:
                s.ok(f"sample {kind}:{sid} fetched {count} articles")
            else:
                s.warn(
                    f"sample {kind}:{sid} fetched 0 articles",
                    fix="可能信源短期内无更新；非系统问题",
                )
        except asyncio.TimeoutError:
            s.warn(
                f"sample {kind}:{sid} timeout > {_SAMPLE_TIMEOUT_SECONDS:.0f}s",
                fix="网络或上游波动；可重试或换信源",
            )
        except Exception as exc:  # noqa: BLE001
            s.warn(
                f"sample {kind}:{sid} error: {exc!r}",
                fix="网络或上游波动；可重试或换信源",
            )


# ---- 命令入口 ---------------------------------------------------------------


def doctor_cmd(home: Path = home_option()) -> None:
    """健康自检：Docker / 配置 / 数据库 / 信源抽样。"""
    typer.echo(f"== news-collector doctor ({home}) ==\n")
    status = _Status()

    _check_docker(status, home)
    _check_config(status, home)
    _check_database(status, home)
    _check_sample_fetch(status, home)

    typer.echo("")
    if status.has_fail:
        typer.echo("doctor: FAIL — 见上方 [FAIL] 项")
        # 用 sys.exit 而非 typer.Exit：保留已 echo 输出，无栈跟踪
        sys.exit(1)
    typer.echo("doctor: OK (warnings 不阻塞)")
