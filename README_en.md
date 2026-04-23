<div align="center">

<img src="assets/logo.svg" width="120" alt="Bonsai logo"/>

# Bonsai

**A tree shaped by deliberate constraint can live for centuries.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/status-early--alpha-yellow)]()
[![Tests](https://img.shields.io/badge/tests-36%20passing-brightgreen)]()

[简体中文](./README.md) | **English**

</div>

---

## One-liner

> **Your AI, on your computer.**
> Remembers you · uses your own keys · swaps models freely · installs in 3 commands.

## In a minute

Bonsai is an AI agent framework **built for a single user — you**. It differs from typical AI tools in 4 fundamental ways:

1. **💾 Your data stays on your disk** — memories / skills / transcripts all in local SQLite; not uploaded, not shared, not used for training
2. **🔑 Your own keys** — swap between GLM / Claude / OpenAI / Qwen / MiniMax / DeepSeek freely; no vendor lock-in
3. **🧠 Learns you over time** — the longer it runs the more it knows your projects / preferences / habitual commands
4. **💰 Eight token-saving techniques built-in** — prompt cache / parallel tool use / structured truncation / semantic dedup / failover … 1/3 the cost for the same task

Summary: **not a one-off chatbot — a tree that grows alongside you.**

## ⚡ 30-second quickstart

```bash
git clone https://github.com/LGYoungCoder/bonsai-agent.git
cd bonsai-agent
pip install -e .

bonsai serve            # open http://localhost:7878 — chat / config / skills / status all in one
```

The first visit lands on the **🔑 Models** tab: fill in a provider + key → save → switch to **💬 Chat** and talk.

> 💡 **No API key yet?** Get one from [Zhipu](https://open.bigmodel.cn) (works in China) or [SiliconFlow](https://cloud.siliconflow.cn) (free tier). 2 minutes.
>
> Prefer terminal? Use `bonsai setup` + `bonsai chat` (see below).

## 🎯 What it does

| Scenario | Entry |
|---|---|
| All-in-one web UI (**Chat / Models / Skills / Status** tabs) | `bonsai serve` → browser |
| Terminal chat + code execution + file ops | `bonsai chat` |
| Plug into Claude Code / Cursor as an external brain | `bonsai mcp` |
| Drive a web browser (shopping, forms, scraping) | `bonsai chat --browser http://127.0.0.1:9222` |
| Bulk-import notes into memory | `bonsai mine ~/notes` |
| Standalone memory search | `bonsai search "keyword"` |
| CLI self-check | `bonsai doctor` (same source as the 🩺 Status tab) |
| **Connect to WeChat / Feishu / WeCom / Telegram / DingTalk** | Web UI "外部渠道绑定" + `bonsai channel-run <kind>` |

## ✨ Features

- 🧠 **Dual-store memory** — skills (distilled SOPs) and transcripts (verbatim) stored separately; no hallucination bleed
- 💰 **Token-frugal** — byte-stable prefix for cache hits · parallel tool use · type-aware truncation · per-provider cache monitoring
- 🔌 **Multi-model** — GLM / Claude / OpenAI / Qwen / MiniMax / DeepSeek / Kimi behind one interface with failover
- 🌐 **Multi-frontend** — CLI · Web (FastAPI+WS) · MCP server · browser automation (CDP + AX tree, **10× cheaper than DOM**)
- 📜 **Skill distillation** — successful tool-call sequences are async-distilled into reusable SOPs; evidence-gated
- 🔒 **Personal-agent trust model** — no enterprise compliance yoke; your agent, your rules
- 🎨 **Friendly onboarding** — `setup` wizard + `doctor` self-check + readable error panels; 8 steps down to 3

## 🛠️ Common commands

```bash
bonsai serve          # All-in-one web UI (default :7878) — recommended for newcomers
bonsai setup          # Headless terminal wizard (for SSH / no-browser setups)
bonsai doctor         # Full-stack health check (same checks as the 🩺 Status tab)
bonsai chat           # Interactive REPL
bonsai chat -v        # With debug logs
bonsai chat --browser http://127.0.0.1:9222   # Attach to Chrome (CDP)

bonsai wake-up        # Show current identity + recent memory digest
bonsai search "text"  # Standalone MemoryStore search
bonsai mine ~/notes   # Bulk ingest a directory
bonsai reembed        # Re-compute vectors after switching embedder
bonsai memory-stats
bonsai providers      # Show configured providers

bonsai mcp            # MCP server (stdio) for Claude Code / Cursor
```

## ⚙️ Config

**Recommended**: `bonsai serve` → open the **🔑 Models** tab, fill the form, hit save. First-run lands here automatically.

**Headless**: `bonsai setup` interactive wizard.

**Hand-edit** `config.toml` directly:

```toml
[agent]
max_turns = 40
budget_hard = 60000

[[providers]]
name = "glm-primary"
kind = "claude"                                 # Anthropic-native wire
model = "glm-5"
base_url = "https://open.bigmodel.cn/api/anthropic"
api_key = "$ref:env:GLM_API_KEY"

[failover]
chain = ["glm-primary"]

[memory]
embed_provider = "openai"
embed_model = "BAAI/bge-m3"
embed_base_url = "https://api.siliconflow.cn/v1"
embed_api_key = "$ref:env:SILICONFLOW_API_KEY"
```

**Supported providers:** `claude` · `openai` · `glm` · `qwen` · `minimax`
**Supported embedders:** SiliconFlow bge-m3 (recommended, free tier) · Zhipu embedding-3 · OpenAI · local sentence-transformers · hash (zero-dep fallback)

Full example: [`config.example.toml`](config.example.toml).

## 🏗️ Architecture at a glance

```
┌──────────────────────────────────────────┐
│              AgentLoop                   │  stateless · ~120 LoC
│     budget check · parallel dispatch     │
└──────┬───────────────────────┬───────────┘
       ↓ read                  ↓ read
┌──────────────┐       ┌──────────────────┐
│ SkillStore   │       │ MemoryStore      │
│ (distilled)  │       │ (verbatim)       │
│ file + index │       │ SQLite+FTS5+vec  │
└──────────────┘       └──────────────────┘
       ↑ async                 ↑ async
       └──── Background Writer ───┘
              (off the hot path)
       ↓
┌──────────────────────────────────────────┐
│      Backend Protocol + Failover         │
└──┬────────────┬──────────┬──────────┬────┘
   ↓            ↓          ↓          ↓
 Claude      OpenAI     GLM/Qwen   MiniMax ...
```

**Code budget (three concentric rings):**

| Ring | Scope | LoC |
|---|---|---|
| Ring 0 | core runtime | ≤ 2000 |
| Ring 1 | stores + writers | ≤ 1800 |
| Ring 2a | 5–7 backend adapters | ≤ 1500 |
| Ring 2b | CLI + Web + MCP frontends | ≤ 2500 |

Core (Ring 0+1) ≤ 3800 LoC · v0.4 total target ≤ 8000 LoC.

## 📱 External channels (plug the agent into your IM)

Bonsai can act as a bot on these 5 platforms:

| Platform | Login | Message I/O | Notes |
|---|---|---|---|
| **WeChat (personal)** | QR (iLink official protocol) | ✅ text + images + files + video | via `ilinkai.weixin.qq.com`, low ban risk |
| **Feishu** (Lark) | app_id + secret | credential verify | runtime TBD |
| **WeCom** | corp_id + agent_id + secret | credential verify | runtime TBD |
| **Telegram** | bot_token | credential verify | runtime TBD |
| **DingTalk** | client_id + secret | credential verify | runtime TBD |

```bash
bonsai serve                           # open Web UI, scan QR under "外部渠道绑定"
bonsai channel-run wechat              # another terminal runs the I/O loop
bonsai channel-run wechat --allow wxid_xxx,wxid_yyy   # allowlist
```

Full flow and troubleshooting in **[docs/USAGE.md](docs/USAGE.md)**.

## 📚 Docs

| | |
|---|---|
| **[docs/USAGE.md](docs/USAGE.md)** | **Operations manual** — zero to channels |
| **[PHILOSOPHY.md](PHILOSOPHY.md)** | Why we built it this way |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | How · 7 invariants |
| **[ROADMAP.md](ROADMAP.md)** | 5-phase development roadmap |

## 🧪 Development

```bash
pip install -e ".[dev]"
pytest -q                            # 36 unit tests
python benchmarks/e2e_glm.py         # end-to-end (needs API key)
python benchmarks/cache_probe.py     # empirical cache hit rate
ruff check bonsai tests
```

## 📄 License

[MIT](LICENSE)

---

<div align="center">

🌱 *Seedling*

**Your agent, shaped by you.**

</div>
