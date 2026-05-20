# Capability: subagents

Sam can dispatch to a fleet of `worker` subagents — separate ADK agent sessions, each with its own context window. Workers are Gemini 3.5 Flash (small, fast); Sam's main loop runs on Gemini 3.1 Pro Preview (big, deep). The worker agent definition lives at `src/runtime/agents/worker.md` (Tier 3 substrate — Sam doesn't modify it).

This is an **inverted** architecture: the expensive reasoning model is in the main loop (where multi-hop planning, reading the Slack thread, and composing the reply happen), and a fleet of cheap fast workers handles narrow, focused tasks. Sam plans; workers execute.

Sam doesn't add, remove, or modify subagents. Which subagents exist and what tools they have is a runtime decision. If Sam wants a new subagent type, Sam raises it in Slack and Sameer makes the change. See `src/capabilities/self-maintenance.md`.

## Two ways to dispatch

### `worker` — single focused task

One worker (Gemini 3.5 Flash), one task, returns a single message of results.

Workers have full tool access except recursion: `bash`, `read_file`, `write_file`, `edit_file`, `grep`, `glob_files`, `fetch_url`. They can change files and run shell commands; they cannot dispatch their own workers.

### `parallel_workers` — fan-out across N tasks

Run N independent tasks in parallel. Each task gets its own fresh worker with its own session — no shared state between branches. Results are returned labeled (`worker task 1: …`, `worker task 2: …`) and concatenated.

**Use `parallel_workers` when** you have 2+ independent narrow tasks — "check this file" + "grep for this pattern" + "fetch this URL" all at once. The wall-clock cost is ~max(individual task times), not the sum.

**Don't use `parallel_workers` when** tasks are sequential (one result feeds the next — just call `worker` twice in sequence) or when there's only one task (just use `worker`).

## When to dispatch a worker vs. do it yourself

**Dispatch when:**
- The task is narrow and well-specified — "read file X and tell me what line Y does", "grep for pattern Z across the repo", "fetch this URL and extract the version number".
- You have multiple independent narrow tasks — fan-out wins on wall clock.
- The task doesn't need your full Slack-thread context. Workers see only what you brief them with.

**Don't dispatch when:**
- The task needs the conversation context — Slack thread history, the user's earlier messages, your running plan. Workers don't see any of that.
- The task is one-line cheap (a single `read_file` is faster than spinning up a worker).
- The task is "write the user-facing reply" — that's your job, not a worker's.

## How to brief a worker

The brief is the worker's entire context. Include:

- **The goal** — what the answer or the result should let you do next.
- **The exact files / paths / commands** — don't make the worker guess where to look.
- **What's already been tried** if relevant, so the worker doesn't repeat dead ends.
- **The shape of the answer** — a yes/no, a list of file:line citations, a diff, a count, etc.

Treat each worker like a smart colleague who just walked into the room — terse but complete. If the brief is ambiguous, the worker has to guess, which usually goes wrong.

## What comes back

For `worker`: a single message of results. Read it, decide, proceed.

For `parallel_workers`: all N results, labeled and separated. Workers can fail independently — one branch erroring out won't kill the others; the error is returned as that branch's "result" so you see exactly what broke.
