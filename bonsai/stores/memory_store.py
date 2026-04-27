"""MemoryStore — verbatim, BM25 + vector hybrid, scope-filterable.

Schema (SQLite + FTS5):

  closets(id, name)                      -- top-level container (e.g. "life", "work")
  wings(id, closet_id, name)             -- project / person / domain
  rooms(id, wing_id, name, ts)           -- conversation or topic episode
  drawers(id, room_id, kind, content,    -- atomic verbatim unit
          content_hash, ts, meta_json)
  drawers_fts(content)                   -- FTS5 mirror of drawers.content (BM25)
  drawers_vec(id, vector BLOB)           -- embeddings; L2-normalized float32

Hybrid search: BM25(FTS5) + cosine(vec) + recency boost.
"""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .embed import Embedder

log = logging.getLogger(__name__)


@dataclass
class Drawer:
    id: int | None
    room_id: int
    kind: str
    content: str
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    score: float = 0.0
    wing: str = ""
    room: str = ""


def _pack_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack_vector(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def _cosine(a: list[float], b: list[float]) -> float:
    # vectors already normalized, so dot product == cosine.
    return sum(x * y for x, y in zip(a, b, strict=False))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS closets (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS wings (
    id INTEGER PRIMARY KEY,
    closet_id INTEGER NOT NULL REFERENCES closets(id),
    name TEXT NOT NULL,
    UNIQUE(closet_id, name)
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    wing_id INTEGER NOT NULL REFERENCES wings(id),
    name TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rooms_wing_ts ON rooms(wing_id, ts DESC);
CREATE TABLE IF NOT EXISTS drawers (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ts REAL NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(content_hash, room_id)
);
CREATE INDEX IF NOT EXISTS idx_drawers_room_ts ON drawers(room_id, ts DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS drawers_fts USING fts5(
    content, content='drawers', content_rowid='id',
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS drawers_vec (
    drawer_id INTEGER PRIMARY KEY REFERENCES drawers(id),
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS drawers_ai AFTER INSERT ON drawers BEGIN
    INSERT INTO drawers_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS drawers_ad AFTER DELETE ON drawers BEGIN
    INSERT INTO drawers_fts(drawers_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS drawers_au AFTER UPDATE ON drawers BEGIN
    INSERT INTO drawers_fts(drawers_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO drawers_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class MemoryStore:
    def __init__(self, db_path: Path, embedder: Embedder | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: WeChat / 其他渠道 runner 用 PerUidSerializer
        # 给每个 uid 起 worker 线程,worker 里要访问主线程建好的 conn
        # (_refresh_prefix → wake_up_l1, memory_search 工具 → search)。默认会抛
        # ProgrammingError "objects created in a thread can only be used in that
        # same thread"。WAL + 读为主 + 写都在 ingest 子进程里,共用 conn 安全。
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self.embedder = embedder

    def close(self) -> None:
        self.conn.close()

    # ---- scope helpers -------------------------------------------------
    def _ensure_closet(self, name: str) -> int:
        self.conn.execute("INSERT OR IGNORE INTO closets(name) VALUES (?)", (name,))
        row = self.conn.execute("SELECT id FROM closets WHERE name=?", (name,)).fetchone()
        return row["id"]

    def _ensure_wing(self, closet: str, name: str) -> int:
        cid = self._ensure_closet(closet)
        self.conn.execute(
            "INSERT OR IGNORE INTO wings(closet_id, name) VALUES (?, ?)", (cid, name))
        row = self.conn.execute(
            "SELECT id FROM wings WHERE closet_id=? AND name=?", (cid, name)).fetchone()
        return row["id"]

    def _ensure_room(self, closet: str, wing: str, name: str,
                     ts: float | None = None) -> int:
        wid = self._ensure_wing(closet, wing)
        row = self.conn.execute(
            "SELECT id FROM rooms WHERE wing_id=? AND name=?", (wid, name)).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO rooms(wing_id, name, ts) VALUES (?, ?, ?)",
            (wid, name, ts or time.time()),
        )
        return cur.lastrowid

    # ---- ingest --------------------------------------------------------
    def ingest(self, *, closet: str, wing: str, room: str,
               kind: str, content: str, meta: dict | None = None,
               ts: float | None = None) -> int | None:
        """Add a drawer. Returns drawer id, or None if it was a duplicate."""
        room_id = self._ensure_room(closet, wing, room, ts=ts)
        h = _content_hash(content)
        existing = self.conn.execute(
            "SELECT id FROM drawers WHERE content_hash=? AND room_id=?",
            (h, room_id),
        ).fetchone()
        if existing:
            return None
        import orjson
        cur = self.conn.execute(
            "INSERT INTO drawers(room_id, kind, content, content_hash, ts, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (room_id, kind, content, h, ts or time.time(),
             orjson.dumps(meta or {}).decode("utf-8")),
        )
        drawer_id = cur.lastrowid

        if self.embedder is not None:
            try:
                vec = self.embedder.embed([content])[0]
                self.conn.execute(
                    "INSERT OR REPLACE INTO drawers_vec(drawer_id, dim, vector) "
                    "VALUES (?, ?, ?)",
                    (drawer_id, len(vec), _pack_vector(vec)),
                )
            except Exception as e:
                log.warning("embed failed for drawer %d: %s", drawer_id, e)

        self.conn.commit()
        return drawer_id

    # ---- read API ------------------------------------------------------
    def wake_up_l1(self, max_items: int = 12) -> str:
        """Byte-stable scope directory: alpha-sorted wing/room names.

        Used in FrozenPrefix, so must not embed timestamps or counts — those
        change as new drawers arrive and would invalidate Claude's prompt cache
        on every new session. Agent wants "what scopes exist" for memory_search
        hints; recency is out-of-scope here (use recall/search for that).
        """
        rows = self.conn.execute(
            """SELECT DISTINCT w.name AS wing, r.name AS room
               FROM wings w JOIN rooms r ON r.wing_id=w.id
               ORDER BY w.name ASC, r.name ASC LIMIT ?""",
            (max_items,),
        ).fetchall()
        if not rows:
            return ""
        lines = ["[memory · scopes]"]
        for r in rows:
            lines.append(f"  {r['wing']}/{r['room']}")
        return "\n".join(lines)

    def recall(self, *, wing: str | None = None, room: str | None = None,
               limit: int = 10) -> list[Drawer]:
        """Non-search scoped list, most recent first."""
        sql = ("SELECT d.*, r.name AS room_name, w.name AS wing_name "
               "FROM drawers d JOIN rooms r ON d.room_id=r.id "
               "JOIN wings w ON r.wing_id=w.id WHERE 1=1")
        args: list[Any] = []
        if wing:
            sql += " AND w.name=?"; args.append(wing)
        if room:
            sql += " AND r.name=?"; args.append(room)
        sql += " ORDER BY d.ts DESC LIMIT ?"; args.append(limit)
        return [self._row_to_drawer(r) for r in self.conn.execute(sql, args).fetchall()]

    def search(self, query: str, *, wing: str | None = None, room: str | None = None,
               n: int = 5, bm25_weight: float = 1.0, vec_weight: float = 1.0,
               recency_weight: float = 0.2) -> list[Drawer]:
        """Hybrid: BM25 (FTS5) ∪ cosine(vec), scope-filtered."""
        self._warn_on_dim_mismatch()
        # --- BM25 candidates (top 50)
        bm25_rows = self._bm25_candidates(query, wing=wing, room=room, limit=50)
        bm25_scores: dict[int, float] = {r["id"]: -r["bm25"] for r in bm25_rows}  # bm25 lower = better; negate.

        # --- vector candidates
        vec_scores: dict[int, float] = {}
        if self.embedder is not None:
            try:
                qv = self.embedder.embed([query])[0]
                for row in self._all_vectors(wing=wing, room=room):
                    dv = _unpack_vector(row["vector"])
                    if len(dv) != len(qv):
                        continue
                    vec_scores[row["drawer_id"]] = _cosine(qv, dv)
            except Exception as e:
                log.warning("vector search failed: %s", e)

        # --- combine
        candidate_ids = set(bm25_scores) | set(vec_scores)
        if not candidate_ids:
            return []

        # Normalize each channel to [0,1]
        def _norm(d: dict[int, float]) -> dict[int, float]:
            if not d: return d
            lo, hi = min(d.values()), max(d.values())
            if hi <= lo: return {k: 1.0 for k in d}
            return {k: (v - lo) / (hi - lo) for k, v in d.items()}
        bm25_n = _norm(bm25_scores)
        vec_n = _norm(vec_scores)

        now = time.time()
        scored: list[tuple[int, float]] = []
        for did in candidate_ids:
            s = bm25_weight * bm25_n.get(did, 0.0) + vec_weight * vec_n.get(did, 0.0)
            row = self.conn.execute("SELECT ts FROM drawers WHERE id=?", (did,)).fetchone()
            if row:
                age_days = max(0.0, (now - row["ts"]) / 86400.0)
                recency = math.exp(-age_days / 30.0)  # half-life ~21 days
                s += recency_weight * recency
            scored.append((did, s))
        scored.sort(key=lambda x: -x[1])
        top_ids = [did for did, _ in scored[:n]]
        if not top_ids:
            return []

        placeholders = ",".join("?" * len(top_ids))
        rows = self.conn.execute(
            f"SELECT d.*, r.name AS room_name, w.name AS wing_name "
            f"FROM drawers d JOIN rooms r ON d.room_id=r.id "
            f"JOIN wings w ON r.wing_id=w.id WHERE d.id IN ({placeholders})",
            top_ids,
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        drawers: list[Drawer] = []
        for did, score in scored[:n]:
            if did not in by_id:
                continue
            d = self._row_to_drawer(by_id[did])
            d.score = score
            drawers.append(d)
        return drawers

    def _bm25_candidates(self, query: str, wing: str | None, room: str | None,
                         limit: int) -> list[sqlite3.Row]:
        sql = ("SELECT d.id, bm25(drawers_fts) AS bm25 "
               "FROM drawers_fts "
               "JOIN drawers d ON d.id = drawers_fts.rowid "
               "JOIN rooms r ON d.room_id=r.id "
               "JOIN wings w ON r.wing_id=w.id "
               "WHERE drawers_fts MATCH ?")
        args: list[Any] = [_sanitize_fts_query(query)]
        if wing:
            sql += " AND w.name=?"; args.append(wing)
        if room:
            sql += " AND r.name=?"; args.append(room)
        sql += " ORDER BY bm25 LIMIT ?"; args.append(limit)
        try:
            return list(self.conn.execute(sql, args).fetchall())
        except sqlite3.OperationalError:
            # FTS syntax error (unlikely after sanitize) — skip BM25 channel
            return []

    def _all_vectors(self, wing: str | None, room: str | None) -> list[sqlite3.Row]:
        sql = ("SELECT v.drawer_id, v.vector FROM drawers_vec v "
               "JOIN drawers d ON d.id=v.drawer_id "
               "JOIN rooms r ON d.room_id=r.id "
               "JOIN wings w ON r.wing_id=w.id WHERE 1=1")
        args: list[Any] = []
        if wing:
            sql += " AND w.name=?"; args.append(wing)
        if room:
            sql += " AND r.name=?"; args.append(room)
        return list(self.conn.execute(sql, args).fetchall())

    def _row_to_drawer(self, r: sqlite3.Row) -> Drawer:
        import orjson
        meta = orjson.loads(r["meta_json"]) if r["meta_json"] else {}
        return Drawer(
            id=r["id"], room_id=r["room_id"], kind=r["kind"],
            content=r["content"], ts=r["ts"], meta=meta,
            wing=r["wing_name"] if "wing_name" in r.keys() else "",
            room=r["room_name"] if "room_name" in r.keys() else "",
        )

    def _warn_on_dim_mismatch(self) -> None:
        """Warn once per process if stored vectors use a different dim than the
        current embedder — otherwise _cosine silently skips them and search
        quietly returns fewer hits, which the user has no way to notice.
        """
        if self.embedder is None or getattr(self, "_dim_check_done", False):
            return
        self._dim_check_done = True
        try:
            cur_dim = len(self.embedder.embed(["dim probe"])[0])
        except Exception:
            return
        rows = self.conn.execute(
            "SELECT dim, COUNT(*) c FROM drawers_vec GROUP BY dim").fetchall()
        mismatched = sum(r["c"] for r in rows if r["dim"] != cur_dim)
        if mismatched:
            log.warning(
                "memory_store: %d stored vectors have dim != current embedder "
                "(%d). Those rows will be skipped in cosine search. "
                "Run `bonsai reembed` to fix.",
                mismatched, cur_dim,
            )

    # ---- maintenance ---------------------------------------------------
    def stats(self) -> dict:
        return {
            "drawers": self.conn.execute("SELECT COUNT(*) c FROM drawers").fetchone()["c"],
            "rooms": self.conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"],
            "wings": self.conn.execute("SELECT COUNT(*) c FROM wings").fetchone()["c"],
            "vectors": self.conn.execute("SELECT COUNT(*) c FROM drawers_vec").fetchone()["c"],
        }

    def reembed_all(self, *, batch_size: int = 32) -> int:
        """Re-embed every drawer with the currently-bound embedder.

        Call after switching embed_provider in config (e.g. hash → bge-m3).
        Old vectors with different dim are replaced. Returns count updated.
        """
        if self.embedder is None:
            raise RuntimeError("no embedder bound")
        rows = list(self.conn.execute("SELECT id, content FROM drawers"))
        total = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            contents = [r["content"] for r in batch]
            try:
                vecs = self.embedder.embed(contents)
            except Exception as e:
                log.warning("batch %d reembed failed: %s", i, e)
                continue
            for row, vec in zip(batch, vecs, strict=False):
                self.conn.execute(
                    "INSERT OR REPLACE INTO drawers_vec(drawer_id, dim, vector) "
                    "VALUES (?, ?, ?)",
                    (row["id"], len(vec), _pack_vector(vec)),
                )
                total += 1
        self.conn.commit()
        return total


def _sanitize_fts_query(q: str) -> str:
    # FTS5 is pernickety — escape anything that could be a syntax char.
    cleaned = "".join(c if c.isalnum() or c.isspace() or c in "一-鿿" else " "
                      for c in q)
    parts = [f'"{tok}"' for tok in cleaned.split() if tok]
    return " OR ".join(parts) if parts else '""'
