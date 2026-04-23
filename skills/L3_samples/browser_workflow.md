---
name: browser_workflow
keywords: [浏览器, 网页, 爬, 登录, 注册, 临时邮箱, web_scan, web_click, 自动化, selenium, playwright]
created: 2026-04-22
verified_on: 2026-04-23
---

# 用 bonsai 的浏览器工具干活

## 你永远有浏览器工具,不用装 selenium/playwright/DrissionPage

v0.2 起 `web_*` 工具**默认始终可用**。第一次调用时 Handler 会自动起一个
managed chromium(冷启动 ~2-3s)。交互场景(CLI / web UI,有 DISPLAY)默认
**headful** 让你能看见过程 + 方便人工介入;IM bot / 后台 scheduler 默认
**headless**。不需要任何 flag、不需要用户重启浏览器、不需要 `pip install`。

**如果你想装 selenium / playwright / DrissionPage / puppeteer,停下** —— 你
已经有完整的 CDP 浏览器控制链(AX Tree + DOM 剪枝 + 任意 JS eval + 新 tab
自动侦测),它们只会重造轮子、失败概率更高、还要装依赖污染环境。

三种模式:

| 模式 | 什么时候用 |
|---|---|
| (默认 managed) | 99% 情况。Handler 懒启动独立 chromium(交互 headful / 无人值守 headless) |
| `--browser attach` | 用户想让你操作他当前登录的 Chrome(他自己加 `--remote-debugging-port=9222` 启动) |
| `--browser bridge` | 用户装了扩展,不想重启 Chrome |

## 工具分工

| 工具 | 用途 |
|---|---|
| `web_navigate` | 打开一个 URL。默认当前 tab;`new_tab=true` 开新 tab |
| `web_scan` | 取当前页简化内容 + 各元素的**短 ID**(a1/a2/...) + 标签页列表。支持 `switch_tab_id=<序号或 url 片段>` 切到目标 tab 再扫 |
| `web_click` / `web_type` / `web_scroll` | 基于短 ID 的便捷动作 |
| `web_execute_js` | **主力工具** —— 任何事都能做:`window.open(url)` / `.click()` / 填表 / 读 localStorage / 任意 DOM。执行后自动侦测新 tab 并把 id 附在返回尾部 |

## 黄金流程

1. **有方向感就直接 execute_js,不用每步先 scan** —— 知道要点什么就写 JS;scan 是探路工具不是必经步骤
2. **需要对新页面确认状态时再 scan** —— 跳转 / 提交后扫一次看结果
3. **优先短 ID 或 execute_js 两条路都 OK**:
   - 简单表单:短 ID 稳定省 token —— `web_click id=a7`
   - 复杂 SPA / 动态组件:直接 JS —— `web_execute_js script="document.querySelector('...').click()"`
4. **滚动加载的页面** —— `web_scroll down 2000` 或 `web_execute_js script="window.scrollBy(0,2000)"`

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
- ❌ 大结果直接全部返回 —— `web_execute_js` 支持 `save_to_file` 参数把结果存文件,再 `file_read` 选看
- ❌ 操作完不验证预期变化 —— 跳转 / 提交后再 scan 一下确认
- ❌ 用 `web_click` 点普通锚点 —— 对静态链接,`web_navigate` 到目标 URL 更直接
- ❌ 开第二个页面还用 `web_navigate` 不带 `new_tab` —— 会覆盖当前 tab 丢上下文

## 典型场景: 临时邮箱 + 注册某站

用户经常会让你"用临时邮箱注册一下 XX 网站"。两 tab 并行的标准流程:

```
# 1. 临时邮箱开第一 tab
web_navigate { url: "https://temp-mail.org/", new_tab: true }
web_scan                                           # 拿到邮箱地址 a1 / 文本元素
# 读到地址, 这个 tab 留着等验证邮件

# 2. 目标注册站开第二 tab(保留临时邮箱 tab 的上下文)
web_navigate { url: "https://target.com/signup", new_tab: true }
web_scan                                           # 扫表单短 ID
web_type { id: "a2", text: "<步骤 1 里那个临时邮箱>" }
web_type { id: "a3", text: "一个强密码" }
web_click { id: "a4" }                             # 提交
# 或者一条 execute_js 全搞:
#   web_execute_js { script: "
#     document.querySelector('#email').value='xx@yy.com';
#     document.querySelector('#pw').value='PassW0rd';
#     document.querySelector('form').submit();
#   " }

# 3. 切回临时邮箱 tab 看验证邮件
web_scan { tabs_only: true }                       # 看所有 tab, 找 temp-mail 那个的 id / 序号
web_scan { switch_tab_id: "temp-mail" }            # url 片段或序号都行, 切完立即扫
# 找到验证邮件 → 点开 → 提取链接 → web_navigate 过去完成验证
```

**Tip**: `web_execute_js` 里 `window.open(url)` 也能开新 tab,执行结果尾部会自动
带 `[new tab(s) opened: <id> <url>]`,下一步用 `switch_tab_id=<id>` 切过去。

Captcha / 图形验证码: `web_*` 不能识别图像。停下来 `ask_user`,让用户手动过一步
(交互模式默认 headful 浏览器用户能看见)。

## 截屏 / 视觉

`web_*` 工具**不是** 视觉工具。需要看图(验证码 / chart / 视觉定位)时:

- 让 JS 截屏:`web_execute_js { code: "<保存 canvas 成 png 的代码>" }`
- 然后 `file_read` 路径、送给有视觉能力的 provider(Claude Sonnet 4.x / GPT-4o)

## 会话复用

Chrome 用 `--user-data-dir` 保持登录态 —— cookies / 本地存储都在那。bot 用户名密码先登一次,之后 bonsai 接上去可以直接复用登录状态。别每次都重新填密码。
