"""CLI 命令模块包。

每条 CLI 命令一个文件，便于并行开发与导航：
- `_helpers` 共享工具（home option / app config 加载）
- `docker_helpers` docker compose subprocess 包装
- `fetch` / `db` / `config` / `sources/` / `state` 等：原 cli.py 中各命令搬迁后落点
  （`sources` 是包：__init__ + _io + _probe + 8 命令模块，s4-sources-management 拆分）

cli.py 只负责创建顶层 typer.Typer + 注册各命令模块导出的 app 或函数。
"""
