# 衣物导入发布与历史数据核对

## 发布顺序

1. 在获准的维护窗口应用 Alembic `0053_harden_import_pipeline`。新增字段为 `import_tasks.run_generation`、`import_tasks.retryable`、`wardrobe_items.embedding_source_version` 和 `item_embeddings.source_version`。旧记录从 generation 0 开始，其他新增字段为空。
2. 停止旧 worker 领取导入、商品图和向量任务。等待正在执行的任务完成，或让新 worker 接手已过期领取。旧 worker 会按旧方式解析来源并写结果，升级期间避免两代 worker 并行领取此链路。
3. 发布同一提交的 API 和 worker，生产保持 `ENABLE_IMPORT_ASYNC_JOBS=true`。确认 worker 注册 `import_drain`、`garment_crop_drain`、`cutout_drain`、`embedding_drain`、领取恢复和 dead-letter 结算。
4. 用测试账户按 CI 的 A23 顺序验证导入、分批确认、补图、首套和重新读取。检查队列和额度终态，再恢复完整流量。

回退应用时可先保留新增数据库字段。旧 worker 不能处理 `garment_crop`，且不能按新来源身份处理向量与重试任务。回滚前应暂停新链路并让新 worker 处理已接受的工作。若必须停在旧版，保留待处理队列和对象，恢复新 worker 后继续。删除新字段前先停用新应用并确认新任务已处理。回退迁移会丢失版本和重试标记。

## 历史数据

已有原图与商品图先走读取时的兼容解析和重新签名。跨账户或当前存储适配器不支持的历史对象由运营核对。历史向量缺少来源版本时走无向量推荐与重复提醒。只有能证明是真实单件原图或该候选裁剪的来源才适合重建。历史付费生成不自动重排。

在获准环境中使用测试账户或经过授权的小范围账户批次。以下查询需设置 psql 变量 `user_id` 和 `from_time`，每次上限为 200 条。

```sql
SELECT t.id, t.input_type, t.task_status, t.input_payload ? 'source_object_key' AS has_source_key
FROM import_tasks AS t
WHERE t.user_id = :'user_id'::uuid
  AND t.created_at >= :'from_time'::timestamptz
ORDER BY t.created_at DESC, t.id DESC
LIMIT 200;

SELECT i.id, i.embedding_source_version, e.model_name, e.source_version
FROM wardrobe_items AS i
LEFT JOIN item_embeddings AS e ON e.item_id = i.id
WHERE i.user_id = :'user_id'::uuid
  AND i.deleted_at IS NULL AND i.created_at >= :'from_time'::timestamptz
ORDER BY i.created_at DESC, i.id DESC
LIMIT 200;
```

用当前存储适配器的解析规则确认主机、桶与账户范围后，才可为已有图片补写对象 key。重建向量时同事务设置期望来源版本并排新任务。对孤儿对象只做账户前缀下的清单核对，检查候选、衣物、派生记录和在途任务，并留出超过重试及领取周期的时间窗口。任何回填、重算或对象删除均由用户将范围核实后另行执行。

真实供应商、私有桶、线上性能与现网历史数据需要在获准环境复验。CI 使用合成图片和受控故障，不产生付费生成调用。
