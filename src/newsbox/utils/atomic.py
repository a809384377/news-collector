"""原子文件写入原语。

POSIX 上 ``os.replace`` 对同一文件系统内的 rename 保证原子（读侧永远看到
完整文件）；本模块封装"先写 .tmp 后 replace"惯用法。

主要消费者：``adapters/twikit_adapter.py`` 持久化 cookies jar（D-auth-1 决策
要求"每次 fetch 跑完原子写回，避免 partial write 损坏"）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """把 ``data`` 序列化为 JSON 并原子写入 ``path``。

    实现：写到同目录下的 ``<path>.tmp`` 后调 ``os.replace``。
    若同目录不可写或 rename 失败，原始异常向上抛。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def atomic_replace(tmp: Path, dst: Path) -> None:
    """把已写完的 ``tmp`` 原子 rename 到 ``dst``。

    用于"调用方自带写入逻辑（如 twikit ``Client.save_cookies``）、newsbox 只
    负责 rename 兜底原子性"的场景。要求 ``tmp`` 与 ``dst`` 在同一文件系统。
    """
    os.replace(str(Path(tmp)), str(Path(dst)))
