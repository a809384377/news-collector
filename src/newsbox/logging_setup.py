"""loguru file sink 初始化。

loguru 默认只挂 stderr sink。本模块负责按 `AppConfig.logging` 配置在
`~/.newsbox/logs/newsbox.log` 挂一个 file sink，让事故排查
有日志可查。

设计要点：
- 复用 `AppConfig.logging` 字段（level / file / rotation / retention_days），
  字符串如 `daily` / `hourly` 翻译为 loguru 接受的形式
- **不**移除 stderr sink；只新增 file sink
- 幂等：模块级 flag 防止重复 add（CLI 多次 import / 测试反复初始化都安全）
- 模块名特意叫 `logging_setup` 避开标准库 `logging` 的导入冲突

入口：`init_logging(logging_cfg, home=DEFAULT_HOME)`，cli.py 的 root 回调调用一次。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from .config import DEFAULT_HOME, LoggingConfig

# 与现有 stderr 默认输出对齐的纯文本 format（去掉颜色标签，文件不需要 ANSI）
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)

# config.yaml 里 rotation 是人类语义字符串；翻译到 loguru 接受的形式。
# loguru 也支持原样 `1 day` / `100 MB` / `00:00` 等多种格式，未命中此表则原样透传。
_ROTATION_ALIAS: dict[str, str] = {
    "daily": "00:00",      # 每天午夜切
    "hourly": "1 hour",
    "weekly": "1 week",
    "monthly": "1 month",
}

# 是否已经挂过 file sink；避免 cli 多次进入或 pytest 反复调用导致重复 sink。
_INITIALIZED: bool = False
_FILE_HANDLER_ID: int | None = None


def _resolve_log_path(file_field: str, home: Path) -> Path:
    """把 config.logging.file 的字符串解析成绝对 Path。

    - 含 `~` → expanduser
    - 相对路径 → 相对 home
    - 绝对路径 → 原样
    """
    raw = file_field.strip()
    if raw.startswith("~"):
        return Path(raw).expanduser()
    p = Path(raw)
    if p.is_absolute():
        return p
    return home / p


def _translate_rotation(rotation: str) -> str:
    """daily/hourly 这类人类语义字符串翻译到 loguru 形式；未命中原样透传。"""
    return _ROTATION_ALIAS.get(rotation.lower().strip(), rotation)


def init_logging(
    logging_cfg: LoggingConfig,
    home: Path = DEFAULT_HOME,
    *,
    force: bool = False,
) -> int | None:
    """挂一个 loguru file sink 到 `<home>/logs/newsbox.log`。

    Args:
        logging_cfg: AppConfig.logging（level / file / rotation / retention_days）
        home: 运行时目录，用于解析相对路径
        force: True → 即使已初始化也重新挂（移除旧 sink 再加），便于测试

    Returns:
        loguru handler id；未挂或已存在返回已存在的 id（首次为 None 兜底）
    """
    global _INITIALIZED, _FILE_HANDLER_ID

    if _INITIALIZED and not force:
        return _FILE_HANDLER_ID

    # 若 force 重新初始化且之前挂过，先 remove 旧 handler
    if force and _FILE_HANDLER_ID is not None:
        try:
            logger.remove(_FILE_HANDLER_ID)
        except ValueError:
            # handler 已被外部移除，忽略
            pass
        _FILE_HANDLER_ID = None

    log_path = _resolve_log_path(logging_cfg.file, Path(home))
    # 防御性确保目录存在（config.load_config 也会建一次，重复 mkdir 安全）
    log_path.parent.mkdir(parents=True, exist_ok=True)

    add_kwargs: dict[str, Any] = {
        "sink": str(log_path),
        "level": logging_cfg.level.upper(),
        "format": _FILE_FORMAT,
        "rotation": _translate_rotation(logging_cfg.rotation),
        "retention": f"{int(logging_cfg.retention_days)} days",
        "encoding": "utf-8",
        "enqueue": False,
    }

    handler_id = logger.add(**add_kwargs)
    _FILE_HANDLER_ID = handler_id
    _INITIALIZED = True
    return handler_id


def reset_for_tests() -> None:
    """仅供测试使用：移除 file sink + 清状态。让多个 test 互不干扰。"""
    global _INITIALIZED, _FILE_HANDLER_ID
    if _FILE_HANDLER_ID is not None:
        try:
            logger.remove(_FILE_HANDLER_ID)
        except ValueError:
            pass
    _FILE_HANDLER_ID = None
    _INITIALIZED = False
