"""集中 twikit 2.3.3 的 runtime monkey-patch 与 cookies rotation helper。

twikit 2.3.3 与当前 X GraphQL schema 存在 4 处不兼容，必须打 patch 才能稳定跑通：

  1. ``ClientTransaction.init`` → no-op
     X 改了 ondemand JS 结构，twikit 解析 KEY_BYTE 的 regex 失效；
     许多 endpoint 不强求真实 ``X-Client-Transaction-Id`` 头，直接跳过即可
  2. ``ClientTransaction.generate_transaction_id`` → 返回空串
     与 patch 1 配套（init 跳过后 generate 也无源数据可算）
  3. ``twikit.user.User.__init__`` → 补 28 个 legacy 字段 default
     twikit 2.3.3 直接索引很多 legacy 字段（如 ``legacy['entities']['description']['urls']``），
     X 已 strip 部分字段；不兜底会 KeyError
  4. ``Client._remove_duplicate_ct0_cookie`` → "keep latest ct0"
     默认逻辑保留磁盘加载的 stale ct0、丢弃 X Set-Cookie 下发的新 ct0；
     反转为 "last write wins" 让 ct0 自动 rotation 真正生效

patch 1/2/3 是 module-level，在 ``apply_patches()`` 中幂等应用（首次后任意次
调用都安全）；patch 4 需要 client 实例上闭包绑定，由 adapter 创建 client 后调
``patch_keep_latest_ct0(client)`` 安装。

twikit 升级时必须在本文件适配；若 patch 目标已不存在，``apply_patches`` 内部
``hasattr`` 防御性检查会 warn 但不崩溃 —— 让维护者注意到 schema 漂移。

来源：x-get 项目实测过的 patches（``src/x_get/adapters/twikit_adapter.py:260-337``）。
依赖：``pyproject.toml`` 严格 pin ``twikit==2.3.3`` —— 见 DECISIONS.md D5。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

_APPLIED = False


def apply_patches() -> None:
    """幂等应用 module-level patch（1/2/3）；多次调用安全。

    Raises:
        RuntimeError: twikit 未安装或上游 schema 变化致核心模块路径失效
            （提示用户检查 pyproject.toml pin + 跑 uv sync）。
    """
    global _APPLIED
    if _APPLIED:
        return

    try:
        from twikit.x_client_transaction import ClientTransaction  # type: ignore
        import twikit.user as _twikit_user  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "twikit 库不可用：检查 pyproject.toml 是否已 pin twikit==2.3.3 "
            f"且 uv sync 完成（详见 docs/twikit-setup.md）。原因：{exc}"
        ) from exc

    # ---- patch 1 + 2: ClientTransaction.init / generate_transaction_id ----

    async def _patched_init(self_ct: Any, session: Any, headers: Any) -> None:
        # truthy sentinel：twikit 内部检查 home_page_response 决定是否需重新 init；
        # 设 True 让后续调用直接跳过 (KEY_BYTE 解析失败的) re-init 流程
        self_ct.home_page_response = True

    def _patched_generate(self_ct: Any, *args: Any, **kwargs: Any) -> str:
        return ""

    if hasattr(ClientTransaction, "init"):
        ClientTransaction.init = _patched_init  # type: ignore[assignment]
    else:
        logger.warning(
            "twikit patch: ClientTransaction.init 不存在；twikit 上游可能已变更 schema"
        )
    if hasattr(ClientTransaction, "generate_transaction_id"):
        ClientTransaction.generate_transaction_id = _patched_generate  # type: ignore[assignment]
    else:
        logger.warning(
            "twikit patch: ClientTransaction.generate_transaction_id 不存在；twikit 上游可能已变更 schema"
        )

    # ---- patch 3: User.__init__ 补 28 个 legacy 字段 default ----

    if not getattr(_twikit_user.User.__init__, "_newsbox_patched", False):
        _orig_user_init = _twikit_user.User.__init__

        def _safe_user_init(self_user: Any, client: Any, data: Any) -> None:
            legacy = data.setdefault("legacy", {})
            entities = legacy.setdefault("entities", {})
            entities.setdefault("description", {}).setdefault("urls", [])
            entities.setdefault("url", {}).setdefault("urls", [])
            for k, default in (
                ("profile_banner_url", None),
                ("pinned_tweet_ids_str", []),
                ("possibly_sensitive", False),
                ("can_dm", False),
                ("can_media_tag", False),
                ("want_retweets", False),
                ("default_profile", False),
                ("default_profile_image", False),
                ("has_custom_timelines", False),
                ("fast_followers_count", 0),
                ("normal_followers_count", 0),
                ("media_count", 0),
                ("is_translator", False),
                ("translator_type", "none"),
                ("withheld_in_countries", []),
                ("verified", False),
                ("url", None),
                ("profile_image_url_https", ""),
                ("location", ""),
                ("description", ""),
                ("listed_count", 0),
                ("favourites_count", 0),
                ("friends_count", 0),
                ("followers_count", 0),
                ("statuses_count", 0),
                ("created_at", ""),
                ("name", ""),
                ("screen_name", ""),
            ):
                legacy.setdefault(k, default)
            data.setdefault("is_blue_verified", False)
            data.setdefault("rest_id", "")
            _orig_user_init(self_user, client, data)

        _safe_user_init._newsbox_patched = True  # type: ignore[attr-defined]
        _twikit_user.User.__init__ = _safe_user_init  # type: ignore[method-assign]

    _APPLIED = True
    logger.debug(
        "twikit module-level patches applied (ClientTransaction.init / "
        "generate_transaction_id / User.__init__)"
    )


def patch_keep_latest_ct0(client: Any) -> None:
    """把 ``client._remove_duplicate_ct0_cookie`` 替换为 "last write wins" 策略。

    默认 twikit 把磁盘加载的旧 ct0 保留、丢弃 X Set-Cookie 下发的新 ct0；本
    patch 反转语义（保留最新值），让 ct0 自动 rotation 真正生效（D7 决策依据）。

    必须在 ``Client(...)`` 实例化后**立即**调用（一次性绑定）；后续每次请求
    twikit 内部触发 cookie 去重时都走这个新逻辑。
    """
    def _keep_latest_ct0() -> None:
        seen: dict[str, str] = {}
        for cookie in client.http.cookies.jar:
            seen[cookie.name] = cookie.value  # last write wins
        client.http.cookies = list(seen.items())

    client._remove_duplicate_ct0_cookie = _keep_latest_ct0  # type: ignore[method-assign]


def patch_safe_get_cookies(client: Any) -> None:
    """把 ``client.get_cookies`` 替换为「jar 按 name dedup → 扁平 dict」实现。

    修 s10 遗留的 CookieConflict 系统性缺陷（s11-twikit-cookieconflict-hotfix）：
    twikit 默认实现是 ``return dict(self.http.cookies)``，触发 httpx
    ``Cookies.__getitem__`` → ``Cookies.get(name)`` 不带 domain，jar 内同名
    跨 domain 时抛 ``CookieConflict('Multiple cookies exist with name=...')``。

    触发场景：``__cf_bm`` 等 Cloudflare ephemeral cookie 被 ``load_cookies``
    带入 jar (domain='')，预热请求触发 X Set-Cookie 又下发一个 domain='.x.com'
    版本，jar 同名两条。twikit ``_base_request`` 每请求一次都会调
    ``self.get_cookies().copy()`` 做 backup（client.py:148），直接 BOOM。

    本 patch 在源头收口：把 ``client.get_cookies`` 改为直接遍历 jar、
    last-write-wins 去重。副作用：``client.save_cookies(path)`` 内部
    ``json.dump(self.get_cookies(), f)`` 也跟着拿到 deduped dict，写出的
    json 不会有同名重复。

    必须在 ``Client(...)`` 实例化后**立即**调用（与 ``patch_keep_latest_ct0``
    同位置绑定）。
    """
    def _safe_get_cookies() -> dict[str, str]:
        seen: dict[str, str] = {}
        for cookie in client.http.cookies.jar:
            seen[cookie.name] = cookie.value
        return seen

    client.get_cookies = _safe_get_cookies  # type: ignore[method-assign]
