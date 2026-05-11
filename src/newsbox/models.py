"""dataclass / pydantic 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawArticle:
    """信源适配器的标准化输出（设计 §3.7）。

    各 adapter 把异构源（RSS-like / Web-like）映射到本结构。
    pipeline/fetch.py 负责把 RawArticle 写入 articles_raw 表，并补足
    url_canonical_hash / content_hash / fetched_at / status / domain_tags 等下游字段。
    domain_tags 是 source-level 标签（来自 sources.yaml 信源条目），不在本 dataclass。
    """

    source_type: str
    """rss / web，对应 sources.yaml 顶层 key（s2-1 Step 4 后两类）。"""

    source_id: str
    """sources.yaml 中条目的 id。"""

    external_id: str
    """信源给的唯一 ID（feed entry.id / reddit post id / FxTwitter status id ...）。"""

    url: str
    title: str
    body: str
    published_at: datetime | None
    """来源未提供发布时间时为 None；下游可回落到 fetched_at。"""

    is_long_form: str | None = None
    """长文标记：``normal`` / ``note_tweet`` / ``article``；普通短文/未识别为 None。
    一期 RSSHub Twitter 路由暂不下发该信号；未来路由若识别 note_tweet 时填充。"""

    skip_url_dedup: bool = False
    """changelog_page 单页多 section 共享 base url 时置 True，pipeline 第二层
    url_canonical_hash 检查跳过；仍走第一层 (source_type, external_id) 去重（D1）。
    """
