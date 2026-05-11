"""``commands/sources/edit_ops.py`` 测试：5 条改类命令端到端覆盖。

策略：
- ``CliRunner`` 直接打 ``newsbox sources <cmd>``；不传 ``mix_stderr``
  （Click 9 已移除该参数，KNOWLEDGE-LOG #17）
- monkeypatch ``edit_ops._stdin_is_tty`` 控制 tty 分支（不动 sys.stdin，CliRunner
  替换了 sys.stdin 让 monkeypatch sys.stdin.isatty 失效，KNOWLEDGE-LOG #15）
- 测试模块禁用 ``setup_*`` / ``teardown_*`` 命名（pytest xunit hook，KNOWLEDGE-LOG #13）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsbox.cli import app
from newsbox.commands.sources import edit_ops, _io


SAMPLE_YAML = """\
# 顶部注释：信源清单
rss:
  # X / KOL 段
  - id: x_dotey  # 宝玉，AI 领域 KOL
    url: "https://example.com/dotey/atom"
    tier: kol
    domain: [ai]
  - id: x_kepano
    url: "https://example.com/kepano/atom"
    tier: kol
    domain: [ai]

  # GitHub atom 段
  - id: gh_anthropic
    url: "https://github.com/anthropics/anthropic-sdk-python/releases.atom"
    tier: official_first_party
    domain: [ai]

# web 段
web:
  - id: anthropic_news
    url: "https://www.anthropic.com/news"
    selector: auto
    tier: official_first_party
    domain: [ai]
    enabled: false
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """tmp home 目录 + 写入 sample sources.yaml。"""
    h = tmp_path / ".newsbox"
    h.mkdir()
    (h / "sources.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
    return h


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _read_yaml(home: Path):
    return _io.load_yaml(home / "sources.yaml")


# =================================================================
# disable
# =================================================================


def test_disable_golden(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(app, ["sources", "disable", "x_dotey", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "[ok] disabled x_dotey" in r.stdout
    # 持久化验证
    data = _read_yaml(home)
    found = _io.find_source(data, "x_dotey")
    assert found is not None
    assert found[2]["enabled"] is False


def test_disable_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(app, ["sources", "disable", "no_such_id", "--home", str(home)])
    assert r.exit_code == 1
    assert "[err] source not found: no_such_id" in r.stderr


def test_disable_idempotent(runner: CliRunner, home: Path) -> None:
    """anthropic_news 在 sample 中已 enabled: false。"""
    r = runner.invoke(
        app, ["sources", "disable", "anthropic_news", "--home", str(home)]
    )
    assert r.exit_code == 0
    assert "(already disabled)" in r.stdout


def test_disable_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    h = tmp_path / "empty"
    h.mkdir()
    r = runner.invoke(app, ["sources", "disable", "x", "--home", str(h)])
    assert r.exit_code == 1
    assert "sources.yaml not found" in r.stderr


def test_disable_preserves_comments(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(app, ["sources", "disable", "x_dotey", "--home", str(home)])
    assert r.exit_code == 0
    text = (home / "sources.yaml").read_text(encoding="utf-8")
    assert "# 顶部注释：信源清单" in text
    assert "# X / KOL 段" in text


# =================================================================
# enable
# =================================================================


def test_enable_golden(runner: CliRunner, home: Path) -> None:
    """anthropic_news 当前 enabled=false，enable 应翻转到 true。"""
    r = runner.invoke(
        app, ["sources", "enable", "anthropic_news", "--home", str(home)]
    )
    assert r.exit_code == 0, r.output
    assert "[ok] enabled anthropic_news" in r.stdout
    assert "(already enabled)" not in r.stdout
    data = _read_yaml(home)
    found = _io.find_source(data, "anthropic_news")
    assert found is not None
    assert found[2]["enabled"] is True


def test_enable_already_enabled_via_missing_field(
    runner: CliRunner, home: Path
) -> None:
    """x_dotey 缺 enabled 字段 → 视为 already enabled。"""
    r = runner.invoke(app, ["sources", "enable", "x_dotey", "--home", str(home)])
    assert r.exit_code == 0
    assert "(already enabled)" in r.stdout


def test_enable_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(app, ["sources", "enable", "ghost", "--home", str(home)])
    assert r.exit_code == 1
    assert "[err] source not found: ghost" in r.stderr


def test_enable_persists(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "enable", "anthropic_news", "--home", str(home)]
    )
    assert r.exit_code == 0
    text = (home / "sources.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text


# =================================================================
# remove
# =================================================================


def test_remove_with_yes_skips_confirm(
    runner: CliRunner, home: Path
) -> None:
    r = runner.invoke(
        app, ["sources", "remove", "x_kepano", "--yes", "--home", str(home)]
    )
    assert r.exit_code == 0, r.output
    assert "[ok] removed x_kepano" in r.stdout
    # 持久化：x_kepano 不在了
    data = _read_yaml(home)
    assert _io.find_source(data, "x_kepano") is None


def test_remove_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "remove", "ghost", "--yes", "--home", str(home)]
    )
    assert r.exit_code == 1
    assert "[err] source not found: ghost" in r.stderr


def test_remove_non_tty_without_yes_blocked(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 tty + 没传 --yes → 报错 Exit 1。"""
    monkeypatch.setattr(edit_ops, "_stdin_is_tty", lambda: False)
    r = runner.invoke(app, ["sources", "remove", "x_kepano", "--home", str(home)])
    assert r.exit_code == 1
    assert "removal requires interactive confirmation" in r.stderr
    # 没删
    data = _read_yaml(home)
    assert _io.find_source(data, "x_kepano") is not None


def test_remove_tty_user_confirms(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(edit_ops, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("typer.confirm", lambda *a, **kw: True)
    r = runner.invoke(app, ["sources", "remove", "x_kepano", "--home", str(home)])
    assert r.exit_code == 0
    assert "[ok] removed x_kepano" in r.stdout
    data = _read_yaml(home)
    assert _io.find_source(data, "x_kepano") is None


def test_remove_tty_user_declines(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(edit_ops, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("typer.confirm", lambda *a, **kw: False)
    r = runner.invoke(app, ["sources", "remove", "x_kepano", "--home", str(home)])
    assert r.exit_code == 0
    assert "[skip] removal cancelled" in r.stdout
    # 没删
    data = _read_yaml(home)
    assert _io.find_source(data, "x_kepano") is not None


def test_remove_yes_outputs_dangling_comment_hint(
    runner: CliRunner, home: Path
) -> None:
    r = runner.invoke(
        app, ["sources", "remove", "x_kepano", "--yes", "--home", str(home)]
    )
    assert r.exit_code == 0
    assert "悬空注释" in r.stdout


# =================================================================
# edit
# =================================================================


def test_edit_no_field(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(app, ["sources", "edit", "x_dotey", "--home", str(home)])
    assert r.exit_code == 1
    assert "no field to update" in r.stderr


def test_edit_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "ghost",
            "--tier", "kol",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 1
    assert "[err] source not found: ghost" in r.stderr


def test_edit_single_tier(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--tier", "official_first_party",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "[ok] edited x_dotey" in r.stdout
    assert "tier=official_first_party" in r.stdout
    data = _read_yaml(home)
    found = _io.find_source(data, "x_dotey")
    assert found is not None
    assert found[2]["tier"] == "official_first_party"


def test_edit_domain_csv(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--domain", "ai, finance",  # 测空格 trim
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0
    data = _read_yaml(home)
    found = _io.find_source(data, "x_dotey")
    assert found is not None
    assert list(found[2]["domain"]) == ["ai", "finance"]


def test_edit_url(runner: CliRunner, home: Path) -> None:
    new_url = "https://newhost.example.com/feed"
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--url", new_url,
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0
    data = _read_yaml(home)
    found = _io.find_source(data, "x_dotey")
    assert found is not None
    assert found[2]["url"] == new_url


def test_edit_enabled_flag(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--disabled",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0
    data = _read_yaml(home)
    found = _io.find_source(data, "x_dotey")
    assert found is not None
    assert found[2]["enabled"] is False


def test_edit_multiple_fields(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--tier", "kol",
            "--domain", "ai",
            "--url", "https://x.com/feed",
            "--enabled",
            "--home", str(home),
        ],
    )
    assert r.exit_code == 0
    out = r.stdout
    assert "tier=kol" in out
    assert "url=https://x.com/feed" in out
    assert "enabled=True" in out


# =================================================================
# rename
# =================================================================


def test_rename_golden(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        ["sources", "rename", "x_dotey", "x_dotey_v2", "--home", str(home)],
    )
    assert r.exit_code == 0, r.output
    assert "[ok] renamed x_dotey -> x_dotey_v2" in r.stdout
    data = _read_yaml(home)
    assert _io.find_source(data, "x_dotey") is None
    assert _io.find_source(data, "x_dotey_v2") is not None


def test_rename_conflict(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        ["sources", "rename", "x_dotey", "x_kepano", "--home", str(home)],
    )
    assert r.exit_code == 1
    assert "[err] new id conflicts: x_kepano" in r.stderr


def test_rename_same_id(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        ["sources", "rename", "x_dotey", "x_dotey", "--home", str(home)],
    )
    assert r.exit_code == 1
    assert "[err] new id conflicts: x_dotey" in r.stderr


def test_rename_old_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        ["sources", "rename", "ghost", "x_new", "--home", str(home)],
    )
    assert r.exit_code == 1
    assert "[err] source not found: ghost" in r.stderr


def test_rename_persists_and_keeps_other_fields(
    runner: CliRunner, home: Path
) -> None:
    r = runner.invoke(
        app,
        ["sources", "rename", "anthropic_news", "anthropic_official", "--home", str(home)],
    )
    assert r.exit_code == 0
    data = _read_yaml(home)
    found = _io.find_source(data, "anthropic_official")
    assert found is not None
    kind, _, item = found
    assert kind == "web"
    assert item["selector"] == "auto"
    assert item["enabled"] is False


# ============================ --json （s9 Step 2） =========================

import json as _json  # noqa: E402


def test_disable_json_ok(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "disable", "x_dotey", "--home", str(home), "--json"]
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["message"] == "source disabled"
    assert payload["details"]["id"] == "x_dotey"
    assert payload["details"]["already"] is False
    assert payload["details"]["enabled"] is False


def test_disable_json_already(runner: CliRunner, home: Path) -> None:
    """anthropic_news 已经 disabled → already=true。"""
    r = runner.invoke(
        app,
        ["sources", "disable", "anthropic_news", "--home", str(home), "--json"],
    )
    assert r.exit_code == 0
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["details"]["already"] is True


def test_disable_json_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "disable", "ghost", "--home", str(home), "--json"]
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "not found" in payload["message"]
    assert payload["details"]["id"] == "ghost"


def test_enable_json_ok(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        ["sources", "enable", "anthropic_news", "--home", str(home), "--json"],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["details"]["already"] is False
    assert payload["details"]["enabled"] is True


def test_enable_json_already(runner: CliRunner, home: Path) -> None:
    """x_dotey 缺 enabled 字段视为 already enabled。"""
    r = runner.invoke(
        app, ["sources", "enable", "x_dotey", "--home", str(home), "--json"]
    )
    assert r.exit_code == 0
    payload = _json.loads(r.stdout)
    assert payload["details"]["already"] is True


def test_remove_json_skips_confirm(
    runner: CliRunner, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 隐含 --yes：confirm 不应被调用，删除直接执行。"""
    # 让 _stdin_is_tty=False 来排除 tty 分支（双保险：--json 也走旁路）
    monkeypatch.setattr(edit_ops, "_stdin_is_tty", lambda: False)

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("typer.confirm should not be called when --json")

    monkeypatch.setattr("typer.confirm", should_not_be_called)

    r = runner.invoke(
        app, ["sources", "remove", "x_kepano", "--home", str(home), "--json"]
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["message"] == "source removed"
    assert payload["details"]["id"] == "x_kepano"
    # 持久化生效
    data = _read_yaml(home)
    assert _io.find_source(data, "x_kepano") is None


def test_remove_json_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "remove", "ghost", "--home", str(home), "--json"]
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "not found" in payload["message"]


def test_edit_json_ok(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "x_dotey",
            "--tier", "official_first_party",
            "--domain", "ai,finance",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["details"]["id"] == "x_dotey"
    assert payload["details"]["changes"]["tier"] == "official_first_party"
    assert payload["details"]["changes"]["domain"] == ["ai", "finance"]


def test_edit_json_no_field(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app, ["sources", "edit", "x_dotey", "--home", str(home), "--json"]
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "no field to update" in payload["message"]


def test_edit_json_not_found(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "edit", "ghost",
            "--tier", "kol",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False


def test_rename_json_ok(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "rename", "x_dotey", "x_dotey_v2",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["details"]["old_id"] == "x_dotey"
    assert payload["details"]["new_id"] == "x_dotey_v2"


def test_rename_json_conflict(runner: CliRunner, home: Path) -> None:
    r = runner.invoke(
        app,
        [
            "sources", "rename", "x_dotey", "x_kepano",
            "--home", str(home),
            "--json",
        ],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "conflict" in payload["message"]
