"""utils.url 测试：规范化 + 哈希。"""

from __future__ import annotations

from news_collector.utils.url import canonicalize_url, content_hash, url_canonical_hash


# ---- canonicalize_url -------------------------------------------------------


def test_scheme_and_host_lowercased() -> None:
    assert (
        canonicalize_url("HTTP://Foo.COM/path")
        == "http://foo.com/path"
    )


def test_drop_fragment() -> None:
    assert (
        canonicalize_url("https://x.com/a/b#section")
        == "https://x.com/a/b"
    )


def test_drop_tracking_params_prefix_and_exact() -> None:
    url = (
        "https://example.com/post"
        "?utm_source=newsletter&utm_medium=email&fbclid=abc&gclid=zzz"
        "&id=1&page=2"
    )
    canonical = canonicalize_url(url)
    # 追踪参数全部去掉，剩余按 key 字典序排列
    assert canonical == "https://example.com/post?id=1&page=2"


def test_query_sorted_alphabetically() -> None:
    assert (
        canonicalize_url("https://x.com/a?b=2&a=1&c=3")
        == "https://x.com/a?a=1&b=2&c=3"
    )


def test_keep_path_trailing_slash_unchanged() -> None:
    # 不强行归一末尾斜杠，避免 /a vs /a/ 的语义误合并
    assert canonicalize_url("https://x.com/a/") == "https://x.com/a/"
    assert canonicalize_url("https://x.com/a") == "https://x.com/a"


def test_default_port_dropped() -> None:
    assert canonicalize_url("http://x.com:80/p") == "http://x.com/p"
    assert canonicalize_url("https://x.com:443/p") == "https://x.com/p"


def test_non_default_port_kept() -> None:
    assert canonicalize_url("http://x.com:8080/p") == "http://x.com:8080/p"


def test_blank_value_query_kept() -> None:
    # ?flag= 形式（值空但 key 存在）保留
    assert (
        canonicalize_url("https://x.com/a?flag=&id=1")
        == "https://x.com/a?flag=&id=1"
    )


def test_idempotent() -> None:
    raw = "HTTPS://Foo.COM/a/?utm_source=x&id=1#bar"
    once = canonicalize_url(raw)
    twice = canonicalize_url(once)
    assert once == twice


def test_strip_whitespace() -> None:
    assert canonicalize_url("  https://x.com/a  ") == "https://x.com/a"


# ---- url_canonical_hash -----------------------------------------------------


def test_url_canonical_hash_is_hex_sha256() -> None:
    h = url_canonical_hash("https://x.com/a")
    assert isinstance(h, str)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_same_canonical_url_same_hash() -> None:
    a = url_canonical_hash("HTTPS://Foo.COM/p?utm_source=x&id=1")
    b = url_canonical_hash("https://foo.com/p?id=1")
    assert a == b


def test_different_canonical_url_different_hash() -> None:
    assert url_canonical_hash("https://x.com/a") != url_canonical_hash(
        "https://x.com/b"
    )


# ---- content_hash -----------------------------------------------------------


def test_content_hash_is_hex_sha256() -> None:
    h = content_hash("title", "body")
    assert isinstance(h, str)
    assert len(h) == 64


def test_content_hash_deterministic() -> None:
    assert content_hash("t", "b") == content_hash("t", "b")


def test_content_hash_changes_with_title_or_body() -> None:
    base = content_hash("t", "b")
    assert content_hash("t2", "b") != base
    assert content_hash("t", "b2") != base


def test_content_hash_truncates_body_at_500() -> None:
    short_body = "x" * 500
    long_body = "x" * 500 + "DIFFERENT_TAIL_NOT_HASHED"
    assert content_hash("t", short_body) == content_hash("t", long_body)
