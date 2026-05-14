"""``newsbox.adapters`` 包公开 API。

- ``ADAPTER_REGISTRY`` / ``supported_types``：单一真相源（``registry.py``）
- ``RSSAdapter`` / ``WebAdapter`` / ``TwikitAdapter``：具体适配器实现
- ``base.SourceAdapter`` Protocol 与重试装饰器另行从 ``base`` 模块导入
- twikit 专属异常类从 ``twikit_adapter`` 模块导出（doctor / sources 命令按
  类型分流诊断时使用）
"""
from .registry import ADAPTER_REGISTRY, supported_types
from .rss_adapter import RSSAdapter
from .twikit_adapter import (
    TwikitAdapter,
    TwikitAuthError,
    TwikitRateLimitError,
    TwikitUserUnavailableError,
)
from .web_adapter import WebAdapter

__all__ = [
    "ADAPTER_REGISTRY",
    "supported_types",
    "RSSAdapter",
    "TwikitAdapter",
    "TwikitAuthError",
    "TwikitRateLimitError",
    "TwikitUserUnavailableError",
    "WebAdapter",
]
