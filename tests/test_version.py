"""版本号同步测试。

锁定三个真相源彼此一致，避免 v0.5.2 的事故重演：
- `pyproject.toml` 的 `[project] version`
- `news_collector.__version__`（运行时 importlib.metadata 取）
- `news-collector --version` CLI 输出

v0.5.2 发版时只升了 pyproject.toml 没升 `__init__.py` 写死的版本号，PyPI 装出来 `--version` 显示 0.5.1。
v0.5.3 起 `__version__` 改 dynamic 从 package metadata 取，本测保证它和 pyproject 不漂移。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

import news_collector
from news_collector.cli import app


def _pyproject_version() -> str:
    """从 pyproject.toml 解析 [project] version。

    不用 tomllib 直接读字符串：tomllib 是 Python 3.11+，本项目 requires-python 也是 ≥3.11，
    但保持低依赖。pyproject 顶部 5 行就有版本号，正则简单稳定。
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', content, flags=re.MULTILINE)
    assert m, f"pyproject.toml 未找到 version: {pyproject}"
    return m.group(1)


def test_dunder_version_matches_pyproject() -> None:
    """`news_collector.__version__` 必须 == pyproject.toml [project].version。

    `__init__.py` 走 `importlib.metadata.version("raw-news-collector")` 读已安装包元数据；
    本测试运行依赖 `uv sync` / `pip install -e .` 把项目装到 venv，metadata 才存在。
    """
    expected = _pyproject_version()
    assert news_collector.__version__ == expected, (
        f"__version__ ({news_collector.__version__}) 与 pyproject.toml ({expected}) 不一致；"
        "若 __version__ 为 '0.0.0+local'，说明 venv 没装本包（跑 uv sync 修复）"
    )


def test_cli_version_flag_matches_dunder() -> None:
    """`news-collector --version` 输出必须含 `__version__` 值。"""
    runner = CliRunner()
    result: Any = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert news_collector.__version__ in result.output
    assert "news-collector" in result.output
