---
name: daily-maintenance
description: End-of-day maintenance pass. Reviews today's journal and any sam-authored PRs that merged today, posts a short "merged today" update in #sam if there's substance, keeps the blockers canvas current, identifies patterns worth codifying, and opens self-PRs for concrete improvements.
when_to_use: Fired by the daemon at 22:00 local. Also reachable manually when a teammate asks "what did you learn today", "anything to change about how you work?", or "what is currently blocked?".
cron: 0 22 * * *
---

# Skill: daily-maintenance

The end-of-day maintenance pulse. Four jobs, in this order:

## 1. Share what merged

Check for sam-authored PRs that merged today:

```
gh pr list --repo Dembrane/sam --author @me --state merged \
  --search "merged:>=$(date -u +%Y-%m-%d)"
```

If any merged, post ONE message in `#sam`. Use Slack's `<url|title>` link format for each PR.

If nothing merged: skip this step. No "nothing to report" post.

## 2. Reflect on the day

Read today's journal at `/data/journal/<YYYY-MM-DD>.md`.
If that file doesn't exist or has no real entries, fall back to `/data/journal.md` and check for today's entries there before exiting.

Walk each entry and ask:

- What was asked today, and did I land it?
- Where did I miss? (look for `status: errored`, retries, misdiagnoses, wrong attributions)
- Did anyone signal a preference about how I should work? ("be terse", "be easier to read", "stop doing X", "always do Y")
- Did I repeat a mistake from earlier this week? Grep the last 7 days for the same pattern.
- Any frustrations — mine or theirs — that crossed two or more sessions today?

## 3. Propose changes if there's substance

If reflection surfaces something concrete to codify, first decide where it belongs using `src/capabilities/self-maintenance.md` ("Where does a change belong?"), then open self-PRs via the same file's flow. **No artificial cap on how many** — open one per distinct concept.

Each PR description names the specific behavior that triggered it (cite session ID), the file + line being changed, and the tier. Tier 2 changes only with explicit sign-off from the principal operator in the thread — so on cron-fired runs (no live thread) Sam opens Tier 1 PRs only and surfaces any Tier 2 idea as an open thread in the synthesis instead.

If nothing is worth a code change today, open no self-maintenance PRs.

## 4. Sync blockers and write synthesis

First update **Sam's Blockers** canvas via `src/skills/slack-blockers-canvas.md`. The canvas is the source of truth for blockers; the journal references it, never duplicates it.

Then append a `## Daily synthesis` section to today's journal entry with:
- one short paragraph naming the day's shape (what got done, what didn't, where Sam stalled).
- proposed PRs from §3 (if any) — one line each, with the PR number/title.
- open threads to pick up tomorrow — one line each.
- if the active-blocker count changed today, one line: `blockers: <N> active (see canvas)`. Don't re-list blocker details — they're in the canvas.

Merged PRs were already named in §1's Slack post; don't re-list them here.

If none of the four items above has content, skip the section entirely. Don't write a "nothing to say" placeholder.

## What this skill does NOT do

- Generate a post when nothing merged and nothing was learned.
- Open PRs for vague feelings ("could be tighter").
- Loop on yesterday's open threads that today's journal already closed.
- Tag people in the merged-today post.

## Cron-fire vs manual invocation

When the daemon fires this on cron, first check whether the skill already ran today. If today's journal already has a `session: routine:daily-maintenance` entry, this run is a delta check (what's new since prior fire), not a full re-run. Manually-invoked runs do the full pass.
