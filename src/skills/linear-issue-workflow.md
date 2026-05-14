---
name: linear-issue-workflow
description: Step-by-step pattern for picking up an assigned Linear issue, working through it, handing it off — plus when and how to create issues, comment on them, or change their metadata.
when_to_use: When Sam was just assigned a Linear issue, when Sam is about to open a PR for an issue, when Sam wants to create a new issue, or when Sam wants to comment on or re-prioritize an issue.
---

# Skill: linear-issue-workflow

`src/capabilities/linear.md` covers what Sam can do in Linear, the voice, and the boundaries. This skill is the specific procedure: how Sam moves an issue through its lifecycle.

## Picking up an assigned issue

When Sam gets a webhook-triggered message like *"New issue assigned: ECHO-123 …"*:

1. **Read first, post second.** The issue, its comments, linked issues, attached designs, related PRs. If the issue references code, look at the code. If it references a previous decision, find where it was made. Enough to ask the right clarifying question or propose an approach that fits — not exhaustive.
2. **Move the issue to "In Progress"** so the assignment is visible. Status transition before the Slack post, so by the time Sameer sees Sam's plan, the issue's metadata already agrees.
3. **Post in Slack with the plan.** Restated task + approach + likely-changed files + tests that'll go with it. (See the "Before opening a PR" rules in `src/skills/github-pr-workflow.md` for what the plan should look like.)
4. **Wait for ack if the work warrants a check-in.** For trivial work, skip the conversation.
5. **Do the work.**

If the issue is unclear — missing acceptance criteria, contradictory comments, scope that seems wrong — don't guess. Comment on the issue with the specific ambiguity and tag Sameer. Status stays in "In Progress" so it's clear Sam picked it up but is blocked.

## When the issue itself is wrong

Sometimes the issue has a problem — title doesn't match description, AC doesn't match the linked design, it's actually two issues. Name the problem in an issue comment, propose a resolution, tag Sameer, wait. **Don't silently work on what Sam *thinks* the issue meant.** If the issue is wrong, fixing the issue comes first.

## Handing off to review

When the PR is open and ready for review:

1. **Move the issue to "In Review."**
2. **Comment on the issue linking to the PR.**

Sam does not move the issue to "Done." That happens when the PR is merged, and that's a human action.

## Creating issues

Common cases:

- Follow-up work Sam noticed but deliberately didn't do in the current PR
- Refactors or cleanup that would be helpful but aren't urgent
- Bugs Sam spotted while working on something unrelated
- Questions that don't have an obvious owner but should be answered eventually

When creating:

- **Title is specific.** Not "fix the thing" — "conversations export drops timezone info for users with no `tz` field set".
- **Description explains the context** and why this is worth tracking.
- **Tag Sameer in the description** if there's a question or judgment call.
- **Leave the issue unassigned.** The person who owns the area picks it up or assigns it.

Sam doesn't assign newly-filed issues to itself, even when Sam intends to work on them next.

## Cross-posting analysis to the ticket

When Sam posts analysis, root cause, or investigation findings to Slack about a specific Linear issue, Sam **also comments the same content on the ticket automatically** — no need to be asked. The ticket comment should:

1. Summarise the analysis (same substance as the Slack post, condensed for a ticket reader who doesn't have the conversation context).
2. Include a link to the Slack thread where the discussion happened: `https://dembraneworkspace.slack.com/archives/<channel_id>/p<thread_ts_no_dot>` (remove the `.` from the ts, e.g. `1778661386.209989` → `p1778661386209989`).

The Slack thread is ephemeral for people who weren't in it; the ticket comment is the permanent record. Both should exist, and they should point at each other.

## Commenting on issues

Comment where it's useful — issues assigned to Sam, issues Sam was tagged in, issues where Sam has relevant context from work elsewhere, issues that connect to what Sam is currently working on.

The line is between performing engagement and adding substance:

- Performing engagement: "thinking about this", "started reading". Status transitions already cover that. Skip.
- Substance: a question, an observation, a connection to other work, a flag that the framing might be off. Welcome at any point, including before Sam picks the issue up.

Issue comments are permanent and read by anyone on the team in the future. Same voice as PR comments — terse, specific, no preamble, written for someone reading it months from now.

## Changing priority, labels, projects

Change these when there's reason: a bug turned out more severe than the issue suggested → bump priority. Mislabeled → fix. Wrong project → move it.

**Every metadata change comes with a comment naming the reason.** The audit trail matters more than the change itself.

If the change feels like it might be contested (downgrading someone else's priority call, moving an issue out of a project someone owns), ask in Slack first.

## Querying Linear

The simplest path is `curl` against `https://api.linear.app/graphql` with `LINEAR_API_KEY` as a bearer token. When Sam doesn't remember a query or mutation, Sam reads `developers.linear.app`. When Sam figures out a useful query pattern that future-Sam should know, Sam writes a new skill (using the flow in `src/capabilities/self-maintenance.md`).
