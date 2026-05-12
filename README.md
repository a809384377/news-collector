# newsbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![PyPI: newsbox](https://img.shields.io/badge/PyPI-newsbox-blue.svg)](https://pypi.org/project/newsbox/)

采集层基础服务：从上游各类内容平台（社交 / 论坛 / 博客 / 官网）抓原料 → 落 SQLite → 用 CLI 或 Python SDK 读出来。适合 AI 日报、行业内容雷达、垂直领域信息追踪等下游系统作为采集层接入；架构上以 `domain_tags` 多标签机制为未来跨领域扩展留位。

---

## 它解决什么

做 AI 日报、信息雷达、行业内容追踪类产品，第一步绕不开"抓数据"。但你不想自己维护几十种平台（X / Reddit / 博客 / changelog）的爬虫、不想每加一个信源就重写一次抓取代码、也不想让上层 LLM 流水线直接吃原始 HTML 噪音。

newsbox 把"抓 + 存"封成基础服务：本仓库负责协议适配、增量记账、最小清洗，落到 `~/.newsbox/raw.db`；下游应用或脚本（LLM 打分 / 摘要 / 聚类 / 报告）通过 25 条 `--json` 命令或 Python SDK 只读消费，不感知 schema 细节，不重复造轮子。

三层角色边界（采集 → 加工 → 报告）见 [docs/product-alignment.md](./docs/product-alignment.md) §1。

---

## 能做什么

| 能力 | 说明 |
|---|---|
| **52 信源开箱即用** | OpenAI / Anthropic / Google 等官方博客 + 头部 AI KOL X 账号 + 主流 changelog（清单见 [docs/sources.seed.yaml](./docs/sources.seed.yaml)） |
| **协议适配子组件** | RSS / Atom / 网页正文 / X via RSSHub / Reddit `.rss` / GitHub releases.atom 全打通 |
| **三层去重** | 信源 ID + URL 规范化 + 内容指纹（指纹暴露给消费方按需用，采集层自身不去重） |
| **增量记账** | `source_state` 表记录每信源最近抓取时间 + 连续失败次数，可恢复 |
| **agent-native CLI** | 所有命令支持 `--json`（NDJSON 输出，agent 管道安全）；`read` 调用量超 10k 时 stderr warn + `typer.confirm` 软阻断，`--yes` 或 `--json` 跳过 confirm，非 tty 必须显式传其一否则 abort |
| **高性能 Python SDK** | 一函数 `read_raw()`，流式 sqlite 游标，下游零 schema 耦合（详见 [docs/sdk-usage.md](./docs/sdk-usage.md)） |

---

## 安装与上手

> 需要先装好 Python 3.11+、pipx、Docker Desktop。X token 可留空（非 X 类信源仍可正常抓）。具体见下方「前置要求」。

```bash
# 1. 装 CLI 到全局（pipx 把 newsbox 装进隔离虚拟环境再暴露到 PATH，不污染其他 Python 项目；
#    或等价用 uv tool install newsbox。若你已在某个项目 venv 里只是想用 SDK，下面消费方段走 uv add / pip install）
pipx install newsbox

# 2. 一键装机：建数据目录 + 启 RSSHub / Redis 容器 + 初始化数据库 + 引导填 X token（可留空）
newsbox setup

# 3. 第一次抓数据
newsbox fetch --since=24h

# 4. 看抓到了什么
newsbox read --since=24h                   # 人类可读表格
newsbox read --since=24h --limit=5 --json  # NDJSON 输出，agent / 脚本用
newsbox stats                              # 库健康度统计
```

下游应用作为依赖装上读 `raw.db`（在自己的项目 venv 里 `uv add newsbox` 或 `pip install newsbox`，import 用 SDK）→ [docs/sdk-usage.md](./docs/sdk-usage.md)（含「何时该用 SDK」+ CLI 阈值机制说明 + CLI vs SDK 对照表）。

开发者改源码：

```bash
git clone https://github.com/a809384377/newsbox.git
cd newsbox
uv sync           # 装依赖
uv run pytest     # 跑测试
```

国内开发者：uv 全局清华镜像配置见 [docs/install-guide.md](./docs/install-guide.md) §2。

---

## Claude Code skill 安装（可选）

让 Claude Code agent 自然知道 newsbox 的 5 类标准用法（查信源 / 读数据 / 加信源 / 看健康 / 看采集状态），不需要每次手动提示：

```bash
# clone 仓库后整目录拷贝（方便后续 git pull 更新）
git clone https://github.com/a809384377/newsbox.git
cp -r newsbox/skills/newsbox ~/.claude/skills/

# 或只拿 SKILL.md 单文件
mkdir -p ~/.claude/skills/newsbox
curl -L -o ~/.claude/skills/newsbox/SKILL.md \
  https://raw.githubusercontent.com/a809384377/newsbox/main/skills/newsbox/SKILL.md
```

agent 具体任务模板以 [skills/newsbox/SKILL.md](./skills/newsbox/SKILL.md) 为准，README 只给安装入口。

---

## 前置要求

> 当前安装说明以 macOS 为准。Linux 用户按各发行版安装 Python / Docker / pipx / uv 即可。

| 要求 | 说明 | macOS 装法 |
|---|---|---|
| Python 3.11+ | 主语言 | `brew install python@3.13` |
| Docker Desktop | 运行 RSSHub + Redis 容器（X 等私有 API 通过它接入） | [docker.com](https://www.docker.com/products/docker-desktop/) |
| pipx | 装 CLI 到全局（Python 命令行工具的 Homebrew） | `brew install pipx && pipx ensurepath` |
| uv（仅下游 / 开发者） | 项目依赖管理 | `brew install uv` |
| X (Twitter) 小号 | 抓 X 信源用；留空时 X 类信源不可用，其他信源照常 | 见 [docs/rsshub-setup.md](./docs/rsshub-setup.md) §3 |

---

## 命令速查

按场景分组（完整 25 命令矩阵见 [docs/product-alignment.md](./docs/product-alignment.md) §3）：

| 场景 | 关键命令 |
|---|---|
| 装机 / 关停 | `setup` / `teardown` / `restart` |
| 健康自检 | `doctor` / `status` / `state` / `logs` |
| 日常采集 | `fetch [--since=24h] [--source-types=rss,web]` |
| 数据查看 | `read [--since=24h] [--json]` / `stats [--json]` |
| 数据维护 | `clean --before=30d [--yes]` |
| 信源管理 | `sources list / show / seed / add / probe / test / disable / enable / remove / edit / rename / export` |
| 配置 | `config init / show` |

机器读取输出时永远加 `--json`：信息查询类命令（list / show / state / status / doctor / stats）输出整块 JSON；流式列表（`read` / `sources list`）输出 NDJSON（每行一条 JSON）。

```bash
newsbox --help              # 总览
newsbox sources --help      # 子组帮助
newsbox fetch --help        # 单命令帮助
```

---

## 故障排查

**`newsbox setup` 报错 docker daemon 未运行** — 启动 Docker Desktop（macOS 状态栏鲸鱼图标）→ 等鲸鱼变稳定 → 重跑 setup。

**`pipx: command not found`** — `brew install pipx && pipx ensurepath`，然后开新终端窗口让 PATH 生效。

**setup 完了但抓 X 信源全失败** — `newsbox doctor` 看 token 是否为空；如显示 `[warn] X auth_token left empty`，编辑 `~/.newsbox/.env` 填入 token（提取步骤见 [docs/rsshub-setup.md](./docs/rsshub-setup.md) §3），然后 `newsbox restart` 让 RSSHub 容器读新 token。

**`newsbox read` 输出空 / `fetch` 报 `fetched=0`** — 信源更新频率天然不固定（官方博客一周 1-2 次很常见），24h 窗口看不到新内容 ≠ 出问题。先把窗口放宽：`newsbox fetch --since=7d` 或 `30d`，再 `newsbox state` 看连续失败次数 / `newsbox sources test <id>` 单独测某信源 / `newsbox logs --tail=100` 看哪个信源在卡。

**fetch 卡住 / 部分信源 timeout** — `newsbox fetch --concurrency=4` 降并发再试。

**想完全重置 / 换台机器** — 数据全部在 `~/.newsbox/`：

```bash
newsbox teardown            # 停容器（不删数据）
rm -rf ~/.newsbox           # 清光所有运行时数据（谨慎）
newsbox setup               # 重建
```

**国内开发者 `uv sync` 太慢** — 配 uv 全局清华镜像（不污染本项目），见 [docs/install-guide.md](./docs/install-guide.md) §2。

---

## 文档

| 文档 | 干什么用 |
|---|---|
| [skills/newsbox/SKILL.md](./skills/newsbox/SKILL.md) | Claude Code skill 说明书（agent 接入必读） |
| [docs/sdk-usage.md](./docs/sdk-usage.md) | SDK 使用指南（下游应用必读） |
| [docs/install-guide.md](./docs/install-guide.md) | 安装路径详解 + 国内镜像配置 |
| [docs/rsshub-setup.md](./docs/rsshub-setup.md) | RSSHub 容器接入 + X auth_token 提取 |
| [docs/product-alignment.md](./docs/product-alignment.md) | 三层角色边界 + 完整 CLI 命令矩阵 + SDK 契约 |
| [docs/sources.seed.yaml](./docs/sources.seed.yaml) | 一期 52 信源种子清单 |

---

## 设计边界（绝不做的事）

- 不做内容判断：噪音过滤 / 质量打分 / 推荐排序 → 下游应用做
- 不做 LLM 调用：采集层无 API key 配置
- 不感知下游：不放 fetch hook 触发下游脚本
- 不替消费方去重内容：第 3 层指纹给消费方，让消费方按需用
- 不 fork RSSHub：通用且值得长期维护的路由 PR 给上游；自家定制走 web 收件口

详见 [docs/product-alignment.md](./docs/product-alignment.md) §8。

---

## 贡献

欢迎 issue / PR。RSSHub 路由级改进请优先 PR 给 [RSSHub 上游](https://github.com/DIYgod/RSSHub)。

## License

[MIT](./LICENSE) © 2026 a809384377
