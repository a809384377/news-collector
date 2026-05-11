"""信源适配器抽象 + HTTP 重试装饰器（设计 §3.7 / §9）。

- ``SourceAdapter`` Protocol：5 类适配器共同接口
- ``with_retry``：async 装饰器，HTTP 5xx / 429 / 网络异常按指数退避（1→2→4→8s，最多 4 次）
- ``raise_for_transient_status``：把 httpx.Response 的状态码翻译成可重试 / 不可重试两类异常

不在本模块导出具体适配器实现，避免启动时 import 全部源类型。
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, TypeVar

import httpx
from loguru import logger

from ..models import RawArticle


# ---- Protocol ---------------------------------------------------------------


class SourceAdapter(Protocol):
    """5 类适配器共同接口。

    约定：
    - ``source_type`` 是类属性，对应 sources.yaml 顶层 key（``rss`` / ``reddit`` 等）
    - ``fetch`` 是 async；接收单个 source 字典（已 enabled 过滤）
    - ``since`` 为 ``None`` 表示不过滤（首次抓全量），否则仅返回 ``published_at >= since`` 的条目
    - 缺 ``published_at`` 的条目无法用 ``since`` 比较，建议 adapter 直接放行（由 pipeline 决定如何记账）
    - 出错时抛异常，由 pipeline 统一捕获并写 ``source_state.last_error``
    """

    source_type: str

    async def fetch(
        self,
        source: dict[str, Any],
        since: datetime | None,
    ) -> list[RawArticle]: ...


# ---- 可重试异常 / 状态码 ------------------------------------------------------


_TRANSIENT_HTTP_STATUS: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)


class TransientHTTPError(Exception):
    """HTTP 5xx / 429 等"应当重试"错误。

    与 ``httpx.HTTPStatusError`` 区分：后者由 ``response.raise_for_status()`` 抛出，
    覆盖所有 4xx/5xx；本异常仅在 _TRANSIENT_HTTP_STATUS 命中时抛，专门触发重试装饰器。
    """

    def __init__(self, status_code: int, url: str, message: str = "") -> None:
        super().__init__(f"HTTP {status_code} {url}: {message}")
        self.status_code = status_code
        self.url = url


def raise_for_transient_status(response: httpx.Response) -> None:
    """对 transient 状态抛 ``TransientHTTPError``，对其他 4xx/5xx 抛 ``HTTPStatusError``。

    2xx / 3xx 不抛（httpx 默认跟随 redirect，3xx 在客户端层不会出现于 .status_code）。
    """
    code = response.status_code
    if code in _TRANSIENT_HTTP_STATUS:
        raise TransientHTTPError(
            code, str(response.request.url), response.reason_phrase
        )
    if 400 <= code < 600:
        response.raise_for_status()


# ---- 重试装饰器 -------------------------------------------------------------


_R = TypeVar("_R")


def with_retry(
    max_attempts: int = 4,
    backoff_base: float = 1.0,
) -> Callable[[Callable[..., Awaitable[_R]]], Callable[..., Awaitable[_R]]]:
    """指数退避重试装饰器（async-only）。

    触发重试的异常：
      - ``httpx.NetworkError`` / ``httpx.TimeoutException`` / ``httpx.RemoteProtocolError``
      - ``TransientHTTPError``（HTTP 5xx / 429 / 408 / 425）

    其他异常（4xx 非 transient / 解析错 / 编程错）原样抛出，不重试。

    退避：第 N 次失败后 sleep ``backoff_base * 2**(N-1)`` 秒
    （N=1 → 1s, N=2 → 2s, N=3 → 4s；max_attempts=4 时累积最多 7 秒等待）。
    """

    def decorator(
        func: Callable[..., Awaitable[_R]],
    ) -> Callable[..., Awaitable[_R]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _R:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except (
                    httpx.NetworkError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                    TransientHTTPError,
                ) as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        logger.warning(
                            f"重试 {attempt}/{max_attempts} 次后仍失败：{exc!r}"
                        )
                        raise
                    delay = backoff_base * (2 ** (attempt - 1))
                    logger.info(
                        f"第 {attempt}/{max_attempts} 次失败（{exc!r}），{delay}s 后重试"
                    )
                    await asyncio.sleep(delay)
            assert last_exc is not None  # pragma: no cover
            raise last_exc

        return wrapper

    return decorator
