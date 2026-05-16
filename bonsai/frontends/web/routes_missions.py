"""Harness missions read-only views.

Backed by the external `harness-cli` binary; this router only shells out
and forwards the JSON. mission state remains a CLI-owned file tree under
~/.bonsai/missions/<id>/ (override via env BONSAI_MISSIONS_DIR).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

import orjson
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)


def _missions_root() -> Path:
    raw = os.environ.get("BONSAI_MISSIONS_DIR") or str(Path.home() / ".bonsai" / "missions")
    return Path(raw).expanduser().resolve()


def _cli_path() -> str | None:
    return shutil.which("harness-cli")


def _snapshot(mroot: Path) -> tuple[str, dict]:
    """Return (signature, payload). Signature changes whenever any mission's
    status or progress changes — clients should dedupe by it."""
    if not _cli_path():
        return "no-cli", {"ok": False, "missions": [], "error": "harness-cli not installed",
                          "missions_root": str(mroot)}
    if not mroot.exists():
        return "empty-root", {"ok": True, "missions": [], "missions_root": str(mroot)}
    code, out, err = _run_cli(["active", str(mroot)])
    if code != 0:
        return f"err-{code}", {"ok": False, "missions": [],
                               "error": err.decode("utf-8", "replace").strip() or f"exit {code}",
                               "missions_root": str(mroot)}
    try:
        data = orjson.loads(out)
    except Exception as e:
        return "parse-err", {"ok": False, "missions": [], "error": f"parse failed: {e}",
                             "missions_root": str(mroot)}
    data["missions_root"] = str(mroot)
    sig_parts = sorted(
        f"{m.get('id')}:{m.get('status')}:{m.get('progress',{}).get('done',0)}:{m.get('progress',{}).get('total',0)}"
        for m in (data.get("missions") or [])
    )
    sig = "|".join(sig_parts) or "empty"
    return sig, data


def _run_cli(args: list[str], *, timeout: float = 10.0) -> tuple[int, bytes, bytes]:
    cli = _cli_path()
    if not cli:
        return 127, b"", b"harness-cli not on PATH"
    try:
        proc = subprocess.run(
            [cli, *args],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, b"", b"harness-cli timed out"
    return proc.returncode, proc.stdout, proc.stderr


def make_router(root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/missions")
    async def list_missions() -> JSONResponse:
        mroot = _missions_root()
        if not _cli_path():
            return JSONResponse({
                "ok": False,
                "error": "harness-cli not installed",
                "missions": [],
                "missions_root": str(mroot),
            }, status_code=200)
        if not mroot.exists():
            return JSONResponse({
                "ok": True,
                "missions": [],
                "missions_root": str(mroot),
            })
        code, out, err = _run_cli(["active", str(mroot)])
        if code != 0:
            return JSONResponse({
                "ok": False,
                "error": err.decode("utf-8", "replace").strip() or f"exit {code}",
                "missions": [],
                "missions_root": str(mroot),
            }, status_code=200)
        try:
            data = orjson.loads(out)
        except Exception as e:
            return JSONResponse({
                "ok": False,
                "error": f"parse failed: {e}",
                "missions": [],
                "missions_root": str(mroot),
            }, status_code=200)
        data["missions_root"] = str(mroot)
        return JSONResponse(data)

    @router.get("/api/missions/stream")
    async def stream_missions():
        """SSE stream — emit a snapshot every poll_interval seconds + when
        state changes. The signature it sends per event is a hash of
        (mission_id, status, done, total) tuples so the client can dedupe.
        """
        mroot = _missions_root()

        async def event_gen():
            poll_interval = 3.0
            last_sig: str | None = None
            yield b": stream-open\n\n"
            while True:
                try:
                    sig, payload = _snapshot(mroot)
                    if sig != last_sig:
                        last_sig = sig
                        data = orjson.dumps(payload).decode("utf-8")
                        yield f"event: snapshot\ndata: {data}\n\n".encode("utf-8")
                    else:
                        yield b": heartbeat\n\n"
                except Exception as e:
                    log.warning("mission stream snapshot failed: %s", e)
                    yield f"event: error\ndata: {orjson.dumps({'error': str(e)}).decode()}\n\n".encode("utf-8")
                try:
                    await asyncio.sleep(poll_interval)
                except asyncio.CancelledError:
                    return

        return StreamingResponse(event_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.get("/api/missions/{mission_id}")
    async def get_mission(mission_id: str, task_id: str | None = None) -> JSONResponse:
        # Guard against traversal — mission_id must be a single path segment.
        if "/" in mission_id or ".." in mission_id or mission_id.startswith("."):
            raise HTTPException(400, "invalid mission_id")
        mroot = _missions_root()
        mdir = (mroot / mission_id).resolve()
        if not str(mdir).startswith(str(mroot)):
            raise HTTPException(400, "mission_id escapes missions_root")
        if not _cli_path():
            raise HTTPException(503, "harness-cli not installed")
        if not (mdir / "mission.json").exists():
            raise HTTPException(404, f"mission {mission_id} not found")
        args = ["get", str(mdir)]
        if task_id:
            if "/" in task_id or ".." in task_id:
                raise HTTPException(400, "invalid task_id")
            args.append(task_id)
        code, out, err = _run_cli(args)
        if code != 0:
            return JSONResponse({
                "ok": False,
                "error": err.decode("utf-8", "replace").strip() or f"exit {code}",
            }, status_code=200)
        try:
            return JSONResponse(orjson.loads(out))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"parse failed: {e}"}, status_code=200)

    return router
