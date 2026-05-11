"""``news-collector sources probe`` 命令测试。

s4-sources-management Step 7 subagent C 产出。

测试组装方式（避开主 cli.app 注册依赖）：
- 用本地 typer.Typer + ``_placeholder`` 二命令 group 模式（KNOWLEDGE-LOG #14）
- mock ``probe_cmd.probe`` 替换 _probe.probe 调用，避免实跑 httpx

注意事项（来自 KNOWLEDGE-LOG）：
- #13 测试模块禁用 ``setup_module`` / ``teardown_module`` 等 xunit hook 别名
  → 本文件 fixture 命名为 ``make_app`` / ``fake_probe_factory`` / ``runner`` 等
- #14 typer 单命令 app 自动扁平化；用 ``_placeholder`` hidden 命令撑成 group
- #17 Click 9 移除 ``CliRunner(mix_stderr=False)``；直接 ``CliRunner()``
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from news_collector.commands.sources._probe import ProbeResult
from news_collector.commands.sources.probe_cmd import sources_probe_cmd


# --------------------------- fixtures ---------------------------------------


@pytest.fixture
def make_app():
    """构造一个二命令 typer.Typer 容纳 probe 命令，避开单命令扁平化。"""

    def _factory() -> typer.Typer:
        app = typer.Typer()
        app.command("probe")(sources_probe_cmd)
        # 多挂一个 hidden 命令，让 typer 走 group 模式（KNOWLEDGE-LOG #14）
        app.command("_placeholder", hidden=True)(lambda: None)
        return app

    return _factory


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_probe_factory(monkeypatch: pytest.MonkeyPatch):
    """工厂：注册一个 fake_probe，根据 url → ProbeResult 映射回放结果。

    用法::

        fake_probe_factory({
            "https://a.com": ProbeResult(...),
            "https://b.com": ProbeResult(...),
        })
    """

    def _factory(url_to_result: dict[str, ProbeResult]):
        async def fake_probe(url: str, *, client=None, timeout: float = 12.0):
            if url in url_to_result:
                return url_to_result[url]
            # 缺省返回一个 generic reachable 结果，避免 KeyError 让测试失败原因模糊
            return ProbeResult(
                url=url,
                reachable=True,
                status_code=200,
                source_type="web",
                suggested_id="generic",
                sample_title="Generic",
                error=None,
            )

        monkeypatch.setattr(
            "news_collector.commands.sources.probe_cmd.probe",
            fake_probe,
        )
        return fake_probe

    return _factory


def _ok_result(url: str, *, source_type: str = "web") -> ProbeResult:
    return ProbeResult(
        url=url,
        reachable=True,
        status_code=200,
        source_type=source_type,  # type: ignore[arg-type]
        suggested_id="anthropic_news",
        sample_title="News - Anthropic",
        error=None,
    )


# ============================ 单 url 模式 ====================================


def test_single_url_golden_path(runner, make_app, fake_probe_factory) -> None:
    """单 url 黄金路径：reachable=True，输出 7 字段，退码 0。"""
    target = "https://www.anthropic.com/news"
    fake_probe_factory({target: _ok_result(target)})

    app = make_app()
    result = runner.invoke(app, ["probe", target])
    assert result.exit_code == 0, result.output

    out = result.stdout
    # 7 字段都出现
    assert "URL" in out and target in out
    assert "reachable" in out and "yes" in out
    assert "status_code" in out and "200" in out
    assert "source_type" in out and "web" in out
    assert "suggested_id" in out and "anthropic_news" in out
    assert "sample_title" in out and "News - Anthropic" in out
    # error 字段存在但值为 — （reachable 时无 error）
    assert "error" in out


def test_single_url_http_404(runner, make_app, fake_probe_factory) -> None:
    """HTTP 404 / 500：reachable=False，status_code 显示数字，退码 1。"""
    target = "https://example.com/dead"
    fake_probe_factory({
        target: ProbeResult(
            url=target,
            reachable=False,
            status_code=404,
            source_type=None,
            suggested_id="example_dead",
            sample_title=None,
            error="HTTP 404",
        )
    })

    app = make_app()
    result = runner.invoke(app, ["probe", target])
    assert result.exit_code == 1
    out = result.stdout
    assert "no" in out  # reachable=no
    assert "404" in out
    assert "HTTP 404" in out
    assert "example_dead" in out


def test_single_url_network_error(runner, make_app, fake_probe_factory) -> None:
    """网络错误：reachable=False，status_code=None → 显示 —，error 携描述。"""
    target = "https://broken.invalid"
    fake_probe_factory({
        target: ProbeResult(
            url=target,
            reachable=False,
            status_code=None,
            source_type=None,
            suggested_id="broken",
            sample_title=None,
            error="ConnectError: Name or service not known",
        )
    })

    app = make_app()
    result = runner.invoke(app, ["probe", target])
    assert result.exit_code == 1
    out = result.stdout
    assert "no" in out
    # status_code 为 None 时显示 —
    # （我们只断言 ConnectError 错误描述与 broken id 出现即可）
    assert "ConnectError" in out
    assert "broken" in out


# ============================ 批量 from-file ================================


def test_batch_all_success(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """批量全成功：表格 + 汇总 ``N/N reachable; 0/N unreachable``，退码 0。"""
    urls = [
        "https://www.anthropic.com/news",
        "https://simonwillison.net/atom/everything/",
    ]
    fake_probe_factory({
        urls[0]: _ok_result(urls[0], source_type="web"),
        urls[1]: ProbeResult(
            url=urls[1],
            reachable=True,
            status_code=200,
            source_type="rss",
            suggested_id="simonwillison",
            sample_title="Simon Willison",
            error=None,
        ),
    })

    list_file = tmp_path / "urls.txt"
    list_file.write_text("\n".join(urls) + "\n", encoding="utf-8")

    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(list_file)])
    assert result.exit_code == 0, result.output

    out = result.stdout
    assert "URL" in out and "STATUS" in out and "TYPE" in out and "SUGGESTED_ID" in out
    assert "anthropic_news" in out
    assert "simonwillison" in out
    assert "200" in out
    assert "rss" in out and "web" in out
    assert "2/2 reachable" in out
    assert "0/2 unreachable" in out


def test_batch_mixed(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """批量混合：成功 + 404 + 网络错误，退码 1，表格行数与汇总数对得上。"""
    urls = [
        "https://ok.example/feed",
        "https://dead.example/404",
        "https://broken.invalid",
    ]
    fake_probe_factory({
        urls[0]: ProbeResult(
            url=urls[0],
            reachable=True,
            status_code=200,
            source_type="rss",
            suggested_id="ok_example",
            sample_title="OK feed",
            error=None,
        ),
        urls[1]: ProbeResult(
            url=urls[1],
            reachable=False,
            status_code=404,
            source_type=None,
            suggested_id="dead_example_404",
            sample_title=None,
            error="HTTP 404",
        ),
        urls[2]: ProbeResult(
            url=urls[2],
            reachable=False,
            status_code=None,
            source_type=None,
            suggested_id="broken",
            sample_title=None,
            error="ConnectError: nope",
        ),
    })

    list_file = tmp_path / "urls.txt"
    list_file.write_text("\n".join(urls) + "\n", encoding="utf-8")

    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(list_file)])
    assert result.exit_code == 1

    out = result.stdout
    assert "200" in out
    assert "404" in out
    assert "ERR" in out  # status_code=None → ERR
    assert "rss" in out
    # 失败行 TYPE 显示 —
    assert "—" in out
    assert "1/3 reachable" in out
    assert "2/3 unreachable" in out


def test_batch_skips_blank_and_comment_lines(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """批量文件支持 # 注释行与空行；只对有效 url 探测。"""
    only = "https://only.example/"
    fake_probe_factory({only: _ok_result(only)})

    list_file = tmp_path / "urls.txt"
    list_file.write_text(
        "# 注释行 1\n"
        "\n"
        "   \n"
        f"{only}\n"
        "# tail comment\n",
        encoding="utf-8",
    )

    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(list_file)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "1/1 reachable" in out
    # 注释字符串本身不应作为 url 进入表格
    assert "tail comment" not in out


def test_batch_empty_file_errors(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """全空 / 全注释 → 友好错误 + 非 0 退码。"""
    fake_probe_factory({})  # 不会被调用，但需要安装 monkeypatch

    list_file = tmp_path / "urls.txt"
    list_file.write_text("# 全是注释\n\n   \n# end\n", encoding="utf-8")

    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(list_file)])
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "没有有效 URL" in err or "no valid" in err.lower()


def test_batch_url_truncation(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """URL > 60 字符截断 + ...。"""
    long_url = "https://example.com/" + "a" * 80  # 远超 60
    fake_probe_factory({long_url: _ok_result(long_url)})

    list_file = tmp_path / "urls.txt"
    list_file.write_text(long_url + "\n", encoding="utf-8")

    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(list_file)])
    assert result.exit_code == 0, result.output
    # 截断标记 ... 出现，原始完整 url 不应作为单段出现
    assert "..." in result.stdout
    assert long_url not in result.stdout


# ============================ 互斥参数 ======================================


def test_mutually_exclusive_both_passed(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """同时传位置 url + --from-file → BadParameter，非 0 退码。"""
    fake_probe_factory({})
    list_file = tmp_path / "urls.txt"
    list_file.write_text("https://a.example/\n", encoding="utf-8")

    app = make_app()
    result = runner.invoke(
        app, ["probe", "https://x.example/", "--from-file", str(list_file)]
    )
    assert result.exit_code != 0
    err = (result.stderr or "") + result.output
    assert "互斥" in err or "exclusive" in err.lower()


def test_mutually_exclusive_neither_passed(
    runner, make_app, fake_probe_factory
) -> None:
    """都不传 → BadParameter，非 0 退码。"""
    fake_probe_factory({})
    app = make_app()
    result = runner.invoke(app, ["probe"])
    assert result.exit_code != 0
    err = (result.stderr or "") + result.output
    assert "must pass" in err or "url" in err.lower()


def test_from_file_path_not_exist(
    runner, make_app, fake_probe_factory, tmp_path: Path
) -> None:
    """--from-file 指向不存在路径 → BadParameter。"""
    fake_probe_factory({})
    missing = tmp_path / "no-such.txt"
    app = make_app()
    result = runner.invoke(app, ["probe", "--from-file", str(missing)])
    assert result.exit_code != 0
    err = (result.stderr or "") + result.output
    assert "不存在" in err or "not" in err.lower()
