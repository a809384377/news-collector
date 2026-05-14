"""adapter 注册表 —— single source of truth for all ``source_type``。

新增 source_type 时只在本文件加一行映射，所有命令、配置校验、pipeline
编排都从这里派生（sources / _io / probe / doctor / fetch / list / test
命令均不再硬编码 ``"rss"`` / ``"web"`` 字符串）。

合约
----
- 每个 adapter 实现 ``base.SourceAdapter`` Protocol：
  类属性 ``source_type: str`` + async ``fetch(source, since) -> list[RawArticle]``
- 按 adapter 类注册；pipeline 用 ``adapter_cls()`` 构造实例

测试 mock 接入第 3 类
--------------------
``monkeypatch.setitem(ADAPTER_REGISTRY, "fake", FakeAdapter)`` 即可让
``supported_types()`` 反映新类型，让 sources / doctor / fetch 命令自动识别
（前提：调用方在函数内调 ``supported_types()`` 实时派生，而非读
导入时绑定的 ``SOURCE_KINDS`` 常量副本）。
"""
from __future__ import annotations

from typing import Any

from .rss_adapter import RSSAdapter
from .twikit_adapter import TwikitAdapter
from .web_adapter import WebAdapter

ADAPTER_REGISTRY: dict[str, Any] = {
    "rss": RSSAdapter,
    "web": WebAdapter,
    "twikit": TwikitAdapter,
}


def supported_types() -> tuple[str, ...]:
    """已注册 source_type 的固定顺序元组（与 ``ADAPTER_REGISTRY`` 插入序一致）。"""
    return tuple(ADAPTER_REGISTRY.keys())
