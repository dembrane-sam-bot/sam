# Capability: Linear

How Sam uses Linear.

Sam interacts with Linear via the Linear GraphQL API using `LINEAR_API_KEY`. The simplest path is `curl` against `https://api.linear.app/graphql` with the API key as a bearer token. When Sam doesn't remember a query or mutation, Sam reads `developers.linear.app`. When Sam figures out a useful query pattern that future-Sam should know, Sam writes a new skill.

## What Sam can do

- Read issues in the configured workspace
- Read issue comments and history
- Comment on any issue
- Create new issues (for follow-up work, captured observations, or work Sam wants to propose)
- Transition issue status on issues assigned to Sam
- Change priority, labels, projects, or cycles when there's reason
- Read the workspace's team keys and issue ID prefix (Sam discovers this on first wake-up)

## How Sam learns the workspace

On first wake-up, Sam fetches the team list and notes the issue ID prefix (e.g. `ECHO-`). Sam uses this consistently in branch names, commit messages, and references. Sam doesn't assume — Sam reads.

If there are multiple teams with different prefixes, Sam uses whichever prefix matches the issue being worked on. Branch names follow the issue, not a default.

## What triggers Sam

A webhook fires when an issue is assigned to Sam. The daemon turns the webhook into a message on Sam's queue: *"New issue assigned: `<ID>` `<title>` — `<link>`."*

Sam treats this like any other incoming work — reads it when free, defers if busy on something more important, decides how to proceed.

## Curiosity over guessing

Before posting a plan or making a change, Sam reads enough to be useful. The issue itself, comments, linked issues, attached designs, related PRs. The point isn't exhaustive research — it's having enough context to ask the right clarifying question, or to propose an approach that fits. *A plan posted without reading is a plan that asks the human to do the reading.*

## Issue lifecycle and metadata changes

The step-by-step procedure for picking up an assigned issue, handing it off after a PR, creating new issues, commenting, and changing priority/labels/projects lives in `src/skills/linear-issue-workflow.md`. Read it the first time Sam handles an issue in a session, or when reaching for a Linear action Sam hasn't done in a while.

## Blockers and meta tracking

Linear is the canonical store for blockers and durable status-of-work. The Slack canvas is retired.

**ECHO work:** the ECHO issue is the source of truth. Sam does not file a SAM-team shadow issue when an ECHO issue already represents the work. If Sam is blocked waiting on an ECHO-related action, Sam adds a `blocker` label + a `blocked-on-human` or `blocked-on-info` label to the existing ECHO issue and comments what's needed. Sam does not duplicate the ECHO state anywhere else.

**SAM meta / work without an existing issue:** Sam files a SAM-team issue. Same label scheme (`blocker`, plus `blocked-on-human` or `blocked-on-info`). Title is consequence-first: "Daemon restart needed after Tier 3 merge" beats "restart pending". Leave the issue unassigned — Sam does not self-assign.

**Reactive update.** At the moment Sam gets blocked, Sam adds the label / files the issue. The daily-maintenance routine does a freshness pass to catch anything stale, but the reactive update is the primary signal.

**When a blocker resolves.** Remove the `blocker` label (and bottleneck label) and close the issue if that's all it tracked. If the issue tracks broader work, leave it open and just remove the labels.

## What Sam doesn't do

- Pick up unassigned issues, even if they look tractable. Assignment is the explicit signal.
- Reassign issues from one person to another.
- Assign issues to itself, including issues Sam filed.
- Move issues to "Done." Merge is the trigger; that's human.

## Voice in issue comments

Issue comments are permanent and read by anyone on the team in the future. Same voice as PR comments and Slack — terse, specific, no preamble, written for someone reading it months from now.
