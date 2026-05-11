# news-collector

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![PyPI: raw-news-collector](https://img.shields.io/badge/PyPI-raw--news--collector-blue.svg)](https://pypi.org/project/raw-news-collector/)

> 采集层基础服务：从上游各类内容平台（社交 / 论坛 / 博客 / 官网）抓原料 → 落库 → 暴露 Python SDK 给下游多领域消费方共用。

一期消费方为 [news-radar-ai](#)（AI 内容雷达），架构已为 finance / crypto 等未来领域留位（domain_tags 多标签机制）。

---

## 它解决什么问题

如果你想做一个 AI 日报、信息雷达、行业内容追踪类产品，第一步绕不开"**抓数据**"——但你不想：

- ❌ 自己维护几十种平台（X / Reddit / 各家博客 / changelog）的爬虫
- ❌ 每次新需求都重写一遍抓取代码
- ❌ 让上层 LLM 流水线直接接触原始 HTML 噪音

`news-collector` 把"**抓 + 存**"这一层封成基础服务：

```
┌──────────────────────┐
│  news-collector      │  ← 抓原料、做最小清洗、落 SQLite
│  （本仓库）          │  ← 不感知任何下游消费方
└──────────┬───────────┘
           │ Python SDK / CLI
           ▼
┌──────────────────────┐
│  你的流水线          │  ← LLM 打分 / 摘要 / 聚类 / 报告
│  （消费方仓库）      │  ← 各自维护加工 schema
└──────────────────────┘
```

详细三层角色边界见 [docs/product-alignment.md](./docs/product-alignment.md) §1。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **52 信源开箱即用** | 一期内置 OpenAI / Anthropic / Google 等官方博客 + 头部 AI KOL X 账号 + 主流 changelog 页面（详见 [docs/sources.seed.yaml](./docs/sources.seed.yaml)） |
| **协议适配子组件** | RSS / Atom / 网页正文 / X via RSSHub / Reddit `.rss` / GitHub releases.atom 全打通 |
| **三层去重** | 信源 ID + URL 规范化 + 内容指纹（SDK 暴露给消费方按需用） |
| **增量记账** | `source_state` 表记录每信源最近抓取时间 + 连续失败次数，可恢复 |
| **CLI 全套运维** | setup / fetch / read / stats / clean / sources 共 27 个命令 |
| **Python SDK** | 一函数 `read_raw()`，流式游标，下游零 schema 耦合 |

---

## 安装

> 包名说明：PyPI 包名 `raw-news-collector`，Python module `news_collector`，CLI 命令 `news-collector`。三者独立设计——你装的是 PyPI 包，import 的是 module，敲的是命令。

### 路径 A：终端用户用（推荐 ⭐）

```bash
# 1. 装命令行工具到全局（pipx 是 Python 命令行工具的"Homebrew"）
pipx install raw-news-collector

# 2. 一键装机：建数据目录 + 启 RSSHub/Redis 容器 + 初始化数据库 + 引导填 X token
news-collector setup

# 3. 第一次抓数据
news-collector fetch --since=24h
```

### 路径 B：下游消费方用（news-radar-ai 等）

在你的消费方项目里：

```bash
uv add raw-news-collector
```

然后 Python 中：

```python
from news_collector.sdk import read_raw
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(hours=24)
for art in read_raw(domain="ai", since=since):
    print(art.published_at, art.title)
```

完整 SDK 契约见 [docs/sdk-usage.md](./docs/sdk-usage.md)。

### 路径 C：开发者用（改源码）

```bash
git clone https://github.com/a809384377/news-collector.git
cd news-collector
uv sync                                  # 装依赖
uv run news-collector setup              # 装机
uv run pytest                            # 351 测试用例全绿
```

国内开发者可配置 uv 全局清华镜像加速装包，见 [docs/install-guide.md](./docs/install-guide.md) §2。

---

## Claude Code skill 安装（可选）

让 Claude Code agent 自然知道 news-collector 的 5 类标准用法（查信源 / 读数据 / 加信源 / 看健康 / 看采集状态），不需要每次手动提示。

```bash
# 路径 A：clone 仓库后整目录拷贝（推荐，方便后续 git pull 更新）
git clone https://github.com/a809384377/news-collector.git
cp -r news-collector/skills/news-collector ~/.claude/skills/

# 路径 B：只拿 SKILL.md 单文件
mkdir -p ~/.claude/skills/news-collector
curl -L -o ~/.claude/skills/news-collector/SKILL.md \
  https://raw.githubusercontent.com/a809384377/news-collector/main/skills/news-collector/SKILL.md
```

装完后 Claude Code 启动时自动加载。agent 接到 news-collector 后会按 [skills/news-collector/SKILL.md](./skills/news-collector/SKILL.md) 的 5 类场景标准用法操作。

---

## 前置要求

| 要求 | 说明 | macOS 装法 |
|---|---|---|
| Python 3.11+ | 主语言 | `brew install python@3.13` 或 [python.org](https://www.python.org/downloads/) |
| Docker Desktop | 运行 RSSHub + Redis 容器（X 等私有 API 通过它接入） | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| pipx（仅路径 A） | 全局装命令行工具 | `brew install pipx && pipx ensurepath` |
| uv（仅路径 B / C） | 项目依赖管理 | `brew install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| X (Twitter) 小号 | 抓 X 信源用（可跳过；不抓 X 不需要） | 见 [docs/rsshub-setup.md](./docs/rsshub-setup.md) §3 |

---

## 命令速查

按场景分组（完整 27 个命令矩阵见 [docs/product-alignment.md](./docs/product-alignment.md) §3）：

| 场景 | 关键命令 |
|---|---|
| 装机 / 关停 | `setup` / `teardown` / `restart` |
| 健康自检 | `doctor` / `status` / `state` / `logs` |
| 日常采集 | `fetch [--since=24h] [--source-types=rss,web]` |
| 数据查看 | `read [--since=24h] [--json]` / `stats [--json]` |
| 数据维护 | `clean --before=30d [--yes]` |
| 信源管理 | `sources list / add / probe / test / disable / remove / edit / rename / export` |
| 配置 | `config init / show` |

```bash
news-collector --help              # 总览
news-collector sources --help      # 子组帮助
news-collector fetch --help        # 单命令帮助
```

---

## 故障排查 FAQ

### 1. `news-collector setup` 报错 "docker daemon 未运行"

启动 Docker Desktop（macOS 状态栏小鲸鱼图标）→ 等鲸鱼变稳定 → 重跑 setup。

### 2. setup 完了但抓 X 信源全失败

```bash
news-collector doctor              # 看 token 是不是空
```

如果显示 `[warn] X auth_token left empty`：编辑 `~/.news-collector/.env` 填入 token（提取步骤见 [docs/rsshub-setup.md](./docs/rsshub-setup.md) §3），然后 `news-collector restart` 让 RSSHub 容器读新 token。

### 3. fetch 卡住 / 部分信源 timeout

```bash
news-collector logs --tail=100     # 看哪个信源在卡
news-collector state               # 看连续失败次数
news-collector fetch --concurrency=4   # 降并发再试
```

### 4. 想完全重置 / 换台机器

数据全部在 `~/.news-collector/`：

```bash
news-collector teardown            # 停容器（不删数据）
rm -rf ~/.news-collector           # 清光所有运行时数据 ⚠️
news-collector setup               # 重建
```

### 5. 我是国内开发者，`uv sync` 太慢

配 uv 全局清华镜像（不污染本项目），见 [docs/install-guide.md](./docs/install-guide.md) §2。

---

## 文档

| 文档 | 干什么用 |
|---|---|
| [skills/news-collector/SKILL.md](./skills/news-collector/SKILL.md) | Claude Code skill 说明书（agent 接入必读） |
| [docs/sdk-usage.md](./docs/sdk-usage.md) | SDK 使用指南（消费方必读） |
| [docs/install-guide.md](./docs/install-guide.md) | 安装路径详解 + 国内镜像配置 |
| [docs/rsshub-setup.md](./docs/rsshub-setup.md) | RSSHub 容器接入 + X auth_token 提取 |
| [docs/product-alignment.md](./docs/product-alignment.md) | 三层角色边界 + 完整 CLI 命令矩阵 + SDK 契约 |
| [docs/sources.seed.yaml](./docs/sources.seed.yaml) | 一期 52 信源种子清单 |

---

## 设计取舍（绝不做的事）

- ❌ **不做内容判断**：噪音过滤 / 质量打分 / 推荐排序 → 消费方流水线脚本做
- ❌ **不做 LLM 调用**：采集层无 API key 配置
- ❌ **不感知下游**：不放 fetch hook 触发下游脚本
- ❌ **不替消费方去重内容**：第 3 层指纹给消费方，让消费方按需用
- ❌ **不 fork RSSHub**：通用且值得长期维护的路由 PR 给 RSSHub 上游；自家定制走 web 收件口

详见 [docs/product-alignment.md](./docs/product-alignment.md) §8。

---

## 贡献

欢迎 issue / PR。

## License

[MIT](./LICENSE) © 2026 a809384377
