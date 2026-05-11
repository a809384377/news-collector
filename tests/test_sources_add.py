"""``commands/sources/add_cmd.py`` 测试：3 形态端到端覆盖。

策略：
- 直接组装 mini typer.Typer，挂 ``sources_add_cmd``——避免主 cli ``__init__.py``
  在本 sprint 整合阶段才把命令注册进 sources sub-app（KNOWLEDGE-LOG #14 兜底）
- 不传 ``mix_stderr``（Click 9 已移除该参数，KNOWLEDGE-LOG #17）
- monkeypatch ``add_cmd._stdin_is_tty`` 控制 tty 分支（KNOWLEDGE-LOG #15）
- monkeypatch ``add_cmd._run_probe`` / ``add_cmd.probe`` 离线伪造 ProbeResult
- typer.prompt mock 用 list pop 实现按调用顺序返回（tier → domain → id → type）
- 测试模块禁用 ``setup_*`` / ``teardown_*`` 命名（KNOWLEDGE-LOG #13）
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from newsbox.commands.sources import _io, add_cmd
from newsbox.commands.sources._probe import ProbeResult


# ---------------------------------------------------------------- helpers


def make_app() -> typer.Typer:
    """构造单命令 mini app。

    需要 ``_placeholder`` 兜底强制 group 模式，否则 typer 单命令扁平化
    会让 ``runner.invoke(app, ["add", ...])`` 报 exit 2（KNOWLEDGE-LOG #14）。
    """
    app = typer.Typer()
    app.command("add")(add_cmd.sources_add_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _make_probe_result(
    *,
    url: str = "https://example.com/news",
    reachable: bool = True,
    status_code: int | None = 200,
    source_type: str | None = "web",
    suggested_id: str | None = "example_news",
    sample_title: str | None = "Example News",
    error: str | None = None,
) -> ProbeResult:
    return ProbeResult(
        url=url,
        reachable=reachable,
        status_code=status_code,
        source_type=source_type,  # type: ignore[arg-type]
        suggested_id=suggested_id,
        sample_title=sample_title,
        error=error,
    )


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch, result: ProbeResult
) -> None:
    """把 add_cmd._run_probe 替换成返回固定 ProbeResult 的同步函数。"""
    monkeypatch.setattr(add_cmd, "_run_probe", lambda url: result)


def _read_yaml(home: Path):
    return _io.load_yaml(home / "sources.yaml")


SAMPLE_YAML = """\
# 顶部注释：信源清单
rss:
  - id: x_dotey
    url: "https://example.com/dotey/atom"
    tier: kol
    domain: [ai]

web:
  - id: anthropic_news
    url: "https://www.anthropic.com/news"
    tier: official_first_party
    domain: [ai]
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".newsbox"
    h.mkdir()
    (h / "sources.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
    return h


@pytest.fixture
def empty_home(tmp_path: Path) -> Path:
    """无 sources.yaml 的 home（首次录入场景）。"""
    h = tmp_path / ".newsbox"
    h.mkdir()
    return h


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# =================================================================
# 形态 A：智能交互式
# =================================================================


def test_form_a_interactive_full_success(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tty + probe 成功 + 用户依次输入 tier/domain/id/type → 录入成功。"""
    pr = _make_probe_result(
        url="https://newsite.example.com/blog",
        suggested_id="newsite_blog",
        source_type="web",
        sample_title="Newsite Blog",
    )
    _patch_probe(monkeypatch, pr)
    monkeypatch.setattr(add_cmd, "_stdin_is_tty", lambda: True)

    # prompt 调用顺序：tier → domain (有 default) → id (有 default) → type (有 default)
    answers = ["official_first_party", "ai,finance", "newsite_blog", "web"]

    def fake_prompt(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr("typer.prompt", fake_prompt)

    app = make_app()
    r = runner.invoke(
        app,
        ["add", "https://newsite.example.com/blog", "--home", str(home)],
    )
    assert r.exit_code == 0, r.output
    assert "[ok] added newsite_blog (web)" in r.stdout
    # probe 摘要被打印
    assert "[probe]" in r.stdout

    data = _read_yaml(home)
    found = _io.find_source(data, "newsite_blog")
    assert found is not None
    kind, _, item = found
    assert kind == "web"
    assert item["tier"] == "official_first_party"
    assert list(item["domain"]) == ["ai", "finance"]
    assert item["url"] == "https://newsite.example.com/blog"


def test_form_a_non_tty_blocked(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 tty + 无字段参数 → 直接 Exit(1)，提示用户用非交互模式。"""
    pr = _make_probe_result()
    _patch_probe(monkeypatch, pr)
    monkeypatch.setattr(add_cmd, "_stdin_is_tty", lambda: False)

    app = make_app()
    r = runner.invoke(
        app, ["add", "https://example.com/news", "--home", str(home)]
    )
    assert r.exit_code == 1
    assert "需要 tty" in r.stderr or "需要 tty" in r.output


def test_form_a_unreachable_user_declines(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe reachable=False + 用户在 confirm 拒绝 → 不录入，exit 0。"""
    pr = _make_probe_result(
        url="https://broken.example.com",
        reachable=False,
        status_code=None,
        source_type=None,
        suggested_id="broken_example",
        sample_title=None,
        error="ConnectError: ...",
    )
    _patch_probe(monkeypatch, pr)
    monkeypatch.setattr(add_cmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("typer.confirm", lambda *a, **kw: False)

    # prompt 不应被调用——用户已拒绝
    def should_not_be_called(*args, **kwargs):
        raise AssertionError("typer.prompt should not be called")

    monkeypatch.setattr("typer.prompt", should_not_be_called)

    app = make_app()
    r = runner.invoke(
        app, ["add", "https://broken.example.com", "--home", str(home)]
    )
    assert r.exit_code == 0, r.output
    assert "[skip] add cancelled" in r.stdout
    # 没新增条目
    data = _read_yaml(home)
    assert _io.find_source(data, "broken_example") is None


# =================================================================
# 形态 B：非交互直录
# =================================================================


def test_form_b_full_fields_success(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式给全字段 → 完全不调 probe / prompt。"""

    # 安全网：probe 不应被调用（type 已显式传）
    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://newhost.example.com/feed.xml",
            "--tier", "kol",
            "--domain", "ai,finance",
            "--id", "newhost_feed",
            "--type", "rss",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "[ok] added newhost_feed (rss)" in r.stdout

    data = _read_yaml(home)
    found = _io.find_source(data, "newhost_feed")
    assert found is not None
    kind, _, item = found
    assert kind == "rss"
    assert item["tier"] == "kol"
    assert list(item["domain"]) == ["ai", "finance"]


def test_form_b_missing_tier_raises(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传了其他字段但漏 --tier → BadParameter / Exit 非 0。"""
    pr = _make_probe_result()
    _patch_probe(monkeypatch, pr)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://newhost.example.com/feed.xml",
            "--id", "newhost_feed",
            "--home", str(home),
        ],
    )
    assert r.exit_code != 0
    # typer/click 把 BadParameter 写到 stderr
    assert "tier" in (r.stderr + r.output)


def test_form_b_id_auto_via_suggest(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 --id；suggest_id 应自动推断 → 入库成功。

    type 显式传以避免触发 probe（_run_probe 没 patch 的话会真发请求）。
    """

    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://newsite.example.org/news",
            "--tier", "secondary",
            "--type", "web",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0, r.output
    # suggest_id("https://newsite.example.org/news") → "newsite_news"
    # 注意 example.org 的 domain 主体是 example
    # 实际：netloc=newsite.example.org，host_parts=[newsite, example, org]
    # domain_main = host_parts[-2] = "example"，path="news"
    # → "example_news"
    assert "[ok] added example_news (web)" in r.stdout
    data = _read_yaml(home)
    assert _io.find_source(data, "example_news") is not None


def test_form_b_type_auto_via_probe(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 --type；probe 给出 source_type=rss → 入 rss 段。"""
    pr = _make_probe_result(
        url="https://blog.example.org/atom",
        source_type="rss",
        suggested_id="example_atom",
    )
    _patch_probe(monkeypatch, pr)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://blog.example.org/atom",
            "--tier", "kol",
            "--id", "blog_atom",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "[ok] added blog_atom (rss)" in r.stdout
    data = _read_yaml(home)
    found = _io.find_source(data, "blog_atom")
    assert found is not None
    assert found[0] == "rss"


def test_form_b_url_already_present_blocked(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """url 已被另一 id 占用 → 友好提示 + Exit(1)。"""

    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    # SAMPLE_YAML 里 anthropic_news 占了 https://www.anthropic.com/news
    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://www.anthropic.com/news",
            "--tier", "kol",
            "--id", "anthropic_dup",
            "--type", "web",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 1
    assert "url already present" in r.stderr
    assert "anthropic_news" in r.stderr

    # 没插入
    data = _read_yaml(home)
    assert _io.find_source(data, "anthropic_dup") is None


def test_form_b_id_conflict(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """url 不同但 id 已被占用 → upsert_source 抛 SourceIdConflictError → Exit(1)。"""

    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://different-url.example.com/feed",
            "--tier", "kol",
            "--id", "x_dotey",  # 已存在
            "--type", "rss",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 1
    assert "id conflict: x_dotey" in r.stderr


# =================================================================
# 形态 C：批量文件
# =================================================================


def test_form_c_batch_all_success(
    runner: CliRunner, empty_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """3 行全成功；末尾汇总 ``3 added, 0 skipped, 0 error``。"""
    # 按 url 给不同 ProbeResult
    probes = {
        "https://a.example.com/news": _make_probe_result(
            url="https://a.example.com/news",
            source_type="web",
            suggested_id="a_news",
        ),
        "https://b.example.com/feed": _make_probe_result(
            url="https://b.example.com/feed",
            source_type="rss",
            suggested_id="b_feed",
        ),
        "https://c.example.com/atom": _make_probe_result(
            url="https://c.example.com/atom",
            source_type="rss",
            suggested_id="c_atom",
        ),
    }

    def fake_run_probe(url):
        return probes[url]

    monkeypatch.setattr(add_cmd, "_run_probe", fake_run_probe)

    batch = tmp_path / "urls.txt"
    batch.write_text(
        "# 注释\n"
        "https://a.example.com/news\n"
        "https://b.example.com/feed kol ai b_feed\n"
        "https://c.example.com/atom\n",
        encoding="utf-8",
    )

    app = make_app()
    r = runner.invoke(
        app, ["add", "--from-file", str(batch), "--home", str(empty_home)]
    )
    assert r.exit_code == 0, r.output
    assert "[ok]   a_news (web) — added" in r.stdout
    assert "[ok]   b_feed (rss) — added" in r.stdout
    assert "[ok]   c_atom (rss) — added" in r.stdout
    assert "3 added, 0 skipped, 0 error" in r.stdout

    data = _read_yaml(empty_home)
    assert _io.find_source(data, "a_news") is not None
    assert _io.find_source(data, "b_feed") is not None
    assert _io.find_source(data, "c_atom") is not None
    # b_feed 显式 tier=kol；其他用默认 secondary
    assert _io.find_source(data, "b_feed")[2]["tier"] == "kol"
    assert _io.find_source(data, "a_news")[2]["tier"] == "secondary"


def test_form_c_batch_mixed(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """混合：1 成功 + 1 id 冲突 skip + 1 probe 失败 err。"""
    probes = {
        "https://newone.example.com/feed": _make_probe_result(
            url="https://newone.example.com/feed",
            source_type="rss",
            suggested_id="newone_feed",
        ),
        "https://broken.example.com": _make_probe_result(
            url="https://broken.example.com",
            reachable=False,
            status_code=None,
            source_type=None,
            suggested_id="broken_example",
            sample_title=None,
            error="ConnectError",
        ),
        # 第三条 url 用 id=x_dotey 触发冲突
        "https://collide.example.com": _make_probe_result(
            url="https://collide.example.com",
            source_type="web",
            suggested_id="collide_example",
        ),
    }

    def fake_run_probe(url):
        return probes[url]

    monkeypatch.setattr(add_cmd, "_run_probe", fake_run_probe)

    batch = tmp_path / "mix.txt"
    batch.write_text(
        "https://newone.example.com/feed\n"
        "https://broken.example.com\n"
        "https://collide.example.com kol ai x_dotey\n",  # id 冲突 (sample 已存在 x_dotey)
        encoding="utf-8",
    )

    app = make_app()
    r = runner.invoke(
        app, ["add", "--from-file", str(batch), "--home", str(home)]
    )
    assert r.exit_code == 0, r.output
    assert "[ok]   newone_feed (rss) — added" in r.stdout
    assert "[err]" in r.stdout and "broken.example" in r.stdout
    assert "[skip] x_dotey — id conflict" in r.stdout
    assert "1 added, 1 skipped, 1 error" in r.stdout


def test_form_c_file_not_found(
    runner: CliRunner, home: Path, tmp_path: Path
) -> None:
    """--from-file 指向不存在的文件 → Exit(1)。"""
    missing = tmp_path / "no_such.txt"
    app = make_app()
    r = runner.invoke(
        app, ["add", "--from-file", str(missing), "--home", str(home)]
    )
    assert r.exit_code == 1
    assert "file not found" in r.stderr


# =================================================================
# 互斥 / 入参检查
# =================================================================


def test_url_and_from_file_mutually_exclusive(
    runner: CliRunner, home: Path, tmp_path: Path
) -> None:
    """同时传 url 位置参数 + --from-file → BadParameter。"""
    batch = tmp_path / "urls.txt"
    batch.write_text("https://x.example.com\n", encoding="utf-8")

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add",
            "https://example.com/news",
            "--from-file", str(batch),
            "--home", str(home),
        ],
    )
    assert r.exit_code != 0
    assert "mutually exclusive" in (r.stderr + r.output)


def test_neither_url_nor_from_file(
    runner: CliRunner, home: Path
) -> None:
    """不传 url 也不传 --from-file → BadParameter。"""
    app = make_app()
    r = runner.invoke(app, ["add", "--home", str(home)])
    assert r.exit_code != 0
    assert "either url or --from-file" in (r.stderr + r.output)


# ============================ --json （s9 Step 2） =========================

import json as _json  # noqa: E402


def test_add_json_missing_required_skips_prompt(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``add <url> --json`` 不带任何字段 → ok=False + exit 2。

    关键：prompt 必须被跳过（agent 自动化场景下不能阻塞读 stdin）。
    """

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("typer.prompt should not be called when --json")

    monkeypatch.setattr("typer.prompt", should_not_be_called)
    monkeypatch.setattr("typer.confirm", should_not_be_called)
    # probe 也不该被调（形态 A 入口在 --json 下直接 emit_err 退出）
    monkeypatch.setattr(
        add_cmd, "_run_probe",
        lambda url: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    app = make_app()
    r = runner.invoke(
        app,
        ["add", "https://newsite.example.com/blog", "--home", str(home), "--json"],
    )
    assert r.exit_code == 2, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "missing required" in payload["message"]
    assert "tier" in payload["details"]["required_fields"]


def test_add_json_form_b_full_fields_ok(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``add <url> --tier=... --type=... --json``：完整字段 → ok=True。"""

    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("prompt/confirm should not run when --json")

    monkeypatch.setattr("typer.prompt", should_not_be_called)
    monkeypatch.setattr("typer.confirm", should_not_be_called)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add", "https://newhost.example.com/feed.xml",
            "--tier", "kol",
            "--domain", "ai,finance",
            "--id", "newhost_feed",
            "--type", "rss",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["message"] == "source added"
    assert payload["details"]["id"] == "newhost_feed"
    assert payload["details"]["type"] == "rss"
    assert payload["details"]["tier"] == "kol"
    assert payload["details"]["domain"] == ["ai", "finance"]
    # 持久化生效
    data = _read_yaml(home)
    assert _io.find_source(data, "newhost_feed") is not None


def test_add_json_form_b_url_duplicate(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``add --json`` 形态 B：url 已被占 → ok=False + exit 1。"""

    def should_not_run_probe(url):
        raise AssertionError("probe should not run when --type is given")

    monkeypatch.setattr(add_cmd, "_run_probe", should_not_run_probe)

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add", "https://www.anthropic.com/news",  # 已被 anthropic_news 占
            "--tier", "kol",
            "--id", "anthropic_dup",
            "--type", "web",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "url already present" in payload["message"]
    assert payload["details"]["occupied_by"] == "anthropic_news"


def test_add_json_batch(
    runner: CliRunner,
    empty_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``add --from-file ... --json`` 聚合输出。"""
    probes = {
        "https://a.example.com/news": _make_probe_result(
            url="https://a.example.com/news",
            source_type="web",
            suggested_id="a_news",
        ),
        "https://b.example.com/feed": _make_probe_result(
            url="https://b.example.com/feed",
            source_type="rss",
            suggested_id="b_feed",
        ),
    }
    monkeypatch.setattr(add_cmd, "_run_probe", lambda url: probes[url])

    batch = tmp_path / "urls.txt"
    batch.write_text(
        "https://a.example.com/news\n"
        "https://b.example.com/feed kol ai b_feed\n",
        encoding="utf-8",
    )

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add", "--from-file", str(batch),
            "--home", str(empty_home),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["details"]["added"] == 2
    assert payload["details"]["skipped"] == 0
    assert payload["details"]["errored"] == 0
    assert len(payload["details"]["items"]) == 2
    statuses = {item["status"] for item in payload["details"]["items"]}
    assert statuses == {"added"}


def test_add_json_mutually_exclusive(
    runner: CliRunner, home: Path, tmp_path: Path
) -> None:
    """``add <url> --from-file ... --json`` 互斥 → ok=False + exit 2。"""
    batch = tmp_path / "urls.txt"
    batch.write_text("https://x.example.com\n", encoding="utf-8")

    app = make_app()
    r = runner.invoke(
        app,
        [
            "add", "https://example.com/news",
            "--from-file", str(batch),
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 2
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "mutually exclusive" in payload["message"]
