"""Managed Chromium — spawn an isolated headed browser with --remote-debugging-port.

Complements the existing "attach" flow (connect to a user-launched Chrome).
Managed mode is opt-in: gives you a one-command browser without asking the
user to relaunch their own Chrome with debug flags. Login state persists in
a dedicated profile dir under ~/.bonsai/chromium_profile/.

Not a process supervisor. Lifetime is scoped to a single BrowserSession —
caller must call .close() to reclaim the port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_DEFAULT_PROFILE = Path.home() / ".bonsai" / "chromium_profile"

_CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_chromium() -> str | None:
    """Locate a usable chromium/chrome binary on PATH or known macOS locations."""
    for name in _CHROME_CANDIDATES:
        # Direct path
        p = Path(name)
        if p.is_absolute() and p.exists() and os.access(p, os.X_OK):
            return str(p)
        # PATH lookup
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port(preferred: int = 9222) -> int:
    """Return preferred if free, else an OS-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ManagedChromium:
    """Owns one chromium subprocess. Not reentrant — one per BrowserSession."""

    profile_dir: Path = field(default_factory=lambda: _DEFAULT_PROFILE)
    headless: bool = False
    port: int = 0  # 0 = auto-pick
    extra_args: tuple[str, ...] = ()
    _proc: subprocess.Popen | None = field(init=False, default=None)
    _binary: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.profile_dir = Path(self.profile_dir).expanduser().resolve()

    @property
    def debug_url(self) -> str:
        if not self.port:
            raise RuntimeError("chromium not started yet")
        return f"http://127.0.0.1:{self.port}"

    async def start(self, *, startup_timeout: float = 15.0) -> str:
        """Launch chromium and block until /json/version responds."""
        if self._proc is not None:
            return self.debug_url
        binary = find_chromium()
        if not binary:
            raise RuntimeError(
                "No chromium/chrome binary found. Install one, or use "
                "`--browser http://127.0.0.1:9222` to attach to a running Chrome."
            )
        self._binary = binary
        self.port = _free_port(self.port or 9222)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Keep it simple — no extensions, no sync. Login state still
            # persists in the profile dir.
            "--disable-features=Translate",
        ]
        if self.headless:
            argv += ["--headless=new", "--disable-gpu"]
        argv += list(self.extra_args)
        log.info("spawning chromium: %s", " ".join(argv[:3]))
        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        await self._wait_ready(startup_timeout)
        return self.debug_url

    async def _wait_ready(self, timeout: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient(timeout=2.0) as cli:
            while asyncio.get_event_loop().time() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise RuntimeError(
                        f"chromium exited with code {self._proc.returncode} "
                        "before becoming ready"
                    )
                try:
                    r = await cli.get(f"{self.debug_url}/json/version")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError(
            f"chromium at {self.debug_url} didn't respond in {timeout}s"
        )

    async def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            proc.terminate()
            for _ in range(50):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
        except Exception as e:
            log.warning("chromium close() failed: %s", e)
