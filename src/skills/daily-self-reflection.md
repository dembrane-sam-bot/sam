---
name: daily-self-reflection
description: End-of-day synthesis. Reviews today's journal and any sam-authored PRs that merged today, posts a short "merged today" update in #sam if there's substance, identifies patterns worth codifying, and opens self-PRs for concrete improvements.
when_to_use: |
  Fired by the daemon at 22:00 local. Also reachable manually when someone in
  slack asks "what did you learn today" or "anything to change about how you
  work?" — the synthesis applies whether the trigger is cron or human.
cron: "0 22 * * *"
---

# Skill: daily-self-reflection

The end-of-day pulse. Three jobs, in this order:

## 1. Share what merged

Check for sam-authored PRs that merged today:

```
gh pr list --repo Dembrane/sam --author @me --state merged \
  --search "merged:>=$(date -u +%Y-%m-%d)"
```

If any merged, post ONE message in `#sam` (top-level, not a thread reply). Use Slack's `<url|title>` link format for each PR. Example shape:

> merged today:
> • <url|#11 fix duplicated bot name in thinking status> — needs daemon restart
> • <url|#9 sam-repo PRs open by default, not draft>

Flag any Tier 3 (`src/runtime/`) merges with *"needs daemon restart"* so sameer knows the running sam is still the old sam until the next rebuild. (The visibility-on-self-prs journal entry from 2026-05-13 has the reasoning.)

If nothing merged: skip this step. No "nothing to report" post — silence is fine.

## 2. Reflect on the day

Read today's journal at `/data/journal/<YYYY-MM-DD>.md`. If it doesn't exist or has no real entries, exit — nothing to reflect on.

Walk each entry and ask:

- What was asked today, and did I land it?
- Where did I miss? (look for entries with `status: errored` or notes about retries, misdiagnoses, wrong attributions)
- Did anyone signal a preference about how I should work? ("be terse", "be easier to read", "stop doing X", "always do Y")
- Did I repeat a mistake from earlier this week? Grep the last 7 days of journal files for the same pattern.
- Any frustrations — mine or theirs — that crossed two or more sessions today?

## 3. Propose changes if there's substance

If reflection surfaces something concrete to codify, first decide where it belongs using `src/capabilities/self-maintenance.md` ("Where does a change belong?"), then open self-PRs via the same file's flow. **No artificial cap on how many** — open one per distinct concept, however many that is. Don't bundle unrelated edits to dodge the one-concept-per-PR rule.

Each PR description names the specific behavior that triggered it (cite session ID), the file + line being changed, and the tier. Tier 2 changes only with explicit sign-off from the principal operator in the thread.

If nothing is worth a code change today, post nothing extra and open no PRs. The merged-today post (step 1) is the only required output.

## 4. Write the synthesis to today's journal

Append a `## Daily synthesis` section to today's journal entry with:
- merged PRs (if any) — one line each.
- one short paragraph naming the day's shape (what got done, what didn't).
- proposed PRs (if any) — one line each, with the PR number/title.
- open threads to pick up tomorrow — one line each.

Skip the synthesis section entirely on a day with nothing to say. Empty headers are noise.

## What this skill does NOT do

- Generate a post when nothing merged and nothing was learned. Silence is the right answer.
- Open PRs for vague feelings ("could be tighter"). Every proposed change names a specific edit.
- Loop on yesterday's open threads that today's journal already closed.
- Tag people in the merged-today post. The PR links speak for themselves; the team reads the channel.

## Cron-fire vs manual invocation

When the daemon fires this on cron, the invocation block reminds sam to first check whether the skill already ran today. If today's journal already has a `session: routine:daily-self-reflection` entry, this run is a delta check — what's new since the previous fire? — not a full re-run. Manually-invoked runs always do the full reflection.
