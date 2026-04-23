---
name: edit_files
keywords: [编辑文件, 修改代码, file_patch, file_write, 改文件]
created: 2026-04-22
verified_on: 2026-04-22
---

# 改文件的铁律

## 何时用

每次改代码 / 配置 / 文档前都看一眼。

## 核心流程

1. **先读** — 改之前一定先 `Read` 一遍,把当前内容拿进上下文
2. **小改** — 优先 `Edit` / `file_patch` 改局部,不要 `Write` / `file_write` 整文件重写
3. **唯一匹配** — `Edit` 的 `old_string` 必须在文件里出现一次;多处就把 context 扩大到唯一

## 什么时候 Write 可以

- 新建文件
- 整文件重构(用户明确要求)
- 内容 80% 以上要改(Edit 一遍不如重写)

其他都用 Edit / 多次小 patch。

## 缩进 / 空白

从 `Read` 的输出里抠 `old_string` 时:

- 行号前缀是 `\d+\t`,真正内容从 Tab 之后开始
- **不要** 把行号前缀带进 `old_string`
- 保留原始的 Tab / 空格缩进(**不要** 转换)

## 改不上怎么办

- `Edit` 报 "old_string not unique" → 把上下 2-3 行带进去扩大 snippet
- `Edit` 报 "old_string not found" → 重新 `Read` 确认当前真实内容(可能已经被前面的 Edit 改过)
- 要跨 3+ 处改同一个名字 → 用 `replace_all: true` 一次搞定,不要逐个 Edit

## 敏感文件先问

- `config.toml`、`pyproject.toml`、`package.json`、CI 配置、`.env` → 改之前告诉用户要改什么,等确认
- 根目录 markdown(README / CHANGELOG)→ 一般要问
- `.gitignore` / `.gitattributes` → 要问

## 不要做

- ❌ 不读就改 —— 猜的内容 90% 概率对不上
- ❌ 用 `Bash` 跑 `sed` / `awk` 改文件 —— 用 `Edit` 更明确更可审阅
- ❌ 留半截改动(改一半转去干别的)
- ❌ 写注释解释"我改了什么 / 为什么改" —— 解释进 PR / commit message,别污染代码
- ❌ 删代码时留 `# removed xxx` 之类的墓碑注释

## 删除代码

确定不用就直接删,不要留:

```python
# ❌ 不要
# old_function = ...  # removed 2026-04-22
# def helper(): pass  # unused

# ✓ 直接删干净
```

grep 确认没人调用再删。
