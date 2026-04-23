# 使用说明 · Bonsai 操作手册

从空目录到"微信里聊天就能用 agent"的完整流程。遇到操作类问题先查这里,其它 doc 偏设计 / 哲学。

---

## 0. 目录

1. [第一次上手 (5 分钟)](#1-第一次上手)
2. [日常命令](#2-日常命令)
3. [Web UI 的每个标签在做什么](#3-web-ui-页面说明)
4. [配置记忆 / Embedder](#4-记忆配置)
5. [外部渠道绑定](#5-外部渠道绑定)
   - [微信个人号 (iLink)](#51-微信个人号-ilink)
   - [飞书 / 企业微信 / Telegram / 钉钉](#52-飞书--企业微信--telegram--钉钉)
6. [定时任务](#6-定时任务)
7. [常见故障 & 排查](#7-故障排查)
8. [目录结构 & 文件](#8-目录结构)

---

## 1. 第一次上手

```bash
git clone https://github.com/LGYoungCoder/bonsai-agent.git
cd bonsai-agent
pip install -e .                       # 核心依赖(含 qrcode + pycryptodome,微信扫码/媒体收发开箱即用)
pip install -e '.[memory]'             # 如果要用本地 embedder,多装 sqlite-vec + sentence-transformers

bonsai serve                           # 默认 127.0.0.1:7878
```

浏览器开 `http://localhost:7878/`,按顺序做完这三步:

1. **🔑 模型** 页 → 填一个 provider(模型 + API key + base_url)→ 点「测试连通」绿灯 → 「保存到 config.toml」
2. 保存成功后,skills 目录和 memory 数据库会自动建好,样例 SOP 会自动种子。
3. 切到 **💬 对话** 开始聊。

想跳过 Web UI:`bonsai setup` 也能走完全一样的流程。

---

## 2. 日常命令

```bash
# 一体 Web(默认入口,推荐)
bonsai serve [--host 0.0.0.0] [--port 7878]

# 终端对话
bonsai chat                            # 交互 REPL
bonsai chat -v                         # 带 debug
bonsai chat --browser http://127.0.0.1:9222    # 挂浏览器

# 自检
bonsai doctor                          # Python / 依赖 / config / provider / embedder / stores / channels

# 配置向导(纯终端)
bonsai setup

# 记忆
bonsai search "关键词"                 # 独立搜 MemoryStore
bonsai mine ~/notes                    # 批量导入
bonsai memory-stats                    # 统计
bonsai wake-up                         # 打印每次对话注入的身份 + 记忆摘要
bonsai reembed                         # 换了 embedder 后重算向量

# 渠道(首选:在 web UI 的 channels 页点「启动」,或勾选「开机自启」让 serve 自动拉起)
bonsai channel-run wechat              # 仅 headless 场景需要另起终端手动跑
bonsai channel-run wechat --allow uid1,uid2

# MCP(作为 Claude Code / Cursor 的外脑)
bonsai mcp
```

常用 flag 都支持 `--project <PATH>` 和 `--config <PATH>`,用于指向非当前目录的项目。

---

## 3. Web UI 页面说明

| 侧边按钮 | 里面有什么 |
|---|---|
| **💬 对话** | WebSocket 接到 agent 的 AgentLoop · 左列显示历史会话(按最近用时间倒序),点进去只读回放,＋ 号新开一条 · 鼠标悬停会话条目右上出现 `×` 删除 |
| **🔑 模型** | 配置中心,左侧导航 6 节:`Providers` · `Failover` · `记忆/Embedder` · `外部渠道` · `Agent 参数` · `日志`。所有 `config.toml` 里的东西都能在这里改 |
| **📜 技能** | 左列 L3 原子 SOP 列表(顶部 ＋ 号**新建**);右侧默认显示「Skill 一览」,点单个 SOP 可以**编辑 / 删除** |
| **🩺 状态** | 跑 `bonsai doctor`,结果卡片化;右上角「重新检查」按钮 |
| **⏰ 定时任务** | cron 风格周期任务(daily / weekday / every_Nh …);新建编辑启停,点历史报告看执行结果 |
| **⚡ 自主任务** | `data/autonomous/{todo.md, history.txt, reports/}` 工作区;agent 按 autonomous_mode SOP 从 TODO 挑一条跑,写编号报告 |
| **📊 数据中心** | 按日 / provider / model 聚合 token 用量 + 缓存命中率 + 粗估成本(¥) |

页头右下角有 🌙/☀️ 按钮,主题切换,`localStorage['bonsai-theme']` 记着。

---

## 4. 记忆配置

记忆分两个独立的库:

- **SkillStore** —— `skills/` 目录。L0(身份)/ L1(关键词索引)/ L2(facts)/ L3(原子 SOP)。agent 每次启动注入 L0+L1,需要时按关键词展开 L3。
- **MemoryStore** —— `memory/memory.db`。SQLite + FTS5 + sqlite-vec,存原文对话记录和语义向量。

### 4.0 记忆不用手动初始化

保存一次配置(🔑 模型页任意改动 → 保存)就会自动做完这些:
- 建 `skills/` 目录 + 种子 L0 身份模板 + 拷 `L3_samples/*.md` 到 `L3/`
- 建 `memory/memory.db` 并跑 schema

🔑 模型页「记忆 / Embedder」最上面一张 **记忆状态** 卡片实时显示:
- SkillStore:L0 / L1 / L2 是否齐全、L3 SOP 个数
- MemoryStore:数据库是否存在、大小、drawer/room/vector 三个计数
- 当前 embedder 提供方和模型

徽章 `已初始化 / 未初始化` 一眼可见。"重新种子样例 SOP" 按钮把 `skills/L3_samples/*.md` 按缺失补到 `skills/L3/`(现有文件不覆盖)。

**首次保存会种 9 个样例 SOP**(都是通用的单用户 agent 操作规范):

操作类:
- `install_python_deps` — 装 Python 依赖的完整踩坑清单
- `git_commit` — 怎么安全提交代码
- `shell_safety` — 跑 shell 命令前的检查
- `fetch_http` — 调 HTTP 接口的最佳实践
- `search_codebase` — 查代码 / 找引用
- `edit_files` — 改文件的铁律
- `browser_workflow` — 用 web_* 工具干活的黄金流程

行为类(agent 自我约束):
- `verify_before_report` — 汇报完成前必须真跑(读代码不是验证)
- `plan_before_big` — 什么时候该先拆步骤再动手

### 4.0.1 自己加 / 改 / 删 Skill

📜 技能页左侧 ＋ 号 → 打开 SOP 编辑器:

- **name**:`a-z 0-9 _ -`,1-40 字符(就是文件名)
- **关键词**:用逗号分隔,会进 L1 索引,agent 按关键词找到它
- **Markdown 内容**:可以直接写正文,服务端自动加 frontmatter;也可以自己带 `--- ... ---` frontmatter,服务端保留原样

已有的 SOP 点进去,右上角「编辑」/「删除」。删除只允许在 `L3/` 下,不能碰 L0/L1/L2 索引文件。

### 4.1 Embedder 怎么选

| 选项 | 适用 | 备注 |
|---|---|---|
| `hash` | 零依赖兜底 | 召回质量差,只是能跑 |
| `openai`(OpenAI-兼容) | 强推荐,覆盖 bge-m3 / Zhipu / OpenAI | 填 `embed_base_url` + `embed_model` + `embed_api_key` |
| `local` | 隐私优先 | 装 `pip install -e '.[memory]'`,首次启动下 ~2GB 模型 |

**省钱推荐**:SiliconFlow 的 `BAAI/bge-m3` 免费额度 + 效果比 text-embedding-3 还好。

Web UI → 🔑 模型 → 「记忆与 Embedder」 → 下拉选 provider,填对应字段,保存。`embed_api_key` 保存后只显示 `••••xxxx`,再保存不会丢原值(除非你主动清空重填)。

---

## 5. 外部渠道绑定

5 家平台的凭据管理 + 连通测试都在 Web UI → 🔑 模型 → 「外部渠道绑定」一处。

### 5.1 微信个人号 (iLink)

目前唯一**完整跑通消息收发**的渠道。走 Tencent 官方 `ilinkai.weixin.qq.com` 协议,封号风险低。

**第一步:扫码登录**

1. Web UI → 🔑 模型 → 滚到最下「外部渠道绑定」→ 找「微信个人号 (iLink)」卡片
2. 勾选「启用」
3. 点「扫码登录」→ 卡片内会渲染一张 SVG 二维码(没装 qrcode 时会直接显示 URL,自己用微信扫)
4. 手机微信扫,点「确认登录」
5. 轮询状态会自动翻到 `confirmed`,页面显示 `已登录 · bot_id=xxx`
6. Token 落到 `<project>/data/wechat_token.json`

**第二步:起收发循环**

**A. 从 Web UI 一键启停(推荐)**

登录成功后,微信卡片下面会出现「收发循环」工具条:

- 徽章显示当前状态:`未启动` / `运行中 · pid 12345` / `已退出`(上次异常退出)
- 「启动」按钮会提示输入白名单(留空 = 所有人),然后 spawn subprocess
- 「停止」发 SIGTERM,3s 后还没退就 SIGKILL
- 「日志」展开运行日志末尾 150 行(跟 `<root>/logs/wechat_runner.log` 同源)
- 前端每 5s 自动轮询一次状态,掉线会自动刷

PID 写到 `<root>/data/wechat_runner.pid`;zombie 子进程会被自动 reap,不会误报"运行中"。

**A-bis. 让 `bonsai serve` 启动时自动拉起**

收发循环工具条上有个「随 serve 启动」复选框。勾上再保存配置,下次 `bonsai serve` 会:

1. 读 `config.toml` 的 `[channels.wechat]`
2. 如果 `enabled=true` 且 `autostart=true` 且 token 文件存在(扫过码)
3. 就 spawn 一个子进程跑 runner,跟你手动点「启动」一模一样

没登录的话会跳过并在服务端日志写 `autostart wechat skipped: not logged in`。
凭据丢失或网络炸掉,子进程会挂,不会死循环重拉 —— pid 文件留在 `data/wechat_runner.pid`,下次 `serve` 重启才会再尝试。

**B. 命令行(headless / 调试)**

```bash
bonsai channel-run wechat                          # 任何人发消息都会触发 agent
bonsai channel-run wechat --allow wxid_xxx,wxid_yyy # 只回白名单里的人
```

Web UI 和命令行都用同一个端口 19528 做单实例锁,同一个 root 下并起两个会报错。

**支持什么消息类型**

| 方向 | 类型 | 实现 |
|---|---|---|
| 用户 → agent | 文字 | 原样传入 |
| 用户 → agent | 图片 / 文件 / 视频 / 语音 | 解密保存到 `data/wechat_media/`,agent 拿到路径,可用 `file_read` / `code_run` 处理 |
| agent → 用户 | 文字 | Markdown 自动转纯文本(微信不渲染 md),按 1800 字分段,**代码块不会被拦腰切断** |
| agent → 用户 | 图片 / 文件 / 视频 | agent 在回复里写 `[FILE:/abs/or/rel/path]`,runner 会剥出来并按扩展名路由到 `send_image` / `send_video` / `send_file` |

Agent 的回复文本里 `[FILE:...]` 标记会被自动剥掉,不会发给用户。

**对话连续性 + 命令**

每个 `from_user_id` 独享一条 `AgentLoop.tail`,连续消息共享上下文(像普通聊天一样记得前文)。空闲 1 小时自动 gc 掉。

用户在对话里可以发这些 `/` 命令(不耗 LLM):

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令 |
| `/new` | 清空当前会话,下一条开新对话 |
| `/stop` | 中止正在跑的长任务(下一回合结束生效) |
| `/status` | 会话信息 + 当前昵称 + 备注数 |
| `/llm` | 列出 failover chain 里的 provider |
| `/name 张三` | 设定昵称,agent 后面知道怎么称呼你;`/name -` 清空 |
| `/note ...` | 加一条关于你的偏好备注(会话首条消息自动注入给 agent);`/note list` 看列表;`/note clear` 清空 |
| `/chat list` | 列出你的所有对话 |
| `/chat new [名字]` | 新开一个对话并切过去(不给名字就按时间自动起)。中文名也行 |
| `/chat switch 工作` | 切换(支持按名字前缀或序号) |
| `/chat rm 学习` | 删掉某个对话 |

**多对话模型**:每个微信用户可以同时维护多条独立对话,每条有自己的 `AgentLoop.tail`。默认对话叫 `default`,`/chat new` 之后切到新对话,`/new` 只清空当前。

出错时会给友好提示("agent 遇到了临时错误,换个说法再试"),不会把 Python traceback 丢给用户。

**Typing 心跳** — 长任务运行中后台线程每 5 秒给 WeChat 发一次 typing,手机上看到"对方正在输入..."一直亮着,不会误以为 bot 挂了。

**档案持久化** — `/name` 和 `/note` 落在 `data/user_profiles/<uid>.json`,重启 serve / runner 不丢。每个新会话(首条消息或 `/new` 后)把 `(系统提示: 对方昵称 X、偏好 ...)` 作为 preface 注入 AgentLoop tail,**不进 frozen prefix**,prompt cache 稳定。

**登出**

```bash
rm ./data/wechat_token.json      # 或 Web UI 的「退出登录」按钮
```

### 5.2 飞书 / 企业微信 / Telegram / 钉钉

目前**只做了凭据存储 + 连通测试**(打各家 auth 端点拿 access_token),消息收发 runtime 还没接。凭据结构:

| 平台 | 字段 |
|---|---|
| 飞书 | `app_id` + `app_secret` + `allowed_users` |
| 企业微信 | `corp_id` + `agent_id` + `secret` + `allowed_users` |
| Telegram | `bot_token` + `allowed_users` |
| 钉钉 | `client_id` + `client_secret` + `allowed_users` |

在 Web UI 填完后点「测试连通」会直接打对应平台的 auth 端点:

- 飞书:`/open-apis/auth/v3/tenant_access_token/internal`
- 企微:`/cgi-bin/gettoken`
- Telegram:`/getMe`
- 钉钉:`/oauth2/accessToken`

回包里 vendor 的错误码会原样显示在 badge 的 hover title 里。凭据以 `••••xxxx` 形式回显,重存不会丢原值。

Doctor 会把**启用但创建失败**的渠道标 `fail`,启用但没填全的标 `warn`,没启用的不检查。

---

## 6. 定时任务

cron 风格的周期任务,让 agent 每天 / 每周 / 每 N 小时自动跑一条 prompt,结果落磁盘等你看。

**运行模型:** FastAPI lifespan 里挂一个 asyncio 后台任务,**每 60 秒**扫一遍 `<root>/sche_tasks/*.json`,到点就拉起 AgentLoop 跑一次。跟 `bonsai serve` 同生命周期,serve 停它就停。不用开第二个进程。

### 6.1 新建一个任务

Web UI → ⏰ 定时任务 → ＋ 号:

| 字段 | 说明 |
|---|---|
| `name` | lower_snake,1-40 字符,也是 JSON 和报告的文件名 |
| `schedule` | `HH:MM` 24 小时制,本机时区 |
| `repeat` | `daily` / `weekday` / `weekly` / `once` / `every_1h` / `every_2h` / `every_6h` / `every_12h` |
| `prompt` | 交给 agent 的任务描述 |

### 6.2 repeat 的行为

- `daily` — 每天到点跑一次;已经跑过当天的会跳过
- `weekday` — 同上但只在周一到周五
- `weekly` — 每周同一天同一点(以上次跑的周几为准)
- `once` — 只跑一次,跑完再到时间点也不触发
- `every_Nh` / `every_Nd` — 以"上次跑完到现在"的时间间隔为准,`schedule` 只作为首次锚点

**迟到保护**:如果电脑关机 / 服务没启等到过 `schedule + max_delay_hours`(默认 6),这次会被跳过,不会醒来补跑一堆历史。

### 6.3 报告落在哪

`<root>/sche_tasks/done/YYYY-MM-DD_HHMM_<name>.md`

Markdown 文件,包含:触发时间 / 耗时 / prompt / agent 回复。左侧点任务名就能看历史列表 + 任意报告全文。

### 6.4 立即跑一次

任务详情页「立即运行一次」按钮。跟到点触发完全一样,就是立刻。调 prompt / 调 embedder 换了之后验证效果用这个。

### 6.5 从命令行看 / 写

任务就是一个 JSON 文件:

```bash
ls sche_tasks/
cat sche_tasks/daily_brief.json
ls sche_tasks/done/
```

直接编辑 JSON / 删文件也行,下次 60s 轮询会加载新状态。

## 7. 故障排查

| 症状 | 检查顺序 |
|---|---|
| `bonsai doctor` 里 provider fail | 1) key 有没有误贴前后空格 2) base_url 尾巴是不是多了 `/` 3) `curl` 手动打一遍 auth 端点 |
| `embedder: hash 召回质量差` warn | 正常,想改选 openai/bge-m3 或 local,见 §4.1 |
| `SkillStore missing: L0.md` | 保存一次配置(哪怕不改东西),`init_stores` 会自动补 |
| Web UI「外部渠道」扫码显示空白 | `qrcode` 在主依赖里,正常不会这样。如果真缺:`pip install qrcode`,或复制 URL 用手机直接打开 |
| `channel-run wechat`:`未登录` | 去 Web UI 扫码,或者看 `<root>/data/wechat_token.json` 在不在 |
| `channel-run wechat`:`19528 被占` | 另一个 runner 还在跑,`lsof -i :19528` 查一下 |
| Web UI 改不动 embed_api_key | 字段显示 `••••xxxx` = 已有值,**点进去会清空让你重填**。不重填的话保存时自动沿用旧值 |
| 保存后 skills 页仍显示"未初始化" | 刷新页面 / 点 📜 技能 强制重拉(保存后前端已清缓存) |

---

## 8. 目录结构

跑起来之后项目目录大致长这样:

```
bonsai-agent/
├── config.toml                 # 主配置(由 web / setup / 手写生成)
├── config.toml.bak             # 上次保存的备份
├── skills/
│   ├── L0.md                   # 身份 — 你是谁、跟 agent 怎么协作
│   ├── L1_index.txt            # 关键词 → SOP 路由表
│   ├── L2_facts.txt            # 稳定事实(路径、环境变量、名词)
│   ├── L3/                     # 原子 SOP,每个 .md 是一个可复用技能
│   ├── L3_samples/             # 出厂样例,保存时会按需 copy 到 L3/
│   └── _meta/evidence/         # 技能蒸馏的证据链
├── memory/
│   └── memory.db               # SQLite + FTS5 + vec,对话原文 + 向量
├── data/
│   ├── wechat_token.json       # 微信扫码后的 bot_token(不要 commit)
│   └── wechat_media/           # 微信收发的文件/图片/视频临时目录
├── sche_tasks/
│   ├── <name>.json             # 一个任务一个 JSON
│   └── done/                   # 每次跑完的 markdown 报告(YYYY-MM-DD_HHMM_name.md)
├── logs/
│   ├── cache_stats.jsonl       # prompt cache 命中统计
│   └── sessions/*.jsonl        # 对话原始流(turn-by-turn)
└── temp/                       # 运行时杂项
```

**别 commit 的**:`config.toml`(含 key)· `data/wechat_token.json`(bot token)· `memory/*.db`(你的对话)· `logs/`。`.gitignore` 默认已经覆盖。

---

本文档跟代码同步更新。如果跟实际行为对不上,以代码为准,并顺手提 issue。
