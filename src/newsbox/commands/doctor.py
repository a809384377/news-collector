"""``newsbox doctor`` — 健康自检。

五组检查，每条输出 ``[OK]`` / ``[WARN]`` / ``[FAIL]`` + 简要"如何修"：

1. **Docker**：daemon 在跑 + rsshub Up + redis Up
2. **Config**：``.env`` + ``TWITTER_AUTH_TOKEN`` 非空 + ``sources.yaml`` 存在
3. **Database**：``raw.db`` + ``schema_migrations`` 含全部迁移
4. **Twikit**：仅当 ``sources.yaml`` 含 ``twikit`` 段时触发；twikit 版本 +
   ``twikit_cookies.json`` 静态字段校验（不发真实网络，避免 X 风控误报）
5. **Sample fetch**：每个 source_type 抽样 1 条，跑 adapter.fetch（不入库），30s 超时

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

from ..adapters import ADAPTER_REGISTRY
from ..sources import iter_sources
from . import docker_helpers
from ._helpers import home_option
from ._json import emit, json_option

_TOKEN_KEY = "TWITTER_AUTH_TOKEN"
_SAMPLE_TIMEOUT_SECONDS = 30.0
_TWIKIT_COOKIES_FILENAME = "twikit_cookies.json"


# ---- 输出工具 ---------------------------------------------------------------


class _Status:
    """收集每条检查的结果，决定最终 exit code。

    ``--json`` 模式下不打印人类视图，而是把每条记录到 ``checks``；最终由
    ``doctor_cmd`` 一次性 ``emit`` 出去。
    """

    def __init__(self, *, json_mode: bool = False, section: str | None = None) -> None:
        self.has_fail = False
        self.json_mode = json_mode
        self.section = section  # 当前 section（docker / config / database / sample_fetch）
        self.checks: list[dict[str, Any]] = []

    def _record(self, level: str, msg: str, fix: str | None) -> None:
        self.checks.append(
            {
                "name": self.section or "",
                "level": level,
                "message": msg,
                "fix": fix,
            }
        )

    def ok(self, msg: str) -> None:
        if self.json_mode:
            self._record("ok", msg, None)
        else:
            typer.echo(f"  [OK]   {msg}")

    def warn(self, msg: str, fix: str | None = None) -> None:
        if self.json_mode:
            self._record("warn", msg, fix)
            return
        suffix = f" — fix: {fix}" if fix else ""
        typer.echo(f"  [WARN] {msg}{suffix}")

    def fail(self, msg: str, fix: str | None = None) -> None:
        if self.json_mode:
            self._record("fail", msg, fix)
            self.has_fail = True
            return
        suffix = f" — fix: {fix}" if fix else ""
        typer.echo(f"  [FAIL] {msg}{suffix}")
        self.has_fail = True


# ---- 各组检查 ---------------------------------------------------------------


def _check_docker(s: _Status, home: Path) -> None:
    s.section = "docker"
    if not s.json_mode:
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
        s.fail(f"docker compose ps 失败: {exc}", fix="newsbox setup")
        return
    for svc in ("rsshub", "redis"):
        st = statuses.get(svc, "Missing")
        if st == "Up":
            s.ok(f"container {svc} = Up")
        else:
            s.fail(
                f"container {svc} = {st}",
                fix=f"newsbox restart 或 setup",
            )


def _read_token(env_path: Path) -> str:
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{_TOKEN_KEY}="):
            return line[len(_TOKEN_KEY) + 1 :].strip()
    return ""


def _check_config(s: _Status, home: Path) -> None:
    s.section = "config"
    if not s.json_mode:
        typer.echo("\n[Config]")
    env_path = home / ".env"
    if not env_path.exists():
        s.fail(f".env not found ({env_path})", fix="newsbox setup")
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
            fix="newsbox setup",
        )


def _expected_migration_count() -> int:
    """扫包内 migrations 目录数 .sql 文件数（实时反映新增 migration）。"""
    pkg_root = Path(__file__).resolve().parents[1]
    return len(list((pkg_root / "migrations").glob("*.sql")))


def _check_database(s: _Status, home: Path) -> None:
    s.section = "database"
    if not s.json_mode:
        typer.echo("\n[Database]")
    db_path = home / "raw.db"
    if not db_path.exists():
        s.fail(f"raw.db not found ({db_path})", fix="newsbox setup")
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
        s.fail(f"raw.db query failed: {exc}", fix="newsbox setup（重新应用迁移）")
        return

    expected = _expected_migration_count()
    if applied >= expected:
        s.ok(f"raw.db migrations applied {applied}/{expected}")
    else:
        s.fail(
            f"raw.db migrations only {applied}/{expected}",
            fix="newsbox setup（应用未跑的迁移）",
        )
    s.ok(f"raw.db articles_raw rows = {article_count}")


def _sources_yaml_has_type(sources_yaml: Path, src_type: str) -> bool:
    """``sources.yaml`` 是否包含至少一条 ``source_type=src_type`` 的 enabled 信源。

    解析异常一律视为 False（``_check_config`` 会单独 FAIL，twikit panel 不重复报错）。
    """
    if not sources_yaml.exists():
        return False
    try:
        return any(
            src.get("source_type") == src_type for src in iter_sources(sources_yaml)
        )
    except Exception:  # noqa: BLE001 — sources.yaml 损坏由 _check_config 报错
        return False


def _check_twikit_cookies(s: _Status, home: Path) -> None:
    """twikit cookie 静态校验 panel。

    触发条件：``sources.yaml`` 含至少一条 enabled twikit 信源；否则整 panel skip
    （渐进式 onboarding：用户没用 twikit 就不该见到 twikit 检查项）。

    检查内容：
        1. ``twikit.__version__`` 可读（配合 D-dep-1 升级流程文档）
        2. ``twikit_cookies.json`` 存在 + ``auth_token`` / ``ct0`` 字段就位
           （复用 ``TwikitAdapter._load_cookies_or_raise``）

    设计取舍：
        - **不发真实网络**：与现有四组 panel 的"诊断系统配置"哲学一致；活体探测
          交给 ``_check_sample_fetch``（已通过 ADAPTER_REGISTRY 派生自动覆盖 twikit）
        - ``TwikitAuthError`` 文案多行（带浏览器 devtools 步骤），doctor 单行
          展示只取首行作 message，详细恢复指引指向 ``docs/twikit-setup.md``
          （KNOWLEDGE-LOG #35：多行异常不强塞 fix）
    """
    s.section = "twikit"
    sources_yaml = home / "sources.yaml"
    if not _sources_yaml_has_type(sources_yaml, "twikit"):
        return  # 没 twikit 信源整 panel skip（不打 header / 不打 OK / 不打 WARN）

    if not s.json_mode:
        typer.echo("\n[Twikit]")

    # 1. twikit 版本号
    try:
        import twikit

        twikit_version = getattr(twikit, "__version__", "<unknown>")
        s.ok(f"twikit version = {twikit_version}")
    except ImportError:
        s.fail(
            "twikit 库未装",
            fix="uv tool install -U newsbox 或 uv sync 重装依赖",
        )
        return

    # 2. cookies 静态校验（复用 adapter）
    cookies_path = home / _TWIKIT_COOKIES_FILENAME
    try:
        from ..adapters import TwikitAdapter, TwikitAuthError
    except ImportError as exc:  # pragma: no cover — adapters/__init__ 已导出
        s.fail(
            f"TwikitAdapter 加载失败: {exc}",
            fix="检查 newsbox.adapters.__init__ 导出",
        )
        return

    try:
        adapter = TwikitAdapter(cookies_path=cookies_path)
        adapter._load_cookies_or_raise()
    except TwikitAuthError as exc:
        head = str(exc).splitlines()[0] if str(exc) else "TwikitAuthError"
        s.fail(
            f"twikit cookies 检查失败: {head}",
            fix="详见 docs/twikit-setup.md §1（cookies 获取步骤）",
        )
        return
    except Exception as exc:  # noqa: BLE001 — 防御性兜底
        s.fail(
            f"twikit cookies 检查异常: {exc!r}",
            fix="详见 docs/twikit-setup.md",
        )
        return

    s.ok(f"twikit_cookies.json present ({cookies_path})")
    s.ok("auth_token + ct0 字段就位")


def _check_sample_fetch(s: _Status, home: Path) -> None:
    s.section = "sample_fetch"
    if not s.json_mode:
        typer.echo("\n[Sample fetch]")
    sources_yaml = home / "sources.yaml"
    if not sources_yaml.exists():
        s.warn("skip: sources.yaml 不存在", fix="先 newsbox setup")
        return

    sources = iter_sources(sources_yaml)
    if not sources:
        s.warn("skip: sources.yaml 中无 enabled 信源", fix="检查 sources.yaml")
        return

    by_type: dict[str, list[dict[str, Any]]] = {k: [] for k in ADAPTER_REGISTRY}
    for src in sources:
        by_type.setdefault(src["source_type"], []).append(src)

    samples: list[dict[str, Any]] = []
    for kind in ADAPTER_REGISTRY:
        bucket = by_type.get(kind) or []
        if bucket:
            samples.append(random.choice(bucket))

    if not samples:
        s.warn(f"skip: 无可抽样的 {'/'.join(ADAPTER_REGISTRY.keys())} 信源")
        return

    for src in samples:
        kind = src["source_type"]
        sid = src.get("id", "<no-id>")
        adapter_cls = ADAPTER_REGISTRY.get(kind)
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


def doctor_cmd(
    home: Path = home_option(),
    json_output: bool = json_option(),
) -> None:
    """健康自检：Docker / 配置 / 数据库 / 信源抽样。"""
    if not json_output:
        typer.echo(f"== newsbox doctor ({home}) ==\n")
    status = _Status(json_mode=json_output)

    _check_docker(status, home)
    _check_config(status, home)
    _check_database(status, home)
    _check_twikit_cookies(status, home)
    _check_sample_fetch(status, home)

    if json_output:
        emit(
            {
                "home": str(home),
                "checks": status.checks,
                "ok": not status.has_fail,
            }
        )
        if status.has_fail:
            sys.exit(1)
        return

    typer.echo("")
    if status.has_fail:
        typer.echo("doctor: FAIL — 见上方 [FAIL] 项")
        # 用 sys.exit 而非 typer.Exit：保留已 echo 输出，无栈跟踪
        sys.exit(1)
    typer.echo("doctor: OK (warnings 不阻塞)")
