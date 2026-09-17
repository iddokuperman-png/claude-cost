---
name: claude-cost
description: Report Claude Code / Cowork token usage and estimated spend by day/week/month, by account (Work/Personal), by project, and by conversation — plus plain-English money-saving suggestions. Use when the user asks "how much have I spent on Claude", "claude cost", "usage report", "token usage today/this week/this month", "what's costing me the most", or wants suggestions for cutting their Claude spend. Works from any project directory since it reads the global ~/.claude/projects logs, not anything project-specific.
---

# Claude Cost report

Portable version of the "Claude Cost Tracker" desktop widget
(`~/Applications/Claude Cost Tracker.app`) — same parsing logic, same
`~/.claude/cc_tracker_buckets.json` config, but no GUI dependency, so it runs
in any project via a plain script call instead of opening the desktop app.

## Running it

```bash
python3 ~/.claude/skills/claude-cost/scripts/usage_report.py --period today --scope all
```

- `--period`: `today` | `week` | `month` | `lastmonth` (default `today`)
- `--scope`: `all` | `work` | `personal` (default `all`)
- `--json`: machine-readable output instead of the text summary

Run it, then read the output yourself and answer the user directly — don't
just paste the raw report unless they ask for the raw numbers.

## How to talk about the numbers (no developer jargon)

- "new text" = input tokens (brand-new text typed that turn)
- "Claude's replies" = output tokens
- "first-time context" = cache_write (a file/instructions being stored for reuse)
- "re-sent context" = cache_read — the whole chat history + open files getting
  resent on every message of a conversation. In any long session this is
  almost always the single biggest cost driver, and it grows the longer the
  session runs.
- "messages" in the report = assistant turns / tool-calls logged, NOT things
  the user typed. One long agentic conversation with lots of tool calls
  (Read/Edit/Bash/etc.) can rack up hundreds of these from a handful of
  actual user prompts. Say this plainly if a message count looks high —
  don't let the user think they typed that many messages.

## Always caveat the total

This is a **local, rough, on-demand-rate ESTIMATE**, not a real invoice.
Confirmed by comparing against the real Anthropic Enterprise usage console
over several days: some days this undercounts badly (a Cowork session that
ran server-side, another device, another team member, or a scheduled job
never writes a transcript to this machine, so it's invisible here); other
days it overcounts badly (one very long local session's re-sent context
balloons past what the org's actual contracted/seat pricing would charge).
There is no reliable fixed correction factor between the two — always be
upfront that for the real, authoritative number the user should check the
Claude Console's Usage page (Settings → Usage limits), not this report. Only
Anthropic's official Usage & Cost Admin API
(`https://api.anthropic.com/v1/organizations/usage_report/messages`, needs an
org Admin API key) would give a number that actually matches that console.

## When asked for money-saving suggestions ("insight")

Don't shell out to `claude -p` — you're already the model running this skill,
so just read the report (project breakdown + top conversations + token mix)
yourself and write the suggestions directly. Rules:

- 4-7 items, ranked by estimated $ saved, biggest first.
- Plain English, no jargon (never say "tokens"/"cache"/"context window" —
  say "the chat history being resent every message", "the long
  back-and-forth conversation", etc.).
- Ground every item in something specific from the report (a named project,
  a conversation topic, a message count) — no generic advice.
- After each item add "→ saves about $X/month" with a rough estimate and, in
  parentheses, the plain-English mechanism.
- Prefer: starting a fresh conversation instead of continuing a huge one,
  keeping one topic per conversation, not pasting in huge files/folders that
  aren't needed, using a cheaper/faster model for quick questions, avoiding
  rambling back-and-forth.
- End with one line starting "Don't worry about:" naming anything that looks
  big in the numbers but isn't actually worth changing.
