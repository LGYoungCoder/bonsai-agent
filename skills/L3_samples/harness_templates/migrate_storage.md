# Mission 模板:数据存储迁移

适用场景: 把数据从存储 A 迁到存储 B(sqlite → postgres / 文件 → DB / schema 升级)。

## mission_plan.md 骨架

```
目标
----
把 <数据集> 从 <源存储> 迁到 <目标存储>,零数据丢失,可回滚。

为什么
----
- <现状不可持续: 性能 / 并发 / 容量>
- <目标的优势>

迁移策略
----
- [ ] 双写期: A 仍然主,B 同步写,读 A
- [ ] 切读: 验证 B 数据一致后,读切 B
- [ ] 停写 A:确认 30 天稳定
- [ ] 删 A

回滚方案
----
任一阶段失败:停止双写 + 切回纯 A 模式。
```

## tasks-draft.json 骨架(7 task)

```json
[
  {"id":"1_schema","title":"在 B 上建 schema","deps":"",
   "contract":{"description":"在目标存储上创建表/集合 + 索引,允许空表",
                "acceptanceCriteria":["schema 部署成功","索引列出"]}},
  {"id":"2_dual_write","title":"加双写路径","deps":"1_schema",
   "contract":{"description":"在写 A 的同时写 B,B 写失败只 log 不阻断",
                "acceptanceCriteria":["代码新增双写","A 写流量不受 B 影响"]}},
  {"id":"3_backfill","title":"backfill 历史数据","deps":"2_dual_write",
   "contract":{"description":"用脚本把 A 的现存数据写到 B,分批 + 幂等",
                "acceptanceCriteria":["脚本可恢复中断","backfill 完成报告记录到 logs/"]}},
  {"id":"4_consistency","title":"一致性校验","deps":"3_backfill",
   "contract":{"description":"对比 A B 的 count + checksum,差异列表",
                "acceptanceCriteria":["count 一致 ±0","checksum 差异 < 0.01%"]}},
  {"id":"5_read_switch","title":"切读取到 B","deps":"4_consistency",
   "contract":{"description":"读路径从 A 切到 B,A 保留作为 fallback",
                "acceptanceCriteria":["读流量 100% 到 B","fallback 路径仍可用"]}},
  {"id":"6_monitor","title":"灰度观察","deps":"5_read_switch",
   "contract":{"description":"加监控指标:B 错误率 / 延迟,定 7 天阈值",
                "acceptanceCriteria":["监控指标已上线","告警阈值已配"]}},
  {"id":"99_final","title":"文档 + commit","deps":"6_monitor",
   "contract":{"description":"写迁移完成文档 + 回滚步骤 + commit",
                "acceptanceCriteria":["MIGRATION.md 落地","回滚步骤可逐条执行"]}}
]
```

## 关键纪律

- **双写期必须有**(2_dual_write),不能直接切
- **一致性校验是 gate**(4_consistency),通不过不切读
- **A 的删除不在本 mission**(留给 30 天后的下个 mission)
