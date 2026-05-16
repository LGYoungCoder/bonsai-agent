---
name: harness_workflow
keywords: [复杂任务, 多步骤, 大需求, 跨会话, 流水线, harness, 可恢复, 工程任务, plan-and-execute, pipeline]
created: 2026-05-16
verified_on: 2026-05-16
---

# 复杂多步骤 code 任务用 harness-cli

## 适用范围:**code 场景独占**,委托式开发不是协作开发

**只用于 code 场景**(写代码 / 重构 / 迁移 / 加测试 / 修 bug 流水线)。
写作 / 微信对话 / 知识检索 / 浏览器抓数据 等场景**不要用**这套。

进入前必须用 `ask_user` 跟用户确认意图:
- ✅ **委托式**: "你帮我搞,搞完叫我" → 进 harness
- ❌ **协作式**: "我们一起看看怎么改" → 回退用 `plan_before_big`

## 触发条件(满足任意两条才升级)

- 5 步以上 / 跨 5 个文件以上
- 用户明确说"按流程走 / 走完整审查 / 要可追溯"
- 这次做不完,下次还要继续(跨会话)
- 涉及风险操作(删除 / 迁移 / 审批),需要零信任 lint 校验
- 用户希望"你自己跑,跑完叫我"(委托式语气)

## 跳过(继续用其它 skill)

- 任务 1-2 步能搞定 → 直接做
- 3-5 步 / 单会话 / 协作式 → 用 `plan_before_big`
- 非 code 场景 → 用各自场景的 skill

## 5 个交互节点(必做,不能省)

```
1. 触发审批   ask_user("切到 harness 模式可以吗?预计 N 分钟", ["同意","否决"])
2. plan 审批  drive=confirm 时 ask_user 贴 mission_plan.md 全文 + ["批准","修改","废弃"]
3. 主循环     默认静默,每个 task DONE 时输出一行 "✓ <id> <title> DONE (X/Y)"
4. blocked    ask_user 贴 reason + lastLintReport + ["改合同","跳过","废弃"]
5. 新会话恢复 首句先 `harness-cli active <root>` 扫,有就主动报"你还有 mission 'X' 在 N/M"
```

## 标准启动

```bash
# Mission 根目录约定: ~/.bonsai/missions/<kebab-id>
ROOT=$HOME/.bonsai/missions
MISSION_ID=<kebab-name>
MISSION_DIR=$ROOT/$MISSION_ID
mkdir -p "$ROOT"

# 1. init
harness-cli init "$MISSION_DIR"

# 2. 第一次 drive → action=plan
harness-cli drive "$MISSION_DIR"
```

得到 plan 后:

1. **Planner 阶段** — `file_write` 写 `$MISSION_DIR/mission_plan.md`,含:
   - 需求拆解 + DAG + 每个 step 目标 + 全局测试方案
2. `harness-cli drive "$MISSION_DIR"` → action=confirm/pending_approval
3. **`ask_user` 把 mission_plan.md 全文给用户,候选 ["批准","修改","废弃"]**
4. 用户批准 → `harness-cli drive "$MISSION_DIR" --approve` → action=create_tasks
5. **Planner Phase 5** — `file_write` 写 `$MISSION_DIR/tmp/tasks-draft.json`:
   - 每条 contract.description 必须 ≥ 50 字符
   - acceptanceCriteria 至少 1 条 ≥ 10 字符
6. `harness-cli import "$MISSION_DIR" "$MISSION_DIR/tmp/tasks-draft.json"`
7. `harness-cli register-plan "$MISSION_DIR" "1_1,1_2,..."` (全 id 列表)
8. 给收尾 task 注入最终 lint-plan:
   ```bash
   cat > "$MISSION_DIR/tmp/lint-plan-99_final.json" <<'EOF'
   [{"id":"check","name":"final","command":"pytest -q","scope":["."],"expected":"退出码 0"}]
   EOF
   harness-cli update "$MISSION_DIR" 99_final \
     --lint-plan-file "$MISSION_DIR/tmp/lint-plan-99_final.json"
   ```
9. `harness-cli drive "$MISSION_DIR"` 进入主循环

## 主循环 — 每轮先 drive,绝不自己猜

```bash
DRV=$(harness-cli drive "$MISSION_DIR")
ACTION=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['action'])" "$DRV")
TASK=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('taskId',''))" "$DRV")
```

12 种 action 分派:

| ACTION | 扮演谁 | 关键动作 |
|---|---|---|
| `plan` | Planner | 写 mission_plan.md(罕见,init 后才有) |
| `confirm` | (你) | ask_user 贴 plan,等审批 |
| `create_tasks` | Planner | 写 tasks-draft.json + import + register-plan |
| `inspect_pre` | Inspector | 读 contract → 写 standards(≥30字) + lint-plan → update --status NEGOTIATING |
| `negotiate` | Operator | 读 standards → 写 ack(≥10字,有实质) → update --status IN_PROGRESS |
| `execute` | Operator | start-execution → 改代码 → 写 report(≥30字) → update --status VERIFYING |
| `inspect_audit` | Inspector | lint → 写 feedback(≥20字) + verdict → update --status DONE/FAILED |
| `final_validation` | (你) | lint 收尾 task → 全绿则 update --verdict PASSED |
| `review` | Analyst | 复盘 → 总结教训 → mission-status REVIEWED |
| `archive` | (你) | archive 然后 done |
| `done` | (你) | 退出循环 |
| `blocked` | (你) | ask_user 贴 reason 给用户 |

每个 task 完成后**用人话输出一行进度**:`✓ task_id title DONE (X/Y)`。

## 临时文件传参规约

所有长字段必须走文件,**不能命令行内联**:

```bash
# 1. 拿标准路径
P=$(harness-cli tmp-path "$MISSION_DIR" report 1_1 | python3 -c "import json,sys;print(json.load(sys.stdin)['filePath'])")
# 2. 写内容(用 file_write 工具)
# 3. 引用
harness-cli update "$MISSION_DIR" 1_1 --status VERIFYING --report-file "$P"
```

合法 field: `contract` · `standards` · `ack` · `operator-prompt` · `report` · `inspector-prompt` · `feedback`

## 跨会话恢复(新对话第一件事)

**新会话开始时(尤其用户没提任何 mission),先 active 扫一下**:

```bash
harness-cli active "$HOME/.bonsai/missions"
```

返回非空 → 主动跟用户说:"你还有活跃 mission 'X' 在 N/M,要继续吗?"

## Web 端反馈(自动,无需你操心)

bonsai web UI 有 `/api/missions` + SSE `/api/missions/stream` 端点,
mission 列表实时刷新 + 进度变化弹 toast。你只需正常 `code_run harness-cli ...`。
ask_user 的候选项会渲染成按钮(WebSocket `kind=ask_user` 协议)。
任务 DAG 用 mermaid 渲染,mission_plan.md 用 marked 渲染。

## 模板复用

`skills/L3_samples/harness_templates/` 下有常见任务模板:

| 模板 | 适用 |
|---|---|
| `refactor_module.md` | 模块拆分/合并/重命名 |
| `migrate_storage.md` | 数据存储迁移(双写 + backfill) |
| `add_test_coverage.md` | 补单元测试,目标覆盖率 |

写 mission_plan 前先 `file_read` 这些模板,**抄骨架**比从零写快 5 倍。

## 关键工具(harness 场景必用)

| 工具 | 何时用 |
|---|---|
| `code_search` | Planner 找 caller / Inspector 验影响范围 / Operator 找 import |
| `working_memory` set/append/get | 长 task 跨 turn 的状态 (inventory / 调用方清单 / 边界笔记) |
| `pytest_run` | Inspector audit / Operator 自检,结构化返回 fail traceback |
| `git_ops` | 每个 task 完成时 commit(message="task <id>: <title>") |
| `file_write` mode=patch | 改大文件只写 diff,不重写全文 |

## 渐进式 lint-plan (新)

mission 级 lint(tsc / ruff / build)用 `harness-cli update-mission-lint-plan`
设到 `mission.globalLintPlan`,task 级只加增量。task lint = global ∪ task。

```bash
# 初始化 mission 后:
cat > "$MISSION_DIR/tmp/global-lint.json" <<'EOF'
[{"id":"ruff","name":"ruff","command":"ruff check","scope":["."],"expected":"退出码 0"},
 {"id":"pytest","name":"pytest","command":"pytest -q","scope":["."],"expected":"退出码 0"}]
EOF
harness-cli update-mission-lint-plan "$MISSION_DIR" \
  --lint-plan-file "$MISSION_DIR/tmp/global-lint.json"
```

之后每个 task 的 lint-plan 自动继承,task 只写自己的增量。

## Contract 调整(不用废 task)

跑到一半发现 task 3 的 contract 描述偏了?用 `patch-contract`:

```bash
cat > "$MISSION_DIR/tmp/new-contract.md" <<EOF
更准确的描述,至少 50 字符...

- acceptance 1
- acceptance 2
EOF
harness-cli patch-contract "$MISSION_DIR" 3_xxx \
  --contract-file "$MISSION_DIR/tmp/new-contract.md"
```

只能 patch 非 DONE/DLQ 的 task,task 状态不重置。

## Analyst review 自动入 memory

review 阶段(action=review)的产出**必须沉淀到 memory**,否则下次新 mission
完全不知道这次的教训。固定流程:

```
1. 读 mission + 所有 tasks (get + 遍历)
2. 提炼 3-5 条"以后还会撞到的教训"
3. 对每条:
     - 写一个 file_write 到 ~/.claude/projects/.../memory/feedback_<topic>.md
       (or 通过 bonsai 的 memory_store API)
     - 用 ask_user 让用户确认要不要持久化(避免噪音)
4. mission-status REVIEWED
```

新 mission 启动时 Planner 第一步: `memory_search "<相关关键词> harness"`,
读出过往教训填进 plan 的"已知坑"段落。

## 反模式(踩过的坑)

- ❌ 用 file_write 直接改 tasks.json / mission.json — CLI 会发现并报错
- ❌ 写脚本调 harness-cli update 来"自动通过 lint" — 验证脚本不得调 update/drive
- ❌ contract 只写一行标题就 import — schema 校验会拒绝
- ❌ verdict PASSED 但没跑 lint — CLI 自动跑了发现失败,会强制降级
- ❌ 导入 task 时 deps 引用了不存在的 id — CLI 拒绝
- ❌ 进 harness 前没 ask_user 确认 → 用户突然被 30 分钟流水线绑架
- ❌ blocked 不报告原因 → 用户被晾着

## 引用

- 二进制: `/usr/local/bin/harness-cli` (源码 `/opt/lg/cli/`)
- 完整命令参考: `/opt/lg/cli/README.md`
- 集成说明: `/opt/lg/cli/INTEGRATION.md`
- Mission 根目录: `~/.bonsai/missions/<kebab-id>/`
