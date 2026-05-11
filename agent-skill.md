---
name: news-collector
description: 使用 news-collector CLI 与 Python SDK 采集 AI 内容信源、读取 raw.db、二次加工成日报/摘要/筛选结果。涵盖信源管理（add / list / probe / test / edit / remove / seed）、日常运行（fetch / read / stats / clean）、故障处理（doctor / status / state / logs / restart）、关停（teardown）四类场景以及拿到数据后用 jq 或 SDK 做二次加工的写法。触发：用户提到「采集」「抓信源」「拉 RSS」「news-collector」「~/.news-collector/raw.db」「想看最近抓到的内容」「按昨天的采集结果写日报」「某个信源加进来」「信源没拉到东西排查一下」「docker-compose.yml 不存在」等场景，即使没明说工具名也应触发。不适用于：搭建 RSS 阅读器/订阅客户端、单篇博客网页转 markdown、通用内容阅读器——这些是别的工具的活。
---

# news-collector — 工具说明书

news-collector 是「采集层」CLI + SDK：从 RSS / 网页采集 AI 信源 → 落进 `~/.news-collector/raw.db`（业务投影是 `articles_raw` 表；另有 `source_state` 表记每个信源的最近抓取/失败状态，你通过 `state` 命令读，不需要自己直查）。它**只采集**，**不做**内容筛选 / 打分 / 摘要 / 日报（这些是消费方/agent 自己干）。

你（agent）通常做两类事：
1. **运维这个工具**：加信源、跑抓取、排故障
2. **消费它的产出**：通过 CLI/SDK 拿到文章 → 用 LLM 二次加工

下面 5 类场景按需查。命令都支持 `--help` 看完整参数。

---

## 数据怎么进、怎么出

```
上游（RSS / 网页 / RSSHub 转译的 X / Reddit）
    │  news-collector fetch
    ▼
~/.news-collector/raw.db （articles_raw 表）
    │  CLI: news-collector read --json   ← 你查数据走这里
    │  SDK: news_collector.sdk.read_raw() ← 在 Python 里走这里
    ▼
你的二次加工（LLM 摘要 / 筛选 / 写日报）
```

**重要**：**不要**直接 `sqlite3 ~/.news-collector/raw.db "SELECT ..."` 写 SQL —— 表结构会演进（已删过死字段、未来还会改），直接查 raw.db 的代码会随之坏。统一走 `read --json`（shell 环境）或 `sdk.read_raw()`（Python 环境），这两条是稳定契约。

---

## 场景 1：信源管理

加 / 看 / 改 / 删信源。信源清单存在 `~/.news-collector/sources.yaml`，CLI 自动管理，不要手编 yaml。

### 加信源
```bash
# 智能录入：探测 url → 提示建议 → 交互确认（有 TTY 时）
news-collector sources add https://example.com/blog

# 非交互式（你最常用这个）
news-collector sources add https://example.com/blog \
  --tier=kol \
  --domain=ai \
  --id=example_blog

# probe 判不准 / url 不可达但你知道类型时，显式指定
news-collector sources add https://example.com/blog --type=rss --tier=kol --domain=ai --id=example_blog

# 批量
news-collector sources add --from-file=urls.txt
```

参数说明：
- `--tier`: `official_first_party` / `kol` / `secondary`（信源权重，影响消费方打分）
- `--domain`: `ai` 是默认；未来可能 `finance` 等（多领域设计）
- `--id`: 信源唯一 id，建议小写下划线（如 `simon_willison_blog`）
- `--type`: `rss` / `web`（一般不传，靠 probe 自动判定；探测不准时手动覆盖）

### 录入前先侦察
```bash
# 单 url 探测：可达性 / type / 建议 id / 样本标题
news-collector sources probe https://example.com/blog

# 批量探测（不写 yaml，只回报）
news-collector sources probe --from-file=urls.txt
```

侦察用来判断「这个 url 能不能采、采到啥」，再决定要不要 add。

### 看信源
```bash
news-collector sources list                          # 全部
news-collector sources list --enabled-only           # 只看启用的
news-collector sources list --type=web --tier=kol    # 多维过滤
news-collector sources show <id>                     # 单条完整配置
news-collector sources export --out=backup.yaml      # 备份（字节级保真）
```

### 改 / 删
```bash
news-collector sources edit <id> --tier=official_first_party
news-collector sources rename <old_id> <new_id>
news-collector sources disable <id>                  # 临停（保留配置）
news-collector sources enable <id>                   # 恢复
news-collector sources remove <id> --yes             # 彻底删（--yes 非交互必须加）
news-collector sources test <id>                     # 试拉一次（不入库），看能不能抓到
```

### 兜底：清单丢失时重铺种子
```bash
news-collector sources seed                          # 拷贝项目种子清单到 ~/.news-collector/sources.yaml
news-collector sources seed --force                  # 覆盖已存在的清单（慎用）
```
`sources.yaml` 不在或被你清空时用；正常路径不需要。

---

## 场景 2：日常运行

### 抓数据
```bash
# 默认拉所有启用信源 24h 内更新
news-collector fetch

# 拉指定时间窗口
news-collector fetch --since=7d
news-collector fetch --since=24h

# 单类型 / 单信源（统一走 --source，CLI 自动判断传的是 type 还是 id）
news-collector fetch --source=rss
news-collector fetch --source=anthropic_news

# 调并发（rss 桶默认 8 并发，web 桶串行）
news-collector fetch --concurrency=4
```

注意：信源更新频率天然不固定（Anthropic 一周 1-2 次很正常），`--since=24h` 看不到新内容 ≠ 出问题，把窗口拉到 `7d` 或 `30d` 再判断。

### 看数据
```bash
# 默认 rich Table 输出，看 24h 内
news-collector read

# JSON 模式（你最常用，便于 jq / Python 解析）
news-collector read --since=24h --json

# 多维过滤
news-collector read --since=7d --source-types=rss --domain=ai --tier=official_first_party --json
news-collector read --since=7d --source-id=anthropic_news --json
news-collector read --since=24h --limit=20 --json
news-collector read --since=24h --limit=0 --json    # 0 = 无限制
```

`read` 字段：`id` / `source_type` / `source_id` / `source_tier` / `external_id` / `url` / `title` / `body` / `published_at` / `fetched_at` / `domain_tags` / `content_hash`。

### 看统计
```bash
news-collector stats                # 4 块 panel：总数 / Top-N 信源 / 近 7 天 ASCII 柱图 / type×domain
news-collector stats --top=20
news-collector stats --json
```

### 清旧数据
```bash
news-collector clean --before=30d              # dry-run：只报会删多少（默认）
news-collector clean --before=30d --yes        # 真删
news-collector clean --before=30d --yes --no-vacuum   # 真删但跳过 VACUUM（删完磁盘不回收）
```
默认 dry-run 是为了防误删；想真动手必须显式 `--yes`。删完默认自动 `VACUUM` 回收磁盘。

---

## 场景 3：拿到数据后二次加工

**核心场景**：你抓到一批文章后要让 LLM 摘要 / 筛选 / 写日报，怎么用最稳？

### 路径 A：shell + jq（agent 在 bash 里跑）

```bash
# 拿 7d 内所有 ai-domain 的官方一手内容，提取 title + url
news-collector read --since=7d --domain=ai --tier=official_first_party --json \
  | jq -r '.title + "\t" + .url'

# 拿某信源的 title 列表给 LLM 输入
news-collector read --since=7d --source-id=anthropic_news --json \
  | jq -r '.title'

# 拿完整记录写进文件给后续脚本读
news-collector read --since=24h --json > today.ndjson
```

输出是 NDJSON（每行一个 JSON 对象），不是 JSON 数组，所以用 `jq` 不需要 `.[]`。

### 路径 B：Python SDK（你在 Python 环境里）

```python
from datetime import datetime, timedelta, timezone
from news_collector import sdk

# 流式游标，不一次性加载内存
since = datetime.now(timezone.utc) - timedelta(days=7)

for art in sdk.read_raw(domain="ai", since=since, source_types=["rss"]):
    print(art.source_id, art.title, art.url)
    # art.title / art.body / art.published_at / art.fetched_at / art.content_hash ...
```

`read_raw()` 签名：
```python
read_raw(
    domain: str = "ai",
    since: datetime | None = None,
    source_types: list[str] | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> Iterator[ArticleRaw]
```

按 `(fetched_at ASC, id ASC)` 排序输出。`db_path` 默认 `~/.news-collector/raw.db`。

### 何时用哪种？

- **写日报 / 一次性筛选** → shell + jq 简单粗暴（一次性 `read --json` 子进程开销可忽略）
- **批量处理几千篇文章给 LLM 的长流水线** → Python SDK 流式游标（避免在循环里反复起 CLI 子进程，每次都重新启 Python 解释器 + 重新打开 sqlite）

### content_hash 字段的用法

不同 url 但同内容（搬运 / 转载）有同样的 `content_hash`（基于 title + 正文前 500 字 sha256）。你可以：
- 按 `content_hash` 分组识别热门转载内容（"被 N 家媒体转发"作为热度信号）
- 按 `content_hash` 去重（消费方自己决定，collector 不替你去重）

---

## 场景 4：故障处理

### 系统不对劲先跑 doctor
```bash
news-collector doctor
```
全面诊断：Docker 起没？X token 填了？数据库通吗？随机抽样信源能否抓到？

### 看运行状态
```bash
news-collector status            # 容器健康 / 上次抓取时间 / 库里多少条 / 最近失败信源
news-collector state             # 列每个信源最近抓取情况、连续失败次数
news-collector logs --tail=50    # 最近日志
```

### 重启 RSSHub
```bash
news-collector restart           # X token 换了、容器抽风时用
```

### 常见报错

**`docker-compose.yml 不存在`** —— v0.5.1 起 compose 文件存在 home 目录，老版本升级会撞这条。修：
```bash
news-collector setup             # 自动补齐 compose 文件 + 启容器（幂等）
```

**某信源 `fetched=0`** —— 不一定是 bug。按顺序判断，不要先下 adapter bug 结论：
1. `news-collector sources test <id> --limit=5` 试拉一次（不入库），看能不能抓到内容
2. `news-collector state` 看连续失败次数；也可只看一类：`news-collector state --source-type=rss`
3. 信源更新频率天然不固定，把窗口拉大单独跑：`news-collector fetch --source=<id> --since=30d`
4. 抓到内容但入库 0 条 → 多半是去重命中（同一信源相同 external_id 不重复入）
5. 扩大窗口仍异常 → 看 `news-collector logs --tail=100` 与 adapter 真实样本，再下 bug 结论（不要凭"页面看着有内容"就判 adapter 错）

**X / Twitter 信源失败** —— `~/.news-collector/.env` 里 `TWITTER_AUTH_TOKEN` 失效，更新后跑 `restart`。

---

## 场景 5：装机与关停

### 首次装机
```bash
pipx install raw-news-collector    # 或 uv tool install raw-news-collector
news-collector setup               # 一步完成：建目录 + 启 RSSHub/Redis + 初始化库 + 铺信源 + 引导 X token
```

### 关停
```bash
news-collector teardown            # 停容器，数据永远保留（不提供 --purge）
```

数据保留是设计取舍：误删的恢复成本远高于"占点磁盘"。重装时 `setup` 自动接续。

---

## 配置

```bash
news-collector config init         # 在 ~/.news-collector/config.yaml 写默认配置
news-collector config show         # 看当前生效配置
```

`~/.news-collector/` 是运行时数据目录（不在项目里）：
- `sources.yaml` — 信源清单
- `config.yaml` — CLI / 抓取参数配置
- `.env` — `TWITTER_AUTH_TOKEN` 等密钥
- `raw.db` — SQLite 采集库
- `docker-compose.yml` — RSSHub + Redis 容器配置（v0.5.1 起）
- `logs/news-collector.log` — 日志

---

## 给 agent 的几条经验法则

1. **看到 `--help` 优先用** —— 命令参数会演进，`--help` 是单一真相源
2. **机器读输出永远加 `--json`** —— rich Table 是给人看的，正则切割会脆
3. **`--since` 默认 24h，但信源更新频率不一定每天有更新** —— 找不到内容前先把窗口拉大再判断
4. **批量加信源用 `--from-file=`** —— 别在循环里调 `add`，CLI 启动开销叠加
5. **要做内容判断 / 写日报 / 摘要** —— 那是你的活，collector 只给你原料
