# news-collector 产品对齐文档

> **生成背景**：2026-05-09 产品对齐对话产物。
> 采集层独立仓库初始化完成后（commit 9b3bb7f）发现前期缺乏"产品原型阶段"对齐——
> 几个 CLI 命令的语义、SDK / CLI 的边界、消费方如何使用，没和需求方系统对过。
> 本文是一次性补做的对齐结论，作为后续所有 sprint 的设计纲要。

---

## §1 三层角色定位

整个数据流涉及三个角色，**不应混淆**。混淆是"是给 agent 用还是消费方用"这类问题反复纠结的根因。

```
┌─────────────────────────────────────────────────────────────┐
│  collector（本仓库）                                        │
│  职责：抓原料 → 存到 raw.db                                 │
│  特点：不感知消费方存在；不做内容判断                       │
└─────────────────────────────────────────────────────────────┘
                           │ 通过 SDK / CLI
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  news-radar-ai 流水线脚本（独立仓库，自动跑）               │
│  职责：从 raw.db 读 → 调 LLM 做初筛/打分/总结/聚类          │
│        → 落消费方加工表                                     │
│  触发方式：agent 触发（agent 一句"开始处理今日新内容"）     │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  agent（Claude Code）                                       │
│  职责：读消费方加工表（已精炼到几十条）→ 写日报、组织内容  │
│  特点：几乎不直接消费 raw.db 原始全量                       │
└─────────────────────────────────────────────────────────────┘
```

### 关键边界
- **collector 不知道消费方存在**：不放下游 hook、不替消费方做内容判断（是噪音/质量分/聚类都不管）
- **流水线脚本属于消费方仓库**：news-radar-ai 各自维护自己的加工表 schema 和 LLM 调用
- **agent 不直接读 raw.db**：原始全量动辄几万条 × 数千字正文，塞 agent 上下文必爆。它读的是消费方加工后的精炼数据。

---

## §2 CLI / SDK 双入口设计

### 各自定位

| 入口 | 给谁用 | 为什么 |
|---|---|---|
| **CLI** | 你（运维） / agent / 接手者 | 一行 bash 即可，零 Python 环境依赖 |
| **SDK** | 消费方流水线脚本（密集消费） | 流式读、不爆内存、Python 程序自然调用 |

### 内部关系：CLI 是 SDK 的薄外壳

```
CLI（统一对外门面）
    │ 内部调用
    ▼
SDK（公共能力 / 核心引擎）
    │
    ▼
raw.db
```

- 同一份读取逻辑只在 SDK 写一份
- CLI 把命令行参数翻译成 SDK 调用 + JSON 输出
- 接手者拿到一份《CLI 命令清单》就能跑通 80% 场景；要做密集消费再去看 SDK

### ⚠️ 流水线脚本必须直连 SDK，不能走 CLI

原因不是技术洁癖，是性能：流水线一次处理几千条文章，每条起 CLI 子进程 = 启动 N 次 Python 解释器。SDK 直连一次连接读完，性能差几个数量级。

---

## §3 完整 CLI 命令矩阵

按用户场景分组（≈ 25 个命令，分组后好记）。状态标记：✅ 已实装 / ⬚ 待做。

### 场景 1：第一次装机
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `setup` | **一键完成**：建数据目录 + 启 RSSHub/Redis 容器 + 初始化数据库 + 铺信源清单 + 引导填 X token（s3-cli-onboarding） |
| ✅ | `doctor` | 健康自检：Docker / token / 数据库 / 抽样信源能否抓到（s3-cli-onboarding） |

### 场景 2：日常运行
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `fetch` | 抓数据（支持 `--source=all\|<type>\|<id>`、`--since=24h`、`--concurrency=N`） |
| ✅ | `read [--since=24h] [--source-types=] [--source-id=] [--domain=] [--tier=] [--limit=N] [--json]` | 看库里最近内容；默认 rich Table，`--json` 输出 NDJSON（s5-data-views） |
| ✅ | `status` | 容器健康 / 上次抓取时间 / 库里多少条 / 最近失败的信源（s3-cli-onboarding） |
| ✅ | `logs [--tail=N]` | 最近日志（s3-cli-onboarding） |

### 场景 3：信源管理（agent 高频使用）— ROADMAP B 段（s4-sources-management 已完成）
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `sources seed` | 拷贝项目种子清单到运行时目录 |
| ✅ | `sources list [--type=] [--tier=] [--enabled-only/--disabled-only]` | 详细表格（type/tier/id/enabled/url） |
| ✅ | `sources show <id>` | 单条完整配置 |
| ✅ | `sources export [--out=path]` | 信源清单备份导出（字节级保真） |
| ✅ | `sources probe <url>` | 录入前侦察（不写 yaml）：reachable / type / suggested_id / sample_title |
| ✅ | `sources probe --from-file=urls.txt` | 批量探测（共享 httpx client） |
| ✅ | `sources add <url>` | **智能录入**：probe → 交互式问 tier/domain/id → 写入 |
| ✅ | `sources add <url> --tier=... --domain=... --id=... [--type=]` | 非交互式（agent 自动） |
| ✅ | `sources add --from-file=urls.txt` | 批量录入（每行 1-4 token） |
| ✅ | `sources test <id> [--limit=N]` | 已录入信源试拉一次（不入库），打印前 N 条预览 |
| ✅ | `sources disable <id>` / `enable <id>` | 临时停用/启用（幂等） |
| ✅ | `sources remove <id> [--yes]` | 彻底删除（非 --yes 需 tty 确认） |
| ✅ | `sources edit <id> [--tier --domain --url --enabled/--disabled]` | 改字段（不含 id） |
| ✅ | `sources rename <old> <new>` | 改 id（跨类唯一性校验） |

### 场景 4：故障处理
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `doctor` | （同场景 1） |
| ✅ | `restart` | 重启 RSSHub 容器（s3-cli-onboarding） |
| ✅ | `state` | 列每个信源最近抓取情况、连续失败次数（s3-cli-onboarding） |

### 场景 5：数据维护 — ROADMAP C 段（s5-data-views 已完成）
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `stats [--top=N] [--json]` | 4 块面板：总数 / 信源 Top-N 排行 / 近 7 天新增 ASCII 柱图 / source_type×domain 分组（s5-data-views） |
| ✅ | `clean --before=30d [--yes] [--vacuum/--no-vacuum]` | 清掉旧文章；默认 dry-run 报数，`--yes` 才真删，自动 VACUUM（s5-data-views） |

### 场景 6：关停
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `teardown` | 停容器（**数据永远保留**，不提供 --purge 防误操作）（s3-cli-onboarding） |

### 场景 7：配置（保留）
| 状态 | 命令 | 干什么 |
|---|---|---|
| ✅ | `config init` / `config show` | 配置文件初始化 / 查看 |

> `db init` **不再单独暴露**，并入 `setup`（s3-cli-onboarding 落地）。

---

## §4 信源管理：分类维度与录入决策树

### 已有的三个分类维度（不需要新增分类机制）

| 维度 | 取值 | 决定 |
|---|---|---|
| `source_type` | `rss` / `web` | 走哪条抓取通道 |
| `tier` | `official_first_party` / `kol` / `secondary` | 内容权重（消费方打分用） |
| `domain` | `[ai]` / 未来 `[finance]` 等 | 领域归属（多领域消费方各取所需） |

外加：
- `enabled`：启用开关（缺省 true）
- `mode`：抓取细节（如 `changelog_page`）
- 类型特定字段：`markdown_url` / `selector` / `max_articles` 等

### `sources add` 的智能录入决策树

```
给我一个 URL
  │
  ├─ 是 X / Twitter 链接？
  │    → rss 类，url 改写成 RSSHub 路由（http://localhost:1200/twitter/user/xxx?format=atom）
  │
  ├─ 是 Reddit 板块链接？
  │    → rss 类，url 改成 .rss 后缀
  │
  ├─ 是 GitHub releases 页面？
  │    → rss 类，url 改成 .../releases.atom
  │
  ├─ 探测 URL，content-type 含 xml/rss/atom？
  │    → rss 类，原样录入
  │
  ├─ 是单页 changelog（platform.claude.com 等）？
  │    → web 类，mode=changelog_page
  │    → 进一步探测有无 .md 端点，有则填 markdown_url
  │
  └─ 是普通官网博客列表页？
       → web 类，selector=auto
```

CLI 自动跑这棵树，给出建议方案 + 探测到的预计条数，用户 y/n 确认即可。

---

## §5 数据流与去重策略

### 三层去重设计与现状

| 层 | 防的场景 | 现状 |
|---|---|---|
| 第 1 层：信源 + 信源内 ID | 同一信源重复抓（增量重叠） | ✅ 实装 |
| 第 2 层：URL 规范化指纹 | 不同信源同 URL 转载 | ✅ 实装 |
| 第 3 层：内容指纹（标题 + 正文前 500 字 hash） | 不同 URL 但同内容（搬运） | ⚠️ **字段生成、SDK 暴露，但 collector 自己不去重** |

### 第 3 层为什么不做强去重

走"**原料型采集层**"路线：
- 字段生成、存进库、SDK 暴露给消费方
- 消费方按需自决：想去重就去、想用作"被多源转发的热度信号"也行
- collector 自己不替消费方决定"内容相同算重复"

### `articles_raw.status` / `last_error` 的处理

实测代码只写 `status='fetched'`，失败信息走 `source_state.last_error`。两个列在 articles_raw 上事实上是死字段。

**决策**：删除（与"原料型"路线一致——失败由 source_state 单独记账，articles_raw 只装成功落地的内容）。SDK 文档原本承诺"默认只返成功记录"自然成立，无需额外 WHERE。

---

## §6 SDK 给消费方的契约

### 一函数

```python
read_raw(
    domain: str = "ai",
    since: datetime | None = None,
    source_types: list[str] | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> Iterator[ArticleRaw]
```

流式游标，不一次性加载内存。按 `(fetched_at ASC, id ASC)` 排序。

### 暴露字段（计划在 D 阶段补 content_hash）

| 字段 | 用途 |
|---|---|
| id / source_type / source_id / source_tier | 来源标识 |
| external_id | 信源给的唯一 ID |
| url | 原文链接 |
| title / body | 内容 |
| published_at / fetched_at | 时间 |
| domain_tags | 领域归属 |
| **content_hash**（新增） | 内容指纹，消费方用作去重 / 热度聚合 |

### 不暴露字段
- `url_canonical_hash` — 内部去重实现细节
- `is_long_form` — 标记字段，未来可能改造

---

## §7 方案 A：一键安装与运维形态

CLI 内部自动管 Docker。用户视角：

| 用户在做什么 | 用户敲什么 | 内部发生什么 |
|---|---|---|
| 装机 | `news-collector setup` | 启容器 / 建库 / 铺信源 / 引导填 token |
| 想抓数据 | `news-collector fetch` | 检测容器，没起就自动起，再抓 |
| 想看一眼 | `news-collector read --since=24h` | 直接 SQL 查 raw.db（CLI 调 SDK） |
| 系统不对劲 | `news-collector doctor` | 全面诊断 |
| 关机 | `news-collector teardown` | 停容器，数据保留 |

**8 步装机变 1 步**。Docker 是黑盒，接手者不需要懂。

---

## §8 边界：collector 不做的事

写明边界，避免未来需求蔓延。

- ❌ **不做内容判断**：是否噪音、质量打分、是否值得推荐 → 消费方流水线脚本做
- ❌ **不做向量化 / 聚类** → 消费方做（用消费方的 embedding 模型）
- ❌ **不做 LLM 调用** → 采集层无 LLM 调用，无 API key 配置
- ❌ **不感知下游消费方** → 不放 fetch hook 触发下游脚本（hook 放在 news-radar 仓库由 agent 协调）
- ❌ **不替消费方去重内容** → 第 3 层指纹给消费方，让消费方按需用
- ❌ **不维护 RSSHub 路由** → 通用且值得长期维护的路由 PR 给 RSSHub 上游；自家定制走 web 收件口

---

## §9 8 条调整决策（2026-05-09 对齐确认）

| # | 议题 | 决策 |
|---|------|------|
| 1 | `teardown --purge` | 砍掉，防 agent 误操作。`teardown` 永远保留数据 |
| 2 | `db init` 单独暴露 | 并入 `setup`，不再单独暴露 |
| 3 | `--help` + agent skill | typer 自带 --help；agent 用的 skill markdown 进 ROADMAP（中优先级） |
| 4 | 批量录入 | 必做 `sources add --from-file=` |
| 5 | 信源导出 | 必做 `sources export` |
| 6 | probe vs test | probe = 录入前侦察（生 url）；test = 录入后体检（已 yaml）。probe 支持批量 |
| 7 | fetch hook | 放在 news-radar 由 agent 协调；collector 不感知下游 |
| 8 | content_hash | 字段生成 + SDK 暴露，但 collector 不去重；消费方按需 |

---

## §10 sprint 规划锚点

ROADMAP.md 已按本文结论编排：
- **A. 方案 A**（一键安装与运维）
- **B. 信源管理 CLI**
- **C. 数据查看与维护**
- **D. SDK / Schema 微调**

中优先级：
- agent skill 说明文档
- fetch hook 边界（不需 collector 改动，仅文档锚定）

具体 sprint 拆分由 /pflow-sprint 推进。
