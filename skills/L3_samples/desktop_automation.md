---
name: desktop_automation
keywords: [桌面, 鼠标, 键盘, 点击, 截屏, pyautogui, 桌面应用]
created: 2026-04-22
verified_on: 2026-04-22
---

# 驱动桌面原生应用(非浏览器)

## 先问一下自己

用浏览器能做的,永远优先走 `web_*` 工具。桌面控制只在这些情况用:

- 要操作的应用**没有 web 版**(IDE / 设计软件 / 游戏 / 桌面 IM)
- 是 webview-based 但你只能从外面点
- 要操作系统级菜单 / 通知 / 输入法

## 前置

```bash
pip install bonsai[desktop]        # 装 pyautogui + opencv-python
```

运行环境要求:

- **Linux**: X11 + `DISPLAY` 环境变量已设。Wayland **不支持**
- **macOS**: 首次需要在 系统偏好 → 隐私 → 辅助功能 里授权终端
- **Windows**: 默认能跑。高 DPI 屏幕坐标参考 §"高 DPI"

## 调用方式(不是工具,是模块)

通过 `code_run` 调,没扩工具表:

```python
from bonsai.tools.desktop import (
    screen_size, screenshot, find_image,
    move, click, drag, scroll,
    type_text, press,
)
```

## 黄金流程

### 1. 永远先 screenshot

不看屏幕就动手 = 瞎点。每次动作前截一张:

```python
path = screenshot()
print(path)
# 然后在回合里用 file_read 或带视觉的 provider 看图
```

小范围截:`screenshot(region=(x, y, w, h))`。

### 2. 定位用哪种方式

| 方式 | 适用 | 优点 | 缺点 |
|---|---|---|---|
| 固定坐标 `click(120, 300)` | UI 不会动 | 快、准 | 分辨率 / 窗口移动一挪就废 |
| 模板匹配 `find_image(png)` | UI 可能变位置 | 鲁棒一些 | 慢、需 opencv、模板要提前做 |
| 视觉模型找 | 完全不确定 | 最灵活 | 贵,要截屏喂多模态 LLM |

**优先级**:已知稳定 UI 用坐标 > 元素可能漂移用模板匹配 > 都不行再上视觉模型。

### 3. 操作后再 screenshot 验证

点完"保存"必须再截一张看有没有跳对话框 / 出 toast。别假设成功。

### 4. 慢即是快

鼠标移动默认 0.15s 补间,不要调到 0 —— 很多应用对瞬移点击没反应(以为是脚本攻击)。键盘输入默认每键 0.02s,超长文本用剪贴板粘贴代替打字(`press("ctrl+v")`)。

## 常见套路

### 关闭烦人弹窗

```python
screenshot()                   # 看是什么弹窗
pos = find_image("./assets/close_x.png", confidence=0.85)
if pos: click(*pos)
```

### 填个密码

**禁止直接 `type_text(password)`** —— 按键有键盘记录风险,而且很多密码框对自动化输入有特殊处理。正解:

1. 把 key 写入剪贴板(`pyperclip` 或 shell `xclip`)
2. `click` 聚焦密码框
3. `press("ctrl+v")`

### 拖放

```python
drag((100, 200), (500, 400), duration=0.4)
```

快拖不识别,至少 0.2s。

## 保命机制

- **屏幕左上角 (0,0) 是紧急停止** —— 任何时候把鼠标甩到左上角会立即抛 `FailSafeException`,中断循环
- 跑死循环之前用 `code_run` 起一个 timeout,别无限点

## 不要做

- ❌ **盲点** —— 不截图就 click 是赌
- ❌ **靠时序 sleep 等 UI 响应** —— 应该 screenshot 轮询直到目标元素出现
- ❌ 用 `type_text` 输中文 —— pyautogui 走键盘事件,非 ASCII 基本都废。走剪贴板粘贴
- ❌ 登录 / 输密码走自动化 —— 让用户自己登一次,cookies / session 留着下次复用
- ❌ 在 headless 服务器上跑桌面控制 —— 没有 DISPLAY,直接崩
