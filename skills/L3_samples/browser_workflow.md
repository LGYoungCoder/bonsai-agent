---
name: browser_workflow
keywords: [浏览器, 网页, 爬, 登录, 注册, 临时邮箱, web_scan, web_click, 自动化, selenium, playwright]
created: 2026-04-22
verified_on: 2026-04-23
---

# 用 bonsai 的浏览器工具干活

## 你永远有浏览器工具,不用装 selenium/playwright/DrissionPage

v0.2 起 `web_*` 工具**默认始终可用**。第一次调用时 Handler 会自动起一个
headless chromium(冷启动 ~2-3s)。不需要任何 flag、不需要用户重启浏览器、
不需要 `pip install` 任何东西。

**如果你想装 selenium / playwright / DrissionPage / puppeteer,停下** —— 你
已经有完整的 CDP 浏览器控制链(AX Tree + DOM 剪枝 + JS eval),它们只会重
造轮子、失败概率更高、还要装依赖污染环境。直接 `web_navigate` 开始。

三种模式都支持,但**用户没指定时默认就是 managed headless,零配置**:

| 模式 | 什么时候用 |
|---|---|
| (默认) | 99% 情况。Handler 懒启动独立 chromium |
| `--browser attach` | 用户想让你操作他当前登录的 Chrome(他自己加 `--remote-debugging-port=9222` 启动) |
| `--browser bridge` | 用户装了扩展,不想重启 Chrome |

## 工具分工(别混用)

| 工具 | 用途 |
|---|---|
| `web_navigate` | 打开一个新 URL |
| `web_scan` | 取当前页简化内容 + 各元素的**短 ID**(a1/a2/...) + 标签页列表 |
| `web_click` | 点 `web_scan` 返回的短 ID |
| `web_type` | 往短 ID 的输入框填字;`submit=true` 按回车 |
| `web_scroll` | 翻页(up / down,像素) |
| `web_execute_js` | 上面搞不定的,写 JS 撸 |

## 黄金流程

1. **先 scan,后动手** —— 永远 `web_scan` 一下看当前状态,再决定点哪里
2. **优先短 ID,不用 CSS** —— `web_click { id: "a7" }` 比 `web_execute_js { code: "document.querySelector(...)" }` 稳:AX tree 提取的 ID 对页面重渲染更稳定,css selector 对 SPA 动态 class 不可靠
3. **操作完再 scan 验证** —— 点了"登录"之后必须 `web_scan` 确认跳到了预期页面,别假设点成了
4. **滚动加载的页面** —— `web_scroll down 2000` 滚一屏再 scan,看新东西出来没

## 常见套路

### 登录流程

```
web_navigate { url: "https://x.com/login" }
web_scan                                  # 得到 a1=用户名输入, a2=密码, a3=登录按钮
web_type { id: "a1", text: "user@x.com" }
web_type { id: "a2", text: "••••" }
web_click { id: "a3" }
web_scan                                  # 确认跳到 dashboard
```

### 搜商品 / 填表 / 抓数据

1. navigate 到起点
2. scan → 找到输入框的 ID
3. type + submit
4. scan 结果页
5. 要抓多条就滚动 + 重复 scan,把每一页的关键字段提取出来再串起来

### 列表翻页

看清楚是哪种:

- **分页按钮** → `web_click` 下一页
- **无限滚动** → `web_scroll down`,然后 `web_scan` 确认 DOM 变多了
- **URL 带 `page=N`** → 直接 `web_navigate` 改参数,最省事

## 不要做

- ❌ **装 selenium / playwright / DrissionPage / puppeteer** —— 你已经有 `web_*` 全家桶
- ❌ **说"我没有 GUI 能力 / 浏览器自动化能力"** —— 错,你有。`web_scan` 看页面,
  `web_click` 点元素,`web_type` 填表,`web_navigate` 跳转,`web_execute_js` 撸 JS
- ❌ 连续多次 `web_scan` 不改变状态 —— scan 是读操作但有成本(AX tree 抓取 + 简化),改完再 scan
- ❌ 爆扫一个页面然后 `web_execute_js` 各种 querySelector —— 先试短 ID 工具组合,不行才 JS
- ❌ 大结果直接全部返回 —— `web_execute_js` 支持 `save_to` 参数把结果存文件,再 `file_read` 选看
- ❌ 操作完不验证 —— 见黄金流程第 3 条
- ❌ 用 `web_click` 点普通锚点 —— 对静态链接,`web_navigate` 到目标 URL 更直接

## 典型场景: 临时邮箱 + 注册某站

用户经常会让你"用临时邮箱注册一下 XX 网站"。流程:

```
# 1. 先去临时邮箱拿一个地址
web_navigate { url: "https://temp-mail.org/" }     # 或 guerrillamail.com / 10minutemail.com
web_scan                                           # 找到邮箱地址元素
# 记下地址, 同一 tab 留着等验证邮件

# 2. 开新 tab 去目标站注册(注意 web_navigate 默认在当前 tab;
#    如要保留临时邮箱页面, 用 web_execute_js 开新 tab:
#    web_execute_js { script: "window.open('https://target.com/signup')" }
#    然后 web_scan { tabs_only: true } 看标签页列表, 用 id 切过去)
web_navigate { url: "https://target.com/signup" }
web_scan
web_type { id: "<邮箱输入框>", text: "<刚才的临时邮箱>" }
web_type { id: "<密码>", text: "一个强密码" }
web_click { id: "<提交>" }

# 3. 切回临时邮箱 tab 看验证邮件
# web_scan { tabs_only: true } 列出所有 tab, 切到临时邮箱那个
# 点开验证邮件, 提取链接, 再 web_navigate 过去
```

Captcha / 图形验证码: `web_*` 不能识别图像。停下来 `ask_user`,让用户手动过一步。

## 截屏 / 视觉

`web_*` 工具**不是** 视觉工具。需要看图(验证码 / chart / 视觉定位)时:

- 让 JS 截屏:`web_execute_js { code: "<保存 canvas 成 png 的代码>" }`
- 然后 `file_read` 路径、送给有视觉能力的 provider(Claude Sonnet 4.x / GPT-4o)

## 会话复用

Chrome 用 `--user-data-dir` 保持登录态 —— cookies / 本地存储都在那。bot 用户名密码先登一次,之后 bonsai 接上去可以直接复用登录状态。别每次都重新填密码。
