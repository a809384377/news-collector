-- s1-schema-cleanup §5：articles_raw 走"原料型"路线 — status / last_error 是死字段
--   • 代码实际只写 status='fetched'（失败的根本不进 articles_raw）
--   • 失败信息全部走 source_state.last_error / consecutive_failures（按信源记账）
--   • 留着会让 doctor / state CLI 在"读哪边"上反复纠结

-- 同步清理两个无效索引：
--   • idx_status：所有值 = 'fetched'，0 区分度
--   • idx_domain：建在 JSON 列上，json_each 查询走全表扫描，索引从未被利用

-- ⚠️ SQLite 要求：删列前必须先删依附在该列上的索引
DROP INDEX IF EXISTS idx_status;
DROP INDEX IF EXISTS idx_domain;

-- SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN
ALTER TABLE articles_raw DROP COLUMN status;
ALTER TABLE articles_raw DROP COLUMN last_error;
