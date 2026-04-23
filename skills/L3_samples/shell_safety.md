---
name: shell_safety
keywords: [shell, bash, 命令, 执行, rm, 危险操作, code_run]
created: 2026-04-22
verified_on: 2026-04-22
---

# 跑 shell 命令之前

## 何时用

每次要通过 `code_run` / `bash` 动系统的时候先过一遍这个清单。

## 破坏性命令 — 先问再做

不确定的,先 `ask_user`。尤其是:

| 危险 | 安全替代 |
|---|---|
| `rm -rf <dir>` | 先 `ls <dir>` 看看内容,拿到确认再删 |
| `git reset --hard` / `git push --force` | 永远先问;永远不在 main/master 强推 |
| `DROP TABLE` / `DELETE FROM ... WHERE 1=1` | 先 `SELECT COUNT(*)` 确认范围 |
| `chmod -R 777` | 几乎没正当理由,先问 |
| `kill -9 <pid>` / `pkill` | 先 `ps aux | grep` 确认抓的是对的进程 |
| `mv` / `cp` 覆盖已有文件 | 目标存在时加 `-n` 或先重命名备份 |

## 路径带空格 / 中文

用双引号包住,**不要** 裸引用:

```bash
cd "/path with spaces/folder"
cp "a b.txt" "c.txt"
```

## 先 dry-run

能测不执行的都先试:

- `bash -n script.sh` 只语法检查,不跑
- `rsync -n ...` dry run
- `find ... -print` 看匹配到啥,再改成 `-delete`
- SQL 先 `EXPLAIN` / `SELECT` 再 `UPDATE`/`DELETE`

## 长命令 / 耗时任务

- 跑超过 30s 的 → 用后台模式(`run_in_background`),别阻塞回合
- 记得加 timeout,避免挂死
- 输出太多就 pipe 到 `head -50` / `tail -50`,别灌满上下文

## Sandbox / 权限模式注意

- 如果运行在沙箱里,系统级的改动(`apt install` / 写 `/etc/`)可能悄悄失败
- 如果被拒绝,不要反复试 —— 告诉用户要用 `dangerouslyDisableSandbox` 或换环境

## 不要做

- ❌ `cd <cwd>` 再跑 git —— git 本来就在当前目录工作,这么写会触发权限弹窗
- ❌ `sudo` —— 除非用户明确要求,`sudo` 几乎永远是错答案
- ❌ 串联危险命令(`rm -rf foo && ...`)—— 前一个挂了会怎样?逐步来
