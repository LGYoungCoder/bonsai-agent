# Mission 模板:补充测试覆盖

适用场景: 给现存代码补单元测试,目标覆盖率 X%,不改业务逻辑。

## mission_plan.md 骨架

```
目标
----
把 <模块> 的测试覆盖率从 <当前> 提到 <目标>,新增测试不破坏现有行为。

为什么
----
- <模块> 是核心 / 经常被改 / 修过 N 个 bug,需要回归保护

边界
----
- 不改业务逻辑(除非发现明显 bug,记录到 BUGS.md 不立即修)
- 不引入新依赖(用 stdlib pytest)
- 不追求 100%,优先 critical path
```

## tasks-draft.json 骨架(5 task)

```json
[
  {"id":"1_baseline","title":"测量当前覆盖率","deps":"",
   "contract":{"description":"跑 pytest-cov 拿到当前覆盖率基线,标出未覆盖的核心函数",
                "acceptanceCriteria":["产出 coverage_baseline.txt","核心函数清单 in working_memory"]}},
  {"id":"2_happy_path","title":"happy-path 测试","deps":"1_baseline",
   "contract":{"description":"给每个 public 函数写 happy-path 测试,正常输入 → 正常输出",
                "acceptanceCriteria":["每个 public 函数 ≥ 1 测试","新测试全绿"]}},
  {"id":"3_edge_cases","title":"边界 + 错误路径","deps":"2_happy_path",
   "contract":{"description":"补 None / 空 / 越界 / 异常的测试用例",
                "acceptanceCriteria":["关键函数 ≥ 3 测试","覆盖率提升 >= X%"]}},
  {"id":"4_integration","title":"端到端集成测试","deps":"3_edge_cases",
   "contract":{"description":"跑通典型 workflow 的 e2e(允许 fixture 共用)",
                "acceptanceCriteria":["e2e 全绿","场景列出在 README 测试章节"]}},
  {"id":"99_final","title":"覆盖率验证 + commit","deps":"4_integration",
   "contract":{"description":"重新跑 coverage,确认达标并落 commit",
                "acceptanceCriteria":["覆盖率达到目标","pytest 全绿","coverage report 落 docs/"]}}
]
```

## 收尾 lint-plan 建议

```json
[
  {"id":"pytest","name":"pytest+cov","command":"pytest -q --cov=<module> --cov-fail-under=<X>","scope":["."],"expected":"退出码 0"}
]
```

## 关键纪律

- **发现 bug 不修**(记 BUGS.md),修 bug 是另一个 mission
- **优先覆盖核心 path**,90% 的价值在 10% 的代码
- **新测试不依赖外部环境**(用 tmp_path / 假数据)
