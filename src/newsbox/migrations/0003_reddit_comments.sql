-- s13-reddit-comments-enrich：reddit 评论富化产出表
--
-- 设计要点（详见 ai/sprints/active/s13-reddit-comments-enrich/DECISIONS.md）：
--   D2 评论入独立表，fk → articles_raw.id，保留采集层"原料路线"
--   D3 帖子级元信息（score/upvote_ratio/num_comments/flair）拼进 articles_raw.body
--       头部 markdown 引用块，不在此表
--   D6 入库前过滤：kind == 't1' / author 非 AutoModerator|[deleted] / body 非 [deleted]|[removed]
--   UNIQUE(article_id, comment_id) 让重复 enrich 幂等

CREATE TABLE reddit_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles_raw(id),
    comment_id TEXT NOT NULL,          -- reddit 给的 t1_xxx
    parent_id TEXT,                    -- 顶层评论时 = 帖子 t3_xxx
    author TEXT NOT NULL,
    score INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_utc TIMESTAMP,             -- reddit 不一定给；缺失允许 NULL
    rank INTEGER NOT NULL,             -- 过滤后 score desc 的位置，1-indexed
    UNIQUE(article_id, comment_id)
);

CREATE INDEX idx_reddit_comments_article ON reddit_comments(article_id);
