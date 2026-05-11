# news-collector SDK 使用指南

采集层 `news-collector` 把上游信源（RSS / 网页）流式抓取并落到本地共享 SQLite
文件 `~/.news-collector/raw.db`。下游消费方（如 `news-radar-ai`、未来的
finance / crypto 工具）通过本 SDK 只读消费，不直接接触 DB schema。

> 决策来源：sprint `s2-1-collector-extract` DECISIONS.md D2 —— 选择
> "Python SDK + 共享 SQLite 文件"，避免 HTTP 服务的运维成本，同时让 SDK
> 隐藏 DB schema 细节，未来切换存储引擎不影响消费方。

---

## 安装

本地 editable 安装（一期推荐）：

```bash
cd ~/Desktop/tools/<your-consumer>
uv pip install -e ../news-collector
```

未来可能发布到 PyPI，那时改为：`uv add news-collector`。

---

## 最小消费示例

3 行代码读取最近 24h AI 领域内容并打印标题：

```python
from news_collector.sdk import read_raw
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(hours=24)
for art in read_raw(domain="ai", since=since):
    print(art.published_at, art.title)
```

---

## 数据类：`ArticleRaw`

`@dataclass(frozen=True, slots=True)`，12 个业务字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | `int` | DB 自增主键，单调递增 |
| `source_type` | `str` | `rss` / `web` |
| `source_id` | `str` | `sources.yaml` 中条目的 id（如 `karpathy`、`anthropic_blog`） |
| `source_tier` | `str` | `official_first_party` / `kol` / `secondary` |
| `external_id` | `str` | 信源给的唯一 ID（feed entry.id / status id 等） |
| `url` | `str` | 原文链接 |
| `title` | `str` | 标题 |
| `body` | `str` | 正文（无长度上限；长文如 X Article 可达数万字） |
| `content_hash` | `str` | 内容指纹：`sha256(title + body[:500])` 输出的 64 字符 hex。消费方可用作"同内容多源转发"的去重键或热度聚合维度。**算法稳定不变**。采集层自身不按此去重（原料型路线，决策见产品对齐文档 §5 / §9-D8）。 |
| `published_at` | `datetime \| None` | 信源未提供时为 `None`，可回落 `fetched_at` |
| `fetched_at` | `datetime` | 采集时刻；`since` 参数即按此字段过滤 |
| `domain_tags` | `list[str]` | 领域标签（一期均为 `["ai"]`，未来支持多领域） |

---

## API：`read_raw`

```python
def read_raw(
    domain: str = "ai",
    since: datetime | None = None,
    source_types: list[str] | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> Iterator[ArticleRaw]:
```

| 参数 | 说明 |
|------|------|
| `domain` | 领域过滤；任一 `domain_tag` 命中即匹配。一期数据都是 `["ai"]`。 |
| `since` | 仅返回 `fetched_at >= since` 的行；`None` 表示不过滤。**按 `fetched_at` 而非 `published_at`** —— 消费方语义"我上次消费到这里"，需要单调可恢复。 |
| `source_types` | 限定 `source_type` 列表（如 `["rss"]` / `["web"]`）；`None` 表示不过滤。 |
| `limit` | 限制返回条数；`None` 表示无限。 |
| `db_path` | 覆盖默认 `~/.news-collector/raw.db`；测试或多 db 场景使用。 |

返回：迭代器，按 `(fetched_at ASC, id ASC)` 排序，**流式游标**（不一次性加载内存）。

异常：`FileNotFoundError` —— `db_path` 指向的文件不存在（一般是首次使用还没跑过
`news-collector fetch`）。

---

## 不暴露的字段

SDK 故意**不**暴露这些采集层内部字段：

- `url_canonical_hash` —— URL 规范化去重实现细节
- `is_long_form` —— 长文标记，未来可能改造

如有特殊需要直接 `sqlite3` 连 `~/.news-collector/raw.db` 查询，但不保证字段稳定。

> 历史说明：早期 schema 含 `articles_raw.status` / `articles_raw.last_error` 列；
> s1-schema-cleanup 通过 `0002_drop_dead_fields.sql` 删除（articles_raw 走"原料型"
> 路线，失败信息按信源记账于 `source_state` 表，由 CLI `status` / `state` / `doctor`
> 命令读取，不进 SDK）。

---

## 数据目录与并发

- `raw.db` 路径：`~/.news-collector/raw.db`
- 多个消费方共享同一文件：**只有 `news-collector fetch` 一个写入方，消费方只读**，
  因此无并发写约束。
- SDK 不会跑迁移；如果 `raw.db` 不存在，先在 collector 仓库执行：
  ```bash
  cd ~/Desktop/tools/news-collector
  uv run news-collector fetch
  ```

---

## 进阶用法

按多 source_type 取最近 100 条：

```python
articles = list(read_raw(
    domain="ai",
    source_types=["rss", "web"],
    limit=100,
))
```

测试场景指向独立 db：

```python
from pathlib import Path
arts = list(read_raw(domain="ai", db_path=Path("/tmp/test_raw.db")))
```
