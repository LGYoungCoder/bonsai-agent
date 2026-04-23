---
name: install_python_deps
keywords: [安装依赖, pip install, python package, 装库, ModuleNotFoundError]
created: 2026-04-21
verified_on: 2026-04-21
platforms: [linux, macos, windows]
evidence_turns: [3, 5, 7]
---

# 给 Python 项目装依赖

## 何时用这份 SOP

- 新项目第一次跑,遇到 `ModuleNotFoundError`
- 已有项目,用户说"把 X 库装上"
- 用户要求"把所有用得上的依赖装好"

## 前置条件

- 有 `python3` / `pip` 可用(如果没有,先读 `install_python.md`)
- 工作目录里有 Python 代码

## 步骤

### 1. 探测当前 Python 环境

```bash
which python3 && python3 --version
python3 -m pip --version
```

如果 `pip` 报错,用 `python3 -m ensurepip --upgrade`。

### 2. 判断装依赖的位置

按优先级:

1. 已有 `.venv/` 或 `venv/` → 激活后装
2. 有 `pyproject.toml` + `[project]` → `pip install -e .`
3. 有 `requirements.txt` → `pip install -r requirements.txt`
4. 代码直接 `import` 但没依赖清单 → 扫 `import` 语句,挑第三方库逐个装

### 3. 扫 import 生成依赖清单(没清单时)

```python
# find third-party imports
import ast, os, sys

stdlib = set(sys.stdlib_module_names)
thirdparty = set()

for root, _, files in os.walk("."):
    if any(skip in root for skip in [".venv", "__pycache__", ".git", "node_modules"]):
        continue
    for f in files:
        if not f.endswith(".py"): continue
        try:
            tree = ast.parse(open(os.path.join(root, f)).read())
        except: continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    mod = n.name.split(".")[0]
                    if mod not in stdlib:
                        thirdparty.add(mod)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module.split(".")[0]
                if mod not in stdlib and node.level == 0:
                    thirdparty.add(mod)

print("\n".join(sorted(thirdparty)))
```

### 4. 安装

**常见 import 名 ≠ pip 包名**的映射表(踩过坑的):

| import 名 | pip 包名 |
|---|---|
| `cv2` | `opencv-python` |
| `PIL` | `pillow` |
| `yaml` | `pyyaml` |
| `sklearn` | `scikit-learn` |
| `bs4` | `beautifulsoup4` |
| `Crypto` | `pycryptodome` |
| `dotenv` | `python-dotenv` |
| `telegram` | `python-telegram-bot` |
| `docx` | `python-docx` |
| `lark_oapi` | `lark-oapi` |

装之前先查映射表,没有的按 import 名直接装(大部分 80% 是一致的)。

用镜像(国内):
```bash
pip install <pkgs> -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 5. 验证

装完后 **一定** 再跑一次主脚本,确认没新的 `ModuleNotFoundError`:

```bash
python3 main.py 2>&1 | head -20
```

如果还有 import 错,**不要继续装**——先读错误,可能是:
- 版本冲突(装了但 import 失败)
- 间接依赖缺失(装 X 但 X 需要 Y)
- Python 版本不兼容(要求 3.10+ 但你是 3.9)

此时循环第 2-5 步,最多 3 轮仍不通就 `ask_user`。

## 典型坑

- **不要用 `sudo pip`**:污染系统 Python,后面各种奇怪问题
- **ARM64 Mac 装旧库**:可能需要 `arch -x86_64 pip install ...` 或找 universal wheel
- **Windows pywin32**:装完需要 `python Scripts/pywin32_postinstall.py`
- **科学计算大库**(numpy/torch/tensorflow):首次装会拉几 GB,加 `--progress-bar on`,timeout 调到 600
- **内网机器**:pip 默认连不上 pypi,需要用镜像或 offline wheel

## 不要做

- ❌ 盲目跑 `pip install <all>` 一次装一堆(一个失败全部 rollback 费时间)
- ❌ 装完不验证就说"搞定了"(真正的"搞定"是 main 能跑起来)
- ❌ 遇到版本冲突就 `pip install --force-reinstall`(可能搞坏其他项目)

## Evidence

- [x] Linux (Ubuntu 22.04, Python 3.11): 在 2026-04-21 测试通过
- [x] macOS (ARM64, Python 3.12): 同日测试通过
- [ ] Windows: 未测试,理论应工作
