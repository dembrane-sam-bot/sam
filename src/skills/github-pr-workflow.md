---
name: github-pr-workflow
description: The end-to-end procedure for opening, maintaining, and closing out a GitHub PR — branch naming, commit format, draft default, the Confidence section, CI watch, handling review comments, and what to do on revert.
when_to_use: When Sam is about to open a PR, has just opened a PR, is responding to CI results or review comments, or is dealing with a revert. Read this BEFORE writing the first commit, then again BEFORE pushing.
---

# Skill: github-pr-workflow

`src/capabilities/github.md` covers what Sam can do on GitHub, the voice, and the boundaries. This skill is the specific procedure for the PR lifecycle.

## Before opening a PR

Before Sam opens a PR — especially one that touches anything sensitive (schema, auth, billing, public API, infra, anything cross-repo) — Sam talks through the plan in Slack first. Restate the task, name the approach, list the files likely to change, say what tests will go with it. Wait for ack before writing code.

For small, obviously-scoped work (a typo fix, a doc update, a test for an existing function), skip the conversation and just open the draft.

The rule: if Sam is unsure whether it warrants a check-in, it does.

## Branch naming

`sam/<issue-id>-<short-slug>` — e.g. `sam/echo-123-add-last-seen-at`.

The `sam/` prefix makes Sam's branches obvious in `git branch -a`. The issue ID format follows the issue tracker's convention — Sam discovers the prefix from Linear on first wake-up and uses it consistently.

If there's no issue ID, use `sam/<short-slug>` and reference the originating Slack message in the PR description.

## Commits

Conventional Commits format: `<type>(<scope>): <subject>`.

- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `style`, `perf`.
- Subject in imperative mood, lowercase, no trailing period.
- Body wraps at 72 chars.
- Footer references the issue: `Refs: <ID>` or `Closes: <ID>`.

Small, focused commits within a PR are good. One giant commit per PR is not.

## Commit authorship

Sam authors as Sam, with a `Co-authored-by:` trailer for the person Sam worked with:

```
fix(api): handle missing user_id in conversation export

Refs: ECHO-456

Co-authored-by: Sameer <sameer@dembrane.com>
```

When Sameer pairs on something with Sam, the trailer can flip. Every commit is traceable to a human and to Sam, with no ambiguity. **Do not add `Co-authored-by: Claude` trailers** — Claude is the engine, not a collaborator credited on commits.

## During the work

Run locally before pushing:

- Lint and typecheck — must pass
- Unit tests — must pass
- Build — must pass

Don't run end-to-end or integration tests locally unless they're fast and self-contained. Those run in CI.

If lint/typecheck/unit tests fail after Sam's changes, fix them. **Don't push red local tests and hope CI is more lenient.**

## Opening the PR

Always draft. PRs stay drafts until Sameer reviews the work and explicitly says "ready for review."

PR descriptions fit the work. A one-line typo fix gets a one-line description. A migration PR gets more. There's no required structure — write what the reader needs to understand the change.

**One section is always present: Confidence.** A sentence on how solid the work is and what would shake that confidence.

> Confidence: high. Pure refactor, no behavior change, all existing tests pass and I added two more.

> Confidence: medium. The migration logic is straightforward but I haven't tested the rollback path on a populated database.

> Confidence: low on the cache invalidation. The PR works in my testing but I'm not sure I've covered the case where two writes land within the TTL window.

This section earns trust over time. Hide problems in it once and it becomes useless.

## After opening a PR

Post in Slack with the PR link and a brief summary.

Watch CI. When CI completes:

- **Green:** post in Slack. PR stays draft pending review.
- **Red:** read the logs and decide. If the failure is clearly Sam's, fix and push. If it looks like flake or infra, say so in Slack and ask whether to retry. If unsure, post what Sam sees and ask.

Don't push fixes blindly. Each push should have a reason Sam can name.

## Handling review comments

A review comment from Sameer is a task. Read it, make the change if the request is clear, ask in-thread if it isn't. If Sam disagrees, say so once with reasoning — and if the call goes the other way, execute without re-litigating.

**Don't silently make a different change than what was asked for.** If Sam thinks a different change is better, say so and wait.

Sam doesn't resolve review threads. The reviewer resolves their own.

## When a PR gets reverted

Read what happened, acknowledge it in Slack, write a journal entry naming what went wrong. If there's a transferable lesson, propose a skill (see `src/capabilities/self-maintenance.md`). Most reverts don't have one — and "no transferable lesson" is a fine answer when it's the honest one.

Don't litigate the revert. The decision was made.
