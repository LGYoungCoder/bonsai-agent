# Bonsai Roadmap

> 从种子到小树的 5 阶段路线图。
>
> **状态**: 🌱 Seedling（种子期 · 仅设计文档）

## 原则

1. **每个 Sprint 必须端到端可用**，不许出现"半成品堆着等下个 Sprint 连起来"
2. **每个 Sprint 完成后跑一遍 benchmark**，token 用量不允许回归
3. **文档 > 代码**。先写文档对齐思路，再动手写代码
4. **Invariants（见 ARCHITECTURE.md）永远不能破**，否则回退 PR

## 成长阶段命名

- 🌱 **Seedling**（种子期）：v0.0，只有设计文档
- 🌿 **Sprout**（萌芽期）：v0.1，基础设施 + 最小闭环
- 🌱 **Sapling**（树苗期）：v0.2，双库记忆系统
- 🌳 **Young Tree**（幼树期）：v0.3，工具精度 + 浏览器 alpha
- 🌲 **Growing**（生长期）：v0.4+，长会话 + 生态

---

## Sprint 1 — 基础设施（2 周）→ v0.1 🌿

### 目标

单用户能在 REPL 跑起来，基础 token 纪律就位。**任何更复杂的能力都建在这个地基之上**。

### 交付物

#### 核心模块
- [ ] `core/backend.py` — 统一 Backend Protocol + FrozenPrefix / DynamicTail 抽象
- [ ] `core/adapters/` — 至少覆盖 5 个 provider：
  - [ ] ClaudeAdapter（显式 cache_control，4 层 breakpoint）
  - [ ] OpenAIAdapter（自动 cache）
  - [ ] QwenAdapter（含温度 clamp）
  - [ ] GLMAdapter（自动 cache）
  - [ ] MiniMaxAdapter（含 M2.7 温度 clamp）
- [ ] `core/loop.py` — stateless agent loop，~100 行
- [ ] `core/handler.py` — 基础 Handler 类 + 5 个核心工具的 do_*
- [ ] `core/session.py` — per-conversation state（cwd / turn counter，history 在 backend）
- [ ] `core/budget.py` — token 估算 + 压缩（B-1 基础版）
- [ ] `core/cache_monitor.py` — 按 provider 分统计命中率

#### 工具（5 个）
- [ ] `file_read` (行号 + 关键词定位 + 20K 截断)
- [ ] `file_write` (patch / overwrite / append 三种模式，patch 强制唯一匹配)
- [ ] `code_run` (Python / bash，stdout 截断 + 存盘)
- [ ] `memory_search` (先返回 stub，实际连接到 Sprint 2)
- [ ] `ask_user`

#### Parallel tool use
- [ ] System prompt 里明确鼓励并行
- [ ] Handler `dispatch_batch` 支持并发（`asyncio.gather`）
- [ ] 冲突检测：同文件 write / 同目录 code_run 串行化

#### 运行形态
- [ ] CLI REPL（`python -m bonsai`）
- [ ] Config 文件 `config.toml` 支持多 provider + failover 链

#### 测试
- [ ] 单元测试覆盖 Handler / Budget / Cache Monitor
- [ ] 集成测试：golden transcripts 录 3 条真实对话，每次 PR 回放
- [ ] `benchmarks/cache_probe.py` 跑通 5 个 provider

### 验收标准

| 项目 | 标准 |
|---|---|
| 基础功能 | REPL 能跑完"读文件 → 改文件 → 执行代码"三件套 |
| Parallel tool use | 读 3 个文件任务实测 1 轮完成（非 3 轮） |
| Cache 命中 | Claude / GLM / Qwen 命中率 ≥ threshold |
| Failover | 主 provider 人为断网，2 秒内切到备用 |
| Token 纪律 | 前缀稳定性 CI 检查通过 |
| 代码量 | Ring 0 ≤ 1200 行 + Sprint 1 的 5 个 adapter ≤ 800 行 |

### 不做

- ❌ 浏览器工具（留给 Sprint 4）
- ❌ 持久化记忆（留给 Sprint 2）
- ❌ 结构感知截断（留给 Sprint 3）
- ❌ Planner/Executor 分离（留给 Sprint 5）

---

## Sprint 2 — 双库记忆系统（3 周）→ v0.2 🌱

### 目标

SkillStore + MemoryStore 最小可用，异步写入链路打通。Agent 跑第二次时能"记得"上次的事。

### 交付物

#### SkillStore
- [ ] `stores/skill_store.py` — 文件系统 + L1 索引 + 关键词查询
- [ ] `skills/L1_index.txt` 的格式规范 + 自动维护脚本
- [ ] Evidence 纪律：`write_sop(name, content, evidence)` 必须带成功 tool_calls 证据
- [ ] `skill_lookup` 工具接入 agent loop

#### MemoryStore
- [ ] `stores/memory_store.py` — sqlite-vec + SQLite 主表
- [ ] 数据模型：Wing / Room / Drawer / Closet 四层
- [ ] Embedding：默认 `bge-m3`（本地），可切换 `text-embedding-3-small`（云端）
- [ ] 混合检索：BM25 + 向量 + 时间邻近 boost（Hybrid v4 思路）
- [ ] `memory_search` / `memory_recall` 两个工具

#### Wake-up 层
- [ ] `core/wakeup.py` — L0 identity + L1 essentials 合成
- [ ] 硬卡 ≤ 1K tokens
- [ ] 注入到 FrozenPrefix，保 cache 稳定

#### Background Writer
- [ ] `writer/session_archiver.py` — 会话结束后异步归档
- [ ] `writer/drawer_ingester.py` — 切分 + 去噪 + embedding + 入库
- [ ] 触发方式：`subprocess.Popen` fire-and-forget（不阻塞主 loop）
- [ ] 幂等：中断再跑不重复入库（基于 content_hash）

#### CLI
- [ ] `bonsai init ./my-project` — 初始化记忆目录
- [ ] `bonsai mine <dir>` — 批量扫入已有文件
- [ ] `bonsai search <query>` — 独立查询 MemoryStore
- [ ] `bonsai wake-up` — 打印当前 L0+L1

### 验收标准

| 项目 | 标准 |
|---|---|
| 记忆隔离 | SkillStore / MemoryStore 完全独立，互不写入 |
| Wake-up 预算 | L0+L1 总 token ≤ 1K |
| Verbatim 保真 | MemoryStore 入库内容 `content == source`，SHA256 一致 |
| 异步写入 | Agent 结束响应 ≤ 500ms（写入已在后台） |
| Evidence 纪律 | 无 evidence 调 `write_sop` 必须报错拒绝 |
| 召回 | 小规模集（100 drawers）检索 R@5 ≥ 80% |

### 风险

- **sqlite-vec 稳定性**：如果遇到严重 bug，降级到 `lancedb`
- **本地 embedding 性能**：bge-m3 在 CPU 上慢，加 `--embed-remote` 选项走云端

---

## Sprint 3 — 工具精度压榨（2 周）→ v0.2.1 🌱

### 目标

F1 结构感知截断 + B2 语义去重。同样任务 token 消耗相比 Sprint 2 再降 30%+。

### 交付物

#### F1 结构感知截断
- [ ] `core/smart_format.py` 重构 —— 类型感知 dispatch：
  - [ ] JSON：保留 schema + 数组取前 N
  - [ ] CSV / TSV：header + 前 20 行 + per-column 统计
  - [ ] Code / logs：头尾 + 所有 ERROR/WARN 行
  - [ ] HTML：走 Sprint 4 的 AX tree（暂时 placeholder）
  - [ ] 其他文本：头尾折叠
- [ ] 每个类型单独 benchmark，确保不丢关键信息

#### B2 语义去重
- [ ] `core/history_compressor.py` 升级：
  - [ ] 同 args hash 的工具调用合并（`file_read` 同文件只留最后一次完整版）
  - [ ] 连续同命令（`git status` × 5）折叠成 `[repeated N times, last result: ...]`
  - [ ] 引用追踪：分析模型输出是否 cite 了前面的 tool_result

#### F2 Query-aware truncation（试验性）
- [ ] `file_read` / `code_run` / `memory_search` 加可选 `interest_hint` 参数
- [ ] 截断时优先保留与 hint BM25 相关度高的行
- [ ] System prompt 里引导模型使用

#### 工具结果落盘机制规范化
- [ ] 统一落盘目录 `temp/tool_artifacts/<session_id>/`
- [ ] `[saved to PATH]` 引用模型可以 `file_read` 回来
- [ ] Session 结束时自动清理

### 验收标准

| 项目 | 标准 |
|---|---|
| F1 正确性 | JSON / CSV / log 截断后模型仍能回答 golden 问题集 80% |
| B2 效果 | 同一任务（读 5 文件 3 次，查 git status 10 次）token 省 ≥ 40% |
| F2 效果 | 带 interest_hint 的大文件读取，关键信息保留率 ≥ 95% |
| 整体 | 重跑 Sprint 1 的 golden transcripts，token 总用量降 ≥ 30% |

---

## Sprint 4 — 浏览器 Alpha（3 周）→ v0.3 🌳

### 目标

浏览器任务的大 alpha。**一次到位**——不是在 DOM 上继续改良，而是换到 AX tree 赛道。

### 交付物

#### CDP 集成
- [ ] `tools/browser/cdp_client.py` — 直接用 `websockets` 连 CDP，避开重依赖
- [ ] 支持连接现有 Chrome（`--remote-debugging-port`）保登录态
- [ ] 备选：Playwright 的 `aria_snapshot`（如果 CDP 维护成本太高）

#### E1 AX Tree 抽取
- [ ] `tools/browser/ax_tree.py` — 调用 `Accessibility.getFullAXTree`
- [ ] 转换为 LLM-friendly 文本格式：
  ```
  [a1] main
    [a2] heading "搜索结果"
    [a3] listbox "排序"
      [a4] option "价格升序" selected
      [a5] option "销量" 
    [a6] button "应用筛选"
  ```
- [ ] 实测对比 DOM：同页面 token 数下降 ≥ 10x

#### E2 元素 ID 系统
- [ ] `tools/browser/element_pool.py` — 每次 scan 时分配短 ID（a1, a2...）
- [ ] ID → CDP node ID 映射（server-side，模型看不到）
- [ ] 操作工具（`web_click`, `web_type`, `web_scroll`）接受 ID 而非 selector
- [ ] ID 过期策略：重新 scan 后旧 ID 失效，避免误操作

#### Progressive Disclosure
- [ ] `web_scan` 默认只返回页面大纲（headings + landmarks）
- [ ] `web_scan(scope="a3")` 展开指定 section
- [ ] `web_scan(full=True)` 强制全量（紧急逃生门）

#### 工具整合
- [ ] `web_scan` / `web_execute_js` 改造完成
- [ ] 新增 `web_click(id)` / `web_type(id, text)` / `web_scroll(direction)` 复合工具（走 C-3 门槛）

#### 可选
- [ ] Vision fallback：验证码 / 视觉布局任务自动切截图模式
- [ ] DOM diff：只返回和上次 scan 的差异

### 验收标准

| 项目 | 标准 |
|---|---|
| Token 节省 | 10 个常见任务（搜索、登录、填表、列表抓取等）token 消耗 ≤ 现有方案 1/5 |
| 可靠性 | 在主流网站（淘宝、B 站、GitHub、Google、微信公众号）能完成基础操作 |
| 延迟 | AX tree 抽取 + 转换 ≤ 300ms |
| 登录态 | 连 Chrome 用户 profile 保留登录 session |

### 风险

- **AX tree 某些网站不完整**：部分重度 JS 渲染的网站 AX tree 缺失语义。加降级：AX 不完整 → 退回简化 DOM
- **网站反爬**：真实 Chrome 连接比 headless 好，但仍可能触发检测。不在 Bonsai 层面对抗反爬

---

## Sprint 5 — 长会话优化（2 周，按需）→ v0.4 🌲

### 目标

Session 跑到 50+ 轮也不崩。支持"昨天聊过的继续聊"。

### 交付物

#### B1 引用追踪
- [ ] `core/history_compressor.py` 再升级：
  - [ ] 分析模型输出的 token-level 引用（BM25 相关度 > 阈值视为被引用）
  - [ ] 被引用的轮次保留完整，未引用的激进压缩
  - [ ] 多轮未引用 → 迁移到 MemoryStore 的 L4 归档

#### L4 会话归档
- [ ] `writer/session_compactor.py` — 超过 40 轮的会话提取 AAAK 摘要
- [ ] 摘要 + 原文 drawer 都入 MemoryStore
- [ ] `/continue` 命令：列出可恢复会话 + 按时间排序
- [ ] `/continue N` 命令：加载第 N 个会话的摘要 + 最后 3 轮原文

#### 跨 Session Cache 复用（实验）
- [ ] Claude 1h cache beta 测试：前缀用 1h TTL，测实际命中率
- [ ] Kimi 的长 cache：预创建包含 sys + tools + L1 的 cache，跨 session 复用

#### Planner / Executor 分离（试点）
- [ ] `core/dual_model.py` — 配置 planner / executor 两个 backend
- [ ] 策略：
  - 新任务第一轮 → Planner（贵模型，输出 2-5 步计划）
  - 后续执行 → Executor（便宜模型）
  - 执行连续失败 2 次 / 步骤偏离计划 → 回调 Planner
- [ ] 实测 Opus（Planner）+ Haiku（Executor）组合对比单模型 Sonnet 的成本差

### 验收标准

| 项目 | 标准 |
|---|---|
| 长会话稳定性 | 50 轮对话无崩溃，cache 命中率 ≥ 70% |
| 上下文保真 | 第 50 轮还能正确引用第 5 轮的信息 |
| Session 恢复 | `/continue 1` 恢复昨天的对话，新回合能接上下文 |
| Planner/Executor | 同一任务成本降 ≥ 50%（对比单 Sonnet） |

---

## Sprint 6+ — 待定（随用户实际使用反馈定）

可能方向：
- **MCP server 模式**：把 Bonsai 能力以 MCP 工具暴露给 Claude Code / Cursor
- **语言支持**：Python 之外的 code_run（Node / Go / Rust）
- **多模态**：图片 / 语音输入输出
- **移动端控制**：ADB / iOS UI 自动化
- **Skill 市场**：社区分享 SOP 的机制

**但不为了"功能完整"而加功能**——每个新方向必须有至少 3 个真实用户场景驱动。

---

## 不变量

详见 [ARCHITECTURE.md](ARCHITECTURE.md#不变量invariants)。每个 Sprint 完成都要过一遍这 7 条;破一条的 PR 直接回退。**进度可以延期,纪律不能破**。

---

## 成本预期

按 [ARCHITECTURE 的三环模型](ARCHITECTURE.md#模块清单--代码规模预算) 展开:

| Sprint | 估时 | Ring 0 | Ring 1 | Ring 2 | Ring 3 | 累计 | 交付 |
|---|---|---|---|---|---|---|---|
| 1 | 2 周 | +1200 | — | +800 (5 backend) | — | **~2000** | 能对话 |
| 2 | 3 周 | +200 | +1600 | +300 (CLI/Web/MCP) | — | **~4100** | 能记忆 |
| 3 | 2 周 | +400 | +200 | — | — | **~4700** | 能省 token |
| 4 | 3 周 | — | — | — | +700 | **~5400** | 能网页 |
| 5 | 2 周 | +200 | — | +2000 (6 IM) | — | **~7600** | 能聊天平台 |
| 6+ | 按需 | — | — | — | 独立 | 7600+ | 能扩展 |

**v0.4 总计 ≈ 7600 行**——这个预算容纳 Agent 主循环 + verbatim memory + 多 provider + 多前端 + 浏览器,每一项都有完整实现,没有占位 stub。

**Bonsai 的定位**:把以上所有能力做进**克制**的代码量里。克制的含义是**"每一行都有理由"**,不是"少到无法运作"。

**真正的红线**:
- Ring 0 + Ring 1(本体)≤ **3800 行**:破线必须开 RFC
- Ring 2 按 provider/frontend 数线性增长:**可以接受,因为是 leaf 层**
- 总 v0.4 ≤ **8000 行**:破线说明抽象失败,要重构

---

## 当前状态

- [x] MISSION / PHILOSOPHY / ARCHITECTURE / MEMORY / TOKEN_BUDGET / TOOLS / BACKENDS / ROADMAP 文档
- [ ] Sprint 1 未开工

**下一步**：开工 Sprint 1 前先写 `CONTRIBUTING.md`（review 纪律）和 `.github/workflows/prefix-audit.yml`（前缀稳定性 CI）。
