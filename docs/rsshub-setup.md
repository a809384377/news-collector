# RSSHub 接入指南

news-collector 的 X / Reddit 等私有 API 信源接入由 RSSHub（社区维护，github.com/DIYgod/RSSHub）外包处理。本文档说明如何在本机部署 RSSHub + Redis 容器并配置 X 的 auth_token。

---

## 1. 前置条件

- macOS / Linux 已装 Docker Desktop（或 Docker Engine）
- 一个 X (Twitter) **小号**（不要用主号——RSSHub 频繁请求可能触发安全验证，牺牲一个小号）
  - 建议小号已开 2FA + 验证邮箱
- Chrome 浏览器（用于从 DevTools 提取 cookie）

---

## 2. 容器启动

```bash
cd ~/Desktop/tools/news-collector

# 首次：准备数据目录 + .env
mkdir -p ~/.news-collector/rsshub/redis-data
cp .env.example ~/.news-collector/.env

# 编辑 ~/.news-collector/.env 填入 TWITTER_AUTH_TOKEN（步骤见 §3）
# ...

# 启动容器
docker compose up -d
docker compose ps         # 应显示两容器 Up 状态
docker compose logs -f rsshub   # 查看 RSSHub 启动日志
```

预期：两容器 `news-collector-rsshub` / `news-collector-redis` 健康运行。

---

## 3. 提取 X auth_token（5 步）

### 3.1 准备

在 Chrome 隐身/普通窗口登录小号到 [x.com](https://x.com)。建议小号已开 2FA + 验证邮箱（防限制）。

### 3.2 提取 cookie

```
1. 打开 x.com 并保持登录
2. 按 F12（或右键 → 检查）打开 DevTools
3. 切到 "Application" 标签页（Storage 区）
4. 左侧 Cookies → 展开 → 点击 https://x.com
5. 在 cookie 列表里找 Name = "auth_token" 的行，复制 Value 列内容（约 40 位 hex 字符串）
```

### 3.3 关键提醒

- `auth_token` 是 **httpOnly cookie**，**只能从 DevTools 的 Application/Storage 看**，不能在 JS console 用 `document.cookie` 读到
- 不要复制 `ct0`（CSRF token）—— RSSHub 会自己用 auth_token 重新请求 x.com 拿 ct0
- token 有效期约数月到数年；过期会触发 RSSHub 401 → 需重新登录小号取新 token
- **绝不**把 token 提交到 git（.env 已在 .gitignore；写入 `~/.news-collector/.env`）

### 3.4 写入 .env

编辑 `~/.news-collector/.env`：

```
TWITTER_AUTH_TOKEN=<刚复制的 40 位 hex>
```

多个 token 用逗号分隔可轮询（更稳定，建议）：

```
TWITTER_AUTH_TOKEN=token1,token2,token3
```

### 3.5 重启容器使 token 生效

```bash
cd ~/Desktop/tools/news-collector
docker compose restart rsshub
```

---

## 4. 验证

### 4.1 容器健康

```bash
curl http://localhost:1200/   # 应返回 RSSHub 主页 HTML
```

### 4.2 X 路由

```bash
# 单 KOL 时间线（应返回 atom，含 ≥ 5 条 entry）
curl 'http://localhost:1200/twitter/user/karpathy?format=atom' | head -80

# OpenAI 官方账号（24h 内通常有多条新推；按 KNOWLEDGE-LOG #24，
# 不要满足于"有 1 条"——应核对数量级）
curl 'http://localhost:1200/twitter/user/openai?format=atom' | head -80
```

如果返回 401 / 403 / 502：

- 检查 `~/.news-collector/.env` 中 `TWITTER_AUTH_TOKEN` 是否正确（无空格、无引号）
- `docker compose logs rsshub | tail -50` 看是否有 cookie 校验错误
- 重启容器：`docker compose restart rsshub`

### 4.3 Reddit 路由

⚠️ RSSHub 不内置 Reddit 路由（D3 决策：news-collector 直接用 Reddit 原生 `.rss`）。本步只验证 X。

Reddit 验证示例（不经 RSSHub）：

```bash
curl 'https://www.reddit.com/r/LocalLLaMA/.rss' | head -40
```

---

## 5. 关闭容器

```bash
docker compose down       # 关闭但保留数据卷
docker compose down -v    # 关闭并删除 redis 数据卷（慎用）
```

---

## 6. 常见问题

| 问题 | 排查 |
|---|---|
| curl 返 502 Bad Gateway | RSSHub 启动中（等 30s）或 token 无效；查 `docker compose logs rsshub` |
| entry 数量异常少 / 全空 | X 账号本身可能限流；试另一个账号 / 等几小时；KNOWLEDGE-LOG #24 提醒：不要假设"少"就是正常 |
| 容器频繁 OOM | Docker Desktop 加内存（推荐 ≥ 2GB） |
| token 过期 | 重新登录小号 → 重新提取 auth_token → 写入 .env → 重启容器 |
| 拉某 KOL 提示 "User suspended" | 该账号已被 X 封禁；确认信源清单内是否需要剔除 |

---

## 7. 引用

- [RSSHub 官方文档 — Twitter 路由](https://docs.rsshub.app/routes/social-media#twitter)
- [Twitter Cookie 配置讨论 — RSSHub Discussion #16746](https://github.com/DIYgod/RSSHub/discussions/16746)
- [Export your X authentication cookies — Readybot.io](https://readybot.io/help/how-to/find-x-twitter-authentication-token)
