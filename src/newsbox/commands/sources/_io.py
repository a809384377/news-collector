"""sources.yaml 读写底座（ruamel.yaml round-trip，保留注释与顺序）。

s4-sources-management Step 2 产出。Step 4 契约冻结后本模块公开 API 在本 sprint 内不变。

公开 API
========

异常
----
- ``SourceIdConflictError``  upsert 时 id 已存在
- ``SourceKindError``        kind 不在已注册 ``ADAPTER_REGISTRY`` 类型集合内

函数
----
- ``load_yaml(path) -> CommentedMap``
    读 sources.yaml；空文件返回空 ``CommentedMap``；顶层非 mapping 抛 ValueError
- ``save_yaml(path, data) -> None``
    写回 sources.yaml；保留 round-trip 注释与原顺序
- ``find_source(data, source_id) -> tuple[str, int, dict] | None``
    跨所有已注册 source_type 查找；返回 ``(kind, idx, item)`` 或 None
- ``upsert_source(data, kind, item) -> None``
    新增条目；id 全局唯一冲突抛 ``SourceIdConflictError``；kind 缺失自动建空列表
- ``remove_source(data, source_id) -> bool``
    删除条目；找不到返回 False
- ``update_source(data, source_id, mutator) -> bool``
    把 ``mutator(item)`` 应用到目标条目（item 是 ruamel ``CommentedMap``，原地改）
    找不到返回 False
- ``rename_source(data, old_id, new_id) -> bool``
    改 id；new_id 已被占用抛 ``SourceIdConflictError``；找不到 old_id 返回 False

注释保留约定
============
本模块用 ``YAML(typ="rt")``（round-trip）。读取的 ``CommentedMap`` / ``CommentedSeq``
内嵌了注释，``save_yaml`` 写回时保留。**直接修改 dict 字段（如 ``item["tier"] = "kol"``）**
保留同列表项的字段级尾随注释。

已知 ruamel 限制：list item 之间的"悬空注释"——即缩进与字段同级、写在某 item 末
尾但语法上挂在下一 item 前置位置的注释——在删除"下一 item" 时会被一并带走。例：

    rss:
      - id: a
        url: u1
        # 这是宝玉                  ← 写给 a 看，但 ruamel 视作 b 的 leading
      - id: b
        ...

    del rss[1]  # 删 b 的同时把 "# 这是宝玉" 一并带走

避免该问题的 best practice：把注释做成 inline 形式 ``- id: a  # 宝玉``（明确挂
在 a 的 id 字段上）；或紧贴 ``id: a`` 之前缩进 2 空格写（明确成为 a 的 leading）。

项目 ``docs/sources.seed.yaml`` 历史注释多为悬空格式；删除 source 时若发现注释
丢失，需要在 sprint LOGBOOK 留笔，并由 ``sources edit`` / ``add`` 命令引导用户改
为 inline 格式。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ...adapters import supported_types

# 与 ``newsbox.sources.SOURCE_KINDS`` 同源（``ADAPTER_REGISTRY`` 派生）；
# 导入时绑定，向后兼容名字。函数内部一律调 ``supported_types()`` 实时派生
# 以兼容测试 mock 第 3 类 adapter 的场景。
SOURCE_KINDS: tuple[str, ...] = supported_types()


class SourceIdConflictError(ValueError):
    """upsert / rename 时目标 id 已存在。"""


class SourceKindError(ValueError):
    """kind 不在已注册的 ``ADAPTER_REGISTRY`` 类型集合内。"""


def _yaml() -> YAML:
    """构造 round-trip YAML 实例。

    ``width=4096`` 防止 ruamel 默认 80 列折行把长 URL 拆成多行（视觉脏）。
    """
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def load_yaml(path: Path) -> CommentedMap:
    """读 ``sources.yaml`` 返回 ``CommentedMap``（带注释）。

    空文件返回空 ``CommentedMap``。顶层非 mapping 抛 ``ValueError``。
    文件不存在抛 ``FileNotFoundError``。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"sources yaml not found: {p}")
    text = p.read_text(encoding="utf-8")
    data = _yaml().load(text)
    if data is None:
        return CommentedMap()
    if not isinstance(data, dict):
        raise ValueError(f"sources.yaml 顶层必须是 mapping: {p}")
    return data  # type: ignore[return-value]


def save_yaml(path: Path, data: CommentedMap) -> None:
    """把 ``data`` 写回 ``path``；父目录不存在自动创建。

    依赖 round-trip 模式保留注释；直接传入 plain ``dict`` 会丢注释（设计如此）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        _yaml().dump(data, f)


def find_source(
    data: CommentedMap, source_id: str
) -> tuple[str, int, dict] | None:
    """跨所有已注册 source_type 查找 ``source_id``；命中返回 ``(kind, idx, item)``。

    item 是底层 ``CommentedMap``，原地修改会被 ``save_yaml`` 持久化。
    """
    for kind in supported_types():
        items = data.get(kind) or []
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get("id") == source_id:
                return (kind, i, item)
    return None


def upsert_source(data: CommentedMap, kind: str, item: dict) -> None:
    """追加条目到 ``kind`` 类列表末尾。

    - kind 必须 ∈ 已注册 source_type；否则抛 ``SourceKindError``
    - item 必须有非空 ``id`` 字段；否则抛 ``ValueError``
    - id 全局唯一（跨所有类型）；冲突抛 ``SourceIdConflictError``
    - 若 ``data`` 中尚无 kind 键，自动建空 ``CommentedSeq``
    """
    kinds = supported_types()
    if kind not in kinds:
        raise SourceKindError(
            f"unknown source kind: {kind!r}; expected one of {kinds}"
        )
    src_id = item.get("id")
    if not src_id:
        raise ValueError("source item missing non-empty 'id' field")
    if find_source(data, src_id) is not None:
        raise SourceIdConflictError(f"source id already exists: {src_id}")
    if kind not in data or data[kind] is None:
        data[kind] = CommentedSeq()
    items = data[kind]
    if not isinstance(items, list):
        raise ValueError(f"{kind!r} key must be a list, got {type(items).__name__}")
    items.append(item)


def remove_source(data: CommentedMap, source_id: str) -> bool:
    """删除 ``source_id``；找不到返回 False。"""
    found = find_source(data, source_id)
    if found is None:
        return False
    kind, idx, _ = found
    del data[kind][idx]
    return True


def update_source(
    data: CommentedMap,
    source_id: str,
    mutator: Callable[[dict], None],
) -> bool:
    """对目标条目应用 ``mutator(item)``（原地改）；找不到返回 False。

    mutator 不应改 ``id`` 字段——改 id 走 ``rename_source``。
    """
    found = find_source(data, source_id)
    if found is None:
        return False
    _, _, item = found
    mutator(item)
    return True


def rename_source(data: CommentedMap, old_id: str, new_id: str) -> bool:
    """把 ``old_id`` 改成 ``new_id``。

    - new_id 已被占用抛 ``SourceIdConflictError``（含 old_id == new_id 的退化情况）
    - 找不到 old_id 返回 False
    """
    if old_id == new_id:
        raise SourceIdConflictError(
            f"source id already exists: {new_id}（old_id 与 new_id 相同）"
        )
    if find_source(data, new_id) is not None:
        raise SourceIdConflictError(f"source id already exists: {new_id}")
    found = find_source(data, old_id)
    if found is None:
        return False
    _, _, item = found
    item["id"] = new_id
    return True
