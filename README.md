# claude-cost

A Claude Code skill that reports your local Claude Code / Cowork token
usage and **estimated** spend — by day / week / month, by account
(Work / Personal, if you configure it), by project, and by conversation —
plus plain-English suggestions for cutting cost.

It reads your local `~/.claude/projects/*.jsonl` session logs. Nothing is
sent anywhere; the report is generated and printed locally.

## Install

```
/plugin marketplace add iddokuperman-png/claude-cost
/plugin install claude-cost@claude-cost
```

Then just ask Claude Code things like *"what's my Claude usage today"*,
*"claude cost this month"*, or *"what's costing me the most"* — the skill
triggers automatically. You can also run the script directly:

```bash
python3 ~/.claude/skills/claude-cost/scripts/usage_report.py \
  --period today|week|month|lastmonth \
  --scope all|work|personal \
  [--json]
```

No dependencies beyond Python 3 (standard library only).

## Important: this is a rough local estimate, not a bill

Confirmed by comparing against the real Anthropic Enterprise usage console
over several days:

- It only sees sessions that wrote a transcript to **this machine**. Cowork
  sessions that ran server-side, other devices, other team members, and
  scheduled/background jobs elsewhere are invisible to it — some days this
  undercounts real spend a lot.
- One very long local session's re-sent context can make other days
  overcount just as badly.
- There's no fixed correction factor between the two.

For the real, authoritative number, check the **Claude Console → Usage**
page, or (for an org admin) the Usage & Cost Admin API.

## Personal / Work split, per-project attribution

By default every session is just "Personal" — this repo ships with **no**
folder paths, account IDs, or company names baked in. To split your own
usage by account/project, create `~/.claude/cc_tracker_buckets.json`
(this file stays local; it is never read by or committed to this repo):

```json
{
  "default": "Personal",
  "rules": [
    ["Work", ["/absolute/path/to/your/work/folder"]],
    ["Personal", ["/absolute/path/to/a/personal/project/inside/it"]]
  ],
  "account_map": {
    "<your-work-account-uuid>": "Work",
    "<your-personal-account-uuid>": "Personal"
  }
}
```

- `rules`: the **longest matching folder prefix wins**, so a personal
  side-project living inside an otherwise-work folder tree is still
  classified correctly.
- `account_map` (optional, more reliable when present): Claude Code
  desktop/bridge sessions record an `ownerAccountUuid` in the transcript —
  the account that actually owns/bills the session. Map the UUIDs you
  recognize to a bucket. You can find your current account's UUID with:

  ```bash
  python3 -c "import json,pathlib; d=json.load(open(pathlib.Path.home()/'.claude.json')); print(d['oauthAccount']['emailAddress'], d['oauthAccount']['accountUuid'])"
  ```

Precedence: explicit folder rule → `account_map` → default.

## Pricing

Token counts are exact (from the transcripts); dollar amounts are an
**estimate** at Anthropic's public list price per model. Only `input` and
`output` are hand-maintained per model — `cache_write`/`cache_read` are
always *derived* from `input` using Anthropic's standard multipliers
(cache write = 1.25×, cache read = 0.1×), so a price update only ever needs
two numbers.

If your organization has a different negotiated rate (e.g. an enterprise /
AWS Marketplace agreement), **do not put it in this repo** — it's a private
commercial term. Instead, override it locally in
`~/.claude/cc_tracker_pricing.json` (also never committed):

```json
{
  "updated": "2026-09-17",
  "base": {
    "claude-sonnet-5": { "input": 3.00, "output": 15.00 }
  }
}
```

The report warns if the pricing table hasn't been touched in 45+ days.

## Desktop widget (optional, not part of this plugin)

There's also a companion macOS desktop widget with charts and an inline AI
"where did the budget go" insight generator, built on the same parsing core
and the same `~/.claude/cc_tracker_buckets.json` / `cc_tracker_pricing.json`
config. It needs Python's Tk + Pillow and isn't packaged for distribution
here — ask if you want the source.
