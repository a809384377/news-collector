# 高性能集成（可选）

newsbox 的对外面貌是 **CLI 大一统**：25 条命令全部支持 `--json`，普通消费场景一句 `newsbox read --json | jq ...` 就够。本文档讲的 SDK 是**高性能后门**，只在 CLI + jq 性能掉队时才升级使用。

## 何时该用 SDK

- **百万级数据流式处理**：CLI `--json` 输出 NDJSON 在 >10000 条量级时序列化 + jq 解析开始成为瓶颈，SDK 直接迭代 sqlite 游标无 JSON 往返。
- **高频毫秒级调用循环**：CLI 每次启动 ~100ms（Python 解释器 + typer 装配），毫秒级循环里这层固定开销不可接受。
- **复杂对象传递**：`datetime` / `Decimal` / generator 等类型经 JSON 序列化会损失精度或扁平化为字符串，SDK 直接返回原生 Python 对象。
- **同 venv 嵌入式集成**：消费方已经 `pip install newsbox` 作为依赖（如 news-radar-ai），再走 subprocess 跑 CLI 是徒增开销。

其余场景一律走 CLI。

---

## CLI 阈值机制：什么时候 CLI 会劝你切 SDK

`newsbox read` 命令在执行前先跑 `SELECT COUNT(*)` 预估返回行数，**超过 10000 条**时 stderr 打 warn 并 `typer.confirm` 软阻断，明确指引切换到 SDK。`--yes` 或 `--json` 旗标会跳过 confirm（脚本化场景）；非 tty 环境（如 CI）必须显式 `--yes` 或 `--json` 否则 abort。

CLI 报错引导示例：

```
$ newsbox read --since=90d
[warn] 预计返回 24,531 条记录（>10,000 阈值），CLI + jq 在此规模性能掉队；考虑切换到 SDK：
  from newsbox.sdk import read_raw
  for art in read_raw(domain='ai', since=...): ...  # 流式不装内存
继续？ [y/N]:
```

跳过阻断继续用 CLI（不推荐 >10k）：

```bash
newsbox read --since=90d --yes        # 交互模式跳过 confirm
newsbox read --since=90d --json       # 脚本模式自动跳过（warn 仍在 stderr）
```

阈值可在 `~/.newsbox/config.yaml` 覆盖：

```yaml
thresholds:
  cli_read_warn: 50000   # 默认 10000
```

切换到 SDK 的等价调用：

```python
from newsbox.sdk import read_raw
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(days=90)
for art in read_raw(domain="ai", since=since):
    process(art)  # 流式游标，不一次性加载内存
```

---

## 安装

```bash
uv add newsbox          # 推荐：消费方仓库直接添加依赖
# 或
uv pip install newsbox  # 临时环境用
```

本地开发协同（消费方与 newsbox 在同一 IDE workspace）：

```bash
cd ~/Desktop/tools/<your-consumer>
uv pip install -e ../news-collector
```

---

## 最小消费示例

3 行代码读取最近 24h AI 领域内容：

```python
from newsbox.sdk import read_raw
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
| `content_hash` | `str` | 内容指纹：`sha256(title + body[:500])` 输出的 64 字符 hex。消费方可用作"同内容多源转发"的去重键或热度聚合维度。**算法稳定不变**。采集层自身不按此去重（原料型路线）。 |
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
| `db_path` | 覆盖默认 `~/.newsbox/raw.db`；测试或多 db 场景使用。 |

返回：迭代器，按 `(fetched_at ASC, id ASC)` 排序，**流式游标**（不一次性加载内存）。

异常：`FileNotFoundError` —— `db_path` 指向的文件不存在（一般是首次使用还没跑过 `newsbox fetch`）。

---

## 不暴露的字段

SDK 故意**不**暴露这些采集层内部字段：

- `url_canonical_hash` —— URL 规范化去重实现细节
- `is_long_form` —— 长文标记，未来可能改造

如有特殊需要直接 `sqlite3` 连 `~/.newsbox/raw.db` 查询，但不保证字段稳定。

> 历史说明：早期 schema 含 `articles_raw.status` / `articles_raw.last_error` 列；s1-schema-cleanup 通过 `0002_drop_dead_fields.sql` 删除（articles_raw 走"原料型"路线，失败信息按信源记账于 `source_state` 表，由 CLI `status` / `state` / `doctor` 命令读取，不进 SDK）。

---

## 数据目录与并发

- `raw.db` 路径：`~/.newsbox/raw.db`
- 多个消费方共享同一文件：**只有 `newsbox fetch` 一个写入方，消费方只读**，因此无并发写约束。
- SDK 不会跑迁移；如果 `raw.db` 不存在，先在 collector 仓库执行：
  ```bash
  newsbox fetch
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

---

## CLI 路径 vs SDK 路径

| 场景 | CLI 命令 | SDK 调用 |
|------|---------|---------|
| 轻量 ad-hoc 查询（看最近抓到啥） | `newsbox read --since=24h --limit=20 --json \| jq` | （没必要）`list(read_raw(since=..., limit=20))` |
| 数据导出脚本（千级条数喂下游 prompt） | `newsbox read --since=7d --json > today.ndjson` | （没必要）NDJSON 文件够用 |
| 嵌入式应用（消费方常驻进程，读 → 评分 → 入库循环） | （不推荐）每次 subprocess 起 newsbox | `for art in read_raw(...): score(art)` |
| 高频循环（agent 毫秒级轮询新内容） | （不可用）100ms 启动开销爆炸 | `read_raw(since=last_seen, limit=50)` |
| 百万级流式（90 天全量回扫，做向量化 / 全文索引） | （会被阈值阻断）超 10k 条 warn | `for art in read_raw(since=ninety_days_ago): index(art)` |

结论：**普通情况下用 CLI，需要这张表右侧的场景才升级到 SDK。** CLI 是默认路径，SDK 是性能后门。
