"""``sources add`` 子命令：录入新信源到 sources.yaml（一命令三形态）。

s4-sources-management Step 7 subagent D 产出。所有 yaml 操作走 ``_io`` 公开 API，
url 探测走 ``_probe`` 公开 API。

三形态判定
==========
- **形态 A 智能交互**：传 ``url`` + 不传任何 ``--tier/--domain/--id/--type``
  → ``probe`` 探测 → tty 下用 ``typer.prompt`` 收字段 → 入库
- **形态 B 非交互直录**：传 ``url`` + 至少传 ``--tier`` → 不需要 tty，agent 自动化场景
- **形态 C 批量文件**：``--from-file=urls.txt`` + 不传 ``url`` → 每行 1-4 token

Probe 一致性
============
形态 A 的 probe 失败（reachable=False）由用户确认是否仍录入；形态 B/C 不阻塞，
按用户提供的字段直接录入（reachable=False 仅在批量输出里标 ``[err]``）。

Url 重复检测
============
形态 B 检测 sources.yaml 里 url 字段已存在 → 友好提示 + Exit(1)，避免一 url 多
id 误录；形态 A 智能交互的 reachable=False 后用户确认录入也走相同检测。形态 C
批量录入对每条独立检测，重复 url 标 ``[skip]``。

Stdin tty 检测
==============
直接 ``sys.stdin.isatty()`` 在 ``CliRunner`` 下不可 monkeypatch（CliRunner 替换 sys.stdin，
KNOWLEDGE-LOG #15）。包了一层 ``_stdin_is_tty()``，测试 monkeypatch 这个名字。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from ruamel.yaml.comments import CommentedMap

from .._helpers import home_option
from . import _io
from ._probe import ProbeResult, probe, suggest_id

# 默认值（形态 B 缺字段时使用）
_DEFAULT_DOMAIN = ["ai"]
_DEFAULT_TYPE_FALLBACK = "rss"
_BATCH_DEFAULT_TIER = "secondary"

# 形态 A 交互式 tier prompt 提示文本
_TIER_HINT = "tier (kol / official_first_party / secondary)"


def _stdin_is_tty() -> bool:
    """检查 stdin 是否为交互终端。

    包一层让测试 monkeypatch ``add_cmd._stdin_is_tty`` 而不动 sys.stdin
    （CliRunner 替换 sys.stdin，monkeypatch ``sys.stdin.isatty`` 失效，
    KNOWLEDGE-LOG #15）。
    """
    return sys.stdin.isatty()


def _split_domain(raw: str | None) -> list[str]:
    """把逗号分隔字符串切成 list；空 / None 走默认。"""
    if raw is None:
        return list(_DEFAULT_DOMAIN)
    parts = [d.strip() for d in raw.split(",") if d.strip()]
    return parts or list(_DEFAULT_DOMAIN)


def _build_item(
    *, source_id: str, url: str, tier: str, domain: list[str]
) -> CommentedMap:
    """构造 source item dict，固定字段顺序 id / url / tier / domain。"""
    item: CommentedMap = CommentedMap()
    item["id"] = source_id
    item["url"] = url
    item["tier"] = tier
    item["domain"] = list(domain)
    return item


def _url_already_present(data: CommentedMap, url: str) -> str | None:
    """扫描所有 source 看 url 是否已存在；返回占用 url 的 id 或 None。"""
    for kind in _io.SOURCE_KINDS:
        items = data.get(kind) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("url") == url:
                return item.get("id")
    return None


def _run_probe(url: str) -> ProbeResult:
    """同步包装 ``await probe(url)``。"""
    return asyncio.run(probe(url))


def _format_probe_summary(pr: ProbeResult) -> str:
    """形态 A 给用户看的 probe 汇总。"""
    lines = [
        "[probe] reachable={reach}  type={t}  status={s}".format(
            reach="yes" if pr.reachable else "no",
            t=pr.source_type or "?",
            s=pr.status_code if pr.status_code is not None else "-",
        ),
    ]
    if pr.suggested_id:
        lines.append(f"        suggested_id={pr.suggested_id}")
    if pr.sample_title:
        lines.append(f"        sample_title={pr.sample_title}")
    if pr.error:
        lines.append(f"        error={pr.error}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 形态 A：智能交互式录入
# ----------------------------------------------------------------------


def _interactive_add(
    *, url: str, home: Path, yaml_path: Path
) -> None:
    """交互式智能录入主流程。"""
    pr = _run_probe(url)
    typer.echo(_format_probe_summary(pr))

    # 非 tty 拒绝交互
    if not _stdin_is_tty():
        typer.echo(
            "[err] add 智能录入需要 tty；非交互环境请用 --tier --domain --id [--type]",
            err=True,
        )
        raise typer.Exit(code=1)

    # reachable=False 让用户决定是否仍录入
    if not pr.reachable:
        proceed = typer.confirm(
            "url 当前不可达，是否仍要录入？", default=False
        )
        if not proceed:
            typer.echo("[skip] add cancelled")
            return

    # 收集字段
    tier = typer.prompt(_TIER_HINT)
    if not tier or not tier.strip():
        typer.echo("[err] tier required", err=True)
        raise typer.Exit(code=1)
    tier = tier.strip()

    domain_raw = typer.prompt("domain (csv)", default="ai")
    domain = _split_domain(domain_raw)

    id_default = pr.suggested_id or ""
    if id_default:
        sid = typer.prompt("id", default=id_default)
    else:
        sid = typer.prompt("id")
    if not sid or not sid.strip():
        typer.echo("[err] id required", err=True)
        raise typer.Exit(code=1)
    sid = sid.strip()

    type_default = pr.source_type or _DEFAULT_TYPE_FALLBACK
    stype_raw = typer.prompt("type (rss / web)", default=type_default)
    stype = (stype_raw or "").strip().lower()
    if stype not in _io.SOURCE_KINDS:
        typer.echo(
            f"[err] type must be one of {_io.SOURCE_KINDS}, got {stype!r}",
            err=True,
        )
        raise typer.Exit(code=1)

    # 持久化
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        # 形态 A 也允许从空 yaml 开始（首次录入）
        data = CommentedMap()

    # url 重复检测
    occupier = _url_already_present(data, url)
    if occupier is not None:
        typer.echo(
            f"[err] url already present under id={occupier}; "
            f"use `sources edit {occupier}` instead",
            err=True,
        )
        raise typer.Exit(code=1)

    item = _build_item(source_id=sid, url=url, tier=tier, domain=domain)

    try:
        _io.upsert_source(data, kind=stype, item=item)
    except _io.SourceIdConflictError:
        typer.echo(f"[err] id conflict: {sid}", err=True)
        raise typer.Exit(code=1)
    except _io.SourceKindError as e:
        typer.echo(f"[err] {e}", err=True)
        raise typer.Exit(code=1)

    _io.save_yaml(yaml_path, data)
    typer.echo(f"[ok] added {sid} ({stype})")


# ----------------------------------------------------------------------
# 形态 B：非交互式直录
# ----------------------------------------------------------------------


def _non_interactive_add(
    *,
    url: str,
    tier: str,
    domain_raw: str | None,
    source_id: str | None,
    source_type: str | None,
    yaml_path: Path,
) -> None:
    """非交互式直录主流程；id / type 缺省走 probe 兜底。"""
    domain = _split_domain(domain_raw)

    # id 缺省：先 suggest_id，仍 None 抛 BadParameter
    sid = source_id
    if sid is None:
        sid = suggest_id(url)
        if not sid:
            raise typer.BadParameter(
                "--id required (suggest_id failed for given url)"
            )
    sid = sid.strip()

    # type 缺省：probe 探测；reachable=False 用 fallback
    stype = source_type
    if stype is None:
        pr = _run_probe(url)
        stype = pr.source_type or _DEFAULT_TYPE_FALLBACK
    stype = stype.strip().lower()
    if stype not in _io.SOURCE_KINDS:
        raise typer.BadParameter(
            f"--type must be one of {_io.SOURCE_KINDS}, got {stype!r}"
        )

    # load yaml；不存在视为空（首次录入）
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        data = CommentedMap()

    # url 重复检测
    occupier = _url_already_present(data, url)
    if occupier is not None:
        typer.echo(
            f"[err] url already present under id={occupier}; "
            f"use `sources edit {occupier}` instead",
            err=True,
        )
        raise typer.Exit(code=1)

    item = _build_item(source_id=sid, url=url, tier=tier, domain=domain)
    try:
        _io.upsert_source(data, kind=stype, item=item)
    except _io.SourceIdConflictError:
        typer.echo(f"[err] id conflict: {sid}", err=True)
        raise typer.Exit(code=1)
    except _io.SourceKindError as e:
        typer.echo(f"[err] {e}", err=True)
        raise typer.Exit(code=1)

    _io.save_yaml(yaml_path, data)
    typer.echo(f"[ok] added {sid} ({stype})")


# ----------------------------------------------------------------------
# 形态 C：批量文件录入
# ----------------------------------------------------------------------


def _parse_batch_line(line: str) -> tuple[str, str | None, str | None, str | None] | None:
    """解析一行：返回 ``(url, tier, domain, id)``；注释 / 空行返回 None。

    简化语法：每行 1-4 个 token（空白分隔）。
    第一个 token 必须是 url；缺省字段为 None 走默认。
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split()
    url = parts[0]
    tier = parts[1] if len(parts) > 1 else None
    domain = parts[2] if len(parts) > 2 else None
    sid = parts[3] if len(parts) > 3 else None
    return (url, tier, domain, sid)


def _batch_add(*, from_file: Path, yaml_path: Path) -> None:
    """批量录入主流程：每行独立处理，逐行输出结果，末尾汇总。"""
    if not from_file.exists():
        typer.echo(f"[err] file not found: {from_file}", err=True)
        raise typer.Exit(code=1)

    try:
        text = from_file.read_text(encoding="utf-8")
    except OSError as e:
        typer.echo(f"[err] cannot read file: {e}", err=True)
        raise typer.Exit(code=1)

    # 加载 yaml；不存在视为空（首次录入也允许）
    try:
        data = _io.load_yaml(yaml_path)
    except FileNotFoundError:
        data = CommentedMap()

    n_added = 0
    n_skipped = 0
    n_errored = 0

    for raw_line in text.splitlines():
        parsed = _parse_batch_line(raw_line)
        if parsed is None:
            continue
        url, tier_opt, domain_opt, id_opt = parsed

        tier = tier_opt or _BATCH_DEFAULT_TIER
        domain = _split_domain(domain_opt)

        # url 重复 → skip
        occupier = _url_already_present(data, url)
        if occupier is not None:
            typer.echo(f"[skip] {url} — url already present under id={occupier}")
            n_skipped += 1
            continue

        # probe + 兜底
        try:
            pr = _run_probe(url)
        except Exception as e:  # noqa: BLE001 — probe 内部已保证不抛，兜底
            typer.echo(f"[err]  {url} — probe failed: {type(e).__name__}: {e}")
            n_errored += 1
            continue

        if not pr.reachable:
            typer.echo(
                f"[err]  {url} — probe failed: {pr.error or 'unreachable'}"
            )
            n_errored += 1
            continue

        sid = id_opt or pr.suggested_id or suggest_id(url)
        if not sid:
            typer.echo(f"[err]  {url} — could not suggest id")
            n_errored += 1
            continue

        stype = pr.source_type or _DEFAULT_TYPE_FALLBACK

        item = _build_item(source_id=sid, url=url, tier=tier, domain=domain)
        try:
            _io.upsert_source(data, kind=stype, item=item)
        except _io.SourceIdConflictError:
            typer.echo(f"[skip] {sid} — id conflict")
            n_skipped += 1
            continue
        except _io.SourceKindError as e:
            typer.echo(f"[err]  {url} — {e}")
            n_errored += 1
            continue

        typer.echo(f"[ok]   {sid} ({stype}) — added")
        n_added += 1

    # 持久化（即使 0 added 也写一次保持幂等）
    if n_added > 0:
        _io.save_yaml(yaml_path, data)

    typer.echo("---")
    typer.echo(f"{n_added} added, {n_skipped} skipped, {n_errored} error")


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------


def sources_add_cmd(
    url: str | None = typer.Argument(
        None, help="要录入的 URL（与 --from-file 互斥）"
    ),
    tier: str | None = typer.Option(None, "--tier", help="信源等级"),
    domain: str | None = typer.Option(
        None, "--domain", help="逗号分隔，如 'ai,finance'"
    ),
    source_id: str | None = typer.Option(
        None, "--id", help="信源 id（不给则用 probe 推荐）"
    ),
    source_type: str | None = typer.Option(
        None, "--type", help="rss / web；不给则用 probe 探测结果"
    ),
    from_file: Path | None = typer.Option(
        None,
        "--from-file",
        help="批量录入；每行：<url>[<空白><tier> <domain> <id>]",
    ),
    home: Path = home_option(),
) -> None:
    """录入信源（交互/非交互/批量三形态）。

    - 智能交互：``add <url>`` 不带其他字段 → probe + 交互 prompt
    - 非交互直录：``add <url> --tier=... [--domain=...] [--id=...] [--type=...]``
    - 批量文件：``add --from-file=urls.txt``（与 url 位置参数互斥）
    """
    yaml_path = home / "sources.yaml"

    # 互斥参数检查
    if url is not None and from_file is not None:
        raise typer.BadParameter(
            "url and --from-file are mutually exclusive"
        )
    if url is None and from_file is None:
        raise typer.BadParameter(
            "either url or --from-file is required"
        )

    # 形态 C：批量
    if from_file is not None:
        _batch_add(from_file=from_file, yaml_path=yaml_path)
        return

    # url 一定不为 None（上面互斥分支保证）
    assert url is not None

    # 形态 A vs B：是否传了任何配置字段
    has_any_field = (
        tier is not None
        or domain is not None
        or source_id is not None
        or source_type is not None
    )

    if not has_any_field:
        # 形态 A：交互
        _interactive_add(url=url, home=home, yaml_path=yaml_path)
        return

    # 形态 B：非交互
    if tier is None:
        raise typer.BadParameter("--tier required for non-interactive add")
    _non_interactive_add(
        url=url,
        tier=tier,
        domain_raw=domain,
        source_id=source_id,
        source_type=source_type,
        yaml_path=yaml_path,
    )
