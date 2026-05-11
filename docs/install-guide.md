# 安装指南

本项目以 PyPI 包 `raw-news-collector` 名义对外分发，CLI 命令名为 `news-collector`，Python module 名为 `news_collector`（三者独立设计，详见 README §安装）。

本文档面向**国内开发者**说明：① 推荐安装路径 ② 国内 PyPI 镜像加速配置（uv 全局，不污染本项目）。

---

## 1. 安装路径速查

| 你是谁 | 推荐方式 |
|---|---|
| 想跑 collector 抓数据，不写 Python 代码 | `pipx install raw-news-collector` |
| 下游消费方（news-radar-ai 等），要 `import news_collector.sdk` | 在你的项目 `uv add raw-news-collector`；本仓库未发布前用 `uv pip install -e ../news-collector` editable |
| 想改 collector 源码 | `git clone` + `uv sync` |

完整步骤见 README。

---

## 2. uv 全局镜像配置（国内加速）

> 为什么放这里、不放进 pyproject.toml：项目级镜像配置会被分发出去强迫**所有用户**走特定源（境外用户/CI 体验差）。镜像加速属于**开发环境个人偏好**，应配在你本机 uv 全局配置里。

### macOS / Linux

创建或编辑 `~/.config/uv/uv.toml`：

```toml
[[index]]
name = "tuna"
url = "https://pypi.tuna.tsinghua.edu.cn/simple/"
default = true

[[index]]
name = "pypi"
url = "https://pypi.org/simple/"
```

效果：
- 你本机所有 uv 项目装包都默认走清华，飞快
- 清华源缺包时自动 fallback 到 PyPI 官方
- 项目本身的 pyproject.toml 干净，分发出去不影响他人

验证：

```bash
uv pip install --dry-run httpx 2>&1 | head -3
# 应看到 https://pypi.tuna.tsinghua.edu.cn/... 字样
```

### Windows

路径改为 `%APPDATA%\uv\uv.toml`，内容同上。

### 备选镜像

| 名字 | URL | 备注 |
|---|---|---|
| 清华 tuna | `https://pypi.tuna.tsinghua.edu.cn/simple/` | 最常用，本文档默认 |
| 阿里云 | `https://mirrors.aliyun.com/pypi/simple/` | 阿里云内网用户首选 |
| 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple/` | 腾讯云内网用户首选 |
| 中科大 | `https://pypi.mirrors.ustc.edu.cn/simple/` | 备选 |

---

## 3. pipx 也想走镜像？

`pipx` 内部用 pip，pip 全局镜像配置在 `~/.config/pip/pip.conf`（macOS/Linux）：

```ini
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple/
extra-index-url = https://pypi.org/simple/
```

之后 `pipx install raw-news-collector` 会经清华下载。

---

## 4. 常见问题

| 问题 | 解决 |
|---|---|
| `uv sync` 卡在某个包 | 清华源该包还没同步，临时 `uv sync --index-url https://pypi.org/simple/` 走官方 |
| pipx 装完命令找不到 | `pipx ensurepath` 后重开 shell；或检查 `~/.local/bin` 是否在 PATH |
| 公司代理拦了 PyPI | 配 pip 代理：`pip config set global.proxy http://your-proxy:port` |
| 想暂时绕过镜像装一次 | `uv pip install <pkg> --index-url https://pypi.org/simple/` |
