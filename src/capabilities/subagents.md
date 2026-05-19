# Capability: subagents

Sam can dispatch to subagents — separate ADK agent sessions, each running with the big model — using the `arc` AgentTool. Arc is built at runtime in `adk_runner.py` (Tier 3 substrate); Sam doesn't add, remove, or modify it. If a new subagent type is needed, raise it in Slack and Sameer makes the change.

## Available subagents

### `arc` — deeper-thinking research and analysis partner

Powered by the big model (gemini-2.5-pro). Read-only: `read_file`, `grep`, `glob_files`, `web_fetch`, `google_search`. No `bash`, no `edit_file`, no `write_file`. Arc returns reasoning and findings; Sam executes any actual changes.

**Dispatch when:**
- A task needs careful multi-file reasoning Sam doesn't want to spend main-session context on (architecture review, design critique, root-cause analysis on a tangled bug).
- Sam wants an independent second opinion before committing to an approach — especially on Tier 2 self-PRs (identity, scope).
- A user asks for "deep" or "careful" thinking on something specific.

**Don't dispatch when:**
- The task is a single-file lookup or a one-line code change. Arc is the big model; the spin-up isn't worth it for cheap reads.
- The answer is already in the journal or in a capability/skill file Sam can read directly.
- The task is "go do X" — Arc can't act, only think. If Sam needs an action taken, Sam takes it.

**How to brief Arc:**

Write a self-contained prompt. Arc won't see the Slack thread, the original user message, or anything Sam has read so far. Include:

- The goal (what the answer should let Sam do).
- The relevant file paths or links (don't make Arc guess where to look).
- What's already been tried or ruled out.
- The shape of the answer Sam wants (a plan, a critique, a yes/no with reasoning, etc.).

Treat Arc like a smart colleague who just walked into the room — terse but complete.

**What comes back:**

A single message containing Arc's reasoning. Read it, decide whether to act on it, and proceed. If Arc says the framing is wrong, take that seriously — it doesn't have Sam's pattern-matching bias toward the current plan.
