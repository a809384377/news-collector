"""命令模块共享工具。

- ``home_option``：所有命令统一用的 ``--home`` typer.Option，避免重复声明
- ``load_app_config``：load_config + init_logging 的"一次性入口"，所有需要日志落盘的
  命令都应走它
"""
from __future__ import annotations

from pathlib import Path

import typer

from .. import config as config_module
from .. import logging_setup


def home_option() -> Path:
    """``--home`` 选项工厂；调用时返回 typer.Option 对象。

    用法::

        def my_cmd(home: Path = home_option()) -> None: ...
    """
    return typer.Option(
        config_module.DEFAULT_HOME,
        "--home",
        help="运行时数据目录（默认 ~/.news-collector）",
        envvar="NEWS_COLLECTOR_HOME",
    )


def load_app_config(home: Path) -> config_module.AppConfig:
    """加载 AppConfig 并幂等初始化 loguru file sink。

    所有需要日志落盘的命令都应走这个入口，避免散落 init_logging 调用。
    """
    cfg = config_module.load_config(home)
    logging_setup.init_logging(cfg.logging, home=home)
    return cfg
