"""``commands/sources/test_cmd.py`` 测试：``sources test <id>`` 端到端覆盖。

策略
----
- 单命令 typer.Typer 用 ``_placeholder`` 强制 group 模式，避免扁平化让 args 报错
  （KNOWLEDGE-LOG #14）
- ``CliRunner()`` 不传 ``mix_stderr`` 参数（Click 9 已移除，KNOWLEDGE-LOG #17）
- monkeypatch ``test_cmd._build_adapter`` 返回 fake adapter；fake adapter 的
  ``.fetch()`` 由各测试预制 list[RawArticle] / 抛异常控制行为
- 模块级名字避开 pytest xunit hook 名（``setup_*`` / ``teardown_*``，KNOWLEDGE-LOG #13）
- RawArticle 是 frozen dataclass，构造时所有字段都要传
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from newsbox.commands.sources import test_cmd as test_cmd_module
from newsbox.commands.sources.test_cmd import sources_test_cmd
from newsbox.models import RawArticle


SAMPLE_YAML = """\
rss:
  - id: x_dotey
    url: "https://example.com/dotey/atom"
    tier: kol
    domain: [ai]
  - id: gh_anthropic
    url: "https://github.com/anthropics/sdk/releases.atom"
    tier: official_first_party
    domain: [ai]

web:
  - id: anthropic_news
    url: "https://www.anthropic.com/news"
    selector: auto
    tier: official_first_party
    domain: [ai]
"""


# ---------- helpers / fixtures ----------


def make_app() -> typer.Typer:
    """单命令 typer app + _placeholder 强制 group 模式（KNOWLEDGE-LOG #14）。"""
    a = typer.Typer()
    a.command("test")(sources_test_cmd)
    a.command("_placeholder", hidden=True)(lambda: None)
    return a


@pytest.fixture
def cli_app() -> typer.Typer:
    return make_app()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sources_home(tmp_path: Path) -> Path:
    """tmp home + 写入 sample sources.yaml。"""
    h = tmp_path / ".newsbox"
    h.mkdir()
    (h / "sources.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
    return h


def make_articles(count: int, source_type: str = "rss", source_id: str = "x_dotey") -> list[RawArticle]:
    """构造 count 条 RawArticle 测试样本。"""
    out: list[RawArticle] = []
    for i in range(count):
        out.append(
            RawArticle(
                source_type=source_type,
                source_id=source_id,
                external_id=f"ext-{i}",
                url=f"https://example.com/post/{i}",
                title=f"Title number {i}",
                body=f"Body content for article {i}.",
                published_at=datetime(2026, 5, 9, 8, 0, 0, tzinfo=timezone.utc),
                is_long_form=None,
                skip_url_dedup=False,
            )
        )
    return out


class FakeAdapter:
    """fake adapter：fetch 返回预制 list[RawArticle] 或抛预制异常。"""

    def __init__(
        self,
        articles: list[RawArticle] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._articles = articles or []
        self._raises = raises
        self.calls: list[tuple[dict, object]] = []

    async def fetch(self, source: dict, since):  # noqa: ANN001 — match adapter sig
        self.calls.append((source, since))
        if self._raises is not None:
            raise self._raises
        return list(self._articles)


def patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: FakeAdapter) -> FakeAdapter:
    """monkeypatch _build_adapter 让所有 kind 都返回同一个 fake adapter。"""
    monkeypatch.setattr(test_cmd_module, "_build_adapter", lambda kind: adapter)
    return adapter


# =================================================================
# 黄金路径：rss 信源拉到 N 条 → 输出 N 条预览
# =================================================================


def test_rss_golden_prints_articles(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = patch_adapter(
        monkeypatch, FakeAdapter(articles=make_articles(3, source_id="x_dotey"))
    )

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home)],
    )

    assert r.exit_code == 0, r.output
    out = r.stdout
    assert "[fetch] x_dotey (rss) → 3 articles, showing first 3" in out
    assert "[1] title:        Title number 0" in out
    assert "[2] title:        Title number 1" in out
    assert "[3] title:        Title number 2" in out
    assert "url:          https://example.com/post/0" in out
    assert "external_id:  ext-0" in out
    assert "published_at: 2026-05-09T08:00:00+00:00" in out
    assert "is_long_form: —" in out
    assert "[ok] tested x_dotey: 3 articles fetched (not persisted)" in out

    # 验证 adapter 被调用，传入纯 dict（不含 ruamel 类型）
    assert len(fake.calls) == 1
    item_arg, since_arg = fake.calls[0]
    assert type(item_arg) is dict
    assert item_arg["id"] == "x_dotey"
    assert since_arg is None


# =================================================================
# 黄金路径：web 信源拉到 1 条
# =================================================================


def test_web_golden_single_article(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_adapter(
        monkeypatch,
        FakeAdapter(
            articles=make_articles(1, source_type="web", source_id="anthropic_news")
        ),
    )

    r = runner.invoke(
        cli_app,
        ["test", "anthropic_news", "--home", str(sources_home)],
    )

    assert r.exit_code == 0, r.output
    assert "[fetch] anthropic_news (web) → 1 articles, showing first 1" in r.stdout
    assert "[1] title:        Title number 0" in r.stdout
    assert "[ok] tested anthropic_news: 1 articles fetched (not persisted)" in r.stdout


# =================================================================
# 0 条：adapter 返回空 list → 警告 + Exit(0)
# =================================================================


def test_zero_articles_warns_but_exits_zero(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_adapter(monkeypatch, FakeAdapter(articles=[]))

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home)],
    )

    assert r.exit_code == 0, r.output
    assert "[fetch] x_dotey (rss) → 0 articles" in r.stdout
    assert "[warn] 0 articles fetched" in r.stdout
    # 不应有 [ok] 行（0 条不视作成功提示）
    assert "[ok] tested" not in r.stdout


# =================================================================
# adapter 抛异常 → Exit(1) + stderr 含异常类型
# =================================================================


def test_adapter_raises_exits_one(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_adapter(
        monkeypatch,
        FakeAdapter(raises=RuntimeError("simulated network failure")),
    )

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home)],
    )

    assert r.exit_code == 1
    assert "[err] adapter raised RuntimeError: simulated network failure" in r.stderr


# =================================================================
# source_id 不存在 → Exit(1) + stderr 含 "source not found"
# =================================================================


def test_source_not_found_exits_one(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # adapter 不应该被调用；如果被调用就让测试炸（仍打 patch 但留意 calls）
    fake = patch_adapter(monkeypatch, FakeAdapter(articles=make_articles(1)))

    r = runner.invoke(
        cli_app,
        ["test", "no_such_id", "--home", str(sources_home)],
    )

    assert r.exit_code == 1
    assert "[err] source not found: no_such_id" in r.stderr
    assert fake.calls == []  # adapter 没被触发


# =================================================================
# sources.yaml 不存在 → Exit(1) + 引导用户 seed
# =================================================================


def test_yaml_missing_exits_one_with_seed_hint(
    runner: CliRunner,
    cli_app: typer.Typer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    fake = patch_adapter(monkeypatch, FakeAdapter(articles=make_articles(1)))

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(empty_home)],
    )

    assert r.exit_code == 1
    assert "[err] sources.yaml not found" in r.stderr
    assert "sources seed" in r.stderr
    assert fake.calls == []


# =================================================================
# limit 截断：adapter 返回 10 条，limit=2 只显示 2 条但报告 "10 articles, showing first 2"
# =================================================================


def test_limit_truncates_display(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_adapter(monkeypatch, FakeAdapter(articles=make_articles(10)))

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--limit", "2", "--home", str(sources_home)],
    )

    assert r.exit_code == 0, r.output
    out = r.stdout
    assert "[fetch] x_dotey (rss) → 10 articles, showing first 2" in out
    assert "[1] title:        Title number 0" in out
    assert "[2] title:        Title number 1" in out
    # 第 3 条不应出现
    assert "[3] title:" not in out
    assert "Title number 2" not in out
    # 末尾汇总仍报告全量 10
    assert "[ok] tested x_dotey: 10 articles fetched (not persisted)" in out


# =================================================================
# body 预览截断到 200 字符 + 换行替换
# =================================================================


def test_body_preview_truncates_and_replaces_newlines(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_body = "head\nline\n" + ("x" * 300)
    article = RawArticle(
        source_type="rss",
        source_id="x_dotey",
        external_id="ext-long",
        url="https://example.com/long",
        title="Long body article",
        body=long_body,
        published_at=None,
        is_long_form="note_tweet",
        skip_url_dedup=False,
    )
    patch_adapter(monkeypatch, FakeAdapter(articles=[article]))

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home)],
    )

    assert r.exit_code == 0, r.output
    out = r.stdout
    # 换行被替换为字面 \n
    assert "head\\nline\\n" in out
    # 截断标记
    assert out.rstrip().endswith("not persisted)")
    assert "..." in out
    # published_at None → "—"
    assert "published_at: —" in out
    # is_long_form 非 None → 原样输出
    assert "is_long_form: note_tweet" in out


# ============================ --json （s9 Step 2） =========================

import json as _json  # noqa: E402


def test_test_cmd_json_ok(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sources test <id> --json`` 黄金路径。"""
    patch_adapter(
        monkeypatch, FakeAdapter(articles=make_articles(3, source_id="x_dotey"))
    )

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home), "--json"],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["id"] == "x_dotey"
    assert payload["type"] == "rss"
    assert payload["ok"] is True
    assert payload["count"] == 3
    assert payload["shown"] == 3
    assert payload["error"] is None
    assert len(payload["items"]) == 3
    # 字段完整
    first = payload["items"][0]
    assert first["external_id"] == "ext-0"
    assert first["title"] == "Title number 0"
    assert first["published_at"] == "2026-05-09T08:00:00+00:00"


def test_test_cmd_json_zero_articles(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sources test --json`` 0 条仍然 ok=True、count=0。"""
    patch_adapter(monkeypatch, FakeAdapter(articles=[]))

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home), "--json"],
    )
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["items"] == []


def test_test_cmd_json_adapter_error(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sources test --json`` adapter 抛 → emit_err schema：``{ok:false, message, details}``（codex P1 修）。

    原行为：emit 信息类 schema ``{id, type, ok:false, count:0, items:[], error: '...'}``
    与 not_found 路径的 emit_err schema 不一致；codex P1 报告 schema 漂移。
    现行为：异常路径与 not_found / yaml-missing 路径一致走 emit_err。
    """
    patch_adapter(
        monkeypatch,
        FakeAdapter(raises=RuntimeError("network down")),
    )

    r = runner.invoke(
        cli_app,
        ["test", "x_dotey", "--home", str(sources_home), "--json"],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    # message 是操作类 schema 的顶层字段
    assert "RuntimeError" in payload["message"]
    assert "network down" in payload["message"]
    # 上下文走 details
    assert payload["details"]["id"] == "x_dotey"
    assert payload["details"]["type"] == "rss"
    assert payload["details"]["error_type"] == "RuntimeError"


def test_test_cmd_json_not_found(
    runner: CliRunner,
    cli_app: typer.Typer,
    sources_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sources test --json`` source id 不存在 → ok=False + exit 1。"""
    patch_adapter(monkeypatch, FakeAdapter(articles=[]))

    r = runner.invoke(
        cli_app,
        ["test", "no_such_id", "--home", str(sources_home), "--json"],
    )
    assert r.exit_code == 1
    payload = _json.loads(r.stdout)
    assert payload["ok"] is False
    assert "not found" in payload["message"]
