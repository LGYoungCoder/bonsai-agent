---
name: search_codebase
keywords: [搜代码, grep, ripgrep, find, 找函数, 查关键字, 项目结构]
created: 2026-04-22
verified_on: 2026-04-22
---

# 在代码库里查东西

## 何时用

- 用户说"找一下哪里定义了 X"
- 改一处前,要先确认还有哪些引用点
- 回答"这个项目怎么处理 Y" 类问题

## 首选 ripgrep,fallback grep

```bash
rg "pattern" -n              # 带行号,默认跳过 .gitignore
rg "pattern" -n --type py    # 只看 py 文件
rg -l "pattern"              # 只出文件名
rg "foo|bar" -n              # 或
rg "defs?\s+foo" -n          # 正则
```

没有 `rg` 用 `grep`:

```bash
grep -rn "pattern" --include="*.py" .
```

## 找文件用 find

```bash
find . -name "*.py" | head
find . -type d -name "tests"
# 长扩展名放前面(alt 用 `-regex` 时):
find . -regex '.*\.\(tsx\|ts\)$'
```

## 搜出一堆,怎么筛

把 grep 结果收窄:

```bash
rg "foo" -n | grep -v test     # 排除测试文件
rg "foo" -n -l | wc -l         # 看涉及多少文件
```

太多结果(>30)的时候:

1. 先告诉用户"匹配 N 处,要看全部吗"
2. 或者用更精确的 pattern(加函数签名前缀)
3. 或者按目录分批 `rg foo src/`, `rg foo tests/`

## 找"这个接口是怎么跑通的"

思路:先找入口,再顺着 import / 调用链下去。

```bash
# 1. 找 URL 路由定义
rg "path.*foo|route.*foo" -n --type py

# 2. 找 handler 函数名
rg "def handle_foo|class FooHandler" -n

# 3. 看 handler 调用了啥
# 用 Read 工具读 handler 文件,比 grep 更好理解
```

## 何时应该用 Explore subagent 而不是自己 grep

如果:

- 问题跨多个目录 / 多种命名约定
- 预计要跑 > 3 次 grep 才能定位
- 范围模糊("这个系统怎么处理 xxx")

→ 用 `Agent(subagent_type=Explore)` 一次搞定,别在当前上下文里刷一堆 grep 结果。

## 不要做

- ❌ 盲扫整个仓库 `grep -r`(慢,污染上下文)
- ❌ 用 `find | xargs grep` —— 慢且不支持 gitignore
- ❌ 发现一处就停 —— 问题往往有 2-3 处同步要改
- ❌ 跑完不总结就回复用户 —— 告诉他"匹配到 N 处,分别在:..."
