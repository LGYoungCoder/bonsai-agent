# Mission 模板:重构模块

适用场景: 把一个大模块拆/合/移,保持外部行为不变。

## mission_plan.md 骨架

```
目标
----
把 <模块>(行数 / 当前位置) 重构成 <目标形态>,保持外部 API 不变。

为什么
----
- <现状的问题: 太大 / 跨职责 / 难测试>
- <重构后的好处: 单一职责 / 可独立测试 / 复用>

边界
----
- 不改:<列出 import 你的下游模块,这些不动>
- 改:<列出你要拆/合/移的内部细节>

回归保证
----
- 单元测试覆盖所有 public API
- 类型检查全绿
- 现有调用方零改动
```

## tasks-draft.json 骨架(6 task,典型)

```json
[
  {"id":"1_inventory","title":"清点现状","deps":"",
   "contract":{"description":"用 code_search 扫描所有 import / caller,列出模块边界 + 接口表(>=50字)",
                "acceptanceCriteria":["产出 inventory.md 列出 public symbols 和 caller","调用方文件清单"]}},
  {"id":"2_tests_first","title":"补充行为冻结测试","deps":"1_inventory",
   "contract":{"description":"在重构前补足 public API 的回归测试,确保改后能立刻发现破坏",
                "acceptanceCriteria":["pytest 命中 >= 80% public API","新增的 test_*_before.py 全绿"]}},
  {"id":"3_split","title":"分块拆分","deps":"2_tests_first",
   "contract":{"description":"按 inventory 把模块拆成子模块,只移动代码不改逻辑",
                "acceptanceCriteria":["新文件结构落地","所有现存测试仍然绿"]}},
  {"id":"4_caller_update","title":"更新调用方 import","deps":"3_split",
   "contract":{"description":"如果 public API 路径变了,改所有 caller import",
                "acceptanceCriteria":["所有 caller import 已更新","grep 不到旧路径"]}},
  {"id":"5_cleanup","title":"清理死代码 + 重复","deps":"4_caller_update",
   "contract":{"description":"重构后梳理死代码 / 重复函数,合并相同实现",
                "acceptanceCriteria":["无被注释的代码","无未使用的 helper"]}},
  {"id":"99_final","title":"全量回归 + commit","deps":"5_cleanup",
   "contract":{"description":"跑所有测试 + lint + type check + commit + 写迁移说明",
                "acceptanceCriteria":["pytest 全绿","ruff 0 issue","新增 MIGRATION.md(如 API 变)"]}}
]
```

## 收尾 lint-plan 建议

```json
[
  {"id":"pytest","name":"pytest","command":"pytest -q","scope":["."],"expected":"退出码 0"},
  {"id":"ruff","name":"ruff","command":"ruff check","scope":["."],"expected":"退出码 0"},
  {"id":"mypy","name":"type","command":"mypy <module>","scope":["."],"expected":"退出码 0"}
]
```

## 关键纪律

- **行为冻结测试先写**(2_tests_first),否则重构是裸奔
- **拆分阶段只移动代码,不改逻辑**(3_split),逻辑改放后续 mission
- 收尾 commit 前 `git_ops status`,确认没有意外暂存
