"""``news-collector setup`` — 一键装机。

执行步骤（每步幂等可重入；已就绪 ``[skip]``，未就绪 ``[do]``）：

1. 建 home 目录树：``home / {logs, cache, rsshub}``
2. 拷 ``docs/sources.seed.yaml`` → ``home/sources.yaml``（缺则拷）
3. 拷包内 ``docker-compose.yml`` → ``home/docker-compose.yml``（缺则拷）
4. 写 ``home/.env`` 模板（缺则建，含 ``TWITTER_AUTH_TOKEN=`` 占位）
5. 交互引导填 X token（检测 ``TWITTER_AUTH_TOKEN`` 为空时；用户敲回车跳过）
6. ``docker compose up -d``（容器都 Up 则 skip）
7. ``init_db``（apply_migrations 幂等：已应用迁移自动跳过）

设计取舍：
- 不提供 ``--force`` / ``--reset``（DECISIONS.md D3：与 teardown 不提供 ``--purge``
  同源精神，防误操作）
- 用户跳过 token 引导后由 ``doctor`` 显示 WARN 提示稍后填
- ``raw.db`` 检测："存在则视为已建"——init_db 内部 apply_migrations 仍会执行，
  但会跳过已应用的 migration（schema_migrations 跟踪）。这给出符合"幂等可重入"
  语义的 [do]/[skip] 输出。
- ``docker-compose.yml`` 作为 package data 内置（``news_collector.data``），
  setup 时拷贝到 home 目录；docker compose 调用统一走 ``-f <home>/docker-compose.yml``，
  让命令运行目录与项目仓库解耦（pipx 安装的 CLI 在任意目录都能跑）
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import typer

from .. import db as db_module
from .. import sources as sources_module
from . import docker_helpers
from ._helpers import home_option

_TOKEN_KEY = "TWITTER_AUTH_TOKEN"
_ENV_TEMPLATE = f"{_TOKEN_KEY}=\n"

# 子目录列表：home 自身 + 三个子目录都要存在（logs 落日志 / cache 复用预留 /
# rsshub 是 docker-compose.yml 中 redis-data 的挂载父）
_SUBDIRS: tuple[str, ...] = ("logs", "cache", "rsshub")


# ---- 步骤分子函数 ----------------------------------------------------------


def _ensure_dirs(home: Path) -> bool:
    """建 home 目录树。返回 True 若任一目录是新建的，False 若全部已存在。"""
    created = False
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)
        created = True
    for sub in _SUBDIRS:
        d = home / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created = True
    return created


def _ensure_sources_yaml(home: Path) -> bool:
    """拷 sources.seed.yaml → home/sources.yaml。返回 True 若有拷贝。"""
    target = home / "sources.yaml"
    if target.exists():
        return False
    sources_module.seed_sources(target, force=False)
    return True


def _ensure_compose_file(home: Path) -> bool:
    """拷包内 docker-compose.yml → home/docker-compose.yml。返回 True 若有拷贝。

    使用 ``importlib.resources`` 读 package data，pipx 安装后也能找到。
    """
    target = home / "docker-compose.yml"
    if target.exists():
        return False
    src = files("news_collector.data") / "docker-compose.yml"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def _ensure_env_file(home: Path) -> bool:
    """写 .env 模板。返回 True 若有写入。"""
    env_path = home / ".env"
    if env_path.exists():
        return False
    env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
    return True


def _read_token(env_path: Path) -> str:
    """读 .env 中 TWITTER_AUTH_TOKEN 的值；不存在或空字符串都返回 ""。"""
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith(f"{_TOKEN_KEY}="):
            return raw_line[len(_TOKEN_KEY) + 1 :].strip()
    return ""


def _write_token(env_path: Path, token: str) -> None:
    """把 token 写入 .env 中 TWITTER_AUTH_TOKEN 行；缺行则追加。其他行原样保留。"""
    out_lines: list[str] = []
    found = False
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            if raw_line.startswith(f"{_TOKEN_KEY}="):
                out_lines.append(f"{_TOKEN_KEY}={token}")
                found = True
            else:
                out_lines.append(raw_line)
    if not found:
        out_lines.append(f"{_TOKEN_KEY}={token}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _stdin_is_tty() -> bool:
    """抽函数便于测试 monkeypatch（CliRunner 会替换 sys.stdin，直接 patch
    sys.stdin.isatty 不生效）。"""
    import sys

    return sys.stdin.isatty()


def _guide_token(home: Path) -> str:
    """检测 token，缺则交互引导。

    Returns:
        ``"already_set"``: .env 中已有非空 token，无需引导
        ``"set"``:         用户输入了 token，已写入 .env
        ``"skip"``:        用户敲回车跳过，或非交互环境（stdin 非 tty / EOF）
    """
    env_path = home / ".env"
    current = _read_token(env_path)
    if current:
        return "already_set"
    typer.echo(
        "  需要 X (Twitter) auth_token 才能用 RSSHub 抓 X 信源。"
        "回车跳过，可稍后手工填 .env（见 docs/rsshub-setup.md §3）。"
    )

    # 非交互环境（CI / piped stdin / nohup）直接当跳过；
    # 否则交互引导。click.prompt 在 EOF 会抛 Abort，统一捕获按 skip 处理。
    if not _stdin_is_tty():
        return "skip"

    try:
        token = typer.prompt(
            "  X auth_token", default="", show_default=False, prompt_suffix="> "
        )
    except (typer.Abort, EOFError):
        return "skip"

    if not token.strip():
        return "skip"
    _write_token(env_path, token.strip())
    return "set"


def _ensure_containers(home: Path) -> str:
    """检测容器状态，未 Up 则 compose_up（compose 文件定位 ``<home>/docker-compose.yml``）。

    Returns:
        ``"already_up"``: rsshub + redis 都 Up，跳过
        ``"started"``:    调过 compose_up（可能是首次启或之前 Exited）

    Raises:
        docker_helpers.DockerError: docker daemon 未启 / compose 调用失败
    """
    if not docker_helpers.docker_daemon_alive():
        raise docker_helpers.DockerError(
            "docker daemon 未运行；请先启动 Docker Desktop（或 dockerd）"
        )
    compose_file = home / "docker-compose.yml"
    try:
        statuses = docker_helpers.container_status(compose_file)
    except docker_helpers.DockerError:
        statuses = {}
    if statuses and all(s == "Up" for s in statuses.values()):
        return "already_up"
    docker_helpers.compose_up(compose_file)
    return "started"


def _ensure_db(home: Path) -> bool:
    """init_db。返回 True 若 raw.db 是新建的（路径之前不存在）。"""
    db_path = home / "raw.db"
    existed = db_path.exists()
    db_module.init_db(db_path)
    return not existed


# ---- 命令入口 ---------------------------------------------------------------


def setup_cmd(home: Path = home_option()) -> None:
    """一键装机：建目录 + 拷信源 + 写 .env + 引导 token + 启容器 + 建库。"""
    typer.echo(f"== news-collector setup ({home}) ==")

    # 1. 目录树
    if _ensure_dirs(home):
        typer.echo(f"  [do]   directories created under {home}")
    else:
        typer.echo("  [skip] directories already exist")

    # 2. sources.yaml
    if _ensure_sources_yaml(home):
        typer.echo(f"  [do]   sources.yaml seeded → {home}/sources.yaml")
    else:
        typer.echo("  [skip] sources.yaml already exists")

    # 3. docker-compose.yml
    if _ensure_compose_file(home):
        typer.echo(f"  [do]   docker-compose.yml copied → {home}/docker-compose.yml")
    else:
        typer.echo("  [skip] docker-compose.yml already exists")

    # 4. .env 模板
    if _ensure_env_file(home):
        typer.echo(f"  [do]   .env template written → {home}/.env")
    else:
        typer.echo("  [skip] .env already exists")

    # 5. X token 引导
    token_state = _guide_token(home)
    if token_state == "already_set":
        typer.echo("  [skip] X auth_token already set in .env")
    elif token_state == "set":
        typer.echo("  [do]   X auth_token saved to .env")
    else:
        typer.echo(
            f"  [warn] X auth_token left empty; X 信源会失败。"
            f"稍后填 {home}/.env"
        )

    # 6. 容器
    try:
        cstate = _ensure_containers(home)
    except docker_helpers.DockerError as exc:
        typer.echo(f"  [err]  docker step failed: {exc}", err=True)
        raise typer.Exit(code=1)
    if cstate == "already_up":
        typer.echo("  [skip] containers already up (rsshub + redis)")
    else:
        typer.echo("  [do]   docker compose up -d")

    # 7. db init
    if _ensure_db(home):
        typer.echo(f"  [do]   raw.db created + migrations applied → {home}/raw.db")
    else:
        typer.echo("  [skip] raw.db already exists (migrations idempotent)")

    typer.echo("")
    typer.echo("Setup complete. Next: news-collector fetch --since=24h")
