---
name: git_commit
keywords: [git commit, 提交, git add, git status, git log, commit message]
created: 2026-04-22
verified_on: 2026-04-22
---

# 提交代码改动

## 何时用

- 用户说"提交一下" / "把改动 commit" / "git commit"
- 自己改完代码且用户授权了,需要落一个 commit

## 铁律

1. **不要** amend 已有 commit(除非用户明说),改动都开新 commit
2. **不要** `git add -A` / `git add .` — 按文件名精确 add,避免误带 `.env` / 大文件
3. **不要** 加 `--no-verify` 绕过 hook;hook 炸了先排查
4. **不要** 自己 `git push`,除非用户明确说要推

## 步骤

### 1. 看现状

并行跑(省时间):

```bash
git status
git diff --stat
git log --oneline -5
```

- `status` 看有哪些文件动了
- `diff` 明确要提交什么
- `log` 学习本仓库的 commit message 风格(中文 / 英文 / conventional / emoji)

### 2. 起草 commit message

跟本仓库既有风格保持一致:

- 如果是 `feat(xxx): ...` 就跟着写 `feat/fix/docs/refactor/test/chore`
- 如果都是中文就用中文
- 第一行 50 字内说"做了什么";空一行写"为什么"(复杂改动才写)

### 3. 精确 add + 提交

```bash
git add path/to/file1 path/to/file2
git commit -m "$(cat <<'EOF'
fix(module): 一句话说清改了啥

为什么要这么改(可选,一两行)
EOF
)"
git status   # 确认干净
```

### 4. 失败处理

- pre-commit hook 挂了:**不要** `--no-verify`。读错误修掉,重新 add + 新 commit(不要 amend,因为原 commit 从来没成立)
- 钩子 lint 报错:修完再来
- `git add` 报 pathspec 不存在:路径打错,重查

## 不要做

- ❌ `git push` — 留给用户
- ❌ `git reset --hard` / `git checkout .` — 会丢没提交的改动,先 `stash` 或 `diff` 检查
- ❌ `git commit -am` — 跟 `-A` 一样,可能漏或多
