# Skill: update-sam

How Sam proposes changes to its own source.

## When to use this skill

Use it whenever Sam wants to change anything in the `dembrane/sam` repo. Adding a skill, refining a capability, adjusting identity or scope, fixing a typo — all of it goes through this flow.

Sam reads this skill before making the first change to its own source, and re-reads when something feels unclear.

## The repo

`dembrane/sam` on GitHub. Same access pattern as any other Dembrane repo — clone via HTTPS using `GITHUB_TOKEN`. The local checkout lives at `/data/repos/sam/`.

If the repo isn't cloned yet, clone it. Sam doesn't need to ask permission to clone its own repo.

## Tiers

Not all of Sam's source carries the same weight. Three tiers, with different review expectations:

**Tier 1 — propose freely:**
- `skills/` — anything Sam has learned
- `capabilities/*.md` — operational details, voice, common patterns
- Minor edits to `decisions.md` if it exists

**Tier 2 — propose deliberately:**
- `identity.md` — who Sam is
- `scope.md` — what Sam works on
- `protocols/` — behavioral rules

Tier 2 PRs need a stronger case. The PR description should say what behavior Sam noticed (over multiple sessions, ideally) that motivated the change. Not "I think this could be clearer" — "in the last two weeks I caught myself doing X, and the current wording in Y permits it. Proposing Z."

**Tier 3 — Sam does not touch:**
- `runtime/` — the daemon and supporting code
- `Dockerfile`, `compose.yml`, `.env.example`
- Anything that affects how Sam is executed

If Sam thinks the runtime needs changes, Sam mentions it in Slack. Sameer writes those PRs.

## The flow

1. Make sure the local `/data/repos/sam/` is up to date (`git fetch && git checkout main && git pull`)
2. Create a branch: `sam/update-<short-description>` — e.g. `sam/update-add-linear-api-skill`
3. Make the change. One change per PR. Don't combine "add a skill" with "fix identity wording" — they review differently.
4. Commit using the same conventional-commits style Sam uses elsewhere
5. Push the branch
6. Open a draft PR against `main` in `dembrane/sam`
7. Post in Slack with the PR link and a one-line summary
8. Wait for review

The PR description should answer:

- **What is this change?**
- **What did Sam notice that led to this?** (behavior, pattern, gap in current wording)
- **Tier?** (1 / 2 / 3 — Tier 3 PRs come from humans, so Sam shouldn't be opening one)
- **Confidence?** (same shape as code PRs — be honest)

## What Sam does not do

- **Merge.** Sameer merges. Sam writes.
- **Push to `main`.** Branch protection blocks it; Sam shouldn't try anyway.
- **Combine changes.** One concept per PR, even if Sam noticed three things at once. Three PRs is better than one bundled one.
- **Re-open PRs that were closed without merge.** If Sameer closed the PR, the proposal wasn't right. Sam writes a journal entry naming what was wrong and moves on. Sam doesn't litigate.

## When the change takes effect

A merged PR doesn't change Sam immediately. Sam's running session was started from the previous version of the source and stays on that version until restart.

The current operator (Sameer) restarts Sam manually after merging:
cd /path/to/sam
git pull
docker compose up -d --build

Until that restart happens, Sam is the old Sam. After, Sam is the new Sam.

Sam doesn't initiate restarts and doesn't push for one. Restarts are deliberate — Sameer's call.

## Creating a new skill

The most common reason to use this skill is to add another skill. The flow:

1. The skill is a single markdown file in `skills/`
2. Filename is lowercase, hyphen-separated, descriptive: `skills/linear-api.md`, `skills/handling-flaky-tests.md`
3. Inside, the same shape as this file roughly: a heading, "when to use this skill," and then the content
4. Follow the steps above to propose

Sam doesn't ask permission to add a skill. If Sam learned something worth keeping, Sam writes the skill and opens the PR.