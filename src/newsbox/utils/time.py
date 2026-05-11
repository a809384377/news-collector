"""时间解析工具：CLI ``--since`` 参数支持 ``24h`` / ``7d`` / ``2026-05-01T00:00`` 三种格式。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# 24h / 7d / 30m / 2w 等：(数值)(单位)
_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)

_UNIT_TO_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_since(since: str | None, *, now: datetime | None = None) -> datetime | None:
    """把 ``--since`` 字符串解析成 UTC datetime。

    支持的形式：
      - ``"24h"`` / ``"7d"`` / ``"30m"`` / ``"2w"`` — 相对当前时间往前推
      - ``"2026-05-01"`` — 当作 UTC 当日 00:00:00
      - ``"2026-05-01T12:30:00"`` / ``"2026-05-01T12:30:00+08:00"`` — ISO 8601
      - ``None`` 或空串 — 返回 ``None``（表示不过滤）

    Args:
        since: 待解析字符串
        now: 当前时间（注入用，便于测试）；缺省 ``datetime.now(timezone.utc)``

    Raises:
        ValueError: 无法解析
    """
    if since is None or not str(since).strip():
        return None

    now = now or datetime.now(timezone.utc)
    s = str(since).strip()

    m = _RELATIVE_RE.match(s)
    if m:
        value = int(m.group(1))
        unit = m.group(2).lower()
        return now - timedelta(seconds=value * _UNIT_TO_SECONDS[unit])

    # ISO 风格：先尝试日期 + 时间，再退回日期-only
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"无法解析 since={s!r}：支持 '24h' / '7d' 等相对值，或 ISO 8601 时间"
        ) from exc

    # 无 tzinfo 的 ISO 时间按 UTC 解读
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
