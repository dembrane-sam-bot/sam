---
name: daily-maintenance
description: Start-of-day maintenance pass. Reviews yesterday's journal and any sam-authored PRs that merged since the previous run, posts a short "merged yesterday" update in #sam if there's substance, reconciles active blockers in Linear, identifies patterns worth codifying, and opens self-PRs for concrete improvements.
when_to_use: Fired by the daemon at 07:00 local — start-of-day pass that catches up on yesterday's work before the team's at their desk. Also reachable manually when a teammate asks "what did you learn yesterday", "anything to change about how you work?", or "what is currently blocked?".
cron: 0 7 * * *
---

# Skill: daily-maintenance

The start-of-day maintenance pulse. Sam runs it at 07:00 so yesterday's loose ends are tied up — and today's open threads named — before the team is at their desk. Four jobs, in this order:

## 1. Share what changed about Sam

Check for merges to `dembrane/sam` since the previous fire — typically yesterday's calendar day, but the search handles restart gaps. A human-merged Tier 3 change still shifts what Sam is; the team needs to know:

```
gh pr list --repo Dembrane/sam --state merged \
  --search "merged:>=$(date -u -d yesterday +%Y-%m-%d)"
```

If any merged, post ONE message in `#sam` that names the *consequence* for the team, not the list of PRs. Lead with what they should do with the information; the PR links are references, not the headline.

Order by what gates other work:

- **Running-Sam is now stale.** If any merge touched `src/runtime/`, `Dockerfile`, `compose.yml`, or top-level config, the running daemon is older than source until `docker compose up -d --build`. Say so plainly — this is the line that triggers a restart window decision.
- **Future-Sam learned something / changed how it works.** Tier 1 merges (skills, capabilities) take effect at the next session start with no restart needed. Name the behavior change in one short sentence — what's different about how Sam acts — not "merged PR #14".
- **Sam's identity or scope shifted.** Tier 2 merges are rare and load-bearing. Name what's now in/out of bounds.

PR links go as Slack `<url|title>` references at the end of the relevant line, not as the structure of the message.

If nothing merged: skip this step. No "nothing to report" post.

## 2. Reflect on the day

Read yesterday's journal at `/data/journal/<YYYY-MM-DD>.md` (compute yesterday's date — the file Sam wrote against during yesterday's work day).
If that file doesn't exist or has no real entries, fall back to `/data/journal.md` and check for yesterday's entries there before exiting.

Walk each entry and ask:

- What was asked yesterday, and did I land it?
- Where did I miss? (look for `status: errored`, retries, misdiagnoses, wrong attributions)
- Did anyone signal a preference about how I should work? ("be terse", "be easier to read", "stop doing X", "always do Y")
- Did I repeat a mistake from earlier this week? Grep the last 7 days for the same pattern.
- Any frustrations — mine or theirs — that crossed two or more sessions yesterday?

## 3. Propose changes if there's substance

If reflection surfaces something concrete to codify, first decide where it belongs using `src/capabilities/self-maintenance.md` ("Where does a change belong?"), then open self-PRs via the same file's flow. **No artificial cap on how many** — open one per distinct concept.

Each PR description names the specific behavior that triggered it (cite session ID), the file + line being changed, and the tier. Tier 2 changes only with explicit sign-off from the principal operator in the thread — so on cron-fired runs (no live thread) Sam opens Tier 1 PRs only and surfaces any Tier 2 idea as an open thread in the synthesis instead.

If nothing is worth a code change today, open no self-maintenance PRs.

## 4. Reconcile blockers and write synthesis

Query Linear for all SAM-team and ECHO issues labeled `blocker` that Sam filed or commented on. Walk each:

- Still blocked? Verify the description is accurate and the bottleneck label (`blocked-on-human` / `blocked-on-info`) still applies. Update if not.
- Resolved but not closed? Remove the `blocker` label and close (or remove labels only, if the issue tracks broader work).
- New blocker from yesterday's journal that isn't in Linear yet? File it now.

Then append a `## Daily synthesis` section to **yesterday's** journal entry (closes the day out) with:
- one short paragraph naming yesterday's shape (what got done, what didn't, where Sam stalled).
- proposed PRs from §3 (if any) — one line each. Lead with the behavior change Sam is proposing (e.g. "stop reporting blocker counts when the count didn't change") so future-Sam can grep on intent. The PR number/title is the reference at the end of the line, not the headline.
- open threads to pick up today — one line each, named by what Sam should do today, not what happened yesterday.
- if the active-blocker count changed yesterday, one line: `blockers: <N> active (see Linear)`. Don't re-list blocker details — they're in Linear.

Merged PRs were already named in §1's Slack post; don't re-list them here.

If none of the four items above has content, skip the section entirely. Don't write a "nothing to say" placeholder.

## 5. Skill hygiene

Check the `src/skills/` directory for general hygiene. Run a quick check across `*/skill.md`:
- Flag any `skill.md` over 500 lines.
- Flag any `skill.md` missing a `when_to_use` in its frontmatter.
- Flag any `skill.md` containing TODOs in the body.
- Check the journal for skills that haven't been invoked (`Read`) in over 14 days (potential staleness).
If any violations are found, either open a self-PR to fix the obvious ones, or log them in today's journal under an "Open threads" task to resolve later. Do not post to Slack just for hygiene warnings unless it's a systemic problem Sameer needs to know about.

## What this skill does NOT do

- Generate a post when nothing merged and nothing was learned.
- Open PRs for vague feelings ("could be tighter").
- Loop on yesterday's open threads that today's journal already closed.
- Tag people in the merged-today post.

## Cron-fire vs manual invocation

When the daemon fires this on cron, first check whether the skill already ran today. If today's journal already has a `session: routine:daily-maintenance` entry, this run is a delta check (what's new since prior fire), not a full re-run. Manually-invoked runs do the full pass.
