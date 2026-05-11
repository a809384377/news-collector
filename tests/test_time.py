"""utils.time.parse_since 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from newsbox.utils.time import parse_since


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_none_returns_none() -> None:
    assert parse_since(None) is None


def test_empty_string_returns_none() -> None:
    assert parse_since("") is None
    assert parse_since("   ") is None


def test_relative_hours() -> None:
    assert parse_since("24h", now=_NOW) == _NOW - timedelta(hours=24)


def test_relative_days() -> None:
    assert parse_since("7d", now=_NOW) == _NOW - timedelta(days=7)


def test_relative_minutes() -> None:
    assert parse_since("30m", now=_NOW) == _NOW - timedelta(minutes=30)


def test_relative_weeks() -> None:
    assert parse_since("2w", now=_NOW) == _NOW - timedelta(weeks=2)


def test_relative_uppercase_unit() -> None:
    # 大小写不敏感
    assert parse_since("24H", now=_NOW) == _NOW - timedelta(hours=24)


def test_iso_date_only() -> None:
    assert parse_since("2026-05-01") == datetime(
        2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc
    )


def test_iso_datetime_naive_treated_as_utc() -> None:
    assert parse_since("2026-05-01T12:30:00") == datetime(
        2026, 5, 1, 12, 30, 0, tzinfo=timezone.utc
    )


def test_iso_datetime_with_offset() -> None:
    # +08:00 → 实际 UTC 04:30
    expected = datetime(2026, 5, 1, 4, 30, 0, tzinfo=timezone.utc)
    assert parse_since("2026-05-01T12:30:00+08:00") == expected


def test_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_since("nonsense")
    with pytest.raises(ValueError):
        parse_since("24x")  # x 不是合法单位
