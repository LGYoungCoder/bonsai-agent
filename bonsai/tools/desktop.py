"""Cross-platform desktop automation helpers — not a bonsai tool, just
a Python module the agent imports from inside `code_run`.

Why a module, not a tool:
- Keeping it out of the core tool surface avoids risk to the ~100-line
  agent loop and lets users without a display skip `pip install bonsai[desktop]`.
- pyautogui is itself cross-platform (Windows / macOS / Linux-X11);
  Wayland and headless Linux need the DISPLAY env set.

Typical agent usage (through code_run):

    from bonsai.tools.desktop import screenshot, click, type_text, press, find_image
    path = screenshot()              # file_read the PNG afterwards for visual reasoning
    click(120, 300)
    type_text("hello")
    press("ctrl+s")
    pos = find_image("./assets/ok_button.png", confidence=0.85)
    if pos: click(*pos)

All calls are synchronous and thin — import pyautogui lazily so the
rest of bonsai stays importable without the dep.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_PYAUTOGUI_ERR = (
    "桌面控制需要 pyautogui,请 `pip install bonsai[desktop]`。"
    " Linux 还要保证 DISPLAY 环境变量已设 + 跑在 X11 会话(Wayland 不支持)。"
)


def _require():
    try:
        import pyautogui   # type: ignore
    except ImportError as e:
        raise RuntimeError(_PYAUTOGUI_ERR) from e
    return pyautogui


def _screens_dir() -> Path:
    d = Path.cwd() / "data" / "desktop_screens"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─────────── Visual ───────────

def screen_size() -> tuple[int, int]:
    p = _require()
    w, h = p.size()
    return int(w), int(h)


def screenshot(path: str | Path | None = None, region: tuple[int, int, int, int] | None = None) -> str:
    """Save a PNG; return absolute path. `region = (x, y, w, h)` 只截部分屏。"""
    p = _require()
    out = Path(path) if path else (_screens_dir() / f"shot_{int(time.time())}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = p.screenshot(region=region) if region else p.screenshot()
    img.save(str(out))
    return str(out.resolve())


def find_image(template_path: str | Path, *, confidence: float = 0.9,
               region: tuple[int, int, int, int] | None = None) -> tuple[int, int] | None:
    """Locate a template PNG on screen. Returns center (x, y) or None.
    需要 opencv-python(pyautogui 靠它做模糊匹配)。"""
    p = _require()
    try:
        box = p.locateOnScreen(str(template_path), confidence=confidence, region=region)
    except Exception:
        return None
    if box is None:
        return None
    return int(box.left + box.width // 2), int(box.top + box.height // 2)


# ─────────── Mouse ───────────

def move(x: int, y: int, *, duration: float = 0.15) -> None:
    _require().moveTo(x, y, duration=duration)


def click(x: int | None = None, y: int | None = None, *,
          button: str = "left", clicks: int = 1, interval: float = 0.05) -> None:
    p = _require()
    if x is None or y is None:
        p.click(button=button, clicks=clicks, interval=interval)
    else:
        p.click(x=x, y=y, button=button, clicks=clicks, interval=interval)


def drag(from_xy: tuple[int, int], to_xy: tuple[int, int], *,
         duration: float = 0.25, button: str = "left") -> None:
    p = _require()
    p.moveTo(*from_xy, duration=0.1)
    p.dragTo(*to_xy, duration=duration, button=button)


def scroll(amount: int) -> None:
    """Positive = up, negative = down. Unit is 'clicks' of the wheel."""
    _require().scroll(amount)


# ─────────── Keyboard ───────────

def type_text(text: str, *, interval: float = 0.02) -> None:
    """ASCII text only. For unicode / IME use the target app's paste hotkey."""
    _require().typewrite(text, interval=interval)


def press(keys: str, *, presses: int = 1) -> None:
    """Keybinding syntax: `enter`, `ctrl+s`, `shift+tab`. Use +-separated for combos."""
    p = _require()
    if "+" in keys:
        p.hotkey(*[k.strip() for k in keys.split("+")])
    else:
        for _ in range(presses):
            p.press(keys)


# ─────────── High-DPI helper ───────────

def dpi_scale() -> float:
    """Logical / physical ratio. pyautogui already accepts logical coords on
    Mac Retina / Win 125%;返回实际 scale 供需要时换算。"""
    p = _require()
    w_logical, _ = p.size()
    try:
        # pyautogui internally uses Xlib/CG/User32 which report the active
        # monitor's pixel size. Good enough as a diagnostic.
        import ctypes, sys
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            w_phys = user32.GetSystemMetrics(0)
            return w_phys / w_logical if w_logical else 1.0
    except Exception:
        pass
    return 1.0


def safe_abort_on_corner() -> None:
    """pyautogui 的保命功能:鼠标扔到屏幕左上角 (0,0) 即抛 FailSafeException。默认已开。"""
    import pyautogui as p   # require()'d already by caller
    p.FAILSAFE = True
