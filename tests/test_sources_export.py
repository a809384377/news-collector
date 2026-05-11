"""``news-collector sources export`` 命令测试。

s4-sources-management Step 5 subagent A 产出。export 是字节级原样复制，
重点验证保真度（含注释 / 缩进 / EOL 风格）。

注意事项（来自 KNOWLEDGE-LOG）：
- #13 测试模块禁用 ``setup_module`` / ``teardown_module`` 等 xunit hook 别名
- #14 typer 多命令 app；本测试用整 CLI app（``sources export ...``）
- #17 Click 9 移除 ``CliRunner(mix_stderr=False)``；直接 ``CliRunner()``
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from news_collector.cli import app

# ------------ fixture：含注释 + 多种缩进的 sample yaml ------------------

SAMPLE_YAML = """\
# 顶部注释：测试用 sources.yaml
# 二行注释
rss:
  - id: x_dotey  # inline 注释
    url: "http://localhost:1200/twitter/user/dotey?format=atom"
    tier: kol
    domain: [ai]

  # 段间注释
  - id: gh_anthropic
    url: "https://github.com/anthropics/anthropic-sdk-python/releases.atom"
    tier: official_first_party
    domain: [ai]

web:
  - id: anthropic_news
    url: "https://www.anthropic.com/news"
    tier: official_first_party
    domain: [ai]
    enabled: false
"""


@pytest.fixture
def make_home(tmp_path: Path):
    """工厂：写一份 yaml 到 ``<tmp>/.news-collector/sources.yaml``，返回 home Path。"""

    def _factory(yaml_text: str = SAMPLE_YAML) -> Path:
        home = tmp_path / ".news-collector"
        home.mkdir(parents=True, exist_ok=True)
        (home / "sources.yaml").write_text(yaml_text, encoding="utf-8")
        return home

    return _factory


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ============================== sources export ==============================


def test_export_stdout_golden_path(runner: CliRunner, make_home) -> None:
    home = make_home()
    result = runner.invoke(app, ["sources", "export", "--home", str(home)])
    assert result.exit_code == 0, result.output

    # stdout 完整保真：注释 / 缩进 / EOL 全保留
    assert result.stdout == SAMPLE_YAML
    # 注释行
    assert "# 顶部注释" in result.stdout
    assert "# inline 注释" in result.stdout
    assert "# 段间注释" in result.stdout


def test_export_stdout_no_extra_newline(runner: CliRunner, make_home) -> None:
    """typer.echo(nl=False) 行为验证：不应在原文末尾追加多余换行。"""
    yaml_text = "rss:\n  - id: a\n"  # 单一换行结尾
    home = make_home(yaml_text)
    result = runner.invoke(app, ["sources", "export", "--home", str(home)])
    assert result.exit_code == 0, result.output
    # 字节级一致，不多换行
    assert result.stdout == yaml_text


def test_export_to_file(runner: CliRunner, make_home, tmp_path: Path) -> None:
    home = make_home()
    out_file = tmp_path / "backup" / "sources-backup.yaml"
    result = runner.invoke(
        app,
        ["sources", "export", "--home", str(home), "--out", str(out_file)],
    )
    assert result.exit_code == 0, result.output

    # 文件成功写入
    assert out_file.exists()
    # 字节级保真：与源文件一致
    src_bytes = (home / "sources.yaml").read_bytes()
    assert out_file.read_bytes() == src_bytes
    # 父目录自动创建
    assert out_file.parent.is_dir()
    # stdout 保持纯净
    assert result.stdout == ""
    # 进度信息打到 stderr
    err = result.stderr or ""
    assert "[ok] exported to" in err
    assert str(out_file) in err


def test_export_to_file_overwrite(runner: CliRunner, make_home, tmp_path: Path) -> None:
    home = make_home()
    out_file = tmp_path / "backup.yaml"
    out_file.write_text("placeholder content\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["sources", "export", "--home", str(home), "--out", str(out_file)],
    )
    assert result.exit_code == 0, result.output

    # 覆盖原文件
    assert out_file.read_bytes() == (home / "sources.yaml").read_bytes()


def test_export_missing_yaml(runner: CliRunner, tmp_path: Path) -> None:
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    result = runner.invoke(app, ["sources", "export", "--home", str(empty_home)])
    assert result.exit_code == 1
    err = result.stderr or result.output
    assert "sources.yaml 不存在" in err


def test_export_preserves_crlf(runner: CliRunner, tmp_path: Path) -> None:
    """CRLF 风格应原样保留（字节复制契约）。"""
    home = tmp_path / ".news-collector"
    home.mkdir(parents=True, exist_ok=True)
    crlf_bytes = b"rss:\r\n  - id: a\r\n  - id: b\r\n"
    (home / "sources.yaml").write_bytes(crlf_bytes)

    out_file = tmp_path / "backup.yaml"
    result = runner.invoke(
        app,
        ["sources", "export", "--home", str(home), "--out", str(out_file)],
    )
    assert result.exit_code == 0, result.output
    # 字节级：CRLF 保留
    assert out_file.read_bytes() == crlf_bytes


def test_export_empty_file(runner: CliRunner, make_home) -> None:
    """空 sources.yaml（合法但无内容）应能 export 而不报错。"""
    home = make_home("")
    result = runner.invoke(app, ["sources", "export", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
