"""Fire-and-forget archiver.

From the agent loop we just call `schedule_ingest(session_file, config)`. It
spawns a subprocess that runs drawer_ingester and returns immediately — the
user's chat is never blocked waiting on embeddings or disk I/O.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def schedule_ingest(session_file: Path, *, db_path: Path,
                    embed_provider: str = "hash",
                    embed_api_key: str = "",
                    embed_base_url: str = "",
                    embed_model: str = "",
                    wing: str | None = None,
                    room: str | None = None,
                    closet: str = "life") -> subprocess.Popen | None:
    """Spawn the drawer_ingester in a detached subprocess. Returns Popen or None."""
    if not session_file.exists():
        log.debug("no session file to archive: %s", session_file)
        return None

    cmd = [sys.executable, "-m", "bonsai.writer.drawer_ingester",
           str(session_file), "--db", str(db_path),
           "--closet", closet,
           "--embed-provider", embed_provider]
    if wing:
        cmd += ["--wing", wing]
    if room:
        cmd += ["--room", room]
    if embed_api_key:
        cmd += ["--embed-api-key", embed_api_key]
    if embed_base_url:
        cmd += ["--embed-base-url", embed_base_url]
    if embed_model:
        cmd += ["--embed-model", embed_model]

    log.info("spawning archiver for %s", session_file)
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return p
    except Exception as e:
        log.warning("failed to spawn archiver: %s", e)
        return None
