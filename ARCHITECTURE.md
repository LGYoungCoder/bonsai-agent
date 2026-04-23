# Bonsai Architecture

## 顶层结构

```
┌──────────────────────────────────────────────────────────┐
│                       User Input                         │
└────────────────────────┬─────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────┐
│  Session (per-conversation state)                        │
│  - history (on backend, not on loop)                     │
│  - cwd / session_id / turn counter                       │
└────────────────────────┬─────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────┐
│  AgentLoop (~100 lines, no state)                        │
│  - dispatch tool calls to Handler                        │
│  - yield streaming output                                │
│  - budget check per turn                                 │
└──────┬───────────────────────────────────┬───────────────┘
       ↓ read-only                         ↓ read-only
┌──────────────────┐                ┌──────────────────────┐
│  SkillStore      │                │  MemoryStore         │
│  (distilled)     │                │  (verbatim)          │
│  - L1 index      │                │  - L0 identity       │
│  - L2 facts      │                │  - L1 essentials     │
│  - L3 SOPs/py    │                │  - L2 scoped recall  │
│                  │                │  - L3 vector search  │
└──────────────────┘                └──────────────────────┘
       ↑                                   ↑
       │ async write (subprocess)          │ async write (hook)
       │                                   │
┌──────────────────────────────────────────────────────────┐
│  Background Writer                                       │
│  - session archive                                       │
│  - SOP distillation (LLM-assisted, off-chat)             │
│  - verbatim drawer ingestion                             │
│  - index compaction                                      │
└──────────────────────────────────────────────────────────┘
```

## 核心原则落到代码

### 原则 1：主循环无状态

```python
# pseudo
def agent_loop(backend, sys_prompt, user_input, handler, tools, max_turns=40):
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_input}]
    for turn in range(max_turns):
        enforce_budget(messages, tools)           # 硬预算检查
        response = yield from backend.chat(messages, tools)
        outcomes = [handler.dispatch(tc) for tc in response.tool_calls]
        if any(o.should_exit for o in outcomes): break
        messages = [{"role": "user", "content": merge_prompts(outcomes)}]
        # 注意：history 在 backend 内部维护，这里只传增量
```

`backend` 持有完整 history；`session` 只记 cwd / turn counter；`loop` 本身无状态。**切换模型、重放对话、多进程复用都是免费的**。

### 原则 2：双库分离

```python
# SkillStore: 关键词查询，file-based
class SkillStore:
    def lookup(self, keyword: str) -> list[str]:         # 返回文件路径列表
    def read(self, path: str) -> str:                    # 读取 SOP
    def write_sop(self, name: str, content: str,
                  evidence: dict):                       # 必须附执行证据

# MemoryStore: 向量+BM25，verbatim
class MemoryStore:
    def wake_up(self, wing: str = None) -> str:          # L0 + L1
    def recall(self, wing=None, room=None) -> list[Drawer]:   # L2
    def search(self, query: str, **scope) -> list[Drawer]:    # L3
    def ingest(self, drawer: Drawer):                    # 后台调用
```

Agent 在 chat window 里能看到的只有**读接口**。所有写接口都由 background writer 调用。

### 原则 3：Handler 用 generator 做工具分发

```python
class Handler:
    def dispatch(self, tool_call):
        method = getattr(self, f"do_{tool_call.name}")
        yield from method(tool_call.args)      # 流式给 UI
        # 返回 StepOutcome(data, next_prompt, should_exit)

    def do_file_read(self, args):
        yield f"[Action] Reading {args['path']}\n"
        content = smart_format(read_file(args['path']), max_chars=20000)
        return StepOutcome(data=content, next_prompt=next_anchor())
```

这个模式的优势：
- 工具执行过程可以**实时 yield** 到 UI（体感快）
- 工具内部可以**抛异常不兜底**（loop 层统一转成 error prompt）
- 工具的**数据流 / 控制流分离**（data 给模型，next_prompt 指导下一轮）

### 原则 4：预算只压不崩

每轮进入 LLM 前估算总 token,超 soft 才压,且带 cooldown 避免模型看到反复被裁的 `<thinking>`:

```python
def on_turn_start(messages, tools):
    total = estimate_tokens(messages) + estimate_tokens(tools)
    cd = (cd + 1) % COMPRESS_COOLDOWN          # 默认 5 轮
    # 刚过 soft 一点点 → 让 tail 稳定几轮; 大幅超才立刻压。
    if total > soft and (cd == 0 or total > soft * 1.25):
        messages = compress_tail(messages, target=soft * 0.6)
    log_budget(total)
```

`soft` / `hard` 是**压缩目标**不是崩溃线 —— bonsai 从不 raise。真正的边界由 `max_turns` (防失控) 和 provider 的 `context_win` (超长自然报错) 两道栅栏负责。对长 context 强模型 (Claude 1M / Gemini 2M) 把 soft/hard 往上调即可。

**不做"智能"的上下文管理**(不引入小模型摘要旧历史等)。纯粹靠规则 + 压缩 + 硬裁。理由:每加一层智能都会引入新的 token 消耗和新的幻觉风险。

## 模块清单 & 代码规模预算

**三环同心圆模型**。内层守紧,外层按需膨胀:

| Ring | 板块 | 行数红线 | 说明 |
|---|---|---|---|
| **Ring 0** | Core runtime | **≤ 2000** | loop / handler / backend protocol / budget / safety / session / commands / redact |
| **Ring 1** | Stores | **≤ 1800** | SkillStore + MemoryStore + background writer(verbatim + 向量,天生偏重) |
| **Ring 2a** | Backend adapters | **≤ 1500** | 7 provider,每个 ~150 行 + 共享 base / failover |
| **Ring 2b** | Frontend adapters | **≤ 2500** | CLI + Web + MCP + 6 IM,每个 IM 约 200 行 |
| **Ring 3** | Capabilities | 独立算 | 浏览器(~700) / 视觉 / ADB 等按功能独立 |

**Ring 0 + Ring 1 = "Bonsai 的本体" ≤ 3800 行**(绝对守)
**+ Ring 2(adapters 线性展开) ≤ 7800 行**(v0.4 目标)
**+ Ring 3(浏览器等) ≤ 8500 行**(v0.3 之后)

**为什么不是 "3K 行"**:
- verbatim + 向量 memory 是完整的一套存储栈,Ring 1 ~1800 独占
- 支持 7 个 provider,每个 adapter ~150 行也是 1000 行量级
- "3K 核心" 这类数字往往是 marketing,真实工程实现通常在 8-10K

这个预算相对 OpenHands / AutoGPT(10 万行+)仍然是**严格克制**的。克制不等于少到不合理。

### 目录结构

```
bonsai/
├── core/
│   ├── loop.py            # ~100 lines, stateless agent loop
│   ├── handler.py         # ~300 lines, ≤12 tool handlers
│   ├── backend.py         # ~400 lines, LLM session + cache + stream
│   ├── budget.py          # ~100 lines, token estimation + compression
│   └── session.py         # ~150 lines, per-conversation state
├── stores/
│   ├── skill_store.py     # ~150 lines, filesystem + keyword index
│   └── memory_store.py    # ~250 lines, SQLite + vector (sqlite-vec)
├── writer/
│   ├── session_archiver.py # 后台归档
│   ├── sop_distiller.py    # 后台提炼（用小模型）
│   └── drawer_ingester.py  # 后台 verbatim 入库
├── tools/
│   └── schema.json        # 工具定义（≤12 个）
├── prompts/
│   └── system.txt         # <500 tokens 系统提示
└── tests/
```

## 关键技术选型

| 选项 | 选择 | 理由 |
|---|---|---|
| 向量存储 | **sqlite-vec** | 单文件、零部署、和 SQLite 主表同一 DB，一次 JOIN 搞定 scope + 向量 |
| Embedding 模型 | **bge-m3** 或 **text-embedding-3-small** | 本地用 bge-m3（多语种），云端用 OpenAI 便宜的那个 |
| LLM 后端 | Claude API (primary) + OpenAI-compatible (fallback) | Prompt cache 是硬需求，Claude 的 cache 折扣最狠 |
| 工具协议 | Anthropic native tool use | Claude 对 native tool 调用训练最到位 |
| 持久化事件 | 外部 hook (Claude Code) + 内部子进程 | 写入离开热路径 |
| 测试 | pytest + golden transcripts | 录一组真实对话，每次 PR 回放验证行为一致 |

## 与 Claude API prompt caching 的集成

这是省 token 的**最大杠杆**，必须在架构层就打通。

```python
# backend.chat() 内部：
def build_request(messages, tools, sys_prompt):
    return {
        "system": [
            {"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}},
        ],
        "tools": tools,   # 注意：tools 也会被算进 cache prefix
        "messages": [
            *older_messages,
            {"role": "user", "content": [
                {"type": "text", "text": second_latest, "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "user", "content": latest},
        ],
    }
```

**关键纪律**：
1. sys_prompt + tools schema + L1 索引**永远**放最前面，打一个 cache breakpoint
2. 倒数第二条 user message 打第二个 cache breakpoint
3. **绝不**在固定前缀中插入动态内容（当前时间、当前目录等）——哪怕只改一个字符，整个 cache 就失效
4. 每次请求记录 `cache_creation_input_tokens` 和 `cache_read_input_tokens`，命中率 < 70% 立即报警

预期效果：长对话场景下，输入 token 成本降到无 cache 时的 **10-20%**。

## 演进路径

**v0.1 Seedling**（种子期）：
- 核心 loop + 5 个基础工具（file_read / file_write / code_run / memory_search / ask_user）
- SkillStore（纯文件 + 关键词）
- 一个命令行 REPL

**v0.2 Sprout**（萌芽期）：
- 加浏览器工具（CDP + AX tree 方案）
- MemoryStore 上线（sqlite-vec）
- Background writer 跑起来

**v0.3 Sapling**（树苗期）：
- Planner / Executor 分离
- Skill distillation 自动化
- 第一个前端（TUI 或 Streamlit）

**v0.4+ Growing**：
- 按实际使用场景加能力
- 每加一个工具必须有成本/收益分析
- 不为了"功能完整"而加功能

## 不变量（Invariants）

任何改动都必须保持：

1. 主循环永远是 stateless 的（history 在 backend）
2. 主循环内永远不做 memory 写入
3. 工具数永远 ≤ 12
4. 每轮固定开销永远 ≤ 3K tokens
5. SkillStore 写入永远需要 execution evidence
6. MemoryStore 永远 verbatim（禁止摘要写入）
7. 系统提示里的固定前缀永远**字节级稳定**（保 cache）
8. 预算超限**只压不崩** —— 从不 raise,由 max_turns + provider context_win 两道栅栏兜底

违反任何一条的 PR 都不合并。
