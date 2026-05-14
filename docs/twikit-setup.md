# twikit 接入指南

X (Twitter) 信源的抓取链路。**v1.1.0 起 X 信源从 RSSHub 迁出到 twikit**（s10-x-twikit-migration sprint），因为 RSSHub 默认 `UserTweets` GraphQL endpoint 对 reply / quote-tweet / self-thread 类条目存在整体截断，机制问题换 token 无解；twikit 通过浏览器 cookie 直连 X GraphQL，三账号实测覆盖率 87–100% 远胜 RSSHub 的 2.9–13%。

Reddit / Anthropic / OpenAI status / 其他 RSSHub 路由仍走 RSSHub 容器（见 [rsshub-setup.md](./rsshub-setup.md)）；本文只覆盖 X 信源。

---

## 1. Cookie 获取步骤（用户首次必做）

newsbox 要求 `~/.newsbox/twikit_cookies.json` 含 `auth_token` 与 `ct0` 两个字段。`newsbox setup` 已生成 `twikit_cookies.example.json` 模板，你需要：

1. **登录 X 小号**：浏览器（建议 Chrome）打开 https://x.com 登录账号
   - 建议**不要用主号**：twikit 直连 X GraphQL，频繁请求可能触发风控，牺牲一个小号更安全
   - 小号建议已开 2FA + 验证邮箱
2. **打开 devtools 复制 cookies**：F12 → Application → Cookies → `https://x.com`
   - 找到 `auth_token`（约 40 字符 hex 串）
   - 找到 `ct0`（约 160 字符 hex 串）
3. **写入 cookies 文件**：
   ```bash
   cat > ~/.newsbox/twikit_cookies.json << 'EOF'
   {
     "auth_token": "<paste auth_token here>",
     "ct0": "<paste ct0 here>"
   }
   EOF
   ```
4. **验证**：`newsbox doctor` 看 `[Twikit]` panel 全 OK

> 想用 example 文件模板？`cp ~/.newsbox/twikit_cookies.example.json ~/.newsbox/twikit_cookies.json` 然后编辑填入实际值。

---

## 2. cookies.json 格式与生命周期

**首次手填**：
```json
{
  "auth_token": "<40 char hex>",
  "ct0": "<160 char hex>"
}
```

**程序首次跑完自动膨胀**（D-auth-1）：
```json
{
  "auth_token": "<40 char hex>",
  "ct0": "<可能 rotate 后的新值>",
  "guest_id": "...",
  "guest_id_ads": "...",
  "guest_id_marketing": "...",
  "personalization_id": "..."
}
```

文件膨胀后**用户无需再碰**——`ct0` 由 X 按自己节奏 rotate，twikit adapter 自动 keep latest 并原子写回（`os.replace` POSIX 原子）。

**Cloudflare `__cf_bm` 不持久化**（s11-twikit-cookieconflict-hotfix）：X 通过 Cloudflare 会下发 `__cf_bm`（30 分钟 TTL 的 bot-management ephemeral cookie），twikit adapter 在 `_save_cookies_atomic` 中主动剥离该字段。如果持久化，下次 `load_cookies` 装入 jar (domain='') 再叠加预热请求 Set-Cookie 下发的 domain='.x.com' 版本，就会形成同名跨 domain 的 jar 状态；twikit 内部 `client.get_cookies()` 走 `dict(self.http.cookies)` 命中 httpx `CookieConflict` 报错。详见 §3 排查表对应行。

---

## 3. 失败排查

`newsbox doctor` 的 `[Twikit]` panel 会区分以下文案，对照修复：

| 文案 | 原因 | 修复 |
|---|---|---|
| `twikit cookies 文件不存在` | 首次未配置 | 按 §1 写入文件 |
| `缺少 'auth_token' 字段（或仍是 <placeholder>）` | 没填实际值或 example 直接当 real 用 | 重新从浏览器复制 auth_token 填入 |
| `缺少 'ct0' 字段（或仍是 <placeholder>）` | 同上 | 重新从浏览器复制 ct0 填入 |
| `不是合法 JSON` | 手编 yaml/json 出错 | 检查引号 / 逗号 / 大括号 |

`newsbox fetch --source=x_<handle>` 的运行时错误：

| 错误 | 含义 | 应对 |
|---|---|---|
| `TwikitAuthError: auth_token 失效 / 401` | auth_token 一般数月才失效 1 次；可能账号被封 | 从浏览器重新复制 auth_token；多次失败考虑换小号 |
| `TwikitRateLimitError: 429` | X 限流（一般几小时恢复） | 等几小时再跑 / 降低 fetch 频率 |
| `TwikitUserUnavailableError: UserNotFound` | 目标账号不存在 / 被封 / 被你拉黑 | 检查 sources.yaml 的 url 字段拼写 |
| `ConnectTimeout` | 偶发网络抖动 | 重试 1–2 次即过；持续超时检查本机网络 |
| `CookieConflict('Multiple cookies exist with name=__cf_bm')` | s10 残留缺陷，s11 已修：`__cf_bm` 同名跨 domain 持久化撞冲突 | 升级到含 s11 patch 的版本；同时一次性清理 `~/.newsbox/twikit_cookies.json` 中的 `__cf_bm` 字段（用 `python3 -c "import json,sys; p='...'; d=json.load(open(p)); d.pop('__cf_bm',None); json.dump(d, open(p,'w'))"` 一行清掉） |

---

## 4. ct0 自动 rotation 工作原理（D-auth-1）

为什么用户不用手动刷新 ct0：

1. 启动 fetch：adapter 读 `twikit_cookies.json` → `client.load_cookies()`
2. 应用 4 处 monkey-patch（见 §6）：含 `keep_latest_ct0` 实例 patch
3. 预热请求：发一次 cheap GraphQL（`UserByScreenName("x")`）触发 X 下发 Set-Cookie
4. 如果 X 下发新 ct0：`keep_latest_ct0` patch 让新值覆盖旧值（twikit 原生 `_remove_duplicate_ct0_cookie` 反向行为，保留**第一个**而非最新；该 patch 翻转）
5. fetch 跑完：`client.save_cookies()` 用 `atomic_write_json` 原子写回完整 jar（含新 ct0）
6. 下次启动：load 拿到的就是 fresh ct0

**长期不跑 fetch（数周）后再启动**：旧 ct0 已失效；预热请求用 `auth_token` 重新拿 fresh ct0 → 仍可用。**只有 `auth_token` 失效时**（一般数月一次）才需要用户介入。

实测观察：单次会话内连续 2–3 次 fetch ct0 可能不变（X 复用同一值），数小时或天级后通常 rotate。

---

## 5. twikit 升级流程（newsbox 维护者视角）

D-dep-1 决策：`twikit==2.3.3` 严格 pin。升级由 newsbox 维护者主动驱动，因为 twikit ↔ X GraphQL schema 存在 4 处 monkey-patch，任一上游版本变化都可能让 patch 失配。

升级流程（维护者跑）：

1. **监控**：定期查 [twikit GitHub Releases](https://github.com/d60/twikit/releases)
2. **试装**：`uv add twikit==<new>` + `uv sync`
3. **测试**：`uv run pytest`（517 + 用例必须全过）
4. **抽样实跑**：`newsbox fetch --source=x_dotey --since=24h` 看是否真能抓到
5. **patch 适配**：若 patch 目标已不存在或行为变化（`apply_patches` 内部有 `getattr` 防御性 warn），改 `src/newsbox/adapters/_twikit_patches.py`
6. **bump pin**：改 `pyproject.toml` 的 `twikit==<new>`
7. **release**：按 PROJECT.md「发布架构」§同步流程跑完 13 步
8. **用户拿到**：`uv tool install -U newsbox` 自动随 newsbox 升级 twikit

用户**不要**自己装 twikit；pipx / uv tool install 把整棵依赖树关进隔离 venv，用户与 newsbox 维护者管控的 twikit 版本完全同步。

---

## 6. 风险说明：4 处 monkey-patch 是 X 反爬不可避免成本

twikit 2.3.3 与当前 X GraphQL schema 之间需要 4 处 monkey-patch 才能稳定跑通（全部抽离在 `src/newsbox/adapters/_twikit_patches.py`）：

| # | patch 目标 | 原因 |
|---|---|---|
| 1 | `ClientTransaction.init` → no-op | X 改了 ondemand JS，KEY_BYTE 索引 regex 失效 |
| 2 | `ClientTransaction.generate_transaction_id` → 返回空串 | 同上，跳过 transaction id 生成 |
| 3 | `twikit.user.User.__init__` 补 28 个 legacy 字段 default | 防 X 响应缺字段时 KeyError |
| 4 | `Client._remove_duplicate_ct0_cookie` → "keep latest ct0" | 翻转原生行为（原版保留第一个 ct0，丢弃 X 新下发的） |

**为什么不 fork twikit**：4 处 patch 集中一个文件，维护面小；fork 要承担"长期跟进上游 + 自己测试"全套成本，远高于"monkey-patch + 严格 pin + 维护者驱动升级"。

**为什么不 vendor twikit 源码**（D-dep-2）：同上理由 + 不违反 CLAUDE.md "绝不 fork RSSHub 长期维护" 边界原则。

**用户视角的风险**：
- twikit 上游崩了 → newsbox 维护者会评估是否切到 [twitterapi.io](https://twitterapi.io/) 付费链路（已在 x-get 项目调研，覆盖率 87–99%，$0.00015/条）
- X 全面封 cookie-based 抓取（理论可能，未发生） → 同上 fallback 路径
- monkey-patch 维护成本变高（升级频繁挂） → 切到范围 pin + CI 自动兼容测试（已列入 ROADMAP）

---

## 附：与 RSSHub 路径对比

| | RSSHub UserTweets（v1.0.x） | twikit（v1.1.0+） |
|---|---|---|
| 鉴权 | 单一 X auth_token（共享给 RSSHub 容器） | 浏览器 cookies（auth_token + ct0） |
| 覆盖率 | 2.9–13%（漏抓 RT / reply / quote / thread） | 87–100% |
| 维护成本 | RSSHub 路由由社区维护 | 4 处 monkey-patch 由 newsbox 维护者维护 |
| 限速 | RSSHub 自带 cache + rate limit | adapter 串行 + page-level 2s sleep |
| 失败模式 | RSSHub 容器 / 上游 X / token 失效 | cookies 失效 / 风控 429 / monkey-patch 漂移 |
| 升级路径 | `docker pull` 拉 RSSHub 新镜像 | `uv tool install -U newsbox` 由维护者驱动 |

X 信源不再用 RSSHub；其他平台（Reddit / Anthropic / OpenAI status / etc.）继续走 RSSHub。
