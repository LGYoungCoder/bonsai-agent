"""SkillStore — distilled, curated, file-based.

Layout:
  skills/
    L1_index.txt      # keyword → filename lines (human-editable)
    L2_facts.txt      # short stable facts / conventions
    L3/               # one SOP per .md file with YAML frontmatter
      install_python_deps.md
      login_wechat.md
      ...
    _meta/
      evidence/       # per-skill evidence trails (JSONL)

Rules:
  - lookup is keyword-only (L1 loaded into FrozenPrefix)
  - full SOP read via file_read (agent uses the path it found in L1)
  - write_sop REQUIRES execution evidence (no execution, no memory)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import orjson

log = logging.getLogger(__name__)


@dataclass
class SkillEntry:
    name: str
    path: Path
    keywords: list[str] = field(default_factory=list)
    created: str = ""
    verified_on: str = ""


class SkillStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.l3_dir = self.root / "L3"
        self.meta_dir = self.root / "_meta"
        self.evidence_dir = self.meta_dir / "evidence"
        self.l1_path = self.root / "L1_index.txt"
        self.l2_path = self.root / "L2_facts.txt"

    # ---- setup ---------------------------------------------------------
    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.l3_dir.mkdir(exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        if not self.l1_path.exists():
            self.l1_path.write_text(
                "# SkillStore L1 index — keyword (space-separated aliases): path\n"
                "# Auto-maintained. Manual edits preserved if followed by '# keep'.\n",
                encoding="utf-8",
            )
        if not self.l2_path.exists():
            self.l2_path.write_text(
                "# SkillStore L2 facts — short stable conventions, one per line.\n",
                encoding="utf-8",
            )

    # ---- read API ------------------------------------------------------
    def lookup(self, keyword: str) -> list[Path]:
        """Return SOP paths matching any alias in L1, ranked by freshness.

        Ordering: fresher verified_on first. SOPs older than _STALE_DAYS get
        bumped to the end (decay). Rationale: a SOP that hasn't been re-used
        successfully in a while is more likely to be wrong than a recent one.
        """
        if not self.l1_path.exists():
            return []
        kw_low = keyword.lower().strip()
        hits: list[Path] = []
        for line in self.l1_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or ":" not in line:
                continue
            keys, _, path_part = line.partition(":")
            path_part = path_part.strip().split("#")[0].strip()
            if not path_part:
                continue
            aliases = {a.strip().lower() for a in keys.split()}
            if any(kw_low in a or a in kw_low for a in aliases):
                p = (self.root / path_part).resolve()
                if p.exists() and p not in hits:
                    hits.append(p)
        return sorted(hits, key=self._freshness_score, reverse=True)

    def _freshness_score(self, p: Path) -> float:
        """Higher = fresher. 0.0 if SOP looks stale (> _STALE_DAYS)."""
        try:
            meta = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            return 0.0
        v = str(meta.get("verified_on") or meta.get("created") or "").strip()
        if not v:
            return 0.0
        try:
            d = _dt.date.fromisoformat(v)
        except ValueError:
            return 0.0
        age_days = (_dt.date.today() - d).days
        if age_days < 0:
            return 1.0
        if age_days > _STALE_DAYS:
            return 0.0
        return max(0.1, 1.0 - (age_days / _STALE_DAYS) * 0.9)

    def read(self, path: str | Path) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = (self.root / p).resolve()
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def l1_text(self, max_chars: int = 2000) -> str:
        if not self.l1_path.exists():
            return ""
        t = self.l1_path.read_text(encoding="utf-8")
        return t if len(t) <= max_chars else t[:max_chars] + "\n# ... (L1 truncated)"

    def l2_text(self, max_chars: int = 500) -> str:
        if not self.l2_path.exists():
            return ""
        t = self.l2_path.read_text(encoding="utf-8")
        return t if len(t) <= max_chars else t[:max_chars] + "\n# ... (L2 truncated)"

    def list_sops(self) -> list[SkillEntry]:
        entries: list[SkillEntry] = []
        for p in sorted(self.l3_dir.glob("*.md")):
            meta = _parse_frontmatter(p.read_text(encoding="utf-8"))
            entries.append(SkillEntry(
                name=meta.get("name") or p.stem,
                path=p,
                keywords=meta.get("keywords") or [],
                created=str(meta.get("created") or ""),
                verified_on=str(meta.get("verified_on") or ""),
            ))
        return entries

    # ---- write API (evidence-gated) ------------------------------------
    def write_sop(self, name: str, content: str, evidence: dict,
                  keywords: list[str] | None = None) -> Path:
        """Write a SOP. `evidence` must contain successful tool_calls.

        Raises ValueError if no evidence or evidence lacks success markers.
        If keywords is empty, auto-extract from content so the L1 index isn't
        reduced to filename-only.
        """
        self._check_evidence(evidence)
        kws = list(keywords or [])
        if not kws:
            kws = extract_keywords(content, k=5) or [name]
        fname = _safe_name(name) + ".md"
        target = self.l3_dir / fname
        frontmatter = self._compose_frontmatter(name, kws, evidence)
        body = content if content.startswith("---") else frontmatter + "\n" + content
        target.write_text(body, encoding="utf-8")
        self._persist_evidence(name, evidence)
        self._rebuild_l1()
        return target

    def _check_evidence(self, evidence: dict) -> None:
        calls = evidence.get("tool_calls") or []
        if not calls:
            raise ValueError("evidence must include tool_calls (no execution, no memory)")
        succeeded = [c for c in calls if not c.get("is_error")]
        if not succeeded:
            raise ValueError("evidence has tool_calls but none succeeded")

    def _compose_frontmatter(self, name: str, keywords: list[str], evidence: dict) -> str:
        today = time.strftime("%Y-%m-%d")
        turns = [c.get("turn") for c in evidence.get("tool_calls") or [] if c.get("turn")]
        return "\n".join([
            "---",
            f"name: {name}",
            f"keywords: {keywords}",
            f"created: {today}",
            f"verified_on: {today}",
            f"evidence_turns: {turns}",
            "---",
            "",
        ])

    def _persist_evidence(self, name: str, evidence: dict) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        out = self.evidence_dir / f"{_safe_name(name)}.jsonl"
        with out.open("ab") as f:
            f.write(orjson.dumps({"t": time.time(), **evidence}) + b"\n")

    def _rebuild_l1(self) -> None:
        """Rescan L3/*.md frontmatter, refresh L1_index.txt."""
        lines: list[str] = []
        lines.append("# SkillStore L1 index — auto-maintained, edit with '# keep'.")
        for entry in self.list_sops():
            kws = entry.keywords or [entry.name]
            keys = " ".join(sorted({k.strip() for k in kws if k}))
            rel = entry.path.relative_to(self.root)
            lines.append(f"{keys}: {rel}")
        preserved = self._preserved_manual_lines()
        if preserved:
            lines.append("# --- manually preserved (# keep) ---")
            lines.extend(preserved)
        self.l1_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _preserved_manual_lines(self) -> list[str]:
        if not self.l1_path.exists():
            return []
        out = []
        for line in self.l1_path.read_text(encoding="utf-8").splitlines():
            if line.rstrip().endswith("# keep"):
                out.append(line)
        return out


# ---- helpers -----------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_STALE_DAYS = 90

_STOP_EN = frozenset(
    "a an the and or but if then else for to of in on at by with from as is "
    "are was were be been being do does did done have has had having this "
    "that these those it its they them their i me my we our you your he she "
    "his her not no yes can could would should will may might must shall "
    "about after again all also any because before between both during each "
    "few more most other over same so some such than through until up where "
    "when why which who whom how".split()
)

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")


def extract_keywords(text: str, k: int = 5) -> list[str]:
    """Pick up to k lowercase keyword candidates from text.

    Heuristic — no TF-IDF corpus, no LLM. Fallback only when author didn't
    write explicit keywords. Users should prefer writing good ones.
    """
    if not text:
        return []
    body = _FRONTMATTER_RE.sub("", text, count=1)
    body = re.sub(r"```[\s\S]*?```", " ", body)
    body = re.sub(r"https?://\S+", " ", body)
    tokens = [w.lower() for w in _WORD_RE.findall(body)]
    order: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for i, w in enumerate(tokens):
        if w in _STOP_EN:
            continue
        if w not in order:
            order[w] = i
        counts[w] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    return [w for w, _ in ranked[:k]]


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out: dict = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            out[k.strip()] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        else:
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _safe_name(name: str) -> str:
    # slugify, keep ascii-ish
    s = re.sub(r"[^\w\-. ]+", "", name).strip().replace(" ", "_")
    if not s:
        s = hashlib.md5(name.encode()).hexdigest()[:8]
    return s
