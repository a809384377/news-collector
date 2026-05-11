"""logging_setup.init_logging 单测。

覆盖点：
1. 调用后 logger.info 写入到目标文件
2. 重复调用幂等（不重复加 sink，日志不重复）
3. force=True 重新挂 sink（旧的被 remove）
4. level / rotation / retention 字段正确传给 loguru.logger.add
5. file 字段含 `~` 时正确 expanduser
6. 相对路径 → 解析到 home 之下
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from newsbox import logging_setup
from newsbox.config import LoggingConfig


@pytest.fixture(autouse=True)
def _reset_logging_setup_state() -> None:
    """每个用例运行前后都重置模块级状态，避免互扰。"""
    logging_setup.reset_for_tests()
    yield
    logging_setup.reset_for_tests()


def _make_cfg(
    file: str = "logs/newsbox.log",
    level: str = "info",
    rotation: str = "daily",
    retention_days: int = 30,
) -> LoggingConfig:
    return LoggingConfig(
        level=level,
        file=file,
        rotation=rotation,
        retention_days=retention_days,
    )


def test_init_logging_writes_to_file(tmp_path: Path) -> None:
    """init_logging 之后 logger.info 落盘到 <home>/logs/newsbox.log。"""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg()

    handler_id = logging_setup.init_logging(cfg, home=tmp_path)
    assert handler_id is not None

    logger.info("hello-from-test")
    # loguru 同步 sink 不需要 flush，但保险显式 complete 一下
    logger.complete()

    log_file = tmp_path / "logs" / "newsbox.log"
    assert log_file.is_file()
    assert "hello-from-test" in log_file.read_text(encoding="utf-8")


def test_init_logging_is_idempotent(tmp_path: Path) -> None:
    """重复调用不会重复挂 sink；日志不出现两次。"""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg()

    h1 = logging_setup.init_logging(cfg, home=tmp_path)
    h2 = logging_setup.init_logging(cfg, home=tmp_path)
    h3 = logging_setup.init_logging(cfg, home=tmp_path)

    assert h1 == h2 == h3

    logger.info("only-once")
    logger.complete()

    log_file = tmp_path / "logs" / "newsbox.log"
    content = log_file.read_text(encoding="utf-8")
    # 同一行 only-once 只应出现 1 次（如果重复挂 sink，会出现 2-3 次）
    assert content.count("only-once") == 1


def test_init_logging_force_resets_sink(tmp_path: Path) -> None:
    """force=True 时移除旧 handler 并重新挂；新 id 不同于旧 id。"""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg()

    h1 = logging_setup.init_logging(cfg, home=tmp_path)
    h2 = logging_setup.init_logging(cfg, home=tmp_path, force=True)

    assert h1 is not None
    assert h2 is not None
    assert h1 != h2  # force 重挂应分到新 id

    logger.info("after-force")
    logger.complete()

    log_file = tmp_path / "logs" / "newsbox.log"
    content = log_file.read_text(encoding="utf-8")
    # force 重挂只剩 1 个 file sink，所以日志只 1 行
    assert content.count("after-force") == 1


def test_init_logging_passes_correct_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """level / rotation / retention 字段正确翻译并传给 loguru.logger.add。"""
    captured: dict[str, Any] = {}

    real_add = logger.add

    def fake_add(*args: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        if args:
            captured["sink"] = args[0]
        # 调真实 add 让幂等状态不破坏（取一个真 handler id）
        return real_add(*args, **kwargs)

    monkeypatch.setattr(logger, "add", fake_add)

    cfg = _make_cfg(
        file="logs/newsbox.log",
        level="warning",
        rotation="daily",
        retention_days=7,
    )
    logging_setup.init_logging(cfg, home=tmp_path)

    assert captured["level"] == "WARNING"  # 大写
    assert captured["rotation"] == "00:00"  # daily → 00:00
    assert captured["retention"] == "7 days"
    assert captured["encoding"] == "utf-8"
    assert "format" in captured
    # sink 应是绝对路径字符串
    sink_path = Path(captured["sink"])
    assert sink_path.is_absolute()
    assert sink_path == tmp_path / "logs" / "newsbox.log"


def test_init_logging_expanduser_for_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file 含 `~` 时正确 expanduser；不会把 ~ 字面量保留。"""
    captured: dict[str, Any] = {}

    def fake_add(*args: Any, **kwargs: Any) -> int:
        captured["sink"] = args[0] if args else kwargs.get("sink")
        # 不真挂 sink（避免污染真用户家目录）
        return 999

    monkeypatch.setattr(logger, "add", fake_add)

    cfg = _make_cfg(file="~/.newsbox/logs/newsbox.log")
    logging_setup.init_logging(cfg, home=tmp_path)

    sink = captured["sink"]
    assert "~" not in sink
    assert str(Path.home()) in sink


def test_init_logging_translate_unknown_rotation_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rotation 是已知别名外的值（如 `100 MB`）原样传给 loguru。"""
    captured: dict[str, Any] = {}

    def fake_add(*args: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 1234

    monkeypatch.setattr(logger, "add", fake_add)

    cfg = _make_cfg(rotation="100 MB")
    logging_setup.init_logging(cfg, home=tmp_path)

    assert captured["rotation"] == "100 MB"  # 原样透传


def test_init_logging_creates_logs_dir(tmp_path: Path) -> None:
    """logs 目录不存在时也能正常初始化（防御性 mkdir）。"""
    cfg = _make_cfg(file="logs/newsbox.log")
    # 注意：tmp_path/logs 不预创建
    logging_setup.init_logging(cfg, home=tmp_path)

    logger.info("dir-created")
    logger.complete()

    log_file = tmp_path / "logs" / "newsbox.log"
    assert log_file.is_file()
