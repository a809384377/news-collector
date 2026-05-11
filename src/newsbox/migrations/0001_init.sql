-- 采集层纯净 schema：articles_raw + source_state；AI 加工字段在消费方仓库自维护表
-- 引用：sprint s2-1-collector-extract DECISIONS.md D2（SDK 形态）+ D5（domain_tags 由 sources.yaml 继承）

CREATE TABLE articles_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 来源标识
    source_type TEXT NOT NULL,        -- rss / web（s2-1 Step 4 后两类）
    source_id TEXT NOT NULL,          -- sources.yaml 条目 id
    source_tier TEXT NOT NULL,        -- official_first_party / kol / secondary
    external_id TEXT NOT NULL,        -- 信源给的唯一 ID

    -- 内容
    url TEXT NOT NULL,
    url_canonical_hash TEXT NOT NULL, -- 规范化 URL 的 sha256
    content_hash TEXT NOT NULL,        -- (title + body[:500]) 的 sha256
    title TEXT NOT NULL,
    body TEXT NOT NULL,                -- 无长度上限：长文如 X Article 可达数万字
    is_long_form TEXT,                 -- normal / note_tweet / article（仅长文标记，否则 NULL）

    -- 时间
    published_at TIMESTAMP,
    fetched_at TIMESTAMP NOT NULL,

    -- 采集状态（D2 历史决策）：原本含 status / last_error 两列；后由 s1-schema-cleanup
    -- 通过 0002_drop_dead_fields.sql 删除（articles_raw 走"原料型"路线，失败信息全部走
    -- source_state 按信源记账）。下方两列保留在本文件以维持 0001 历史快照不被改写；
    -- 实际生产 schema 以 0002 应用后为准。
    status TEXT NOT NULL,             -- 已废弃：见 0002_drop_dead_fields.sql
    last_error TEXT,                  -- 已废弃：见 0002_drop_dead_fields.sql

    -- 领域标签（D5：一期所有信源都是 AI，从 sources.yaml 信源 domain 字段继承）
    domain_tags JSON NOT NULL DEFAULT '["ai"]',

    UNIQUE(source_type, external_id)
);

CREATE INDEX idx_url_canonical ON articles_raw(url_canonical_hash);
CREATE INDEX idx_content_hash ON articles_raw(content_hash);
CREATE INDEX idx_published ON articles_raw(published_at);
-- 下方两索引（idx_status / idx_domain）已被 0002_drop_dead_fields.sql 删除：
-- idx_status 区分度为 0；idx_domain 建在 JSON 列上 json_each 查询走不到。
-- 保留 CREATE 语句以维持 0001 历史快照；实际生产 schema 以 0002 应用后为准。
CREATE INDEX idx_status ON articles_raw(status);
CREATE INDEX idx_domain ON articles_raw(domain_tags);

CREATE TABLE source_state (
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    last_fetch_at TIMESTAMP,
    last_success_external_id TEXT,
    last_error TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    PRIMARY KEY (source_type, source_id)
);
