---
name: autonomous_mode
keywords: [自主, 无人值守, todo, 离线, 夜间任务, 自主运行]
created: 2026-04-22
verified_on: 2026-04-22
---

# 用户不在线 · 按 TODO 自己动手

## 触发

你收到的 prompt 是类似 "按 autonomous_mode.md 协议跑一轮" 或 "处理 TODO 清单里的一条"。
这种场景下用户通常不在,决策要靠你自己,但要**踩红线**就停下来写报告待审。

## 工作区

全部文件在 `data/autonomous/` 下:

- `todo.md` — 用户维护的清单,`- [ ] <desc>` 是待做,`- [x]` 是已完成
- `history.txt` — 最新一行在最上面,格式 `<date> | <title> | <one-line-outcome>`
- `reports/R<NN>_<slug>.md` — 每次跑完写一份,编号自增

## 一轮的 6 步

### 1. 摸状态

```
file_read("data/autonomous/todo.md")
file_read("data/autonomous/history.txt")   # 看最近 20 行避免重复
```

### 2. 挑 1 条

选择标准(按优先级):

1. 从来没在 history 里出现过的条目 > 出现过的
2. "价值公式":**AI 训练数据覆盖不到 × 对未来协作有持久收益**(这条最重要)
3. 能在 ≤20 回合内小步验证的 > 开放式无限大的
4. **不要**连续两次选同一个子任务;连续 3 次失败的条目也跳过,标注进报告让用户处理

挑完的条目用 `update_working_memory` 记下来,防止长任务中途忘了主线。

### 3. 执行前写计划

参考 `plan_before_big.md`:3 步以上必须先拆步骤再动手。计划写进 working memory,不必写文件。

### 4. 小步快跑,边试边调

- ≤30 回合为硬上限
- 每完成一步先验证再往下(参考 `verify_before_report.md`)
- 失败也是有价值的数据 — 写清"试了 A,因为 B 失败了"

### 5. 写报告(回合结束前的最后一件事)

先算报告路径。已有 `R01_*`、`R02_*`...就用下一个编号:

```python
# code_run 里跑
from bonsai.autonomous import AutonomousWorkspace
from pathlib import Path
w = AutonomousWorkspace(Path("."))
path = w.next_report_path("本次任务一句话标题")
print(path)
```

报告格式:

```markdown
# <一句话标题>

- 耗时: <N 回合>
- 结论: 完成 / 部分完成 / 失败 / 需要用户决策
- 条目: (贴 todo.md 里那一条原文)

## 做了什么

1. 第一步结果...
2. ...

## 发现

- 意外 1
- 意外 2

## 遗留 / 建议

- [ ] 用户需要处理的东西
- 建议添加到 skills/L3 的模式: ...
```

`file_write` 到上一步算出来的 path。

### 6. 更新 history + 标记已完成

```python
# code_run
from bonsai.autonomous import AutonomousWorkspace
from pathlib import Path
import time
w = AutonomousWorkspace(Path("."))
line = f"{time.strftime('%Y-%m-%d %H:%M')} | <一句话标题> | <结论一句话>"
w.append_history(line)
# 如果完整做完了这条,打勾:
if done_completely:
    w.mark_item_done("<todo 里那条的开头 20 字>")
```

## 权限 / 红线

**自由做**:
- 只读探测(grep / file_read / web_scan / curl / code_run 只读)
- cwd 内的临时脚本实验
- 写 `data/autonomous/reports/*.md`
- 写 `data/autonomous/history.txt`
- 在 todo.md 里打勾(不要改描述,不要删除条目)

**报告待审(写报告但不动手)**:
- 修改 `skills/L3/*.md` 里已有的 SOP
- 修改 `config.toml`
- 调用要花钱的外部 API(付费 LLM 之外的)
- 安装新依赖

**绝对禁止(停下来,报告说"需要用户")**:
- 读 / 打印 / 往外传任何 API key(包括 `*.env` / keyring)
- 改 bonsai 核心代码(`bonsai/**/*.py`)
- 不可逆的:`rm -rf`、数据库 DROP、`git push --force`、清空 `memory/` 或 `skills/` 任何子目录
- 往外发消息(Slack / email / 渠道 bot)—— 没人授权

## 不要做

- ❌ 瞎挑宏大目标:"重构整个 bonsai 让它更快"(用户来挑选这种,不是你)
- ❌ 冲刺式做完:一轮 60 回合(会撞 budget 炸)
- ❌ 报告写模糊:"试了一下" — 要具体:工具 / 输入 / 输出 / 结论
- ❌ 偷偷改用户代码不记录:所有改动都进报告
- ❌ 不打勾就说"完成了" — history 是你跟用户的唯一信任界面
