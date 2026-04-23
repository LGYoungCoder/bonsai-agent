"""Token usage analytics — reads `logs/cache_stats.jsonl` and aggregates.

The JSONL is append-only, one line per LLM request:
  {t, provider, model, cache_read, cache_creation, input_tokens, output_tokens, ...}

Kept as a read-only module — the source of truth is whatever CacheMonitor
writes. We only aggregate for display; numbers may lag one request.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path


# Rough per-Mtoken pricing in CNY for a first-order cost estimate.
# Snapshot date: 2026-04. Providers change pricing constantly and have
# tiered rates / batch discounts we don't model. Convert USD at ~¥7.2.
# Users should override via config if they need accuracy.
_DEFAULT_PRICES = {
    # model name or prefix → (input ¥/Mtok, output ¥/Mtok, cached-read ¥/Mtok)
    # Anthropic — Opus at $15/$75, Sonnet $3/$15, Haiku $1/$5; cache read 10%.
    "claude-opus":        (108.0, 540.0, 10.8),
    "claude-sonnet":      (21.6, 108.0,   2.16),
    "claude-haiku":       (7.2,   36.0,   0.72),
    # OpenAI — GPT-5 $1.25/$10 (cache 10%); GPT-4o $2.50/$10 (cache 50%);
    # GPT-4o-mini $0.15/$0.60.
    "gpt-5":              (9.0,   72.0,   0.9),
    "gpt-4o-mini":        (1.08,  4.32,   0.54),
    "gpt-4o":             (18.0,  72.0,   9.0),
    "gpt-4":              (30.0, 120.0,   7.5),
    # Zhipu GLM — 5 ~¥4.3/¥15.8 (USD 0.6/2.2); 4.6 priced around ¥5/¥20.
    "glm-5":              (4.32, 15.84,   0.43),
    "glm-4.6":            (5.0,  20.0,    0.5),
    "glm-4":              (1.0,   4.0,    0.2),
    # Aliyun Qwen — qwen3-max ¥6/¥24; plus roughly ¥0.8/¥2.
    "qwen3-max":          (6.0,  24.0,    0.6),
    "qwen-max":           (20.0, 60.0,    4.0),
    "qwen3":              (4.0,  12.0,    0.4),
    "qwen-plus":          (0.8,   2.0,    0.1),
    # DeepSeek — V4 $0.30/$0.50; R1 $0.55/$2.19; V3 $0.27/$0.41.
    "deepseek-v4":        (2.16,  3.6,    0.22),
    "deepseek-r1":        (3.96, 15.77,   1.0),
    "deepseek-v3":        (1.94,  2.95,   0.1),
    "deepseek":           (1.94,  2.95,   0.1),
    # MiniMax — M2 $0.255/$1.00.
    "minimax-m2":         (1.84,  7.2,    0.2),
    "minimax":            (1.0,   4.0,    0.3),
    # Moonshot — K2.6 $0.95/$4.00 cache $0.16; K2 $0.55/$2.20.
    "kimi-k2.6":          (6.84, 28.8,    1.15),
    "kimi-k2":            (3.96, 15.84,   0.4),
    "kimi":               (5.0,  15.0,    0.5),
}


def _match_price(model: str | None) -> tuple[float, float, float]:
    if not model:
        return (0.0, 0.0, 0.0)
    low = model.lower()
    # Longest prefix wins.
    best = ""
    for key in _DEFAULT_PRICES:
        if low.startswith(key.lower()) and len(key) > len(best):
            best = key
    return _DEFAULT_PRICES.get(best, (0.0, 0.0, 0.0))


@dataclass
class Bucket:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_in(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def hit_rate(self) -> float:
        pre = self.cache_read_tokens + self.cache_creation_tokens
        return self.cache_read_tokens / pre if pre else 0.0

    def estimate_cost_cny(self, model: str | None) -> float:
        pin, pout, pcache = _match_price(model)
        m = 1_000_000
        return (
            self.input_tokens * pin / m
            + self.output_tokens * pout / m
            + self.cache_read_tokens * pcache / m
            # cache creation is billed at input rate by most providers
            + self.cache_creation_tokens * pin / m
        )


@dataclass
class UsageReport:
    window_days: int
    log_path: str
    log_exists: bool
    log_size: int
    line_count: int
    first_ts: float | None
    last_ts: float | None
    total: Bucket
    daily: list[dict]                                # [{date, requests, in, out, cache_read, cost}]
    per_provider: dict[str, dict]
    per_model: dict[str, dict]
    est_cost_cny: float


def _bucket_to_dict(b: Bucket, model: str | None = None) -> dict:
    d = asdict(b)
    d["hit_rate"] = round(b.hit_rate, 3)
    d["total_in"] = b.total_in
    if model is not None:
        d["est_cost_cny"] = round(b.estimate_cost_cny(model), 4)
    return d


def load_usage(log_path: Path, *, window_days: int = 14) -> UsageReport:
    exists = log_path.exists()
    size = log_path.stat().st_size if exists else 0
    if not exists:
        return UsageReport(
            window_days=window_days, log_path=str(log_path),
            log_exists=False, log_size=0, line_count=0,
            first_ts=None, last_ts=None,
            total=Bucket(), daily=[], per_provider={}, per_model={}, est_cost_cny=0.0,
        )

    cutoff = time.time() - window_days * 86400
    total = Bucket()
    daily: dict[str, Bucket] = defaultdict(Bucket)
    per_prov: dict[str, Bucket] = defaultdict(Bucket)
    per_model: dict[str, Bucket] = defaultdict(Bucket)
    prov_model: dict[str, str] = {}          # provider → representative model
    first_ts: float | None = None
    last_ts: float | None = None
    total_cost = 0.0
    line_count = 0

    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            if not raw.strip():
                continue
            line_count += 1
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = e.get("t", 0)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            if ts < cutoff:
                continue
            prov = e.get("provider") or "unknown"
            model = e.get("model") or "unknown"
            prov_model.setdefault(prov, model)
            r = int(e.get("cache_read", 0))
            c = int(e.get("cache_creation", 0))
            i = int(e.get("input_tokens", 0))
            o = int(e.get("output_tokens", 0))
            for b in (total, daily[_day_of(ts)], per_prov[prov], per_model[model]):
                b.requests += 1
                b.input_tokens += i
                b.output_tokens += o
                b.cache_read_tokens += r
                b.cache_creation_tokens += c
            total_cost += Bucket(
                requests=1, input_tokens=i, output_tokens=o,
                cache_read_tokens=r, cache_creation_tokens=c,
            ).estimate_cost_cny(model)

    # Expand daily to a continuous range so the bar chart has zeros for quiet days.
    today = datetime.now().date()
    daily_rows = []
    for offset in range(window_days - 1, -1, -1):
        d = today - timedelta(days=offset)
        key = d.isoformat()
        b = daily.get(key, Bucket())
        daily_rows.append({
            "date": key,
            **_bucket_to_dict(b),
        })

    return UsageReport(
        window_days=window_days,
        log_path=str(log_path),
        log_exists=True,
        log_size=size,
        line_count=line_count,
        first_ts=first_ts,
        last_ts=last_ts,
        total=total,
        daily=daily_rows,
        per_provider={k: _bucket_to_dict(v, prov_model.get(k)) for k, v in per_prov.items()},
        per_model={k: _bucket_to_dict(v, k) for k, v in per_model.items()},
        est_cost_cny=round(total_cost, 4),
    )


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def report_to_dict(r: UsageReport) -> dict:
    d = asdict(r)
    d["total"] = _bucket_to_dict(r.total)
    return d


# ═══════════════════════════════════════════════════════════════════════════
#                    New in 0.2: 更丰富的时间切片
# ═══════════════════════════════════════════════════════════════════════════


def _iter_events(log_path: Path):
    """Generator yielding parsed JSONL events. Skips malformed lines."""
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def _accumulate(b: Bucket, e: dict) -> None:
    b.requests += 1
    b.input_tokens += int(e.get("input_tokens", 0))
    b.output_tokens += int(e.get("output_tokens", 0))
    b.cache_read_tokens += int(e.get("cache_read", 0))
    b.cache_creation_tokens += int(e.get("cache_creation", 0))


def load_today(log_path: Path) -> dict:
    """Today-so-far vs yesterday-same-window vs 7-day average.

    "Same window" = from 00:00 up to current wall-clock time each day. If it's
    14:37 now, compare to yesterday 00:00-14:37 and the last-7-days average
    for that same slice. Prevents the 4am-user from seeing "way less than
    yesterday" just because yesterday is a full day.
    """
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    seconds_into_day = now.timestamp() - today_start

    today_b = Bucket()
    yesterday_b = Bucket()
    prior7_buckets = [Bucket() for _ in range(7)]   # days -1..-7
    today_cost = 0.0
    yest_cost = 0.0
    prior7_cost = [0.0] * 7
    # Track the dominant model of today for cost estimate
    today_models: dict[str, int] = defaultdict(int)

    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts == 0:
            continue
        delta_days = (today_start - ts) / 86400
        model = e.get("model")
        row_cost_bucket = Bucket()
        _accumulate(row_cost_bucket, e)
        cost = row_cost_bucket.estimate_cost_cny(model)
        if ts >= today_start:
            _accumulate(today_b, e)
            today_cost += cost
            if model:
                today_models[model] += 1
            continue
        # yesterday same window: between -1d and -1d + seconds_into_day
        if -1 <= delta_days < 0:
            continue  # shouldn't hit (above guard)
        same_slot_elapsed = today_start - ts   # seconds before today-start
        # day_idx = how many days before today. 0..7 maps to -1..-7
        day_idx = int(same_slot_elapsed / 86400)
        if day_idx >= 7:
            continue
        # within the "first `seconds_into_day` seconds of that day"?
        day_start = today_start - (day_idx + 1) * 86400
        offset_within_day = ts - day_start
        if offset_within_day > seconds_into_day:
            continue
        if day_idx == 0:
            _accumulate(yesterday_b, e)
            yest_cost += cost
        _accumulate(prior7_buckets[day_idx], e)
        prior7_cost[day_idx] += cost

    avg7_requests = sum(b.requests for b in prior7_buckets) / 7
    avg7_tokens = sum(b.input_tokens + b.output_tokens + b.cache_read_tokens
                      + b.cache_creation_tokens for b in prior7_buckets) / 7
    avg7_cost = sum(prior7_cost) / 7

    def _delta(cur, prev):
        if not prev:
            return None
        return round((cur - prev) / prev * 100, 1)

    top_model = max(today_models, key=today_models.get) if today_models else None
    return {
        "today": {
            **_bucket_to_dict(today_b, top_model),
            "cost_cny": round(today_cost, 4),
            "top_model": top_model,
            "seconds_into_day": int(seconds_into_day),
        },
        "yesterday_same_window": {
            **_bucket_to_dict(yesterday_b),
            "cost_cny": round(yest_cost, 4),
        },
        "avg_last_7_days": {
            "requests": round(avg7_requests, 1),
            "tokens_total": round(avg7_tokens, 1),
            "cost_cny": round(avg7_cost, 4),
        },
        "delta_vs_yesterday_pct": {
            "requests": _delta(today_b.requests, yesterday_b.requests),
            "tokens": _delta(
                today_b.input_tokens + today_b.output_tokens,
                yesterday_b.input_tokens + yesterday_b.output_tokens,
            ),
            "cost": _delta(today_cost, yest_cost),
        },
    }


def load_hourly(log_path: Path, *, date: str | None = None) -> dict:
    """24 hour-buckets for a given date (default today). Useful to see
    when in the day the agent was busy."""
    if date is None:
        date = datetime.now().date().isoformat()
    target_day_start = datetime.fromisoformat(date).timestamp()
    target_day_end = target_day_start + 86400
    buckets: list[Bucket] = [Bucket() for _ in range(24)]
    models_seen: dict[str, int] = defaultdict(int)
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts < target_day_start or ts >= target_day_end:
            continue
        hr = int((ts - target_day_start) // 3600)
        _accumulate(buckets[hr], e)
        if m := e.get("model"):
            models_seen[m] += 1
    return {
        "date": date,
        "hours": [{
            "hour": h,
            **_bucket_to_dict(b),
        } for h, b in enumerate(buckets)],
        "top_model": max(models_seen, key=models_seen.get) if models_seen else None,
    }


def load_weekly(log_path: Path, *, weeks: int = 8) -> dict:
    """ISO week buckets. Returns `weeks` weeks back (including current)."""
    now = datetime.now()
    today = now.date()
    monday = today - timedelta(days=today.weekday())  # current Monday
    buckets: dict[str, Bucket] = {}
    week_models: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Pre-seed so empty weeks still render
    for offset in range(weeks - 1, -1, -1):
        wk = (monday - timedelta(weeks=offset)).isoformat()
        buckets[wk] = Bucket()

    start_cutoff = datetime.combine(
        monday - timedelta(weeks=weeks - 1), datetime.min.time()).timestamp()
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts < start_cutoff:
            continue
        d = datetime.fromtimestamp(ts).date()
        wk_key = (d - timedelta(days=d.weekday())).isoformat()
        if wk_key in buckets:
            _accumulate(buckets[wk_key], e)
            if m := e.get("model"):
                week_models[wk_key][m] += 1
    return {
        "weeks": [{
            "week_starting": k,
            **_bucket_to_dict(v,
                (max(week_models[k], key=week_models[k].get) if week_models[k] else None)),
        } for k, v in buckets.items()],
    }


def load_monthly_compare(log_path: Path) -> dict:
    """This month so far vs last month (full) + linear forecast for this month."""
    now = datetime.now()
    this_month_start = datetime(now.year, now.month, 1).timestamp()
    # Last month
    if now.month == 1:
        last_month_start = datetime(now.year - 1, 12, 1).timestamp()
        last_month_end = this_month_start
    else:
        last_month_start = datetime(now.year, now.month - 1, 1).timestamp()
        last_month_end = this_month_start

    this_b = Bucket()
    last_b = Bucket()
    this_cost = 0.0
    last_cost = 0.0
    this_models: dict[str, int] = defaultdict(int)
    last_models: dict[str, int] = defaultdict(int)
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        row_b = Bucket()
        _accumulate(row_b, e)
        cost = row_b.estimate_cost_cny(e.get("model"))
        if ts >= this_month_start:
            _accumulate(this_b, e)
            this_cost += cost
            if m := e.get("model"):
                this_models[m] += 1
        elif last_month_start <= ts < last_month_end:
            _accumulate(last_b, e)
            last_cost += cost
            if m := e.get("model"):
                last_models[m] += 1

    # Linear forecast: extrapolate current month at current pace.
    days_in_month = _days_in_month(now.year, now.month)
    seconds_into_month = now.timestamp() - this_month_start
    forecast_cost = (this_cost / seconds_into_month * days_in_month * 86400
                     if seconds_into_month > 0 else 0.0)
    forecast_tokens = ((this_b.input_tokens + this_b.output_tokens +
                        this_b.cache_read_tokens + this_b.cache_creation_tokens)
                       / seconds_into_month * days_in_month * 86400
                       if seconds_into_month > 0 else 0)

    def _mtop(d):
        return max(d, key=d.get) if d else None
    return {
        "this_month": {
            **_bucket_to_dict(this_b, _mtop(this_models)),
            "cost_cny": round(this_cost, 4),
            "days_elapsed": round(seconds_into_month / 86400, 1),
            "days_in_month": days_in_month,
        },
        "last_month": {
            **_bucket_to_dict(last_b, _mtop(last_models)),
            "cost_cny": round(last_cost, 4),
        },
        "forecast_this_month": {
            "cost_cny": round(forecast_cost, 2),
            "tokens_total": int(forecast_tokens),
        },
    }


def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        next_month = datetime(y + 1, 1, 1)
    else:
        next_month = datetime(y, m + 1, 1)
    return (next_month - datetime(y, m, 1)).days


def hit_rate_trend(log_path: Path, *, days: int = 14) -> list[dict]:
    """Per-day cache hit rate trend line. Detects cache drift early."""
    today = datetime.now().date()
    buckets: dict[str, Bucket] = {
        (today - timedelta(days=offset)).isoformat(): Bucket()
        for offset in range(days - 1, -1, -1)
    }
    cutoff = time.time() - days * 86400
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts < cutoff:
            continue
        day_key = _day_of(ts)
        if day_key in buckets:
            _accumulate(buckets[day_key], e)
    return [{"date": k, "hit_rate": round(v.hit_rate, 3),
             "requests": v.requests} for k, v in buckets.items()]


def detect_anomalies(log_path: Path, *, days: int = 14,
                     sigma: float = 2.0) -> list[dict]:
    """Days whose total-token usage is > mean + sigma*stdev of the window.
    Returns flagged entries with a 'severity' score (# sigmas)."""
    import statistics
    today = datetime.now().date()
    daily: dict[str, int] = {}
    for offset in range(days - 1, -1, -1):
        daily[(today - timedelta(days=offset)).isoformat()] = 0
    cutoff = time.time() - days * 86400
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts < cutoff:
            continue
        k = _day_of(ts)
        if k in daily:
            tot = (int(e.get("input_tokens", 0)) + int(e.get("output_tokens", 0))
                   + int(e.get("cache_read", 0)) + int(e.get("cache_creation", 0)))
            daily[k] = daily.get(k, 0) + tot
    vals = [v for v in daily.values() if v > 0]
    if len(vals) < 3:
        return []
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    out = []
    for k, v in daily.items():
        if v == 0:
            continue
        z = (v - mu) / sd
        if z > sigma:
            out.append({
                "date": k, "tokens": v, "zscore": round(z, 2),
                "vs_avg_multiplier": round(v / mu, 2) if mu else 0,
            })
    return sorted(out, key=lambda r: -r["zscore"])


def export_csv(log_path: Path, *, days: int = 30) -> str:
    """CSV export of raw events in the window. One row per request."""
    import csv
    import io
    cutoff = time.time() - days * 86400
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp_iso", "provider", "model",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "hit_rate_cumulative", "est_cost_cny",
    ])
    for e in _iter_events(log_path):
        ts = e.get("t", 0)
        if ts < cutoff:
            continue
        row_b = Bucket()
        _accumulate(row_b, e)
        cost = row_b.estimate_cost_cny(e.get("model"))
        writer.writerow([
            datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
            e.get("provider") or "",
            e.get("model") or "",
            e.get("input_tokens", 0),
            e.get("output_tokens", 0),
            e.get("cache_read", 0),
            e.get("cache_creation", 0),
            round(float(e.get("hit_rate_cumulative", 0)), 3),
            round(cost, 4),
        ])
    return buf.getvalue()
