"""URL 规范化与去重哈希工具（设计 §6 去重三层兜底）。

- ``canonicalize_url``      — scheme/host 小写 + 去 fragment + 剥离追踪参数 + query 按 key 排序，
                              其余位（path / 端口 / 用户名密码 / 非追踪 query）保持原样
- ``url_canonical_hash``    — 规范化 URL 的 sha256，作 articles_raw.url_canonical_hash（去重第二层）
- ``content_hash``          — ``(title + body[:500])`` 的 sha256，作 articles_raw.content_hash（去重第三层）

不做 path 末尾斜杠归一：部分 CMS 区分 ``/a`` 与 ``/a/``，归一会引入误合并风险；
追踪参数不存在标准化清单，这里收口在主流广告/邮件分析平台（utm_*/fbclid/gclid 等），
新参数发现后再扩。
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# utm_* 等以前缀方式过滤
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)

# 精确匹配（小写）的追踪/分析参数
_TRACKING_PARAM_EXACT: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "_hsenc",
        "_hsmi",
        "yclid",
        "msclkid",
        "spm",
    }
)

# 协议默认端口，URL 显式带这些端口时归一掉
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    if k in _TRACKING_PARAM_EXACT:
        return True
    return any(k.startswith(p) for p in _TRACKING_PARAM_PREFIXES)


def canonicalize_url(url: str) -> str:
    """返回规范化后的 URL 字符串。

    幂等：``canonicalize_url(canonicalize_url(x)) == canonicalize_url(x)``。
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    # netloc 重组：保留 userinfo + 必要时端口
    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    # 过滤 + 排序 query
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    cleaned = [(k, v) for k, v in pairs if not _is_tracking_param(k)]
    cleaned.sort(key=lambda kv: kv[0])
    query = urlencode(cleaned, doseq=True)

    # 丢弃 fragment（第 5 元素传空串）
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def url_canonical_hash(url: str) -> str:
    """规范化 URL 的 sha256（hex）。"""
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def content_hash(title: str, body: str) -> str:
    """``(title + body[:500])`` 的 sha256（hex）。

    body 取前 500 字符是设计 §5 schema 注释里固定的截断长度。
    超过 500 字符之外的差异不影响 hash，避免长文小改产生噪声重复。
    """
    payload = f"{title}{body[:500]}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
