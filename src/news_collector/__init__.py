"""news-collector — 采集层基础服务。"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # 单一真相源：从已安装包的 metadata 读取版本，与 pyproject.toml 同源。
    # 避免「pyproject.toml 升了但 __init__.py 漏升」的双源漂移（v0.5.2 真实事故）。
    __version__ = _pkg_version("raw-news-collector")
except PackageNotFoundError:
    # 开发模式下未 `uv sync` / `pip install -e .` 时（如直接 PYTHONPATH=src 运行）
    # 包 metadata 不可见。给个明显的占位让 CI/手测能立刻看出"装包没装"。
    __version__ = "0.0.0+local"
