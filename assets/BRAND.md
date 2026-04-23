# Bonsai Brand Identity

## 核心意象

**一棵被刻意约束的小树，在精心培育中成为独一无二的艺术品。**

品牌的每一个视觉元素都要传达两个张力：
- **克制**（约束、最小化、刻意）
- **生长**（演化、耐心、独特）

不要热闹，不要跑马灯，不要渐变。**安静的、可以看很久的东西**。

---

## 色板

### 主色（Primary）

| 名称 | Hex | 用法 |
|---|---|---|
| **Ink 墨** | `#1A1F1A` | 正文字体 / 深色主背景 |
| **Matcha 抹茶** | `#7A9268` | 主品牌色（logo 树冠、按钮、强调） |
| **Paper 宣纸** | `#F5F0E8` | 浅色背景 / 卡片 |

### 辅色（Accent）

| 名称 | Hex | 用法 |
|---|---|---|
| **Clay 陶土** | `#B8754A` | logo 盆 / 品牌副色 |
| **Bark 树皮** | `#3D2817` | 树干 / 深色描边 |
| **Moss 苔藓** | `#5A7248` | 深一档的抹茶，用于 hover |
| **Leaf 嫩叶** | `#A0B88C` | 亮一档的抹茶，用于高光 |

### 状态色（Semantic）

| 名称 | Hex | 用法 |
|---|---|---|
| **Cinnabar 朱砂** | `#B84E3D` | Error / Destructive |
| **Amber 琥珀** | `#D4A574` | Warning / Spending |
| **Jade 青瓷** | `#4A8B7C` | Success |
| **Mist 雾灰** | `#8A8F86` | Disabled / Secondary text |

### 使用比例（Dark Mode 为主）

```
Background (Ink)          ████████████████  60%
Card bg (Ink +5% warm)    ████              15%
Text (Paper)              ████              15%
Matcha / Clay             ██                 8%
Accent (jade/amber/...)   ▌                  2%
```

**永远不要**让品牌色占超过 15% 的视觉面积。克制是核心。

---

## 字体

### Sans-serif（UI 主体）

- **英文/数字**: `Inter`, `IBM Plex Sans`, `system-ui`
- **中文**: `Noto Sans SC`, `PingFang SC`, `Hiragino Sans GB`

回退栈：
```css
font-family: "Inter", "Noto Sans SC", "PingFang SC", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
```

### Monospace（代码 / 工具调用）

- `JetBrains Mono`, `IBM Plex Mono`, `Menlo`

### 字重纪律

- 只用 3 个字重：**Regular (400)**, **Medium (500)**, **Bold (700)**
- 不用 Light/Thin（在简体中文上渲染不清）
- 不用 Black/Heavy（视觉过重，违背克制）

### 字号阶梯

```
xs   11px   辅助信息、token 用量
sm   13px   次要文字、说明
base 15px   正文（默认）
lg   17px   小标题
xl   20px   标题
2xl  24px   页面大标题
3xl  32px   品牌名展示（罕用）
```

行高：正文 1.6，标题 1.3。

---

## Logo 设计

### 理念

**一个盆 + 一棵树 + 微小的生长点**：
- 盆是"约束"
- 树是"已经生长的部分"
- 顶端的小点是"永远在生长的未来"

### 变体

`assets/logo.svg` — 主 logo，用于网站 header、Web UI、README。

设计特点：
- 盆：梯形简化（陶土色）
- 树干：单笔曲线，带"悬崖式"（半悬崖 bonsai 姿态）
- 树冠：2-3 块不等大的云朵形
- 顶点：一粒琥珀色"种子点"（生长的暗示）

### 使用规范

- **最小尺寸**：24px（favicon）不再小
- **留白**：logo 四周留至少 1/4 logo 宽度的空白，不贴边
- **禁止**：
  - 改颜色（除非到白色单色版）
  - 加投影 / 渐变 / 发光
  - 改比例 / 拉伸
  - 配文字做组合 logo（品牌名 `Bonsai` 用 Inter Medium 单独放，不贴着 logo）

### 单色版

白色单色版：所有元素统一为 `#F5F0E8`（Paper）。用于深色背景、印刷、水印。

黑色单色版：所有元素统一为 `#1A1F1A`（Ink）。用于浅色背景、文档图标。

---

## 吉祥物（Mascot）

`assets/mascot.svg` — **盆小友**（Pén Xiǎo Yǒu），一个拟人化的小盆栽。

用法：
- 空状态插图（"暂无记忆"、"开始你的第一个任务"）
- 引导教程
- 错误页面的陪伴元素
- 不用在严肃的数据展示区域

性格设定：
- 安静、耐心
- 不张扬，但有主见
- 喜欢被慢慢关照
- **表情克制**，大多是眯着眼的淡笑（而不是卡通式的大笑）

---

## 语气（Tone of Voice）

### 品牌写作原则

1. **安静**：不感叹号，不全大写，不浮夸形容词
2. **诚实**：做不到就说做不到，不承诺"无所不能"
3. **克制**：三句话能说清的不用五句
4. **温和的主见**：给出推荐但不强迫；用户坚持另一种方式就尊重

### 好/坏示例

❌ `🎉🎉🎉 欢迎使用 Bonsai！最智能的 AI Agent！让我们一起开启超棒的旅程吧！！！`

✅ `🌱 欢迎。你可以用 /help 查看命令，或直接说需求。`

---

❌ `处理失败了，请检查你的配置！！`

✅ `我连接 GLM 时超时了（15 秒）。可能是网络，也可能是密钥过期。要不要试试 DeepSeek？`

---

❌ `🔥 BONSAI 全新升级，性能飙升 300%！`

✅ `v0.2 发布：记忆系统接入。旧会话可以 /continue 恢复。`

---

### 多语言

- 中文用户优先用中文，但不强制（用户第一句是英文就全程英文）
- 技术术语保留英文（`cache`, `embedding`, `tool_use`），不生硬翻译
- 避免中英混搭的浮夸风（"这是 amazing 的 feature"）

---

## 声音（未来预留）

如果 Bonsai 将来做语音前端：
- 中速、温柔、不卡顿
- 不做"播报员"那种过度精神的声音
- 男声 / 女声都提供，但默认中性略女性（研究表明多数用户这样更舒服）

---

## 品牌禁忌

❌ 不要用**动物拟人**（不做"AI 助理猫"、"agent 小狗"，和 bonsai 意象冲突）

❌ 不要用**机器人意象**（齿轮、金属、机械臂）

❌ 不要用**赛博朋克风**（霓虹、故障艺术、矩阵字幕）

❌ 不要用**强烈渐变**（紫蓝渐变是 AI 产品滥俗标签）

✅ **可以**借鉴的视觉系：
- 日式禅意（枯山水、茶道器具、版画）
- 明清文人画（墨竹、梅兰、砚台）
- 现代极简（Dieter Rams / Muji）
- 手工艺（陶器、木工、纸制）

---

## 资产清单

| 文件 | 用途 |
|---|---|
| `assets/logo.svg` | 主 logo |
| `assets/logo-white.svg` | 深色背景用 |
| `assets/logo-black.svg` | 浅色背景印刷用 |
| `assets/mascot.svg` | 盆小友吉祥物 |
| `assets/favicon.svg` | 浏览器标签 / 桌面图标 |
| `assets/og-image.png` | 分享卡片（1200×630） |
| `assets/prototypes/config.html` | 配置页 UI 原型 |
| `assets/prototypes/chat.html` | 对话页 UI 原型 |

---

## License

品牌资产采用 CC BY-NC-SA 4.0（允许非商业使用 + 署名 + 同方式共享）。二次创作做成 skill 卡片、emoji、社区周边都欢迎，但**商业用途需联系授权**。
