"""pytest_run — structured pytest invocation with parsed results.

Returns a compact summary instead of dumping raw stdout. Why: LLMs handle
"3 passed, 1 failed: test_foo.py::test_bar (AssertionError: ...)" better
than 500 lines of pytest output.

Strategy:
  1. prefer json-report plugin if installed (cleanest output)
  2. fall back to stdout parsing (works without extra deps)
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


_SUMMARY_RE = re.compile(
    r"(?:=+\s+)?(?:(\d+)\s+failed)?,?\s*(?:(\d+)\s+passed)?,?\s*"
    r"(?:(\d+)\s+skipped)?,?\s*(?:(\d+)\s+error)?",
)
_FAIL_HEADER_RE = re.compile(r"^_+ (.+?) _+$")


def _try_json_report(stdout: str) -> dict | None:
    """If pytest-json-report ran, parse the artifact path from stdout."""
    for line in stdout.splitlines():
        if "JSON report written to" in line:
            path = line.split("JSON report written to")[-1].strip().strip(":")
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _parse_failures(stdout: str, limit: int = 5) -> list[dict]:
    """Extract `path::test_name: message` snippets from FAILURES section."""
    out: list[dict] = []
    in_failures = False
    current: dict | None = None
    body_lines: list[str] = []

    for line in stdout.splitlines():
        if line.startswith("=") and "FAILURES" in line:
            in_failures = True
            continue
        if line.startswith("=") and in_failures and "short test summary" in line.lower():
            break
        if not in_failures:
            continue

        m = _FAIL_HEADER_RE.match(line)
        if m:
            if current is not None:
                current["traceback"] = "\n".join(body_lines)[-800:]
                out.append(current)
                if len(out) >= limit:
                    return out
            current = {"nodeid": m.group(1).strip(), "traceback": ""}
            body_lines = []
        elif current is not None:
            body_lines.append(line)

    if current is not None and len(out) < limit:
        current["traceback"] = "\n".join(body_lines)[-800:]
        out.append(current)
    return out


def _parse_summary_line(stdout: str) -> dict:
    """Find the last 'N passed, M failed in X.Xs' line."""
    summary = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for line in reversed(stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for key, pat in (
                ("passed", r"(\d+)\s+passed"),
                ("failed", r"(\d+)\s+failed"),
                ("skipped", r"(\d+)\s+skipped"),
                ("errors", r"(\d+)\s+error"),
            ):
                m = re.search(pat, line)
                if m:
                    summary[key] = int(m.group(1))
            if any(summary.values()):
                return summary
    return summary


def pytest_run(scope: str | None = None, *, pattern: str | None = None,
               extra_args: list[str] | None = None,
               timeout: int = 300, cwd: Path | None = None) -> str:
    base = cwd or Path.cwd()
    args = ["pytest", "--tb=short", "-q", "--no-header"]
    if scope:
        args.append(scope)
    if pattern:
        args.extend(["-k", pattern])
    if extra_args:
        args.extend(extra_args)

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            cwd=str(base), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[error] pytest timed out after {timeout}s"
    except FileNotFoundError:
        return "[error] pytest not installed (pip install pytest)"

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    js = _try_json_report(stdout)
    if js:
        s = js.get("summary", {})
        failed = [
            {
                "nodeid": t.get("nodeid"),
                "message": (t.get("call") or {}).get("longrepr", "")[-500:],
            }
            for t in (js.get("tests") or [])
            if t.get("outcome") == "failed"
        ][:5]
        return _format(
            passed=s.get("passed", 0),
            failed=s.get("failed", 0),
            skipped=s.get("skipped", 0),
            errors=s.get("error", 0),
            duration=js.get("duration"),
            fail_details=failed,
            exit_code=proc.returncode,
        )

    summary = _parse_summary_line(stdout)
    failures = _parse_failures(stdout)
    return _format(
        **summary,
        duration=None,
        fail_details=failures,
        exit_code=proc.returncode,
        raw_tail=(stderr or stdout).splitlines()[-12:] if proc.returncode not in (0, 1) else None,
    )


def _format(passed: int, failed: int, skipped: int, errors: int,
            duration: float | None, fail_details: list[dict],
            exit_code: int, raw_tail: list[str] | None = None) -> str:
    head = f"[pytest] passed={passed} failed={failed} skipped={skipped} errors={errors}"
    if duration is not None:
        head += f" duration={duration:.2f}s"
    head += f" exit={exit_code}"
    if not fail_details and exit_code == 0:
        return head + " ✓"
    if fail_details:
        body = "\n\n".join(
            f"FAIL {f['nodeid']}\n{f.get('traceback') or f.get('message', '')}"
            for f in fail_details
        )
        return f"{head}\n\n{body}"
    if raw_tail:
        return head + "\n\n[tail]\n" + "\n".join(raw_tail)
    return head
