#!/usr/bin/env python3
"""Claude Code / Cowork usage + estimated-cost report from local session logs.

Standalone extraction of the parsing/pricing core of the Claude Cost Tracker
desktop widget (~/Applications/Claude Cost Tracker.app) — no Tkinter/Pillow,
so it runs anywhere `python3` runs. Reads the SAME ~/.claude/projects/*.jsonl
logs and the SAME ~/.claude/cc_tracker_buckets.json config, so numbers here
always match the widget.

KNOWN LIMITATION (confirmed by comparing against the real Anthropic Enterprise
usage console): this only sees Claude Code sessions that wrote a transcript to
THIS machine. Cowork sessions that ran server-side, other devices, other team
members, and scheduled/background jobs elsewhere are invisible to it — on some
days that undercounts real spend by a lot; on others (one very long local
session) it overcounts just as badly. Treat this as a local, rough, on-demand
-rate ESTIMATE, never as the real bill. The real bill lives in the Claude
Console's Usage page (or the org's Usage & Cost Admin API), not here.

Usage:
    usage_report.py [--period today|week|month|lastmonth] [--scope all|work|personal] [--json]
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Only "input"/"output" are hand-maintained — cache_write/cache_read are
# ALWAYS DERIVED via Anthropic's standard multipliers (cache write = 1.25x
# input, cache read = 0.1x input), so a model only ever needs two numbers.
# No live pricing API exists for this local estimate, so staleness is instead
# handled by date-stamping this table (warn past ~45 days) plus an optional
# override so a price change never needs a code edit:
#   ~/.claude/cc_tracker_pricing.json
#   {"updated": "2026-09-17", "base": {"claude-sonnet-5": {"input":3,"output":15}}}
# NOTE: keep this file's pricing in sync with the desktop widget
# (~/Applications/Claude Cost Tracker.app/Contents/Resources/CC_tracker.py).
PRICING_UPDATED = "2026-09-17"
_BASE_PRICING = {
    "claude-opus-5":     {"input":  5.00, "output": 25.00},
    "claude-sonnet-5":   {"input":  3.00, "output": 15.00},   # promo ended 2026-08-31
    "claude-fable-5":    {"input": 10.00, "output": 50.00},
    "claude-opus-4-8":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-6":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-5":   {"input":  5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00},
    "claude-sonnet-4":   {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00},
}

_PRICING_CFG = Path.home() / ".claude" / "cc_tracker_pricing.json"
if _PRICING_CFG.exists():
    try:
        _pd = json.loads(_PRICING_CFG.read_text())
        _BASE_PRICING.update(_pd.get("base", {}))
        PRICING_UPDATED = _pd.get("updated", PRICING_UPDATED)
    except Exception:
        pass


def _derive_pricing(base):
    return {m: {"input": p["input"], "output": p["output"],
                "cache_write": round(p["input"] * 1.25, 6),
                "cache_read": round(p["input"] * 0.10, 6)}
            for m, p in base.items()}


PRICING = _derive_pricing(_BASE_PRICING)
_DEFAULT_PRICING = PRICING["claude-sonnet-4-6"]

try:
    _stale_days = (datetime.now() - datetime.strptime(PRICING_UPDATED, "%Y-%m-%d")).days
    PRICING_STALE = _stale_days > 45
except Exception:
    PRICING_STALE = False

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# No folder/account rules ship by default here — this is a public repo, so it
# never bakes in anyone's real folder paths, employer, or account UUIDs.
# Everything falls into DEFAULT_BUCKET until you add your own rules via
# ~/.claude/cc_tracker_buckets.json (see README.md for the exact format and a
# worked example). That file lives outside this repo and is never committed,
# so it's the right place for your real paths / account UUIDs.
BUCKET_RULES = []
DEFAULT_BUCKET = "Personal"
ACCOUNT_BUCKET = {}

_cfg = Path.home() / ".claude" / "cc_tracker_buckets.json"
if _cfg.exists():
    try:
        _d = json.loads(_cfg.read_text())
        BUCKET_RULES = [(b, list(p)) for b, p in _d.get("rules", BUCKET_RULES)]
        DEFAULT_BUCKET = _d.get("default", DEFAULT_BUCKET)
        ACCOUNT_BUCKET.update(_d.get("account_map", {}))
    except Exception:
        pass


def project_name(cwd: str) -> str:
    if not cwd:
        return "(unknown)"
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    if not parts:
        return cwd
    if cwd.rstrip("/") == str(Path.home()):
        return "(home)"
    name = parts[-1]
    if name in ("My Drive", "Desktop", "Documents", "Downloads", "Projects"):
        return f"{name} (loose)"
    return name


def explicit_bucket_for(cwd: str):
    if not cwd:
        return None
    best_len, best_bucket = -1, None
    for name, prefixes in BUCKET_RULES:
        for p in prefixes:
            p = p.rstrip("/")
            if (cwd == p or cwd.startswith(p + "/")) and len(p) > best_len:
                best_len, best_bucket = len(p), name
    return best_bucket


def resolve_bucket(cwd: str, session_bucket, account_uuid=None) -> str:
    return (explicit_bucket_for(cwd)
            or ACCOUNT_BUCKET.get(account_uuid or "")
            or session_bucket
            or DEFAULT_BUCKET)


def _blank():
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}


_SESSIONS_FILE = Path.home() / ".claude" / "cc_tracker_sessions.json"


def _load_session_buckets() -> dict:
    try:
        m = json.loads(_SESSIONS_FILE.read_text())
        return {k: v.get("bucket") for k, v in m.items()
                if isinstance(v, dict) and v.get("bucket")}
    except Exception:
        return {}


def _load_file_records(path: Path):
    recs = []
    meta = {"title": "", "ai_title": "", "first_prompt": ""}
    last_cwd = ""
    last_acct = ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd"):
                    last_cwd = obj["cwd"]
                if obj.get("ownerAccountUuid"):
                    last_acct = obj["ownerAccountUuid"]
                if obj.get("customTitle") and not meta["title"]:
                    meta["title"] = str(obj["customTitle"])[:120]
                if obj.get("aiTitle") and not meta["ai_title"]:
                    meta["ai_title"] = str(obj["aiTitle"])[:120]
                if obj.get("type") == "user" and not meta["first_prompt"]:
                    c = obj.get("message", {}).get("content")
                    txt = ""
                    if isinstance(c, str):
                        txt = c
                    elif isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "text":
                                txt = b.get("text", "")
                                break
                    txt = txt.strip()
                    if txt and not txt.startswith("<"):
                        meta["first_prompt"] = txt[:240]
                if obj.get("type") != "assistant":
                    continue
                ts_str = obj.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                msg = obj.get("message", {})
                usage = msg.get("usage", {})
                if not usage:
                    continue
                recs.append((
                    ts, msg.get("model", "unknown"),
                    usage.get("input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    obj.get("cwd") or last_cwd,
                    obj.get("ownerAccountUuid") or last_acct,
                ))
    except OSError:
        return [], meta
    if meta["first_prompt"].startswith("You are a friendly money-saving coach"):
        recs = []
    return recs, meta


def parse_usage(projects_dir: Path, since: datetime, until: datetime = None,
                bucket_filter: str = None) -> dict:
    by_model, proj_model, proj_msgs, proj_bucket, sessions = {}, {}, {}, {}, {}
    messages = 0
    session_buckets = _load_session_buckets()
    for jsonl in projects_dir.rglob("*.jsonl"):
        sid = jsonl.stem
        sess_bucket = session_buckets.get(sid)
        recs, meta = _load_file_records(jsonl)
        for rec in recs:
            ts, model, tin, tcw, tcr, tout, cwd, acct = rec
            bucket = resolve_bucket(cwd, sess_bucket, acct)
            if bucket_filter and bucket != bucket_filter:
                continue
            if ts < since or (until and ts >= until):
                continue
            proj = project_name(cwd)
            bm = by_model.setdefault(model, _blank())
            pm = proj_model.setdefault(proj, {}).setdefault(model, _blank())
            for tgt in (bm, pm):
                tgt["input"] += tin; tgt["cache_write"] += tcw
                tgt["cache_read"] += tcr; tgt["output"] += tout
            messages += 1
            proj_msgs[proj] = proj_msgs.get(proj, 0) + 1
            proj_bucket.setdefault(proj, bucket)
            s = sessions.setdefault(sid, {"project": proj, "bucket": bucket,
                                          "msgs": 0, "model_tok": {}})
            s["msgs"] += 1
            mt = s["model_tok"].setdefault(model, _blank())
            mt["input"] += tin; mt["cache_write"] += tcw
            mt["cache_read"] += tcr; mt["output"] += tout
        if sid in sessions:
            sessions[sid]["title"] = meta.get("title") or meta.get("ai_title") or sessions[sid]["project"]
            sessions[sid]["first_prompt"] = meta.get("first_prompt", "")
    return {"by_model": by_model, "messages": messages, "proj_model": proj_model,
            "proj_msgs": proj_msgs, "proj_bucket": proj_bucket, "sessions": sessions}


def _normalize_model(model: str) -> str:
    m = re.sub(r"-\d{8}.*$", "", model).split(":")[0]
    return re.sub(r"^(us\.|eu\.|ap\.)?anthropic\.", "", m)


def _cost_of(model_tok: dict) -> float:
    tot = 0.0
    for model, tm in model_tok.items():
        p = PRICING.get(_normalize_model(model), _DEFAULT_PRICING)
        tot += sum(tm.get(k, 0) / 1_000_000 * p[k] for k in p)
    return tot


def calc_cost(usage: dict) -> dict:
    costs = {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
    tokens = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for model, t in usage["by_model"].items():
        p = PRICING.get(_normalize_model(model), _DEFAULT_PRICING)
        for k in costs:
            tokens[k] += t[k]
            costs[k] += t[k] / 1_000_000 * p[k]
    costs["total"] = sum(costs[k] for k in ("input", "cache_write", "cache_read", "output"))
    project_costs = {p: _cost_of(mm) for p, mm in usage.get("proj_model", {}).items()}
    sessions = []
    for sid, s in usage.get("sessions", {}).items():
        sessions.append({"id": sid, "project": s.get("project", ""),
                         "bucket": s.get("bucket", ""), "msgs": s.get("msgs", 0),
                         "title": s.get("title", ""),
                         "first_prompt": s.get("first_prompt", ""),
                         "cost": _cost_of(s.get("model_tok", {}))})
    sessions.sort(key=lambda x: -x["cost"])
    return {"costs": costs, "tokens": tokens, "project_costs": project_costs,
            "project_msgs": usage.get("proj_msgs", {}),
            "project_bucket": usage.get("proj_bucket", {}), "sessions": sessions}


def period_window(now: datetime, period: str):
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first_this = mid.replace(day=1)
    last_first = (first_this - timedelta(days=1)).replace(day=1)
    return {
        "today":     ("Today",      mid, None),
        "week":      ("This week",  mid - timedelta(days=(now.weekday() + 1) % 7), None),
        "month":     ("This month", first_this, None),
        "lastmonth": ("Last month", last_first, first_this),
    }[period]


def build_report(period: str, scope: str) -> dict:
    now = datetime.now().astimezone()
    label, since, until = period_window(now, period)
    bucket_filter = None if scope == "all" else scope.capitalize()
    usage = parse_usage(PROJECTS_DIR, since, until, bucket_filter=bucket_filter)
    res = calc_cost(usage)
    return {
        "generated": now.isoformat(),
        "pricing_updated": PRICING_UPDATED, "pricing_stale": PRICING_STALE,
        "period": label, "since": since.isoformat(),
        "until": until.isoformat() if until else None,
        "scope": scope,
        "messages": usage["messages"],
        "costs": res["costs"], "tokens": res["tokens"],
        "by_project": sorted(
            ({"project": p, "cost": c, "messages": usage["proj_msgs"].get(p, 0),
              "bucket": usage["proj_bucket"].get(p)}
             for p, c in res["project_costs"].items() if c >= 0.005),
            key=lambda x: -x["cost"]),
        "top_sessions": [
            {"project": s["project"], "bucket": s["bucket"], "title": s["title"],
             "first_prompt": s["first_prompt"], "cost": s["cost"], "messages": s["msgs"]}
            for s in res["sessions"] if s["cost"] >= 0.02][:10],
    }


def format_text(r: dict) -> str:
    c, t = r["costs"], r["tokens"]
    out = [f"Period: {r['period']}  (scope: {r['scope']})",
           f"Total (local on-demand-rate estimate): ${c['total']:.2f}  |  "
           f"{r['messages']:,} assistant turns/tool-calls logged locally",
           f"  new text ${c['input']:.2f}  Claude's replies ${c['output']:.2f}  "
           f"first-time context ${c['cache_write']:.2f}  "
           f"re-sent context ${c['cache_read']:.2f}"]
    if PRICING_STALE:
        out.append(f"  ⚠ pricing table last checked {PRICING_UPDATED} — verify "
                   f"rates haven't changed (Claude Console → Usage)")
    out += ["", "By project:"]
    for p in r["by_project"][:10]:
        out.append(f"  - [{p['bucket']}] {p['project']}: ${p['cost']:.2f}  ({p['messages']} msgs)")
    if r["top_sessions"]:
        out.append("")
        out.append("Top conversations (topic — cost):")
        for s in r["top_sessions"]:
            fp = s["first_prompt"].replace("\n", " ")[:140]
            out.append(f"  - [{s['bucket']}/{s['project']}] \"{s['title']}\" "
                       f"— ${s['cost']:.2f}, {s['messages']} msgs")
            if fp:
                out.append(f"      opened with: {fp}")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["today", "week", "month", "lastmonth"], default="today")
    ap.add_argument("--scope", choices=["all", "work", "personal"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = build_report(args.period, args.scope)
    print(json.dumps(report, indent=2) if args.json else format_text(report))
