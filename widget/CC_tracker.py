import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Bedrock on-demand pricing, US East (per 1M tokens, USD). Only "input" and
# "output" are hand-maintained below — cache_write/cache_read are ALWAYS
# DERIVED from input via Anthropic's standard multipliers (5-min cache write
# = 1.25x input, cache read = 0.1x input), so they can never drift out of
# sync and a new/changed model only needs two numbers, not four.
#
# "Dynamic" in practice: there is no live public pricing API for this local
# estimate to poll, so staleness is handled by date-stamping this table and
# warning when it's old, plus an optional override file so a price change
# never needs a code edit:  ~/.claude/cc_tracker_pricing.json
#   {"updated": "2026-09-17", "base": {"claude-sonnet-5": {"input":3,"output":15}}}
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

# ── palette ────────────────────────────────────────────────────────────────
# An instrument panel, not an alert screen: cool near-black, a brass headline
# accent used only for the live figure and the active-period marker, and two
# quiet identity colours for Work / Personal.
BG         = "#0E1116"   # ink
CARD_BG    = "#161A21"   # raised panel
ACCENT     = "#141922"   # header band
PANEL_2    = "#1C222B"   # nested surface
LINE       = "#2A313C"   # hairline
HIGHLIGHT  = "#E8C15A"   # brass — the one accent
TEXT_MAIN  = "#E6E9EF"
TEXT_DIM   = "#8A94A3"
GREEN      = "#5FBFA0"   # Personal (verdigris)
BLUE       = "#5A9FD4"   # Work (steel)
BTN_ACTIVE = "#2C333F"
BTN_IDLE   = "#191E26"

# ── Personal / Work split ───────────────────────────────────────────────────
# Claude Code does not record which Claude account was connected for a given
# session, so we attribute each session by its working directory (cwd), which
# every transcript line carries. The LONGEST (most specific) prefix that
# matches cwd wins, regardless of rule order — so a personal sub-project can
# live inside an otherwise-Work tree. Unmatched sessions -> DEFAULT_BUCKET.
#
# This is public source, so it ships with NO real folder paths, employers, or
# account UUIDs baked in — everything falls into DEFAULT_BUCKET until you add
# your own rules. Edit the prefixes below to match your folders, or (better)
# leave this empty and override with a JSON file at
# ~/.claude/cc_tracker_buckets.json (never committed anywhere):
#   {"default":"Personal",
#    "rules":[["Work",["/abs/prefix", ...]], ["Personal",["/abs/prefix"]]]}
BUCKET_RULES = []
DEFAULT_BUCKET = "Personal"

# Authoritative when present: every session transcript the desktop app writes
# carries `ownerAccountUuid` — the Claude account that actually owns (and is
# billed for) the session. Map the UUIDs you know to a bucket via
# cc_tracker_buckets.json's "account_map" (see README) — none ship by default.
ACCOUNT_BUCKET = {}

_cfg = Path.home() / ".claude" / "cc_tracker_buckets.json"
if _cfg.exists():
    try:
        _d = json.loads(_cfg.read_text())
        BUCKET_RULES  = [(b, list(p)) for b, p in _d.get("rules", BUCKET_RULES)]
        DEFAULT_BUCKET = _d.get("default", DEFAULT_BUCKET)
        ACCOUNT_BUCKET.update(_d.get("account_map", {}))
    except Exception:
        pass

# Bucket display order + colours (unknown buckets appended, greyed).
BUCKET_ORDER = [b for b, _ in BUCKET_RULES]
if DEFAULT_BUCKET not in BUCKET_ORDER:
    BUCKET_ORDER.append(DEFAULT_BUCKET)
BUCKET_COLOR = {"Work": BLUE, "Personal": GREEN}


def project_name(cwd: str) -> str:
    """Short, human label for a working directory.

    'My Drive' / 'Desktop' / 'Documents' etc. are not real projects — they mean
    the session ran straight from that folder's root without cd-ing into a
    project, so label them '<folder> (loose)'.  Home dir -> '(home)'.
    """
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
    """Bucket from an explicit folder rule, or None if nothing matched.
    Longest (most specific) matching prefix wins, regardless of rule order."""
    if not cwd:
        return None
    best_len, best_bucket = -1, None
    for name, prefixes in BUCKET_RULES:
        for p in prefixes:
            p = p.rstrip("/")
            if (cwd == p or cwd.startswith(p + "/")) and len(p) > best_len:
                best_len, best_bucket = len(p), name
    return best_bucket


def bucket_for(cwd: str) -> str:
    return explicit_bucket_for(cwd) or DEFAULT_BUCKET


def resolve_bucket(cwd: str, session_bucket, account_uuid=None) -> str:
    """Precedence:
      1. explicit folder rule       (user-declared, e.g. health-tracker → Personal)
      2. ownerAccountUuid            (authoritative — the account that owns the
                                     session, straight from the transcript)
      3. session account tag         (login e-mail, written by the hook)
      4. DEFAULT_BUCKET
    A personal side-project stays Personal even on the work login; everything
    else follows the account that actually owns the session."""
    return (explicit_bucket_for(cwd)
            or ACCOUNT_BUCKET.get(account_uuid or "")
            or session_bucket
            or DEFAULT_BUCKET)


FILTERS      = ["Daily", "Weekly", "Monthly", "Last Month"]


# ── incremental parsing ─────────────────────────────────────────────────────
# Session transcripts are large and rarely change. Parse each .jsonl once into
# a compact list of (ts, model, in, cache_w, cache_r, out) records and cache it
# keyed by (mtime, size). On refresh we only re-read files that actually
# changed; everything else is served from RAM.
_FILE_CACHE = {}  # Path -> {"key": (mtime, size), "recs": [tuple, ...]}


def _load_file_records(path: Path):
    try:
        st = path.stat()
    except OSError:
        return _FILE_CACHE.pop(path, {}).get("recs", [])
    key = (st.st_mtime, st.st_size)
    ent = _FILE_CACHE.get(path)
    if ent and ent["key"] == key:
        return ent["recs"]

    recs = []
    last_cwd = ""
    last_acct = ""
    meta = {"title": "", "ai_title": "", "first_prompt": "", "cwd": "",
            "account_uuid": ""}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd"):
                    last_cwd = obj["cwd"]
                    meta["cwd"] = obj["cwd"]
                if obj.get("ownerAccountUuid"):
                    last_acct = obj["ownerAccountUuid"]
                    meta["account_uuid"] = last_acct
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
                    ts,
                    msg.get("model", "unknown"),
                    usage.get("input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    obj.get("cwd") or last_cwd,   # raw cwd; bucket resolved later
                    obj.get("ownerAccountUuid") or last_acct,
                ))
    except OSError:
        return ent["recs"] if ent else []

    # Ignore the tracker's own "AI Insight" calls so it never bills itself.
    if meta["first_prompt"].startswith("You are a friendly money-saving coach"):
        recs = []

    _FILE_CACHE[path] = {"key": key, "recs": recs, "meta": meta}
    return recs


def _file_meta(path: Path) -> dict:
    _load_file_records(path)
    return _FILE_CACHE.get(path, {}).get("meta", {})


def _blank():
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}


# Per-session account tag written by the SessionStart / UserPromptSubmit hook
# ~/.claude/hooks/cc_tracker_tag.py  -> {session_id: {"bucket": "...", ...}}.
# When present it OVERRIDES the folder heuristic, so attribution follows the
# actual Claude login e-mail. Re-read each refresh (tiny file).
_SESSIONS_FILE = Path.home() / ".claude" / "cc_tracker_sessions.json"


def _load_session_buckets() -> dict:
    try:
        m = json.loads(_SESSIONS_FILE.read_text())
        return {k: v.get("bucket") for k, v in m.items()
                if isinstance(v, dict) and v.get("bucket")}
    except Exception:
        return {}


def parse_usage(projects_dir: Path, since: datetime, until: datetime = None,
                bucket_filter: str = None) -> dict:
    """bucket_filter: if set ('Work'/'Personal'/…), only records resolved to
    that bucket are counted — every downstream metric is then scoped to it."""
    by_model = {}
    by_bucket = {}          # bucket -> token dict
    bucket_model = {}       # bucket -> {model -> token dict}
    proj_model = {}         # project short name -> {model -> token dict}
    proj_msgs = {}
    proj_bucket = {}        # project short name -> bucket (first seen)
    sessions = {}           # session id -> aggregate for the AI insight
    messages = 0
    bucket_msgs = {}
    seen = set()
    session_buckets = _load_session_buckets()
    for jsonl in projects_dir.rglob("*.jsonl"):
        seen.add(jsonl)
        sid = jsonl.stem
        sess_bucket = session_buckets.get(sid)
        recs = _load_file_records(jsonl)
        for rec in recs:
            ts, model, tin, tcw, tcr, tout = rec[:6]
            cwd = rec[6] if len(rec) > 6 else ""
            acct = rec[7] if len(rec) > 7 else ""
            bucket = resolve_bucket(cwd, sess_bucket, acct)
            if bucket_filter and bucket != bucket_filter:
                continue
            if ts < since:
                continue
            if until and ts >= until:
                continue
            proj = project_name(cwd)
            for tgt in (by_model.setdefault(model, _blank()),
                        by_bucket.setdefault(bucket, _blank()),
                        bucket_model.setdefault(bucket, {}).setdefault(model, _blank()),
                        proj_model.setdefault(proj, {}).setdefault(model, _blank())):
                tgt["input"]       += tin
                tgt["cache_write"] += tcw
                tgt["cache_read"]  += tcr
                tgt["output"]      += tout
            messages += 1
            bucket_msgs[bucket] = bucket_msgs.get(bucket, 0) + 1
            proj_msgs[proj] = proj_msgs.get(proj, 0) + 1
            proj_bucket.setdefault(proj, bucket)

            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = {"project": proj, "bucket": bucket,
                                     "msgs": 0, "model_tok": {}}
            s["msgs"] += 1
            mt = s["model_tok"].setdefault(model, _blank())
            mt["input"] += tin; mt["cache_write"] += tcw
            mt["cache_read"] += tcr; mt["output"] += tout
    # attach titles / first prompt to the sessions that had in-window activity
    for jsonl in projects_dir.rglob("*.jsonl"):
        sid = jsonl.stem
        if sid in sessions:
            m = _file_meta(jsonl)
            sessions[sid]["title"] = (m.get("title") or m.get("ai_title")
                                      or sessions[sid]["project"])
            sessions[sid]["first_prompt"] = m.get("first_prompt", "")
    for gone in set(_FILE_CACHE) - seen:
        _FILE_CACHE.pop(gone, None)
    return {"by_model": by_model, "messages": messages,
            "by_bucket": by_bucket, "bucket_model": bucket_model,
            "bucket_msgs": bucket_msgs, "proj_model": proj_model,
            "proj_msgs": proj_msgs, "proj_bucket": proj_bucket,
            "sessions": sessions}


def _normalize_model(model: str) -> str:
    """Normalize Bedrock model IDs to match PRICING keys.
    Handles: us.anthropic.claude-X-20251001-v1:0 → claude-X
    """
    import re
    m = re.sub(r"-\d{8}.*$", "", model).split(":")[0]
    m = re.sub(r"^(us\.|eu\.|ap\.)?anthropic\.", "", m)
    return m


def _short_model(model: str) -> str:
    m = _normalize_model(model).replace("claude-", "")
    return (m.replace("sonnet", "Sonnet")
             .replace("opus", "Opus")
             .replace("haiku", "Haiku")
             .replace("fable", "Fable"))


def calc_cost(usage: dict) -> dict:
    costs       = {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
    tokens      = {"input": 0,   "cache_write": 0,   "cache_read": 0,   "output": 0}
    model_costs = {}
    for model, t in usage["by_model"].items():
        p = PRICING.get(_normalize_model(model), _DEFAULT_PRICING)
        mc = 0.0
        for k in costs:
            tokens[k] += t[k]
            c = t[k] / 1_000_000 * p[k]
            costs[k]  += c
            mc        += c
        model_costs[model] = mc
    costs["total"] = sum(costs[k] for k in ("input", "cache_write", "cache_read", "output"))

    def _cost_of(model_tok: dict) -> float:
        tot = 0.0
        for model, tm in model_tok.items():
            pm = PRICING.get(_normalize_model(model), _DEFAULT_PRICING)
            tot += sum(tm.get(k, 0) / 1_000_000 * pm[k] for k in pm)
        return tot

    bucket_costs = {b: _cost_of(mm)
                    for b, mm in usage.get("bucket_model", {}).items()}
    project_costs = {p: _cost_of(mm)
                     for p, mm in usage.get("proj_model", {}).items()}

    # cost per model split by bucket  -> {model: {bucket: cost}}
    model_bucket_costs = {}
    for b, mm in usage.get("bucket_model", {}).items():
        for model, tm in mm.items():
            model_bucket_costs.setdefault(model, {})[b] = _cost_of({model: tm})

    sessions = []
    for sid, s in usage.get("sessions", {}).items():
        sessions.append({
            "id": sid, "project": s.get("project", ""),
            "bucket": s.get("bucket", ""), "msgs": s.get("msgs", 0),
            "title": s.get("title", ""), "first_prompt": s.get("first_prompt", ""),
            "cost": _cost_of(s.get("model_tok", {})),
        })
    sessions.sort(key=lambda x: -x["cost"])

    return {"costs": costs, "tokens": tokens, "model_costs": model_costs,
            "bucket_costs": bucket_costs, "project_costs": project_costs,
            "model_bucket_costs": model_bucket_costs,
            "bucket_msgs": usage.get("bucket_msgs", {}),
            "project_msgs": usage.get("proj_msgs", {}),
            "project_bucket": usage.get("proj_bucket", {}), "sessions": sessions}


# ── AI insight ─────────────────────────────────────────────────────────────
def period_specs(now: datetime):
    """(label, since, until) for Today / This week / This month / Last month,
    all in local time — same boundaries the widget's filters use."""
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first_this = mid.replace(day=1)
    last_first = (first_this - timedelta(days=1)).replace(day=1)
    return [
        ("Today",      mid, None),
        ("This week",  mid - timedelta(days=(now.weekday() + 1) % 7), None),
        ("This month", first_this, None),
        ("Last month", last_first, first_this),
    ]


def gather_insight_report(projects_dir: Path, label: str, since: datetime,
                          until: datetime = None, bucket_filter: str = None) -> str:
    """Plain-language text summary of spend for ONE period/account scope —
    whichever the user currently has selected in the filters — broken down
    by project and by conversation topic."""
    now = datetime.now().astimezone()
    scope_txt = f" ({bucket_filter} account only)" if bucket_filter else ""
    usage = parse_usage(projects_dir, since, until, bucket_filter=bucket_filter)
    res = calc_cost(usage)
    c = res["costs"]
    out = [f"Claude usage report for: {label}{scope_txt}  "
           f"(generated {now:%Y-%m-%d %H:%M %Z})",
           "Money terms used below: 'new text' is what was typed fresh; "
           "'Claude's replies' is everything written back; 'first-time "
           "context' is a file or instructions being stored for reuse; "
           "'re-sent context' is the whole chat history and open files being "
           "resent again on every single message of that conversation.", ""]
    out.append(f"Total for {label}: ${c['total']:.2f}  |  new text ${c['input']:.2f}  "
               f"Claude's replies ${c['output']:.2f}  first-time context "
               f"${c['cache_write']:.2f}  re-sent context ${c['cache_read']:.2f}  "
               f"|  {usage['messages']:,} messages")
    bc = res["bucket_costs"]
    out.append("By account: " + ("  ".join(
        f"{b} ${bc[b]:.2f}" for b in sorted(bc, key=lambda x: -bc[x])) or "-"))
    pc = res["project_costs"]; pm = res["project_msgs"]
    out.append("By project:")
    for p in sorted(pc, key=lambda x: -pc[x])[:8]:
        if pc[p] < 0.005:
            continue
        out.append(f"  - {p}: ${pc[p]:.2f}  ({pm.get(p, 0)} messages)")
    top = [s for s in res["sessions"] if s["cost"] >= 0.02][:8]
    if top:
        out.append("Top conversations (topic — cost):")
        for s in top:
            fp = s["first_prompt"].replace("\n", " ")
            if len(fp) > 140:
                fp = fp[:140] + "…"
            out.append(f"  - [{s['bucket']}/{s['project']}] "
                       f"\"{s['title']}\" — ${s['cost']:.2f}, {s['msgs']} messages")
            if fp:
                out.append(f"      opened with: {fp}")
    if not top and c['total'] <= 0:
        out.append("(No spend in this period/account.)")
    return "\n".join(out)


INSIGHT_INSTRUCTION = (
    "You are a friendly money-saving coach explaining Claude usage costs to "
    "someone who is NOT a developer and does not know technical terms. Using "
    "ONLY the report below — their real conversations for the period and "
    "account they have selected — output ACTION ITEMS, nothing else.\n\n"
    "Rules:\n"
    "- 4 to 7 items, ranked by how much money they'd save, biggest first. If "
    "the report shows no spend, say so plainly in one line instead.\n"
    "- Each item is ONE plain-English sentence, no jargon — never say "
    "'tokens', 'cache', or 'context window'; instead say things like 'the "
    "chat history being resent every message' or 'the long back-and-forth "
    "conversation'.\n"
    "- Ground every item in something specific from the report: name the "
    "project, conversation topic, or number (e.g. 'the 2,600-message "
    "health-tracker conversation', 'the quick chats that kept growing past "
    "100 messages'). No generic advice that isn't tied to the data.\n"
    "- After the sentence add ' → saves about $X/month' with a rough "
    "estimate, and in plain words why (e.g. '(because the whole conversation "
    "gets resent every time you type)').\n"
    "- Prefer suggestions like: start a fresh conversation instead of "
    "continuing a huge one, keep one topic per conversation, don't paste in "
    "huge files or folders you don't need, use a cheaper/faster model for "
    "quick questions, avoid rambling back-and-forth.\n"
    "- Then one final line starting 'Don't worry about:' naming anything "
    "that looks big in the numbers but isn't actually worth changing, in "
    "plain words.\n"
    "Plain text. No preamble, no headings, no closing summary, no technical "
    "terms. Under 220 words.\n\n"
    "=== REPORT ===\n"
)


# Left unspecified, `claude -p` picks up whatever model your account/org has
# configured as default — which breaks on setups routing through a custom AI
# proxy (seen in the wild: an inaccessible "gemini-3.8-flash-via-aiproxy[1m]"
# alias). Pin a plain, widely-available alias instead, with a fallback and an
# env-var escape hatch for unusual setups.
INSIGHT_MODEL = os.environ.get("CC_TRACKER_INSIGHT_MODEL", "")
_INSIGHT_MODEL_TRY_ORDER = ([INSIGHT_MODEL] if INSIGHT_MODEL else []) + ["sonnet", "haiku"]


def run_claude_insight(report: str, timeout: int = 90) -> str:
    exe = (shutil.which("claude")
           or os.path.expanduser("~/.local/bin/claude"))
    if not exe or not os.path.exists(exe):
        return ("Claude CLI not found — install/authenticate `claude` to use "
                "AI insight.\n\nRaw report:\n\n" + report)

    attempts = []
    for model in dict.fromkeys(_INSIGHT_MODEL_TRY_ORDER):  # dedupe, keep order
        try:
            # Fresh isolated session id so this never resumes / pollutes a
            # real conversation transcript.
            p = subprocess.run(
                [exe, "-p", "--output-format", "text", "--model", model,
                 "--session-id", str(uuid.uuid4())],
                input=INSIGHT_INSTRUCTION + report,
                capture_output=True, text=True, timeout=timeout,
                cwd=str(Path.home()),
                env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            )
            txt = (p.stdout or "").strip()
            if p.returncode == 0 and txt:
                return txt
            err = (p.stderr or txt or "").strip()[:300]
            attempts.append(f"  model={model}: exit {p.returncode} — {err}")
            # only keep trying other models if this looks like a model
            # availability problem, not a real failure (auth, network, ...)
            if "model" not in err.lower():
                break
        except subprocess.TimeoutExpired:
            attempts.append(f"  model={model}: timed out after {timeout}s")
        except Exception as e:  # noqa: BLE001
            return f"AI insight error: {e}\n\nRaw report:\n\n{report}"

    return ("AI insight failed for every model tried:\n" + "\n".join(attempts) +
            "\n\nIf your org routes Claude Code through a custom model/proxy, "
            "set CC_TRACKER_INSIGHT_MODEL to a model name your account can "
            "actually reach, then relaunch the widget.\n\nRaw report:\n\n" +
            report)


# ── modern chart rendering — Pillow, anti-aliased ─────────────────────────
from PIL import Image, ImageDraw, ImageFont, ImageTk

_SF      = "/System/Library/Fonts/SFNS.ttf"
_SF_MONO = "/System/Library/Fonts/SFNSMono.ttf"

C_INPUT  = "#6B7280"
C_OUTPUT = GREEN
C_CWRITE = "#E0A83E"
C_CREAD  = "#3B4351"
C_BAR    = BLUE
C_DIM    = "#333B47"

MOTION = os.environ.get("CC_TRACKER_NO_MOTION", "").lower() not in ("1", "true", "yes")
_UI_CFG = Path.home() / ".claude" / "cc_tracker_ui.json"
_font_cache = {}


def ease_out(t):
    return 1 - pow(1 - t, 3)


def _font(path, px, weight=None):
    key = (path, px, weight)
    f = _font_cache.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(path, px)
            if weight:
                try:
                    f.set_variation_by_name(weight)
                except Exception:
                    pass
        except Exception:
            f = ImageFont.load_default()
        _font_cache[key] = f
    return f


def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    ra, rb = _rgb(a), _rgb(b)
    return tuple(round(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))


def _fmt_money(v):
    return f"${v:,.0f}" if v >= 100 else (f"${v:,.1f}" if v >= 10 else f"${v:.2f}")


def _track(s):
    return " ".join(s.upper())


class _Canvas:
    """Tiny wrapper: draws on a supersampled PIL image, returns a PhotoImage."""
    SS = 2

    def __init__(self, w, h):
        self.w, self.h = int(w), int(h)
        self.im = Image.new("RGBA", (self.w * self.SS, self.h * self.SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def _f(self, px, weight=None, mono=False):
        return _font(_SF_MONO if mono else _SF, int(px * self.SS), weight)

    def bar(self, x0, y0, x1, y1, color, radius=8, sheen=True):
        s = self.SS
        x0, y0, x1, y1, r = x0 * s, y0 * s, x1 * s, y1 * s, radius * s
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        self.d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=_rgb(color) + (255,))
        if sheen:
            hh = min((y1 - y0) * 0.5, r * 2 + s)
            self.d.rounded_rectangle([x0, y0, x1, y0 + hh], radius=r,
                                     fill=_mix(color, "#FFFFFF", 0.16) + (70,))

    def hline(self, x0, x1, y, color, width=1):
        s = self.SS
        self.d.line([(x0 * s, y * s), (x1 * s, y * s)], fill=_rgb(color) + (255,),
                    width=max(1, int(width * s)))

    def text(self, x, y, s, px, color, weight=None, mono=False, anchor="la"):
        self.d.text((x * self.SS, y * self.SS), s, font=self._f(px, weight, mono),
                    fill=_rgb(color) + (255,), anchor=anchor)

    def measure(self, s, px, weight=None, mono=False):
        b = self.d.textbbox((0, 0), s, font=self._f(px, weight, mono))
        return (b[2] - b[0]) / self.SS

    def photo(self):
        return ImageTk.PhotoImage(self.im.resize((self.w, self.h), Image.LANCZOS))


def render_vbars(w, h, items, sel, k):
    c = _Canvas(w, h)
    n = len(items) or 1
    slot = w / n
    bw = min(slot * 0.42, 46)
    tot = lambda v: sum(s[0] for s in v) if isinstance(v, list) else v
    mx = max((tot(v) for _, v in items), default=0) or 1
    base = h - 22
    c.hline(6, w - 6, base, LINE, 1)
    for i, (lab, v) in enumerate(items):
        cx = slot * i + slot / 2
        total = tot(v)
        ki = ease_out(max(0.0, min(1.0, k * 1.15 - i * 0.05)))
        fh = (base - 18) * (total / mx) * ki
        x0, x1 = cx - bw / 2, cx + bw / 2
        if isinstance(v, list):
            y = base
            for val, col in v:
                seg = fh * (val / (total or 1))
                if seg < 1:
                    continue
                c.bar(x0, y - seg, x1, y, col, radius=7)
                y -= seg
        else:
            selb = i == sel
            c.bar(x0, base - max(fh, 3), x1, base,
                  HIGHLIGHT if selb else C_DIM, radius=7)
        if k > 0.5:
            t = _fmt_money(total)
            c.text(cx, base - max(fh, 3) - 7, t, 10.5,
                   TEXT_MAIN if i == sel else TEXT_DIM, mono=True, anchor="ms")
        c.text(cx, base + 6, lab, 10.5,
               TEXT_MAIN if i == sel else TEXT_DIM,
               weight="Semibold" if i == sel else None, anchor="ma")
    return c.photo()


def render_stack(w, h, segs, k):
    c = _Canvas(w, h)
    total = sum(v for _, v, _ in segs) or 1
    full = w * ease_out(min(1.0, k))
    x = 0.0
    for lab, v, col in segs:
        seg = full * (v / total)
        if seg <= 0.5:
            continue
        c.bar(x, 2, x + seg, h - 2, col, radius=min(h / 2 - 2, 9), sheen=True)
        if seg > 34 and k > 0.9:
            c.text(x + seg / 2, h / 2, f"{100 * v / total:.0f}%", 10,
                   BG, weight="Bold", mono=True, anchor="mm")
        x += seg
    return c.photo()


ROW_H = 30


def render_hbars(w, rows, k):
    h = len(rows) * ROW_H + 6
    c = _Canvas(w, h)
    mx = max((v for _, v, _ in rows), default=0) or 1
    lblw = min(150, w * 0.34)
    valw = 66
    bh = 17
    for i, (lab, v, col) in enumerate(rows):
        y = 6 + i * ROW_H
        ki = ease_out(max(0.0, min(1.0, k * 1.12 - i * 0.05)))
        c.text(2, y + bh / 2, lab[:26], 10.5, TEXT_DIM, anchor="lm")
        bwmax = w - lblw - valw
        c.bar(lblw, y, lblw + max(bwmax * (v / mx) * ki, 2), y + bh, col, radius=6)
        if k > 0.55:
            c.text(w - 2, y + bh / 2, f"${v:,.2f}", 10.5, TEXT_MAIN,
                   mono=True, anchor="rm")
    return c.photo(), h


# ── UI ────────────────────────────────────────────────────────────────────
UI_STEPS = [0.80, 0.90, 1.00, 1.15, 1.30, 1.50]


class Panel(tk.Frame):
    def __init__(self, master, title, fonts, pad):
        super().__init__(master, bg=CARD_BG, padx=pad, pady=pad)
        self.head = tk.Frame(self, bg=CARD_BG)
        self.head.pack(fill="x")
        tk.Label(self.head, text=_track(title), bg=CARD_BG, fg=TEXT_DIM,
                 font=fonts["eyebrow"]).pack(side="left")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", pady=(5, 8))
        self.body = tk.Frame(self, bg=CARD_BG)
        self.body.pack(fill="x")


class Tooltip:
    """Plain hover help. Borderless Toplevel (fine inside a .app bundle)."""
    _open = None

    def __init__(self, widget, text, font):
        self.w, self.text, self.font, self.tip = widget, text, font, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        try:
            if Tooltip._open and Tooltip._open.winfo_exists():
                Tooltip._open.destroy()
        except Exception:
            pass
        x = self.w.winfo_rootx() + 16
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        t = self.tip = tk.Toplevel(self.w)
        Tooltip._open = t
        t.wm_overrideredirect(True)
        t.attributes("-topmost", True)
        try:
            t.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        border = tk.Frame(t, bg=HIGHLIGHT)
        border.pack()
        tk.Label(border, text=self.text, bg=PANEL_2, fg=TEXT_MAIN, font=self.font,
                 justify="left", wraplength=330, padx=11, pady=9).pack(padx=1, pady=1)
        t.wm_geometry(f"+{x}+{y}")
        t.update_idletasks()

    def _hide(self, _e=None):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


TOK_HELP = {
    "input":
        "Input — brand-new text you send that Claude hasn't seen this turn: "
        "your message plus any file or command output attached for the first "
        "time. Cheapest kind. Usually a tiny share in Claude Code.",
    "output":
        "Output — everything Claude writes back: explanations, the code it "
        "produces, the tool commands it runs. Most expensive per word, but "
        "normally a small slice of the bill.",
    "cache_write":
        "Cache write — the one-time cost of storing a big block of context "
        "(the system instructions, a file you just opened, the conversation so "
        "far) so it can be re-used cheaply on later turns. You pay a small "
        "premium now to avoid paying full price every turn.",
    "cache_read":
        "Cache read — re-feeding everything already stored, on every single "
        "turn. In a long chat with lots of open files this is the whole "
        "conversation + all those files, resent each message. It is almost "
        "always the #1 cost in Claude Code and it grows the longer a session "
        "runs. Shrink it with shorter sessions and /clear between tasks.",
}
TOK_HELP_INTRO = (
    "Every message is billed as four kinds of tokens (a token ≈ ¾ of a word). "
    "Automated runs — scheduled tasks, /loop, background agents — use the same "
    "four buckets and show up here mixed in with your interactive work; a "
    "long-running agent with a big context is billed just like a long chat.")

# Plain-English, one-per-section help — shown by hovering the ⓘ next to each
# panel's title, so every metric on screen has its own explanation.
TOTAL_HELP = (
    "Total — what your conversations would cost at standard cloud pricing, "
    "for whichever period and account you've selected above (Daily/Weekly/"
    "Monthly/Last Month, All/Work/Personal). It's an estimate, not a real "
    "invoice.")
PERIOD_HELP = (
    "By period — the same total shown four ways side by side: today, this "
    "week, this month, and last month — so you can see at a glance whether "
    "spend is climbing or holding steady.")
SPLIT_HELP = (
    "Split — how much of the selected period's spend came from your Work "
    "account versus your Personal account.")
PROJECT_HELP = (
    "By project — which folder or project each conversation happened in, "
    "ranked by cost, so you can see what's actually driving the bill.")
MODEL_HELP = (
    "By model — which version of Claude answered. Smaller/faster models "
    "cost less per message than the most capable ones; this shows how much "
    "each one cost you in the selected period.")
INSIGHT_HELP = (
    "AI insight — asks Claude to read your own conversations from the "
    "period and account you've selected above, and suggest plain-English "
    "changes that would lower next month's bill.")


class CostTracker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude Cost")
        self.configure(bg=BG)
        self.minsize(340, 200)
        try:
            self.ui = float(json.loads(_UI_CFG.read_text()).get("scale", 1.0))
        except Exception:
            self.ui = 1.0
        if self.ui not in UI_STEPS:
            self.ui = 1.0
        self.filter_idx = 0
        self.scope = "All"
        self.scope_names = ["All", "Work", "Personal"]
        self._collapsed = False
        self._pinned = True            # visible alongside other apps by default
        self._tok_help_open = False
        self._insight_busy = False
        self._midnight_armed = False
        self._prev_total = 0.0
        self._prev_tok = {}
        self._ref = {}                 # keep PhotoImage refs alive
        self._tw_id = None
        self._zoom_pending = False
        self._resize_job = None

        # global bindings — set ONCE (rebuilding the UI must not stack these)
        self.bind_all("<MouseWheel>", self._on_wheel)
        for seq in ("<Command-equal>", "<Command-plus>", "<Command-KP_Add>"):
            self.bind_all(seq, lambda e: self._zoom(+1))
        for seq in ("<Command-minus>", "<Command-KP_Subtract>"):
            self.bind_all(seq, lambda e: self._zoom(-1))

        self._build()
        self.refresh(animate=False)

    # ── fonts / metrics ────────────────────────────────────────────────
    def _mkfonts(self):
        u = self.ui
        def base(px, weight="normal"):
            f = tkfont.Font(font="TkDefaultFont")
            f.configure(size=max(8, round(px * u)), weight=weight)
            return f
        def mono(px, weight="normal"):
            f = tkfont.Font(family="Menlo")
            f.configure(size=max(8, round(px * u)), weight=weight)
            return f
        self.F = {
            "eyebrow": base(9, "bold"),
            "label":   base(10),
            "label_b": base(10, "bold"),
            "small":   base(9),
            "btn":     base(10),
            "hcost":   mono(13, "bold"),
            "big":     mono(24, "bold"),
            "mono":    mono(10),
            "mono_s":  mono(9),
            "insight": mono(11),
        }
        self.COLW = round(438 * u)
        self.PAD = round(14 * u)

    # ── build (also called on zoom change) ────────────────────────────
    def _build(self):
        for w in list(self.children.values()):
            w.destroy()
        self._tips = []            # keep tooltip helpers alive
        self._mkfonts()
        F = self.F

        # header
        hd = tk.Frame(self, bg=ACCENT, padx=round(12 * self.ui),
                      pady=round(8 * self.ui))
        hd.pack(fill="x")
        self._header = hd
        tk.Label(hd, text="⚡", bg=ACCENT, fg=HIGHLIGHT, font=F["label_b"]).pack(
            side="left", padx=(0, 6))
        tk.Label(hd, text=_track("Claude Cost"), bg=ACCENT, fg=TEXT_MAIN,
                 font=F["label_b"]).pack(side="left")

        def hbtn(txt, cmd, fg=TEXT_DIM):
            b = tk.Label(hd, text=txt, bg=ACCENT, fg=fg, font=F["btn"],
                         cursor="hand2", padx=5)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e: cmd())
            return b

        hbtn("✕", self.destroy)
        self.lbl_hcost = tk.Label(hd, text="", bg=ACCENT, fg=HIGHLIGHT,
                                  font=F["hcost"])
        self.lbl_hcost.pack(side="right", padx=8)
        self.collapse_btn = hbtn("▲", self._toggle_collapse)
        self.pin_btn = hbtn("📌", self._toggle_pin)
        if self._pinned:
            self.pin_btn.configure(fg=HIGHLIGHT)
        hbtn("+", lambda: self._zoom(+1))
        hbtn("−", lambda: self._zoom(-1))

        tk.Frame(self, bg=HIGHLIGHT, height=2).pack(fill="x")
        hd.bind("<ButtonPress-1>", self._drag_start)
        hd.bind("<B1-Motion>", self._drag_move)

        # scroll area — a fixed-width column centred on a dark ground
        self._scrollwrap = tk.Frame(self, bg=BG)
        self._scrollwrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self._scrollwrap, bg=BG, highlightthickness=0)
        self.vsb = tk.Scrollbar(self._scrollwrap, orient="vertical",
                                command=self.canvas.yview)
        # 1px increment turns yview_scroll("units") into precise pixel steps,
        # instead of Tk's default (~1/9 of the visible height) which is what
        # made scrolling feel too fast / jump around.
        self.canvas.configure(yscrollcommand=self.vsb.set, yscrollincrement=1)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=BG, width=self.COLW)
        self._cwin = self.canvas.create_window((0, 0), window=self.content,
                                               anchor="n")
        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        C = self.content
        pack = dict(fill="x", pady=(0, round(6 * self.ui)))

        # period + scope selectors
        row = tk.Frame(C, bg=BG)
        row.pack(fill="x", pady=(round(10 * self.ui), 3))
        self._filter_btns = []
        for i, lab in enumerate(FILTERS):
            b = tk.Label(row, text=lab, bg=BTN_IDLE, fg=TEXT_DIM, font=F["small"],
                         padx=round(10 * self.ui), pady=round(5 * self.ui),
                         cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, x=i: self._set_filter(x))
            self._filter_btns.append(b)

        row = tk.Frame(C, bg=BG)
        row.pack(fill="x", pady=(0, round(8 * self.ui)))
        tk.Label(row, text=_track("account"), bg=BG, fg=TEXT_DIM,
                 font=F["eyebrow"]).pack(side="left", padx=(0, 6))
        self._scope_btns = {}
        for name in self.scope_names:
            col = {"Work": BLUE, "Personal": GREEN}.get(name, TEXT_DIM)
            b = tk.Label(row, text=name, bg=BTN_IDLE, fg=col, font=F["small"],
                         padx=round(10 * self.ui), pady=round(4 * self.ui),
                         cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, n=name: self._set_scope(n))
            self._scope_btns[name] = b

        # headline
        tot = tk.Frame(C, bg=CARD_BG, padx=round(16 * self.ui),
                       pady=round(14 * self.ui))
        tot.pack(**pack)
        tot_hd = tk.Frame(tot, bg=CARD_BG)
        tot_hd.pack(fill="x", pady=(0, 2))
        self.lbl_eyebrow = tk.Label(tot_hd, text=_track("total"), bg=CARD_BG,
                                    fg=TEXT_DIM, font=F["eyebrow"])
        self.lbl_eyebrow.pack(side="left", anchor="w")
        self._info_icon(tot_hd, TOTAL_HELP)
        self.lbl_total = tk.Label(tot, text="$0.00", bg=CARD_BG, fg=HIGHLIGHT,
                                  font=F["big"])
        self.lbl_total.pack(anchor="w")
        self.lbl_sub = tk.Label(tot, text="", bg=CARD_BG, fg=TEXT_DIM,
                                font=F["small"], justify="left",
                                wraplength=self.COLW - 2 * self.PAD - 32)
        self.lbl_sub.pack(anchor="w", pady=(3, 0))

        cw = self.COLW - 2 * self.PAD

        p = Panel(C, "By period", F, self.PAD); p.pack(**pack)
        self._info_icon(p.head, PERIOD_HELP)
        self.img_periods = tk.Label(p.body, bg=CARD_BG, bd=0)
        self.img_periods.pack()
        self._ph_periods = round(132 * self.ui)

        p = Panel(C, "Split", F, self.PAD); p.pack(**pack)
        self._info_icon(p.head, SPLIT_HELP)
        self.img_split = tk.Label(p.body, bg=CARD_BG, bd=0)
        self.img_split.pack(pady=(0, 4))
        self.split_legend = tk.Frame(p.body, bg=CARD_BG)
        self.split_legend.pack(fill="x")

        p = Panel(C, "By project", F, self.PAD); p.pack(**pack)
        self._info_icon(p.head, PROJECT_HELP)
        self.img_proj = tk.Label(p.body, bg=CARD_BG, bd=0)
        self.img_proj.pack()

        p = Panel(C, "Tokens", F, self.PAD); p.pack(**pack)
        info = tk.Label(p.head, text="ⓘ what am I looking at?", bg=CARD_BG,
                        fg=HIGHLIGHT, font=F["small"], cursor="hand2")
        info.pack(side="right")
        info.bind("<Button-1>", lambda e: self._toggle_tok_help())
        _htext = (TOK_HELP_INTRO + "\n\n" + "\n\n".join(
            TOK_HELP[k] for k in ("input", "output", "cache_write", "cache_read"))
        ) if self._tok_help_open else TOK_HELP_INTRO
        self._tok_help = tk.Label(
            p.body, text=_htext, bg=BG, fg=TEXT_DIM, font=F["small"],
            justify="left", wraplength=self.COLW - 2 * self.PAD - 6,
            padx=10, pady=8)
        if self._tok_help_open:
            self._tok_help.pack(fill="x", pady=(0, 6))
        self.img_tok = tk.Label(p.body, bg=CARD_BG, bd=0)
        self.img_tok.pack(pady=(0, 6))
        self.tok_rows = {}
        for key, lab, col in (("input", "Input", C_INPUT),
                              ("output", "Output", C_OUTPUT),
                              ("cache_write", "Cache write", C_CWRITE),
                              ("cache_read", "Cache read", C_CREAD)):
            r = tk.Frame(p.body, bg=CARD_BG)
            r.pack(fill="x", pady=1)
            sw = tk.Canvas(r, width=9, height=9, bg=CARD_BG, highlightthickness=0)
            sw.pack(side="left", padx=(0, 5))
            sw.create_rectangle(0, 0, 9, 9, fill=col, width=0)
            nm = tk.Label(r, text=lab, bg=CARD_BG, fg=TEXT_DIM, font=F["small"],
                          width=11, anchor="w", cursor="question_arrow")
            nm.pack(side="left")
            self._tips.append(Tooltip(nm, TOK_HELP[key], F["small"]))
            cst = tk.Label(r, text="$0.00", bg=CARD_BG, fg=TEXT_MAIN,
                           font=F["mono_s"], width=9, anchor="e")
            cst.pack(side="right")
            tkn = tk.Label(r, text="0", bg=CARD_BG, fg=TEXT_DIM,
                           font=F["mono_s"], anchor="e")
            tkn.pack(side="right", padx=8)
            self.tok_rows[key] = (tkn, cst)

        p = Panel(C, "By model", F, self.PAD); p.pack(**pack)
        self._info_icon(p.head, MODEL_HELP)
        self.model_rows = p.body

        p = Panel(C, "AI insight", F, self.PAD); p.pack(**pack)
        self._info_icon(p.head, INSIGHT_HELP)
        self.insight_desc = tk.Label(
            p.body, text="what to change next for the selected period — "
            "ranked, from your own conversations", bg=CARD_BG, fg=TEXT_DIM,
            font=F["small"])
        self.insight_desc.pack(anchor="w", pady=(0, 5))
        ctl = tk.Frame(p.body, bg=CARD_BG)
        ctl.pack(fill="x", pady=(0, 4))
        self.insight_btn = tk.Label(ctl, text="✨ Generate", bg=HIGHLIGHT, fg=BG,
                                    font=F["label_b"], padx=12,
                                    pady=round(4 * self.ui), cursor="hand2")
        self.insight_btn.pack(side="left")
        self.insight_btn.bind("<Button-1>", lambda e: self._run_insight())
        self.insight_status = tk.Label(ctl, text="not run yet", bg=CARD_BG,
                                       fg=TEXT_DIM, font=F["small"])
        self.insight_status.pack(side="left", padx=8)
        self.insight_text = tk.Text(
            p.body, bg=BG, fg=TEXT_MAIN, wrap="word", relief="flat", height=4,
            width=1,  # natural request stays tiny; fill="x" stretches it to
                      # whatever width the panel actually has — a Text widget
                      # otherwise defaults to 80 CHARACTERS wide and forces
                      # the whole window wide no matter how far you shrink it
            font=F["insight"], padx=12, pady=10, state="disabled",
            highlightthickness=1, highlightbackground=LINE, spacing1=2, spacing3=4)
        self.insight_text.pack(fill="x")
        self._set_insight("Press Generate for ranked, plain-English action "
                          "items covering the period and account you've "
                          "picked above — what to do differently to spend "
                          "less, based on what those conversations actually "
                          "did.")

        ft = tk.Frame(C, bg=BG, pady=round(6 * self.ui))
        ft.pack(fill="x")
        self.lbl_updated = tk.Label(ft, text="", bg=BG, fg=TEXT_DIM,
                                    font=F["small"])
        self.lbl_updated.pack()
        rb = tk.Label(ft, text="↻ Refresh now", bg=BG, fg=TEXT_DIM,
                      font=F["small"], cursor="hand2")
        rb.pack()
        rb.bind("<Button-1>", lambda e: self.refresh(animate=True))

        self._paint_selectors()

        # size + place
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = self.COLW + round(30 * self.ui)
        h = min(self._header.winfo_reqheight() + self.content.winfo_reqheight()
                + 6, sh - 130)
        x = sw - w - 24
        y = max(sh - h - 60, 40)
        self.geometry(f"{w}x{h}+{x}+{y}")
        try:
            self.withdraw(); self.update_idletasks(); self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self._pinned or self.attributes("-topmost", False))
        except tk.TclError:
            pass
        print(f"[diag] ui={self.ui} colw={self.COLW} win={w}x{h}", flush=True)

    # ── selectors / state ────────────────────────────────────────────
    def _paint_selectors(self):
        for i, b in enumerate(self._filter_btns):
            on = i == self.filter_idx
            b.configure(bg=BTN_ACTIVE if on else BTN_IDLE,
                        fg=TEXT_MAIN if on else TEXT_DIM)
        for n, b in self._scope_btns.items():
            on = n == self.scope
            col = {"Work": BLUE, "Personal": GREEN}.get(n, TEXT_MAIN)
            b.configure(bg=BTN_ACTIVE if on else BTN_IDLE,
                        fg=TEXT_MAIN if on else col)

    def _set_filter(self, i):
        self.filter_idx = i
        self._paint_selectors()
        self._flash(self._filter_btns[i])
        self.refresh(animate=True)

    def _set_scope(self, n):
        self.scope = n
        self._paint_selectors()
        self._flash(self._scope_btns[n])
        self.refresh(animate=True)

    def _zoom(self, step):
        try:
            i = UI_STEPS.index(self.ui)
        except ValueError:
            i = 2
        i = max(0, min(len(UI_STEPS) - 1, i + step))
        if UI_STEPS[i] == self.ui or self._zoom_pending:
            return
        self.ui = UI_STEPS[i]
        try:
            _UI_CFG.write_text(json.dumps({"scale": self.ui}))
        except Exception:
            pass
        # defer: never tear down the widget tree from inside a widget's own
        # click/key callback — that's what was closing the window.
        self._zoom_pending = True
        self.after(20, self._apply_zoom)

    def _apply_zoom(self):
        self._zoom_pending = False
        try:
            self._build()
            self.refresh(animate=False)
        except Exception as e:  # noqa: BLE001
            print("zoom rebuild failed:", e, flush=True)

    def _info_icon(self, head, text):
        """Small ⓘ in a panel's header row; hovering it shows a plain-English
        explanation of that panel's metric."""
        ic = tk.Label(head, text="ⓘ", bg=CARD_BG, fg=HIGHLIGHT,
                     font=self.F["small"], cursor="question_arrow", padx=2)
        ic.pack(side="right")
        self._tips.append(Tooltip(ic, text, self.F["small"]))
        return ic

    def _toggle_tok_help(self):
        self._tok_help_open = not self._tok_help_open
        if self._tok_help_open:
            full = TOK_HELP_INTRO + "\n\n" + "\n\n".join(
                TOK_HELP[k] for k in ("input", "output", "cache_write",
                                      "cache_read"))
            self._tok_help.configure(text=full)
            self._tok_help.pack(fill="x", pady=(0, 6), before=self.img_tok)
        else:
            self._tok_help.pack_forget()
        self.after_idle(self._on_content_configure_force)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        try:
            self.attributes("-topmost", self._pinned)
        except tk.TclError:
            pass
        self.pin_btn.configure(fg=HIGHLIGHT if self._pinned else TEXT_DIM)

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._scrollwrap.pack_forget()
            self.collapse_btn.configure(text="▼")
            self.geometry(f"{self.winfo_width()}x{self._header.winfo_reqheight() + 2}")
        else:
            self._scrollwrap.pack(fill="both", expand=True)
            self.collapse_btn.configure(text="▲")
            sh = self.winfo_screenheight()
            h = min(self._header.winfo_reqheight()
                    + self.content.winfo_reqheight() + 6, sh - 130)
            self.geometry(f"{self.winfo_width()}x{h}")

    def _drag_start(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # ── scroll plumbing (stable — no feedback loop) ──────────────────
    def _on_canvas_configure(self, e):
        self.canvas.coords(self._cwin, e.width / 2, 0)
        # Content used to be a fixed-width column, so shrinking the window
        # below that width just clipped text/bars off the right edge. Instead
        # track the canvas's real width and reflow charts/wraplengths to it.
        neww = max(300, e.width - 8)
        if abs(neww - self.COLW) > 6:
            self.COLW = neww
            self.content.configure(width=self.COLW)
            if self._resize_job:
                self.after_cancel(self._resize_job)
            self._resize_job = self.after(120, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        try:
            self._tok_help.configure(
                wraplength=max(120, self.COLW - 2 * self.PAD - 6))
            self.lbl_sub.configure(
                wraplength=max(120, self.COLW - 2 * self.PAD - 32))
        except tk.TclError:
            pass
        self.refresh(animate=False)

    def _on_content_configure(self, e):
        bb = self.canvas.bbox("all")
        if bb and bb != getattr(self, "_last_bbox", None):
            self._last_bbox = bb
            self.canvas.configure(scrollregion=bb)

    def _on_wheel(self, e):
        try:
            first, last = self.canvas.yview()
        except tk.TclError:
            return
        # canvas.yscrollincrement is set to 1px in _build, so this scrolls in
        # real pixels: clamp per-event magnitude so a fast trackpad swipe
        # can't fling the view (that read as "too fast" / sudden jumps), and
        # stop dead at each edge instead of bouncing past it and snapping back.
        amt = max(-36, min(36, -int(e.delta)))
        if amt == 0:
            return
        if amt < 0 and first <= 0.0:
            return
        if amt > 0 and last >= 1.0:
            return
        self.canvas.yview_scroll(amt, "units")

    # ── motion ──────────────────────────────────────────────────────
    def _tween(self, dur, on_frame):
        if not MOTION:
            on_frame(1.0)
            return
        if self._tw_id:
            try:
                self.after_cancel(self._tw_id)
            except Exception:
                pass
        t0 = time.monotonic()

        def step():
            p = min((time.monotonic() - t0) / dur, 1.0)
            try:
                on_frame(p)
            except tk.TclError:
                return
            if p < 1.0:
                self._tw_id = self.after(24, step)
            else:
                self._tw_id = None
        step()

    def _animate_number(self, label, old, new, dur=0.30):
        if not MOTION or abs(new - old) < 0.005:
            try:
                label.configure(text=f"${new:,.2f}")
            except tk.TclError:
                pass
            return
        t0 = time.monotonic()

        def step():
            p = min((time.monotonic() - t0) / dur, 1.0)
            v = old + (new - old) * ease_out(p)
            try:
                label.configure(text=f"${v:,.2f}")
            except tk.TclError:
                return
            if p < 1.0:
                self.after(24, step)
        step()

    def _flash(self, w):
        if not MOTION:
            return
        try:
            cur = w.cget("bg")
            w.configure(bg="#3A424F")
            self.after(110, lambda: w.winfo_exists() and w.configure(bg=cur))
        except tk.TclError:
            pass

    # ── AI insight ──────────────────────────────────────────────────
    def _set_insight(self, s):
        t = self.insight_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", s)
        n = int(t.index("end-1c").split(".")[0])
        t.configure(height=min(max(n, 4), 80), state="disabled")
        self.after_idle(self._on_content_configure_force)

    def _on_content_configure_force(self):
        bb = self.canvas.bbox("all")
        if bb:
            self._last_bbox = bb
            self.canvas.configure(scrollregion=bb)

    def _run_insight(self):
        if self._insight_busy:
            return
        self._insight_busy = True
        now = datetime.now().astimezone()
        label, since, until = period_specs(now)[self.filter_idx]
        bf = None if self.scope == "All" else self.scope
        scope_txt = f" · {self.scope}" if bf else ""
        self.insight_desc.configure(
            text=f"what to change next for {label}{scope_txt} — ranked, from "
                 f"your own conversations")
        self.insight_btn.configure(bg=BTN_ACTIVE, fg=TEXT_DIM, text="… working")
        self.insight_status.configure(text="reading your conversations + asking claude…")
        self._set_insight(f"Looking at your {label.lower()}{scope_txt} "
                          "conversations — long back-and-forths, big files, "
                          "model choice, repeats — and turning it into "
                          "ranked, plain-English suggestions…")

        def work():
            try:
                rep = gather_insight_report(PROJECTS_DIR, label, since, until,
                                            bucket_filter=bf)
                out = run_claude_insight(rep)
            except Exception as e:  # noqa: BLE001
                out = f"Insight failed: {e}"

            def done():
                self._insight_busy = False
                self.insight_btn.configure(bg=HIGHLIGHT, fg=BG, text="✨ Regenerate")
                self.insight_status.configure(
                    text=f"updated {datetime.now():%H:%M:%S}  ·  ~a few ¢")
                self._set_insight(out)
            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    # ── refresh ─────────────────────────────────────────────────────
    def refresh(self, animate=False):
        now = datetime.now().astimezone()
        specs = period_specs(now)
        idx = self.filter_idx
        scope = self.scope
        bf = None if scope == "All" else scope
        short = ["Today", "Week", "Month", "Last mo"]

        results, full = [], []
        for _, since, until in specs:
            u = parse_usage(PROJECTS_DIR, since, until, bucket_filter=bf)
            r = calc_cost(u)
            r["messages"] = u.get("messages", 0)
            results.append(r)
            full.append(r if bf is None
                        else calc_cost(parse_usage(PROJECTS_DIR, since, until)))
        res = results[idx]
        costs, tokens = res["costs"], res["tokens"]

        self._animate_number(self.lbl_total, self._prev_total, costs["total"])
        self._animate_number(self.lbl_hcost, self._prev_total, costs["total"])
        self._prev_total = costs["total"]
        self.lbl_eyebrow.configure(text=_track(
            f"total · {short[idx]}" + ("" if scope == "All"
                                       else f" · {scope.lower()}")))

        reset = {0: "resets local midnight", 1: "resets Sun 00:00 local",
                 2: "resets 1st, 00:00 local",
                 3: (now.replace(day=1) - timedelta(days=1)).strftime("%B %Y")}[idx]
        fbc = full[idx].get("bucket_costs", {})
        extra = ("\n" + "   ".join(f"{b} ${fbc.get(b, 0.0):,.0f}"
                                   for b in BUCKET_ORDER if fbc.get(b))
                 ) if scope == "All" and fbc else ""
        self.lbl_sub.configure(text=f"{res['messages']:,} messages · {reset}{extra}")

        # legend
        bc = full[idx].get("bucket_costs", {})
        bmsg = full[idx].get("bucket_msgs", {})
        order = BUCKET_ORDER + [b for b in bc if b not in BUCKET_ORDER]
        for w in self.split_legend.winfo_children():
            w.destroy()
        any_seg = False
        for b in order:
            if bc.get(b, 0.0) <= 0:
                continue
            any_seg = True
            cell = tk.Frame(self.split_legend, bg=CARD_BG)
            cell.pack(side="left", padx=(0, 12))
            sw = tk.Canvas(cell, width=9, height=9, bg=CARD_BG, highlightthickness=0)
            sw.pack(side="left", padx=(0, 4))
            sw.create_rectangle(0, 0, 9, 9, fill=BUCKET_COLOR.get(b, C_BAR), width=0)
            tk.Label(cell, text=f"{b}  ${bc.get(b, 0.0):,.2f}  ({bmsg.get(b, 0):,} msg)",
                     bg=CARD_BG, fg=TEXT_DIM, font=self.F["small"]).pack(side="left")
        if not any_seg:
            tk.Label(self.split_legend, text="no spend", bg=CARD_BG, fg=TEXT_DIM,
                     font=self.F["small"]).pack(anchor="w")

        # token rows
        for key, (tkn, cst) in self.tok_rows.items():
            tkn.configure(text=f"{tokens[key]:,}")
            self._animate_number(cst, self._prev_tok.get(key, costs[key]),
                                 costs[key], dur=0.26)
        self._prev_tok = {k: costs[k] for k in self.tok_rows}

        # by model
        for w in self.model_rows.winfo_children():
            w.destroy()
        mbc = res.get("model_bucket_costs", {})
        for model, mc in sorted(res["model_costs"].items(), key=lambda x: -x[1]):
            if mc <= 0:
                continue
            r = tk.Frame(self.model_rows, bg=CARD_BG)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=_short_model(model), bg=CARD_BG, fg=TEXT_MAIN,
                     font=self.F["small"], anchor="w").pack(side="left")
            tk.Label(r, text=f"${mc:,.2f}", bg=CARD_BG, fg=HIGHLIGHT,
                     font=self.F["mono_s"], width=10, anchor="e").pack(side="right")
            if scope == "All":
                sub = mbc.get(model, {})
                parts = "  ".join(f"{b[0]} ${sub[b]:,.0f}" for b in BUCKET_ORDER
                                  if sub.get(b))
                if parts:
                    tk.Label(r, text=parts, bg=CARD_BG, fg=TEXT_DIM,
                             font=self.F["mono_s"], anchor="e").pack(side="right",
                                                                    padx=8)

        self.lbl_updated.configure(
            text=f"updated {datetime.now():%H:%M:%S}  ·  auto every 60s")

        # chart data
        if scope == "All":
            period_items = [
                (short[i], [(full[i]["bucket_costs"].get("Work", 0.0), BLUE),
                            (full[i]["bucket_costs"].get("Personal", 0.0), GREEN)])
                for i in range(4)]
        else:
            period_items = [(short[i], results[i]["costs"]["total"])
                            for i in range(4)]
        split_segs = [(b, bc.get(b, 0.0), BUCKET_COLOR.get(b, C_BAR))
                      for b in order if bc.get(b, 0.0) > 0] or [("none", 1, C_DIM)]
        pc = res.get("project_costs", {}); pb = res.get("project_bucket", {})
        proj_rows = [(p, v, BUCKET_COLOR.get(pb.get(p), C_BAR))
                     for p, v in sorted(pc.items(), key=lambda x: -x[1])[:7]
                     if v > 0.004] or [("(no spend)", 0.0, C_DIM)]
        tok_segs = [("input", costs["input"], C_INPUT),
                    ("output", costs["output"], C_OUTPUT),
                    ("cache_write", costs["cache_write"], C_CWRITE),
                    ("cache_read", costs["cache_read"], C_CREAD)]

        cw = self.COLW - 2 * self.PAD
        hbar = round(24 * self.ui)

        def paint(k):
            im1 = render_vbars(cw, self._ph_periods, period_items, idx, k)
            self._ref["p"] = im1
            self.img_periods.configure(image=im1)
            im2 = render_stack(cw, hbar, split_segs, k)
            self._ref["s"] = im2
            self.img_split.configure(image=im2)
            im3, hh = render_hbars(cw, proj_rows, k)
            self._ref["j"] = im3
            self.img_proj.configure(image=im3)
            im4 = render_stack(cw, hbar, tok_segs, k)
            self._ref["t"] = im4
            self.img_tok.configure(image=im4)
            self._on_content_configure_force()

        if animate and MOTION:
            self._tween(0.34, paint)
        else:
            paint(1.0)

        self.after(60_000, lambda: self.refresh(animate=False))
        if not self._midnight_armed:
            self._midnight_armed = True
            nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) \
                + timedelta(days=1)
            ms = max(int((nxt - now).total_seconds() * 1000) + 1000, 1000)

            def _mid():
                self._midnight_armed = False
                self.refresh(animate=False)
            self.after(min(ms, 2_147_000_000), _mid)


if __name__ == "__main__":
    CostTracker().mainloop()
