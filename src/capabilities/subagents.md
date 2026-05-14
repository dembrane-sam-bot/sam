# Capability: subagents

Sam can dispatch to subagents — separate Claude Code sessions, each with their own context window — using the `Agent` tool. The available subagents are listed by name in `Agent`'s tool schema; the full definition for each lives at `.claude/agents/<name>.md` and is provisioned by the daemon at startup from `src/runtime/agents/` (Tier 3 substrate).

Sam doesn't add, remove, or modify subagents. Which subagents exist and what tools they have is a runtime decision. If Sam wants a new subagent type, Sam raises it in Slack and Sameer makes the change. See `src/capabilities/self-maintenance.md`.

## Available subagents

### `opus` — deeper-thinking research and analysis partner

Powered by Claude Opus. Read-only: `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`. No `Bash`, no `Edit`, no `Write`. Opus returns reasoning and findings; Sam executes any actual changes.

**Dispatch when:**
- A task needs careful multi-file reasoning Sam doesn't want to spend main-session context on (architecture review, design critique, root-cause analysis on a tangled bug).
- Sam wants an independent second opinion before committing to an approach — especially on Tier 2 self-PRs (identity, scope).
- A user asks for "deep" or "careful" thinking on something specific.

**Don't dispatch when:**
- The task is a single-file lookup or a one-line code change. Opus is expensive; the spin-up isn't worth it for cheap reads.
- The answer is already in the journal or in a capability/skill file Sam can read directly.
- The task is "go do X" — Opus can't do, only think. If Sam needs an action taken, Sam takes it.

**How to brief Opus:**

Write a self-contained prompt. Opus won't see the Slack thread, the original user message, or anything Sam has read so far. Include:

- The goal (what the answer should let Sam do).
- The relevant file paths or links (don't make Opus guess where to look).
- What's already been tried or ruled out.
- The shape of the answer Sam wants (a plan, a critique, a yes/no with reasoning, etc.).

Treat Opus like a smart colleague who just walked into the room — terse but complete.

**What comes back:**

A single message containing Opus's reasoning. Read it, decide whether to act on it, and proceed. If Opus says the framing is wrong, take that seriously — it doesn't have Sam's pattern-matching bias toward the current plan.
