"""``news-collector sources export`` 子命令实装。

s4-sources-management Step 5 subagent A 产出。备份导出，原始字节复制，零解析重写
（连原文件 EOL 风格都保留），保证 100% 保真。
"""
from __future__ import annotations

from pathlib import Path

import typer

from .._helpers import home_option


def sources_export_cmd(
    home: Path = home_option(),
    out: Path = typer.Option(
        None,
        "--out",
        help="导出文件路径；不传则打到 stdout",
    ),
) -> None:
    """导出 sources.yaml 原始字节（备份用）。"""
    yaml_path = home / "sources.yaml"
    if not yaml_path.exists():
        typer.echo(
            f"[err] sources.yaml 不存在: {yaml_path}\n"
            f"      请先 `news-collector sources seed`",
            err=True,
        )
        raise typer.Exit(code=1)

    raw = yaml_path.read_bytes()

    if out is None:
        # stdout：用 typer.echo 输出 decoded 文本（避免追加额外换行）
        text = raw.decode("utf-8")
        # typer.echo 默认 nl=True 会追加换行；sources.yaml 通常已以 \n 结尾，
        # 关掉自动换行避免多一个空行
        typer.echo(text, nl=False)
        return

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    # stdout 保持纯净；进度信息打到 stderr 方便 shell 重定向
    typer.echo(f"[ok] exported to {out_path}", err=True)
