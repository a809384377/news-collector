"""twikit 适配器 —— X (Twitter) 信源 cookie-based 直连抓取（设计 §3.7 / s10）。

走 ``twikit.Client`` 用浏览器 ``auth_token`` + ``ct0`` 直接打 X 的 GraphQL
endpoints，绕开 RSSHub 默认 ``UserTweets`` endpoint 对 reply / quote-tweet /
self-thread 类条目的整体截断（s10 sprint 起因，BRIEF.md §问题描述）。

合约
----
- ``source_type = "twikit"`` 类属性
- ``__init__`` 全部 keyword-only 可选参数（pipeline 走无参构造路径）
- ``fetch(source, since)``：
  - ``source["url"]`` 是 X handle（裸字符串或带前导 ``@``）—— 不是完整 URL
  - 返回 ``RawArticle`` 列表，``external_id`` 是纯 tweet.id 数字字符串
    （D-arch-3）；``url`` 是 ``https://x.com/<handle>/status/<id>`` 形式
    （命中第二层 ``url_canonical_hash`` 去重，跨 source_type 拦截重复入库）

since 语义（D-arch-2）
---------------------
twikit 不支持原生 since_id / since_time；本适配器实现等价：page-by-page
翻页，遇到第一个 ``published_at < since`` 的条目即停（X 倒序保证之后都更老）。
``max_pages`` 上限保护（默认 5，可在 ``~/.newsbox/config.yaml`` 的
``fetch.twikit.max_pages`` 覆盖）防止误填 since 穷举翻页。

错误传播
--------
adapter 抛 ``TwikitAuthError`` / ``TwikitRateLimitError`` /
``TwikitUserUnavailableError``，由 pipeline 的 ``except Exception`` 兜底写到
``source_state.last_error``；与 RSSAdapter 抛 ``httpx.HTTPStatusError`` 的
契约保持一致。

cookies 持久化（D-auth-1）
-------------------------
- 用户首次手填 ``~/.newsbox/twikit_cookies.json`` 含 ``{auth_token, ct0}`` 两键
- adapter 启动 ``client.load_cookies(...)`` → ``apply_patches()`` →
  ``patch_keep_latest_ct0(client)`` → 预热请求触发 X 下发 fresh ct0
- fetch 结束（不论成败）原子写回完整 cookies jar（含新 ct0）；下次启动
  load 拿到的就是 fresh ct0，无需用户介入
- 仅当 ``auth_token`` 失效时（一般数月一次）需用户重新从浏览器复制
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from ..config import DEFAULT_HOME
from ..models import RawArticle
from ..utils.atomic import atomic_replace
from ._twikit_patches import apply_patches, patch_keep_latest_ct0, patch_safe_get_cookies

# ---- 常量 -------------------------------------------------------------------

_SOURCE_TYPE = "twikit"
_PAGE_SIZE = 40
"""twikit 单页推文数上限（X GraphQL ``UserTweets`` endpoint 限制 ~40）。"""

_TITLE_FALLBACK_LEN = 120
"""``RawArticle.title`` 强制非空。tweet 无 text（纯媒体推等）时取 body 前 N
字；body 也空时回落到 ``[tweet by @handle]`` 占位。"""

_DEFAULT_THROTTLE_SECS = 2.0
"""页与页之间 sleep 秒数；X 风控敏感，串行 + 2s 间隔实测稳定。"""

_DEFAULT_MAX_PAGES = 5
"""未配置时的 max_pages 上限（与 ``_defaults/config.default.yaml`` 一致）。"""


# ---- 异常类 -----------------------------------------------------------------


class TwikitAuthError(RuntimeError):
    """cookies 缺失 / 格式不正 / ``auth_token`` 或 ``ct0`` 失效。

    错误文案包含可执行的恢复指引（去浏览器 devtools 拿 cookie 的具体步骤）。
    """


class TwikitRateLimitError(RuntimeError):
    """X 返回 429（信源级失败，pipeline 走 consecutive_failures 计数）。"""


class TwikitUserUnavailableError(RuntimeError):
    """目标用户不存在 / 被封 / 不可见（``UserNotFound`` / ``UserUnavailable``）。"""


# ---- 模块级 helper ----------------------------------------------------------


def _parse_created_at(raw_value: Any) -> datetime:
    """twikit 暴露 Twitter legacy 格式 ``Mon Mar 18 09:23:34 +0000 2024``。

    若解析失败回落到 ``datetime.now(UTC)`` —— 让条目仍可入库，由 pipeline 的
    fetched_at 兜底；不抛异常以免拖垮整源。
    """
    if not raw_value:
        return datetime.now(timezone.utc)
    if isinstance(raw_value, datetime):
        return raw_value if raw_value.tzinfo else raw_value.replace(tzinfo=timezone.utc)
    s = str(raw_value)
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return datetime.now(timezone.utc)


def _note_tweet_text(tweet: Any) -> str | None:
    """note_tweet（X Premium 长推）的展开全文；缺则返回 None。

    twikit 不把 ``note_tweet`` 暴露为属性，从 ``tweet._data`` 字典直接挖。
    """
    data = getattr(tweet, "_data", None)
    if not isinstance(data, dict):
        return None
    note = data.get("note_tweet")
    if not isinstance(note, dict):
        return None
    results = note.get("note_tweet_results") or {}
    inner = results.get("result") or {}
    text = inner.get("text")
    return text if isinstance(text, str) and text else None


def _article_payload(tweet: Any) -> dict[str, Any] | None:
    """X long-form Article 元数据（含 title / body）；缺则返回 None。"""
    data = getattr(tweet, "_data", None)
    if not isinstance(data, dict):
        return None
    art = data.get("article")
    if isinstance(art, dict) and art:
        return art
    return None


def _article_text(article: dict[str, Any]) -> str | None:
    """从 article 元数据尽力抽 plain text body；schema 版本不一，试几个 key。"""
    # 新 schema：article.article_results.result.content_state.text
    res = (article.get("article_results") or {}).get("result") or {}
    cs = res.get("content_state") or {}
    body = cs.get("text") if isinstance(cs, dict) else None
    if isinstance(body, str) and body.strip():
        return body
    # 老 / 直接 schema
    for key in ("contents", "content", "text", "body", "full_text"):
        v = article.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            chunks: list[str] = []
            for block in v:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content")
                    if isinstance(t, str) and t.strip():
                        chunks.append(t)
            if chunks:
                return "\n\n".join(chunks)
    title = article.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return None


def _truthy(v: Any) -> bool:
    """空 dict/list/str/None 视为假；其他按 ``bool`` 判定。"""
    if v is None:
        return False
    if isinstance(v, (dict, list, str)):
        return len(v) > 0
    return bool(v)


def _classify_long_form(tweet: Any) -> str | None:
    """推导 ``is_long_form`` 取值：``article`` / ``note_tweet`` / ``normal``。

    优先级：显式 article 字段 > 显式 note_tweet 字段 > 普通短推。x-get
    ``classifier.py`` 的 displayTextRange / "/i/web/status/" 启发式仅
    twitterapi.io / RSSHub 路径才需要 —— twikit 既然能直接看 ``_data``
    里的 article / note_tweet，无需启发式回落。
    """
    data = getattr(tweet, "_data", None) or {}
    if isinstance(data, dict):
        if _truthy(data.get("article")):
            return "article"
        if _truthy(data.get("note_tweet")):
            return "note_tweet"
    return "normal"


# ---- 主类 -------------------------------------------------------------------


class TwikitAdapter:
    """cookie-based twikit 适配器。

    实例化:
        无参 (pipeline 路径):
            ``TwikitAdapter()`` — 从 ``~/.newsbox/twikit_cookies.json`` 读 cookies，
            从 ``~/.newsbox/config.yaml`` 读 max_pages（fallback 5）

        测试 / 调试注入:
            ``TwikitAdapter(cookies_path=..., max_pages=..., throttle_secs=...,
                            client_factory=...)``
    """

    source_type: str = _SOURCE_TYPE

    def __init__(
        self,
        *,
        cookies_path: Path | None = None,
        max_pages: int | None = None,
        throttle_secs: float = _DEFAULT_THROTTLE_SECS,
        client_factory: Any = None,
    ) -> None:
        self._cookies_path: Path = (
            Path(cookies_path)
            if cookies_path is not None
            else DEFAULT_HOME / "twikit_cookies.json"
        )
        self._max_pages_override: int | None = max_pages
        self._throttle_secs: float = max(0.0, float(throttle_secs))
        # client_factory: 测试注入。生产路径走 _build_client 真实构造。
        # 必须是 async callable 返回已 wire 好 (load_cookies + patches + 预热) 的 client
        self._client_factory: Any = client_factory

    # ---- 主入口 ------------------------------------------------------------

    async def fetch(
        self,
        source: dict[str, Any],
        since: datetime | None,
    ) -> list[RawArticle]:
        source_id = source["id"]
        handle = str(source["url"]).lstrip("@").strip()
        if not handle:
            raise TwikitAuthError(
                f"twikit source {source_id} url 字段必须是 X handle（如 'dotey'），"
                f"实际拿到空字符串"
            )

        max_pages = self._resolve_max_pages()

        # 构造 client（生产路径会触发 load_cookies / patches / 预热）
        client = await self._build_client()

        # 解析目标用户
        user = await self._resolve_user(client, handle)

        # 首页 + 翻页
        articles = await self._collect_pages(
            client=client,
            user=user,
            handle=handle,
            source_id=source_id,
            since=since,
            max_pages=max_pages,
        )

        # cookies 持久化（fetch 成败都写回，让 fresh ct0 落盘；失败仅 warn）
        self._save_cookies_atomic(client)

        return articles

    # ---- 子流程 ------------------------------------------------------------

    def _resolve_max_pages(self) -> int:
        """优先 __init__ 注入；否则 lazy load_config；fallback 5。

        config 读取失败（home 不存在 / yaml 损坏 / 字段缺失）一律 fallback 而非
        抛异常 —— max_pages 不是关键路径，不该让"配置文件局部损坏"打挂 fetch。
        """
        if self._max_pages_override is not None:
            return int(self._max_pages_override)
        try:
            from ..config import load_config

            cfg = load_config(DEFAULT_HOME)
            return int(cfg.fetch.twikit.max_pages)
        except Exception as exc:  # noqa: BLE001 — 配置读失败不致命
            logger.debug(
                f"twikit max_pages 读 config 失败，回落到默认 {_DEFAULT_MAX_PAGES}: {exc!r}"
            )
            return _DEFAULT_MAX_PAGES

    def _load_cookies_or_raise(self) -> dict[str, str]:
        """检查 cookies.json 存在性 + auth_token / ct0 形状。

        本方法不解析 twikit 自身的完整 cookies jar（那由 ``client.load_cookies``
        负责）；只校验"用户起步必填两字段"是否就位，让错误文案在 fetch 早期
        给出可执行恢复指引。
        """
        import json

        path = self._cookies_path
        if not path.exists():
            raise TwikitAuthError(
                f"twikit cookies 文件不存在: {path}\n"
                f"首次配置：\n"
                f"  1. 打开 X.com 登录账号\n"
                f"  2. F12 → Application → Cookies → x.com\n"
                f"  3. 复制 auth_token 与 ct0 两个值\n"
                f"  4. 写入 {path}：{{\"auth_token\": \"...\", \"ct0\": \"...\"}}\n"
                f"详见 docs/twikit-setup.md"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TwikitAuthError(
                f"{path} 不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise TwikitAuthError(
                f"{path} 应为 JSON 对象，实际是 {type(data).__name__}"
            )
        auth_token = str(data.get("auth_token") or "").strip()
        ct0 = str(data.get("ct0") or "").strip()
        if not auth_token or auth_token.startswith("<"):
            raise TwikitAuthError(
                f"{path} 缺少 'auth_token' 字段（或仍是 <placeholder>）。"
                f"从浏览器 devtools 重新复制后填入。详见 docs/twikit-setup.md §1"
            )
        if not ct0 or ct0.startswith("<"):
            raise TwikitAuthError(
                f"{path} 缺少 'ct0' 字段（或仍是 <placeholder>）。"
                f"从浏览器 devtools 复制 ct0（~160 字符 hex 串）后填入。"
                f"详见 docs/twikit-setup.md §1"
            )
        return {"auth_token": auth_token, "ct0": ct0}

    async def _build_client(self) -> Any:
        """构造已完成 patches / load_cookies / 预热的 twikit Client。

        测试路径走 ``self._client_factory()`` 返回 mock；生产路径走 twikit
        真实构造。预热请求 401/403 是常态（旧 ct0 stale），用 try/except
        吞掉 —— ``patch_keep_latest_ct0`` 已让 Set-Cookie 下发的 fresh ct0
        覆盖到 client.http.cookies。
        """
        if self._client_factory is not None:
            return await self._client_factory()

        # 校验起步必填两字段（早期失败给可执行文案）
        self._load_cookies_or_raise()

        # 应用 module-level patches（幂等）
        apply_patches()

        try:
            from twikit import Client
        except ImportError as exc:  # pragma: no cover — pyproject 已 pin
            raise RuntimeError(
                f"twikit 未安装（pyproject 应 pin twikit==2.3.3）: {exc}"
            ) from exc

        client = Client("en-US")

        # 实例级 patch：保留最新 ct0 覆盖磁盘旧值
        patch_keep_latest_ct0(client)
        # 实例级 patch：get_cookies 按 name dedup（s11 修 __cf_bm CookieConflict）
        patch_safe_get_cookies(client)

        try:
            client.load_cookies(str(self._cookies_path))
        except Exception as exc:  # noqa: BLE001 — twikit load_cookies 异常 schema 未文档化
            raise TwikitAuthError(
                f"twikit 无法 load cookies 自 {self._cookies_path}: {exc}"
            ) from exc

        # 预热：发一次 cheap GraphQL 让 X 下发 fresh ct0
        # patch_keep_latest_ct0 已在 client 上绑定；Set-Cookie 触发去重时会跑新逻辑
        try:
            await client.http.get(
                "https://x.com/i/api/graphql/NimuplG1OB7Fd2btCLdBOw/UserByScreenName",
                params={"variables": '{"screen_name":"x"}', "features": "{}"},
                headers=client._base_headers,
            )
        except Exception:  # noqa: BLE001 — 预热失败常态，正式请求仍会带 fresh ct0
            pass

        return client

    async def _resolve_user(self, client: Any, handle: str) -> Any:
        """``get_user_by_screen_name`` 包装，把 twikit 异常分类抛 newsbox 异常。"""
        try:
            from twikit.errors import (  # type: ignore[import-untyped]
                TooManyRequests,
                Unauthorized,
                UserNotFound,
                UserUnavailable,
            )
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"twikit 未安装: {exc}") from exc

        try:
            return await client.get_user_by_screen_name(handle)
        except UserNotFound as exc:
            raise TwikitUserUnavailableError(
                f"X 用户不存在: @{handle}"
            ) from exc
        except UserUnavailable as exc:
            raise TwikitUserUnavailableError(
                f"X 用户不可见 @{handle}: {exc}"
            ) from exc
        except Unauthorized as exc:
            raise TwikitAuthError(
                f"twikit unauthorized 解析 @{handle}: auth_token/ct0 可能已过期。"
                f"从浏览器 devtools 重新复制 auth_token + ct0 写入 "
                f"{self._cookies_path}"
            ) from exc
        except TooManyRequests as exc:
            raise TwikitRateLimitError(
                f"twikit 429 解析 @{handle}: X 限流中，几小时后再试 ({exc})"
            ) from exc

    async def _collect_pages(
        self,
        *,
        client: Any,
        user: Any,
        handle: str,
        source_id: str,
        since: datetime | None,
        max_pages: int,
    ) -> list[RawArticle]:
        """翻页直到窗外 / max_pages 用尽 / cursor 不再前进。

        mid-pagination 拿到 429 / 401 / 其他异常 → break（已抓的 articles 仍
        返回），不重抛 —— 部分数据比整源 0 数据更有用，与 RSSAdapter
        "feedparser bozo 不抛"哲学一致。
        """
        try:
            from twikit.errors import (  # type: ignore[import-untyped]
                TooManyRequests,
                Unauthorized,
            )
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"twikit 未安装: {exc}") from exc

        # 首页
        try:
            page = await user.get_tweets("Tweets", count=_PAGE_SIZE)
        except TooManyRequests as exc:
            raise TwikitRateLimitError(
                f"twikit 429 首页 @{handle}: {exc}"
            ) from exc
        except Unauthorized as exc:
            raise TwikitAuthError(
                f"twikit unauthorized 首页 @{handle}: {exc}"
            ) from exc

        articles: list[RawArticle] = []
        seen_ids: set[str] = set()
        pages_consumed = 0

        while page is not None and pages_consumed < max_pages:
            page_list = list(page)
            if not page_list:
                break

            out_of_window = False
            for tweet in page_list:
                tid = getattr(tweet, "id", None)
                if not tid:
                    continue
                tid_str = str(tid)
                if tid_str in seen_ids:
                    continue
                seen_ids.add(tid_str)

                published = _parse_created_at(getattr(tweet, "created_at", None))
                if since is not None and published < since:
                    out_of_window = True
                    continue  # 本页剩余继续看，下一页不再翻

                try:
                    articles.append(
                        self._build_article(tweet, source_id, handle, published)
                    )
                except Exception as exc:  # noqa: BLE001 — 单条解析失败不拖垮整源
                    logger.warning(
                        f"twikit {source_id} tweet={tid_str} 字段映射失败，跳过: {exc!r}"
                    )

            pages_consumed += 1
            if out_of_window or pages_consumed >= max_pages:
                break

            await asyncio.sleep(self._throttle_secs)

            try:
                next_page = await page.next()
            except TooManyRequests as exc:
                logger.warning(
                    f"twikit {source_id} mid-pagination 429（已抓 "
                    f"{len(articles)}）: {exc}"
                )
                break
            except Unauthorized as exc:
                logger.warning(
                    f"twikit {source_id} mid-pagination unauthorized（已抓 "
                    f"{len(articles)}）: {exc}"
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"twikit {source_id} mid-pagination 失败（已抓 "
                    f"{len(articles)}）: {exc!r}"
                )
                break

            if next_page is None or len(next_page) == 0:
                break
            # 防 cursor 不前进的 infinite loop
            if getattr(next_page, "next_cursor", None) == getattr(
                page, "next_cursor", None
            ):
                break

            page = next_page

        return articles

    def _build_article(
        self,
        tweet: Any,
        source_id: str,
        handle: str,
        published: datetime,
    ) -> RawArticle:
        """tweet → RawArticle 字段映射（D-arch-3 + 长文优先级 + s11 RT/quote 嵌套）。

        RT 处理（s11 Step 6）：X timeline 给 wrapper 的 ``tweet.text`` 是
        ``"RT @<原作>: <140 字精简版>…"`` 形式（X 在 timeline 层硬截断），完整
        正文在 ``tweet.retweeted_tweet`` 上（被转推原推的 note_tweet /
        article / full_text）。本方法若检测到 RT，把"内容源"切到
        ``retweeted_tweet``，title 加 ``"RT @<原作>: "`` 前缀保留转推关系信号。

        Quote 处理（s11 Step 6）：若 ``tweet.quote`` 非空，把引用推的内容
        拼接到 body 末尾，前缀 ``"\n\n--- 引用 @<原作> ---\n"``，让消费方在
        body 中即可看到引用上下文。
        """
        tid_str = str(getattr(tweet, "id", "") or "")
        if not tid_str:
            raise ValueError("tweet 缺 id")

        # RT 检测：内容源切到被转推原推（其上才有完整 note_tweet / article）
        rt_source = getattr(tweet, "retweeted_tweet", None)
        primary = rt_source if rt_source is not None else tweet

        # text 优先 full_text（twikit property 自动 fallback 到 note_tweet）
        text = getattr(primary, "full_text", None)
        if not isinstance(text, str):
            text = getattr(primary, "text", "") or ""

        # body 优先级：note_tweet 全文 > article body > text
        body: str = text
        note_full = _note_tweet_text(primary)
        if note_full:
            body = note_full
        else:
            article = _article_payload(primary)
            if article is not None:
                article_body = _article_text(article)
                if article_body:
                    body = article_body

        # quote 推：拼到 body 末尾保留引用上下文
        quote = getattr(tweet, "quote", None)
        if quote is not None:
            q_text = getattr(quote, "full_text", None)
            if not isinstance(q_text, str):
                q_text = getattr(quote, "text", "") or ""
            q_note = _note_tweet_text(quote)
            q_body = q_note or q_text
            q_user = getattr(quote, "user", None)
            q_handle = getattr(q_user, "screen_name", "") if q_user is not None else ""
            if q_body:
                sep = (
                    f"\n\n--- 引用 @{q_handle} ---\n"
                    if q_handle
                    else "\n\n--- 引用 ---\n"
                )
                body = f"{body}{sep}{q_body}"

        # title 强制非空；RT 加前缀保留转推信号
        title_seed = text or body
        if rt_source is not None:
            rt_user = getattr(rt_source, "user", None)
            rt_handle = (
                getattr(rt_user, "screen_name", "") if rt_user is not None else ""
            )
            rt_prefix = f"RT @{rt_handle}: " if rt_handle else "RT: "
            if title_seed:
                title = (rt_prefix + title_seed)[:_TITLE_FALLBACK_LEN]
            else:
                title = f"[RT by @{handle}]"
        elif title_seed:
            title = title_seed[:_TITLE_FALLBACK_LEN]
        else:
            title = f"[tweet by @{handle}]"

        return RawArticle(
            source_type=_SOURCE_TYPE,
            source_id=source_id,
            external_id=tid_str,
            url=f"https://x.com/{handle}/status/{tid_str}",
            title=title,
            body=body,
            published_at=published,
            is_long_form=_classify_long_form(primary),
        )

    def _save_cookies_atomic(self, client: Any) -> None:
        """``client.save_cookies(tmp) + 剥 ephemeral cookies + os.replace(tmp, dst)`` 三步原子。

        twikit ``Client.save_cookies`` 自身是 ``open().write()`` 非原子；
        失败仅 warn —— cookies rotation 不是关键路径，下次启动用旧文件 +
        预热请求拿 fresh ct0 仍可恢复。

        s11 hotfix：写入 tmp 后剥离 Cloudflare ``__cf_bm`` ephemeral cookie
        再 replace。``__cf_bm`` 30 分钟 TTL，持久化会导致下次启动 ``load_cookies``
        + 预热请求叠加成 jar 内同名跨 domain，再加上 twikit ``_base_request``
        每次都 ``self.get_cookies().copy()`` → ``CookieConflict``（虽然
        ``patch_safe_get_cookies`` 已在源头收口，但 ephemeral cookie 持久化
        本身就是设计冗余，直接剥）。
        """
        import json

        dst = self._cookies_path
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        try:
            client.save_cookies(str(tmp))
            # 剥 Cloudflare ephemeral cookies（详见 docstring）
            try:
                data = json.loads(tmp.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "__cf_bm" in data:
                    data.pop("__cf_bm", None)
                    tmp.write_text(
                        json.dumps(data, ensure_ascii=False),
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    f"twikit cookies 剥 __cf_bm 失败（仍按原文件 replace）: {exc!r}"
                )
            atomic_replace(tmp, dst)
        except Exception as exc:  # noqa: BLE001 — 非致命
            logger.warning(
                f"twikit cookies 原子写回失败（下次仍可用旧文件 + 预热请求恢复）"
                f": {exc!r}"
            )
            # 清理 tmp 残留
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


__all__ = [
    "TwikitAdapter",
    "TwikitAuthError",
    "TwikitRateLimitError",
    "TwikitUserUnavailableError",
]
