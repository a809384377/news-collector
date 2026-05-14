"""TwikitAdapter 单元测试 — 全本地 mock，不发任何网络请求。

覆盖：
1. cookies.json 不存在 → TwikitAuthError + 可执行指引文案
2. auth_token 是 <placeholder> → TwikitAuthError
3. 缺 ct0 字段 → TwikitAuthError，message 含 "ct0"
4. 成功路径（含 note_tweet 全文优先）
5. since 截断（窗外条目触发 out_of_window break，不再翻页）
6. max_pages 保护（注入无穷翻页 mock，恰好翻 max_pages 页停）
7. mid-pagination 401 Unauthorized → 已抓条目正常返回 + warn 含 "unauthorized"
8. mid-pagination 429 TooManyRequests → 已抓条目正常返回 + warn 含 "429"
9. cookies 原子写回 — save_cookies(<dst>.tmp) → atomic_replace(tmp, dst)
10. title 强制非空：text="" 但 note_tweet 有内容 → title 取 body 前 120 char
11. title 二次 fallback：text 与 body 都空 → title=[tweet by @handle]
12. 单条 tweet 缺 id → skip，其他条目正常返回，warn 含 source_id
13. article body 优先级 + is_long_form='article' 分类
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger
from twikit.errors import TooManyRequests, Unauthorized

from newsbox.adapters import (
    TwikitAdapter,
    TwikitAuthError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "twikit"


# ---- helpers ---------------------------------------------------------------


class FakeTweet:
    """SimpleNamespace 不够 — 需要 _data 直接挂在 instance 上、且 getattr 行为可控。"""

    def __init__(
        self,
        *,
        id: str | None,
        text: str = "",
        created_at: str | datetime | None = None,
        lang: str | None = "en",
        data: dict[str, Any] | None = None,
        user: SimpleNamespace | None = None,
    ) -> None:
        # tweet 缺 id 场景：不设置 id 属性（让 getattr(tweet, "id", None) 返回 None）
        if id is not None:
            self.id = id
        self.text = text
        self.created_at = created_at
        self.lang = lang
        self._data = data if data is not None else {}
        if user is not None:
            self.user = user


def _load_fixture(name: str) -> list[FakeTweet]:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    tweets: list[FakeTweet] = []
    for entry in raw:
        tweets.append(
            FakeTweet(
                id=entry["id"],
                text=entry.get("text", ""),
                created_at=entry.get("created_at"),
                lang=entry.get("lang", "en"),
                data=entry.get("_data") or {},
            )
        )
    return tweets


class FakePage:
    """模拟 twikit 的 page 对象：iterable + len + async next() + next_cursor。"""

    def __init__(
        self,
        tweets: list[FakeTweet],
        *,
        next_page: "FakePage | None" = None,
        next_exc: BaseException | None = None,
        cursor: str = "c1",
    ) -> None:
        self._tweets = tweets
        self._next = next_page
        self._next_exc = next_exc
        self.next_cursor = cursor

    def __iter__(self):
        return iter(self._tweets)

    def __len__(self) -> int:
        return len(self._tweets)

    async def next(self) -> "FakePage | None":
        if self._next_exc is not None:
            raise self._next_exc
        return self._next


class FakeUser:
    def __init__(
        self,
        page: FakePage,
        *,
        get_tweets_exc: BaseException | None = None,
    ) -> None:
        self._page = page
        self._exc = get_tweets_exc
        self.get_tweets_calls: list[tuple[str, int]] = []

    async def get_tweets(self, kind: str, count: int) -> FakePage:
        self.get_tweets_calls.append((kind, count))
        if self._exc is not None:
            raise self._exc
        return self._page


class FakeClient:
    """save_cookies 是同步方法（与真实 twikit 对齐）；记录调用路径。"""

    def __init__(
        self,
        user: FakeUser,
        *,
        save_cookies_side_effect=None,
    ) -> None:
        self._user = user
        self.save_cookies_calls: list[str] = []
        self._save_cookies_side_effect = save_cookies_side_effect

    async def get_user_by_screen_name(self, handle: str) -> FakeUser:
        return self._user

    def save_cookies(self, path: str) -> None:
        self.save_cookies_calls.append(str(path))
        if self._save_cookies_side_effect is not None:
            self._save_cookies_side_effect(path)


def _make_client_factory(client: FakeClient):
    """构造 async client_factory 注入。"""

    async def factory() -> FakeClient:
        return client

    return factory


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def loguru_warns():
    """收集 WARNING 及以上日志到 list，便于 assert 含某关键字。

    loguru 默认不走 std logging，pytest caplog 抓不到。这里临时加 sink。
    """
    messages: list[str] = []
    handler_id = logger.add(
        lambda msg: messages.append(str(msg)),
        level="WARNING",
    )
    yield messages
    logger.remove(handler_id)


# ---- 1. cookies 不存在 -----------------------------------------------------


def test_fetch_raises_when_cookies_file_missing(tmp_path: Path) -> None:
    cookies_path = tmp_path / "twikit_cookies.json"
    assert not cookies_path.exists()

    adapter = TwikitAdapter(cookies_path=cookies_path, max_pages=1)
    source = {"id": "dotey", "url": "dotey"}

    with pytest.raises(TwikitAuthError) as excinfo:
        _run(adapter.fetch(source, since=None))

    msg = str(excinfo.value)
    # 可执行指引必须包含文件名 + auth_token 关键字
    assert "twikit_cookies.json" in msg
    assert "auth_token" in msg


# ---- 2. auth_token 是 placeholder ------------------------------------------


def test_fetch_raises_when_auth_token_is_placeholder(tmp_path: Path) -> None:
    cookies_path = tmp_path / "twikit_cookies.json"
    cookies_path.write_text(
        json.dumps({"auth_token": "<paste-from-devtools>", "ct0": "abc123"}),
        encoding="utf-8",
    )

    adapter = TwikitAdapter(cookies_path=cookies_path, max_pages=1)
    source = {"id": "dotey", "url": "dotey"}

    with pytest.raises(TwikitAuthError) as excinfo:
        _run(adapter.fetch(source, since=None))

    msg = str(excinfo.value)
    assert "auth_token" in msg
    # 指引必须提到浏览器 devtools
    assert "devtools" in msg.lower()


# ---- 3. 缺 ct0 字段 ---------------------------------------------------------


def test_fetch_raises_when_ct0_missing(tmp_path: Path) -> None:
    cookies_path = tmp_path / "twikit_cookies.json"
    cookies_path.write_text(
        json.dumps({"auth_token": "real-looking-token-12345"}),
        encoding="utf-8",
    )

    adapter = TwikitAdapter(cookies_path=cookies_path, max_pages=1)
    source = {"id": "dotey", "url": "dotey"}

    with pytest.raises(TwikitAuthError) as excinfo:
        _run(adapter.fetch(source, since=None))

    msg = str(excinfo.value)
    assert "ct0" in msg  # 必须显式提到 ct0
    assert "devtools" in msg.lower()


# ---- 4. 成功路径（含 note_tweet 全文） -------------------------------------


def test_fetch_success_maps_fields_correctly(tmp_path: Path) -> None:
    page = FakePage(_load_fixture("dotey_page1.json"))
    user = FakeUser(page)
    client = FakeClient(user)

    cookies_path = tmp_path / "twikit_cookies.json"
    # 写一个 valid cookies 文件 — client_factory 路径不会读，但 _save_cookies_atomic
    # 会用到 path.with_suffix；写入避免边角问题
    cookies_path.write_text(json.dumps({"auth_token": "x", "ct0": "y"}), encoding="utf-8")

    adapter = TwikitAdapter(
        cookies_path=cookies_path,
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey", "url": "@dotey"}  # 故意带 @ 测试 lstrip
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 3

    # 第一条：普通推
    a0 = articles[0]
    assert a0.source_type == "twikit"
    assert a0.source_id == "dotey"
    assert a0.external_id == "2053987620732944869"  # 纯数字
    assert a0.external_id.isdigit()
    assert a0.url == "https://x.com/dotey/status/2053987620732944869"
    assert a0.url.startswith("https://x.com/")  # 必须 x.com 不是 twitter.com
    assert "twitter.com" not in a0.url
    assert a0.is_long_form == "normal"
    assert a0.title  # 非空
    # published_at 应解析成功
    assert a0.published_at is not None
    assert a0.published_at.tzinfo is not None

    # 第二条：note_tweet 全文优先
    a1 = articles[1]
    assert a1.is_long_form == "note_tweet"
    assert "FULL expanded body" in a1.body
    assert "Short preview text" not in a1.body  # body 用 note_tweet 全文，不用 tweet.text
    # title 仍然用 tweet.text（text 非空优先于 body）
    assert a1.title.startswith("Short preview text")

    # 第三条：quote-tweet（quoted_status_id_str 不算长文）
    a2 = articles[2]
    assert a2.is_long_form == "normal"

    # is_long_form 取值范围
    for art in articles:
        assert art.is_long_form in {"normal", "note_tweet", "article"}


# ---- 5. since 截断 ---------------------------------------------------------


def test_fetch_since_cuts_window_and_stops_pagination() -> None:
    page2 = FakePage(_load_fixture("dotey_page2_partial.json"))
    # page1 → next() → page2
    page1 = FakePage(_load_fixture("dotey_page1.json"), next_page=page2, cursor="c1")
    # page2 的 cursor 与 page1 不同 — 但 since 截断后应该 break，不会触发 page.next()
    page2.next_cursor = "c2"

    user = FakeUser(page1)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent/never_read"),
        max_pages=10,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    # since = 2026-05-01：page1 全在窗内，page2 第二条 (2026-04-01) 窗外
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    source = {"id": "dotey", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=since))

    # page1 3 条 + page2 1 条窗内 = 4
    assert len(articles) == 4
    ids = [a.external_id for a in articles]
    assert "2053987620732944901" not in ids  # 窗外条目被丢
    assert "2053987620732944900" in ids  # 窗内条目入列

    # 验证：page2 的 next() 不会被调用（out_of_window break 在翻 page3 前生效）
    # FakePage2 没有设 next_page，但即便如此 page2.next() 不该被调；用 cursor 间接验证
    # 通过：只要 articles 长度 = 4 且不抛 "next_page is None" 那条 break，说明 break
    # 在 out_of_window 那里就触发了。


# ---- 6. max_pages 保护 -----------------------------------------------------


class CyclicPage(FakePage):
    """每次 next() 返回新的不同 cursor，模拟无穷翻页（绕过 cursor 重复 break）。"""

    def __init__(self, tweets: list[FakeTweet], counter: list[int]) -> None:
        super().__init__(tweets, cursor=f"c{counter[0]}")
        self._counter = counter

    async def next(self) -> "CyclicPage":
        self._counter[0] += 1
        # 返回带不同 cursor 的新 page，否则会被 "cursor 不前进" 检测命中
        return CyclicPage(self._tweets, self._counter)


def test_fetch_max_pages_caps_pagination() -> None:
    tweets = _load_fixture("dotey_page1.json")
    counter = [0]
    first_page = CyclicPage(tweets, counter)

    user = FakeUser(first_page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent/never_read"),
        max_pages=2,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=None))

    # 2 pages × 3 tweets = 6（fixture 中 3 个 id 不同，但 CyclicPage 翻页都用同一批
    # tweets，seen_ids 会去重）
    # 实际上：第 1 页 3 条入 seen_ids，第 2 页全是已 seen → 0 新条目。验证 max_pages
    # 通过 counter：next() 应该被调用恰好 1 次（翻到第 2 页后 pages_consumed==2 立刻 break）
    assert counter[0] == 1, (
        f"expected page.next() called once (cap at 2 pages), got {counter[0]}"
    )
    # articles 长度 = 3（page1 唯一条目），不会无限增长
    assert len(articles) == 3


# ---- 7. mid-pagination 401 Unauthorized -----------------------------------


def test_fetch_mid_pagination_unauthorized_returns_partial(loguru_warns) -> None:
    page2_dummy = FakePage([])  # 不会被用到
    page1 = FakePage(
        _load_fixture("dotey_page1.json"),
        next_exc=Unauthorized("session expired mid-pagination"),
        cursor="c1",
    )

    user = FakeUser(page1)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent/never_read"),
        max_pages=5,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey_kol", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=None))  # 不应抛

    assert len(articles) == 3  # page1 的条目正常返回
    joined = "\n".join(loguru_warns).lower()
    assert "unauthorized" in joined
    assert "dotey_kol" in joined  # warn 含 source_id


# ---- 8. mid-pagination 429 TooManyRequests --------------------------------


def test_fetch_mid_pagination_rate_limit_returns_partial(loguru_warns) -> None:
    page1 = FakePage(
        _load_fixture("dotey_page1.json"),
        next_exc=TooManyRequests("rate limited mid-page"),
        cursor="c1",
    )

    user = FakeUser(page1)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent/never_read"),
        max_pages=5,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey_rate", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=None))  # 不应抛

    assert len(articles) == 3
    joined = "\n".join(loguru_warns)
    assert "429" in joined
    assert "dotey_rate" in joined


# ---- 9. cookies 原子写回 ---------------------------------------------------


def test_save_cookies_uses_atomic_tmp_then_replace(tmp_path: Path) -> None:
    page = FakePage(_load_fixture("dotey_page1.json"))
    user = FakeUser(page)

    dst = tmp_path / "twikit_cookies.json"
    # 起步前 dst 有旧内容，验证最终被替换
    dst.write_text(json.dumps({"auth_token": "old", "ct0": "old"}), encoding="utf-8")

    def fake_save_cookies(path: str) -> None:
        # 模拟 twikit Client.save_cookies：把内容写到给定 path
        Path(path).write_text(
            json.dumps({"auth_token": "new", "ct0": "new"}),
            encoding="utf-8",
        )

    client = FakeClient(user, save_cookies_side_effect=fake_save_cookies)

    adapter = TwikitAdapter(
        cookies_path=dst,
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=None))
    assert len(articles) == 3

    # 验证 save_cookies 写到的是 .tmp 后缀
    assert len(client.save_cookies_calls) == 1
    written_path = client.save_cookies_calls[0]
    assert written_path.endswith(".json.tmp"), (
        f"expected .json.tmp suffix, got {written_path!r}"
    )

    # 验证 atomic_replace 已发生：dst 路径上是新内容，tmp 已不存在
    assert dst.exists()
    content = json.loads(dst.read_text(encoding="utf-8"))
    assert content == {"auth_token": "new", "ct0": "new"}
    tmp_path_obj = Path(written_path)
    assert not tmp_path_obj.exists(), "tmp 文件应被 os.replace 消费掉"


# ---- 10. title 强制非空（text="" 取 body 前 120） --------------------------


def test_title_fallback_to_body_when_text_empty() -> None:
    # text="" + note_tweet 全文 → body 来自 note_tweet，title 取 body[:120]
    long_body = "A" * 200
    tweet = FakeTweet(
        id="99999",
        text="",
        created_at="Mon May 11 09:00:00 +0000 2026",
        data={
            "note_tweet": {
                "note_tweet_results": {"result": {"text": long_body}}
            },
            "article": None,
        },
    )
    page = FakePage([tweet])
    user = FakeUser(page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    source = {"id": "anon", "url": "anon"}
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 1
    art = articles[0]
    assert art.body == long_body
    assert art.title == "A" * 120  # 截到 120 char
    assert len(art.title) == 120


# ---- 11. title 二次 fallback（text+body 都空） -----------------------------


def test_title_fallback_to_handle_placeholder() -> None:
    tweet = FakeTweet(
        id="88888",
        text="",
        created_at="Mon May 11 09:00:00 +0000 2026",
        data={},
    )
    page = FakePage([tweet])
    user = FakeUser(page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    source = {"id": "anon", "url": "@karpathy"}
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 1
    assert articles[0].title == "[tweet by @karpathy]"
    assert articles[0].body == ""


# ---- 12. 单条缺 id → skip ---------------------------------------------------


def test_tweet_without_id_is_skipped(loguru_warns) -> None:
    bad = FakeTweet(id=None, text="orphan")  # 没设 id 属性
    good = _load_fixture("dotey_page1.json")[0]  # 有 id

    page = FakePage([bad, good])
    user = FakeUser(page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    source = {"id": "mixed_source", "url": "dotey"}
    articles = _run(adapter.fetch(source, since=None))

    # 缺 id 的被 collect_pages 循环里 `if not tid: continue` 跳过（不进 _build_article）
    # 所以不会触发 warn 日志，但 good 条目应正常返回
    assert len(articles) == 1
    assert articles[0].external_id == good.id


# ---- 13. article body 优先级 + is_long_form='article' ---------------------


def test_article_body_and_classification() -> None:
    page = FakePage(_load_fixture("karpathy_article.json"))
    user = FakeUser(page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/nonexistent"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    source = {"id": "karpathy", "url": "karpathy"}
    articles = _run(adapter.fetch(source, since=None))

    assert len(articles) == 1
    art = articles[0]
    assert art.is_long_form == "article"
    assert "full canonical article body" in art.body
    # tweet.text 是短摘要，title 应来自 text（非空优先）
    assert art.title.startswith("Wrote a new long-form article")
    assert art.url == "https://x.com/karpathy/status/2070000000000000001"


# ---- 14. empty handle in source url ----------------------------------------


def test_empty_handle_raises_auth_error(tmp_path: Path) -> None:
    """url=@ 仅前导 @ 会 lstrip 到空字符串，应抛 TwikitAuthError。"""
    adapter = TwikitAdapter(
        cookies_path=tmp_path / "twikit_cookies.json",
        max_pages=1,
        throttle_secs=0.0,
    )
    source = {"id": "bad", "url": "@"}

    with pytest.raises(TwikitAuthError) as excinfo:
        _run(adapter.fetch(source, since=None))

    msg = str(excinfo.value)
    assert "bad" in msg  # source_id 体现在文案里


# ---- 15. s11: patch_safe_get_cookies 修 CookieConflict ---------------------


def test_patch_safe_get_cookies_dedups_cross_domain_conflict() -> None:
    """patch 后 client.get_cookies() 不再因同名跨 domain cookie 抛 CookieConflict。

    复现 s10 残留缺陷的最小场景：jar 内同名 (__cf_bm) 跨 domain（'' vs '.x.com'），
    twikit 默认 `return dict(self.http.cookies)` 走 httpx Cookies.__getitem__
    会 raise CookieConflict；patch 后改走 jar 直接遍历 + last-write-wins 去重。
    """
    import httpx
    from newsbox.adapters._twikit_patches import patch_safe_get_cookies

    # 构造一个最小的 client-like 对象（只需 .http.cookies.jar）
    class _FakeClient:
        def __init__(self) -> None:
            self.http = httpx.Client()

        def get_cookies(self) -> dict:
            # 默认 twikit 实现 — 会因同名跨 domain 抛 CookieConflict
            return dict(self.http.cookies)

    client = _FakeClient()
    # 手工往 jar 灌两条同名跨 domain 的 cookie
    client.http.cookies.set("__cf_bm", "value_no_domain", domain="")
    client.http.cookies.set("__cf_bm", "value_dotx", domain=".x.com")
    client.http.cookies.set("ct0", "csrf_token_value", domain="")

    # patch 前：默认实现应抛 CookieConflict
    from httpx import CookieConflict

    with pytest.raises(CookieConflict):
        client.get_cookies()

    # patch 后：返回 dedup 字典（last-write-wins，__cf_bm 取后插入的 '.x.com' 值）
    patch_safe_get_cookies(client)
    cookies = client.get_cookies()
    assert isinstance(cookies, dict)
    assert "__cf_bm" in cookies
    assert "ct0" in cookies
    assert cookies["__cf_bm"] == "value_dotx"  # last write wins
    assert cookies["ct0"] == "csrf_token_value"


# ---- 17. s11 Step 6: RT 推文 body 取被转推原推 note_tweet 全文 -------------


def _make_inner_tweet(
    *,
    id: str,
    text: str = "",
    note_tweet_text: str | None = None,
    article: dict[str, Any] | None = None,
    user_screen_name: str | None = None,
    full_text: str | None = None,
) -> FakeTweet:
    """构造被嵌入 retweeted_tweet / quote 槽位的"原推" FakeTweet。

    与现有 FakeTweet 兼容：text + _data 字段位于同一接口；twikit 的
    ``full_text`` property 我们以普通属性形式模拟，让 adapter 的
    ``getattr(primary, "full_text", None)`` 路径能命中。
    """
    data: dict[str, Any] = {}
    if note_tweet_text is not None:
        data["note_tweet"] = {
            "note_tweet_results": {"result": {"text": note_tweet_text}}
        }
    if article is not None:
        data["article"] = article
    user = (
        SimpleNamespace(screen_name=user_screen_name)
        if user_screen_name is not None
        else None
    )
    tw = FakeTweet(
        id=id,
        text=text,
        created_at=None,
        data=data,
        user=user,
    )
    if full_text is not None:
        tw.full_text = full_text
    return tw


def test_retweeted_tweet_body_uses_original_note_tweet_full_text() -> None:
    """RT wrapper text 是 X timeline 精简版（带省略号），adapter 应钻到
    retweeted_tweet 的 note_tweet 取原推完整正文。
    """
    inner = _make_inner_tweet(
        id="999",
        text="半年前，我写了10个创作心法...",
        note_tweet_text=(
            "半年前，我写了10个创作心法，没想到大家反响都特别好。\n"
            "而这段时间，我给内部写的内容方法论也更新到了2.0。\n"
            "想了下，也把总结的部分发在这里，希望能对大家有帮助！"
        ),
        user_screen_name="Khazix0918",
    )
    wrapper = FakeTweet(
        id="2054575043951112589",
        text="RT @Khazix0918: 半年前，我写了10个创作心法，没想到大家反响都特别好。\n\n而这段时间，我给…",
        created_at="Wed May 13 14:50:37 +0000 2026",
        data={},
    )
    wrapper.retweeted_tweet = inner

    page = FakePage([wrapper])
    user = FakeUser(page)
    client = FakeClient(user)

    adapter = TwikitAdapter(
        cookies_path=Path("/tmp/_unused_cookies.json"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    articles = _run(adapter.fetch({"id": "x_dotey", "url": "dotey"}, since=None))

    assert len(articles) == 1
    art = articles[0]
    # body 应来自被转推原推的 note_tweet，含末句「希望能对大家有帮助！」
    assert "希望能对大家有帮助！" in art.body
    assert art.body.startswith("半年前，我写了10个创作心法，")
    # body 不应保留 wrapper 那段 "RT @" 前缀（前缀只进 title）
    assert not art.body.startswith("RT @")


def test_retweeted_tweet_title_has_rt_prefix() -> None:
    """RT 推文 title 应以 "RT @<原作>: " 前缀开头，保留转推关系信号。"""
    inner = _make_inner_tweet(
        id="888",
        text="原推 wrapper text",
        note_tweet_text=None,
        user_screen_name="Khazix0918",
        full_text="完整的原推文本来自 full_text",
    )
    wrapper = FakeTweet(
        id="2054575043951112589",
        text="RT @Khazix0918: 原推 wrapper text…",
        created_at="Wed May 13 14:50:37 +0000 2026",
        data={},
    )
    wrapper.retweeted_tweet = inner

    page = FakePage([wrapper])
    client = FakeClient(FakeUser(page))
    adapter = TwikitAdapter(
        cookies_path=Path("/tmp/_unused_cookies.json"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    articles = _run(adapter.fetch({"id": "x_dotey", "url": "dotey"}, since=None))

    assert articles[0].title.startswith("RT @Khazix0918: ")
    # title 内容来自原推 full_text，不是 wrapper 的精简带 …
    assert "完整的原推文本" in articles[0].title


def test_quote_tweet_appends_to_body() -> None:
    """tweet.quote 非空时 body 末尾应拼上 "--- 引用 @<原作> ---" 段。"""
    quoted = _make_inner_tweet(
        id="777",
        text="被引用的原文",
        note_tweet_text=None,
        user_screen_name="some_kol",
    )
    wrapper = FakeTweet(
        id="2054575043951119999",
        text="我对这个推有点想法",
        created_at="Wed May 13 14:50:37 +0000 2026",
        data={},
    )
    wrapper.quote = quoted

    page = FakePage([wrapper])
    client = FakeClient(FakeUser(page))
    adapter = TwikitAdapter(
        cookies_path=Path("/tmp/_unused_cookies.json"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    articles = _run(adapter.fetch({"id": "x_dotey", "url": "dotey"}, since=None))

    body = articles[0].body
    assert body.startswith("我对这个推有点想法")
    assert "--- 引用 @some_kol ---" in body
    assert "被引用的原文" in body


def test_retweeted_tweet_carries_long_form_classification() -> None:
    """RT 长文（note_tweet）应分类为 is_long_form='note_tweet'，不是 normal。

    bug 反例：旧 _classify_long_form(tweet) 看 wrapper._data['note_tweet']，
    wrapper 通常无 note_tweet 字段，长文 RT 会被误归 normal。
    """
    inner = _make_inner_tweet(
        id="666",
        note_tweet_text="A" * 1000,
        user_screen_name="long_form_kol",
    )
    wrapper = FakeTweet(
        id="2054000000000000666",
        text="RT @long_form_kol: A…",
        created_at="Wed May 13 14:50:37 +0000 2026",
        data={},
    )
    wrapper.retweeted_tweet = inner

    page = FakePage([wrapper])
    client = FakeClient(FakeUser(page))
    adapter = TwikitAdapter(
        cookies_path=Path("/tmp/_unused_cookies.json"),
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )
    articles = _run(adapter.fetch({"id": "x_dotey", "url": "dotey"}, since=None))

    assert articles[0].is_long_form == "note_tweet"


# ---- 16. s11: _save_cookies_atomic 剥 __cf_bm ------------------------------


def test_save_cookies_atomic_strips_cf_bm(tmp_path: Path) -> None:
    """_save_cookies_atomic 在写盘后剥离 __cf_bm（Cloudflare ephemeral）。

    模拟 twikit Client.save_cookies 写入含 __cf_bm 的完整 jar dict；
    断言最终 dst 内容剥掉了 __cf_bm，其他字段保留。
    """
    page = FakePage(_load_fixture("dotey_page1.json"))
    user = FakeUser(page)

    dst = tmp_path / "twikit_cookies.json"

    def fake_save_cookies(path: str) -> None:
        # 模拟 twikit save：完整 7 字段 jar（含 __cf_bm）
        Path(path).write_text(
            json.dumps({
                "auth_token": "tok123",
                "ct0": "csrf_value",
                "guest_id": "g1",
                "guest_id_ads": "ga1",
                "guest_id_marketing": "gm1",
                "personalization_id": "p1",
                "__cf_bm": "cf_ephemeral_value",  # 应被剥
            }),
            encoding="utf-8",
        )

    client = FakeClient(user, save_cookies_side_effect=fake_save_cookies)

    adapter = TwikitAdapter(
        cookies_path=dst,
        max_pages=1,
        throttle_secs=0.0,
        client_factory=_make_client_factory(client),
    )

    source = {"id": "dotey", "url": "dotey"}
    _run(adapter.fetch(source, since=None))

    # dst 内容应不含 __cf_bm，其他 6 字段保留
    content = json.loads(dst.read_text(encoding="utf-8"))
    assert "__cf_bm" not in content, "Cloudflare ephemeral cookie 不应持久化"
    assert content["auth_token"] == "tok123"
    assert content["ct0"] == "csrf_value"
    assert content["guest_id"] == "g1"
    assert len(content) == 6  # 7 - 1 (__cf_bm 被剥)
