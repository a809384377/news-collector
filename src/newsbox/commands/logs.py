"""``newsbox logs`` — 查看采集层日志尾部 N 行。

只读命令：从 ``AppConfig.logging.file`` 解析出绝对路径，读取尾部 N 行打印到 stdout。

设计要点：
- 不一次性 load 整个文件（日志可能很大）；用 ``collections.deque(maxlen=n)``
  做流式读取，内存 O(n) 行，IO 仍是 O(filesize) 但稳定。
- 容忍半截多字节字符：``errors='replace'``，避免日志切割边界导致 UnicodeDecodeError
  把命令搞挂。
- 文件不存在视为「fetch 还没跑过」友好报错并退出 1（区别于「文件存在但 0 字节」exit 0）。
- 副作用：``load_app_config`` 会幂等初始化 file sink；本命令只读语义不受影响，
  且不引入额外 sink 重复挂的风险（logging_setup 自带 _INITIALIZED flag）。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import typer

from ..logging_setup import _resolve_log_path
from ._helpers import home_option, load_app_config
from ._json import emit, emit_err, json_option


def _tail_lines(path: Path, n: int) -> list[str]:
    """读文件尾部 n 行；用 deque 避免一次性 load 大文件到内存。

    使用 ``errors='replace'`` 容忍半截多字节字符（rotation 切割边界容易出现）。
    """
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=n))


def logs_cmd(
    home: Path = home_option(),
    tail: int = typer.Option(50, "--tail", help="显示尾部 N 行（默认 50）"),
    json_output: bool = json_option(),
) -> None:
    """查看采集层日志尾部 N 行。"""
    cfg = load_app_config(home)
    log_path = _resolve_log_path(cfg.logging.file, home)

    if not log_path.exists():
        if json_output:
            emit_err(f"log file not found: {log_path}", path=str(log_path))
        else:
            typer.echo(
                f"[err] 日志文件不存在: {log_path}，可能 fetch 还没跑过",
                err=True,
            )
        raise typer.Exit(code=1)

    if log_path.stat().st_size == 0:
        if json_output:
            emit({"path": str(log_path), "lines": [], "tail": int(tail), "empty": True})
        else:
            typer.echo(f"  (log file is empty: {log_path})")
        raise typer.Exit(code=0)

    lines = _tail_lines(log_path, max(0, int(tail)))
    if json_output:
        # JSON 模式去掉末尾换行符，避免每个字符串都带 \n；空文件已在上方处理
        emit(
            {
                "path": str(log_path),
                "lines": [ln.rstrip("\n") for ln in lines],
                "tail": int(tail),
            }
        )
        return

    for line in lines:
        # deque 读出的每行通常含末尾 '\n'；用 nl=False 避免 typer.echo 再加一个
        # 换行造成双倍空行；同时对最后一行（可能没 '\n'）保持原样。
        typer.echo(line, nl=False)
        if not line.endswith("\n"):
            typer.echo("")
