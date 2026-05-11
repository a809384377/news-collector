"""信源清单 seed/list/iter 逻辑。

- ``seed_sources``  把 sources.seed.yaml 拷到 ~/.newsbox/sources.yaml
- ``list_sources``  按 2 类（rss/web）统计条目数与启用数
- ``iter_sources``  返回完整条目列表（带 source_type 字段，已 enabled 过滤）— fetch 编排消费

模块独立：不依赖 newsbox.config / newsbox.cli，便于薄封装与测试。
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

import yaml

# 种子清单作为 package data 内置（``newsbox.data``）；pipx / uv tool install
# 装到任意目录都能找到。``docs/sources.seed.yaml`` 是该文件的对外可读镜像，由 README
# 引用（s6-distribution-package 引入双副本，后续 sprint 统一）。
DEFAULT_SEED_PATH = Path(
    str(files("newsbox.data").joinpath("sources.seed.yaml"))
)

# 2 类信源固定顺序（list_sources 输出 key 集稳定）
SOURCE_KINDS: tuple[str, ...] = ("rss", "web")


def seed_sources(
    target_path: Path,
    seed_path: Path = DEFAULT_SEED_PATH,
    force: bool = False,
) -> Path:
    """把 ``seed_path`` 拷贝到 ``target_path``。

    一般用法：把项目内 ``docs/sources.seed.yaml`` 拷到
    ``~/.newsbox/sources.yaml``。

    Args:
        target_path: 目标路径。父目录不存在则自动创建。
        seed_path: 种子文件路径，默认 ``DEFAULT_SEED_PATH``。
        force: 目标已存在时是否覆盖。

    Returns:
        ``target_path.resolve()``，方便调用方打印。

    Raises:
        FileNotFoundError: ``seed_path`` 不存在。
        FileExistsError: ``target_path`` 已存在且 ``force=False``。
    """
    seed = Path(seed_path)
    if not seed.exists():
        raise FileNotFoundError(f"seed file not found: {seed}")

    target = Path(target_path)
    if target.exists() and not force:
        raise FileExistsError(
            f"target already exists: {target} (use force=True to overwrite)"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # copy2 保留 mtime，便于看新鲜度
    shutil.copy2(seed, target)
    return target.resolve()


def list_sources(yaml_path: Path) -> dict[str, dict[str, int]]:
    """读取 sources.yaml，按 2 类统计条目数与启用数。

    输出结构（2 个 key 必出现，缺失类填 0）::

        {
            "rss": {"total": 45, "enabled": 44},
            "web": {"total":  7, "enabled":  6},
        }

    enabled 计数规则：条目缺 ``enabled`` 字段视为启用（与设计 §7.1 保持一致）。
    顶层未识别 key 静默忽略，不抛错。

    Args:
        yaml_path: sources.yaml 路径。

    Returns:
        ``dict[str, dict[str, int]]``，固定 2 个 key。

    Raises:
        FileNotFoundError: yaml 文件不存在。
        yaml.YAMLError: yaml 解析失败。
    """
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"sources yaml not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}
    if not isinstance(data, dict):
        # YAML 顶层必须是 mapping；否则按空处理（5 类全 0）
        data = {}

    result: dict[str, dict[str, int]] = {}
    for kind in SOURCE_KINDS:
        items = data.get(kind) or []
        if not isinstance(items, list):
            items = []
        total = 0
        enabled = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            total += 1
            # 缺 enabled 字段 → 视为 true
            if item.get("enabled", True):
                enabled += 1
        result[kind] = {"total": total, "enabled": enabled}
    return result


def iter_sources(yaml_path: Path) -> list[dict]:
    """返回 sources.yaml 所有 enabled 条目，每个 dict 注入 ``source_type`` 字段。

    返回顺序：2 类按 ``SOURCE_KINDS`` 固定顺序（rss → web），组内保持 yaml 原顺序。
    enabled=false 的条目跳过；其余字段（id / url / tier / 类型特有字段）原样保留。

    示例返回（截断）::

        [
            {"source_type": "rss", "id": "x_karpathy",
             "url": "http://localhost:1200/twitter/user/karpathy?format=atom",
             "tier": "kol", "domain": ["ai"]},
            {"source_type": "web", "id": "anthropic_news",
             "url": "https://www.anthropic.com/news",
             "selector": "auto", "tier": "official_first_party", "domain": ["ai"]},
            ...
        ]
    """
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"sources yaml not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}

    out: list[dict] = []
    for kind in SOURCE_KINDS:
        items = data.get(kind) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("enabled", True):
                continue
            out.append({"source_type": kind, **item})
    return out
