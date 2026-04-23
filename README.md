<div align="center">

<img src="assets/logo.svg" width="120" alt="Bonsai logo"/>

# Bonsai · 盆栽

**一棵被刻意约束的树，才能活上几百年。**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.2.0-brightgreen)]()

**简体中文** | [English](./README_en.md)

</div>

---

## 这是什么

**你自己一个人用的 AI 助手。装在你自己的电脑上，用你自己的 API key，越用越懂你。**

跟市面上 AI 工具的区别，用人话说：

| | 普通 AI 产品 | Bonsai |
|---|---|---|
| 跟谁说话 | 记不住你是谁 | 认识你，记得你的习惯、项目、口头禅 |
| 聊过的内容 | 关了就没了 | 写在你自己盘里，下次接着聊 |
| 用谁的 key | 平台的（按月订阅） | 你自己的（用多少花多少） |
| 能干啥 | 只会打字回复 | 可以打开浏览器、改文件、跑代码 |
| 跑在哪 | 云上黑盒 | 你的笔记本 / 小服务器，你说了算 |
| 装在微信/QQ 里 | 想都别想 | 五大 IM 随便挂 |

**一句话**：它不是一次性 chatbot，是一棵会陪你长大的树。扔到你的服务器上 7×24 跑着，你在 QQ 上问、微信上问、电脑上问，都是同一个它。

---

## 能帮你干什么

**聊天 · 记事**
> "上周我们讨论的那个 XX 你还记得吗？"——它记得。session 自动续，跨天、跨设备、跨通道都能接上。

**自动化事务**
> "帮我用临时邮箱注册一下 XX 网站"——它开一个浏览器，真的帮你注册。
> "读一下这个 PDF 然后总结三点重点"——它读，它总结。
> "把这个文件夹里所有 md 整理进记忆库"——它整理，以后你问"上次记过的 XX"能翻到。

**当代码助手**
> 接进 Claude Code / Cursor 当外脑（MCP server），或者直接在终端让它读文件、改代码、跑测试。

**挂在 IM 里**
> 绑定微信/QQ/Telegram/飞书/钉钉任意一个或多个，你人在哪手机在哪就能用。
> 后台一直跑着，隔一周回来它也记得你们上次聊到哪。

---

## 怎么启动

### 第一次用（5 步）

```bash
# 1. 装它
git clone https://github.com/LGYoungCoder/bonsai-agent.git
cd bonsai-agent
pip install -e .

# 2. 打开网页版
bonsai serve
```

然后浏览器打开 **http://localhost:7878**——你会看到 4 个标签页：

```
💬 对话    🔑 模型    🛠 技能    🩺 状态
```

```
# 3. 进 🔑 模型 标签，填一个 provider：
#    - Provider Kind 选：claude / openai / glm ...
#    - Model 填具体模型名
#    - Base URL 填对应地址（Claude 留空；GLM 填 https://open.bigmodel.cn/api/anthropic）
#    - API Key 粘你自己的 key
#    - 点 「Test」—— 看到绿色「连通」就成
#    - 点 「保存」

# 4. 回 💬 对话 标签，开聊
```

### 后面要做的（都是一次性）

| 想要的 | 怎么办 |
|---|---|
| 重启后还能用 | `bonsai serve` 用 systemd / pm2 / Docker 挂起来跑 |
| 绑微信 | 🔑 模型 旁的「外部渠道」tab → 扫码 → `bonsai channel-run wechat` 另开终端跑 |
| 绑 Telegram | 同上，填 bot_token，跑 `bonsai channel-run telegram` |
| 绑 QQ / 飞书 / 钉钉 | 同上，填对应凭据 |
| 别人访问你的 web | `bonsai serve --host 0.0.0.0 --port 7878` + 反代加密 |

### 没图形界面（headless / 远程服务器）

```bash
bonsai setup              # 纯终端引导配完 provider
bonsai chat               # 命令行聊天
bonsai doctor             # 一键自检，看哪里没配好
```

---

## 启动后有哪些东西自动帮你干活

**这些完全不用管，自己就会发生**：

- 🌐 **浏览器自动接**：你让它「打开网页 X」，它第一次会自动起一个无头 Chrome。不用你预先装 ChromeDriver 不用你敲 flag
- 📚 **记忆自动存**：每次聊完自动进 MemoryStore，下回相关话题自动召回。长会话每 10 轮后台备份一次
- 📜 **学到的东西会加回来**：你手写了新 SOP 或者蒸馏了一条，下一轮模型立刻就看得到——不用重启
- 🗑️ **日志自己清**：每 24 小时清一次超过 15 天的旧日志，盘不会爆
- 🔁 **provider 挂了自动切**：配了两个以上 provider，失败自动转下一个
- 💾 **对话自动续**：手机上问过的事，隔几天换电脑问也接得上（7 天窗口内）
- 🛡 **压缩不破坏 cache**：4 轮压缩策略保字节稳定，帮你省一半以上 token 费

**要自己点的只有这些**：
- 至少配一个 provider + api_key（没有这个它就没法说话）
- 想用哪个 IM，就跑对应的 `bonsai channel-run <名字>`（不跑就不会扫你不用的渠道）

---

## 要花多少钱 / 多少性能

- **API 费用**：你自己的 key，token 怎么用都透明可看（💬 对话 页面右下角实时显示）
- **机器**：笔记本就能跑，后台常驻 ~200MB 内存，不吃 CPU
- **存储**：一年聊天 + 记忆库 估计 <1GB（log 还自动 gc）
- **首次浏览器冷启动**：2-3 秒（之后共享同一个 chromium 实例）

---

## 支持的模型 / 通道

**模型**：Claude、OpenAI、智谱 GLM、阿里通义 Qwen、MiniMax、DeepSeek、Kimi。随便切，支持 failover（主挂了自动转备）。

**通道**：

| 通道 | 是否跑通 | 需要什么 |
|---|:---:|---|
| 微信个人号 | ✅ | 扫码（走 iLink 官方协议） |
| Telegram | ✅ | `@BotFather` 要 bot_token |
| QQ | ✅ | QQ 开放平台拿 AppID + Secret |
| 飞书（Lark） | ✅ | 飞书开放平台 app_id + secret |
| 钉钉 | ✅ | 钉钉开放平台 client_id + secret |
| 企业微信 | ⏸ | 要公网 HTTPS，暂留 placeholder |

**浏览器驱动** 三种模式按需选：

- 默认（不加 `--browser`）：首次用自动起一个无头 Chrome
- `--browser attach`：接管你已经开着的 Chrome（要加 `--remote-debugging-port=9222` 启动）
- `--browser bridge`：装一个 Chrome 扩展，无需重启你的 Chrome 就能操作已登录页面

---

## 配置文件长这样

在网页里点点就能配，不想点也可以写 `config.toml`：

```toml
[agent]
max_turns = 40               # 单次对话最多几轮
budget_soft = 40000          # 软上限，超了开始压缩
budget_hard = 60000          # 硬上限

[[providers]]
name = "claude-primary"
kind = "claude"
model = "claude-sonnet-4-6"
api_key = "$ref:env:ANTHROPIC_API_KEY"    # 也可以直接填 key

[[providers]]
name = "glm-fallback"
kind = "claude"                            # 走 Anthropic 接口
model = "glm-5"
base_url = "https://open.bigmodel.cn/api/anthropic"
api_key = "$ref:env:GLM_API_KEY"

[failover]
chain = ["claude-primary", "glm-fallback"]  # 主挂了转 GLM

[memory]
skill_dir = "./skills"
memory_db = "./memory.db"

[maintenance]
gc_enabled = true            # 自动清旧日志
gc_retention_days = 15       # 保留几天
gc_interval_hours = 24       # 多久跑一次

[channels.telegram]
enabled = true
bot_token = "$ref:env:TG_BOT_TOKEN"
allowed_users = "12345"      # 你自己的 Telegram user_id
```

**都不配会怎样**：启动失败，提示缺 provider。其他所有东西都有合理默认。

---

## 常见问题

**Q: 装完运行 `bonsai serve`，打开网页说 "config not found" / "provider not configured"？**
A: 进「🔑 模型」tab 填一个 provider 即可。这是唯一强制的配置。

**Q: 我填了 key，测试通过了，重启后又说连不上？**
A: 0.2 已修。如果还遇到，到「🔑 模型」tab 重新粘一下 key 再 Test（旧版本 UI 只显示脱敏串，点 Test 会发脱敏值导致失败）。

**Q: IM bot 跑一段时间自己停了？**
A: 用 systemd / supervisor 管理进程，`Restart=always`。bonsai 退出时会自动 flush 数据，重启后 7 天内的对话自动续上。

**Q: 我写了一个 SOP（技能），bot 能立刻用上吗？**
A: 能。wakeup 每回合动态刷新——你 save 完下一句话就生效，不用重启。

**Q: 怎么知道它现在都装了什么本事？**
A: `bonsai wake-up` 或网页 🩺 状态 tab，会打印它现在大脑里有什么（L0 身份 + L1 技能索引 + 最近记忆）。

---

## 更技术的内容

功能面和架构决策在这几份文档：

- [`PHILOSOPHY.md`](PHILOSOPHY.md) — 为什么是这种取向（单用户、永运行、本地优先）
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 7 条不变量 + 关键设计决策
- [`docs/USAGE.md`](docs/USAGE.md) — 完整操作手册

## 开发

```bash
pip install -e ".[dev]"
pytest -q                # 154 tests
ruff check bonsai
```

## License

[MIT](LICENSE)

---

<div align="center">

**v0.2 · 你的 agent，长成你的样子。**

</div>
