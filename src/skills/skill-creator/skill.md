---
name: skill-creator
description: How to write, structure, and refine Sam's skills so they are reliable and effective.
when_to_use: When creating a new skill, heavily modifying an existing one, or refactoring recurring journal patterns into codified routines.
---

# Skill: skill-creator

This skill codifies the patterns for writing *good* skills for Sam. A skill is a codified pattern that Sam can load on demand. Because skills are lazy-loaded based on their frontmatter, their discoverability and internal structure determine whether future-Sam actually uses them correctly.

## Anatomy of a Skill

A skill lives in `src/skills/<name>/skill.md`. It can be accompanied by optional directories:
- `scripts/` — executable helpers (e.g., shell scripts, python scripts)
- `references/` — supporting documentation for the skill
- `assets/` — static files, templates, or fixtures

### The Frontmatter

The frontmatter is the **only** part of a skill loaded into the default context. It must be perfectly optimized for trigger-matching.

```yaml
---
name: kebab-case-slug
description: Action-oriented summary (What it does + Why).
when_to_use: Concrete, observable trigger conditions.
---
```

**Writing a good `description`:**
- Lead with the action. "How to do X" or "Handles Y."
- Be specific enough to distinguish from general knowledge, but broad enough to cover the whole skill.

**Writing a good `when_to_use`:**
- Focus on the *inbound task shape* or *slack message context*.
- Example: "When the user asks for a PR review..." or "Before sending a message that cites code..."
- Do not just repeat the description. This is the boolean trigger.

### The Body

The body of `skill.md` is loaded *after* Sam decides it's relevant.
- **Explain the WHY:** Don't just list steps. Explain *why* the steps matter. If Sam understands the goal, Sam can adapt when steps fail.
- **Keep it lean:** Target < 300 lines. If it grows > 500 lines, it's doing too much and should be split.
- **Look for repeated work:** If you find yourself telling Sam "don't do X, do Y" repeatedly in the journal, that belongs in the constraints section of a skill.
- **Be concrete:** Use examples, exact file paths, or CLI commands. Vague advice ("be careful with database changes") is ignored. Concrete advice ("Never use `ALTER TABLE` without explicitly verifying the lock wait timeout") is followed.

## Iterating on Skills

Skills are not write-once. They evolve.
1. **Notice friction:** When following a skill leads to a mistake or requires clarifying questions, the skill is incomplete.
2. **Update the skill:** Immediately open a self-PR to fix the skill. Add a constraint, refine a step, or adjust the `when_to_use`.
3. **Use the journal:** If a one-off task feels like it will repeat, note it in the journal. When it repeats *again*, write a skill.

## Routine vs Reactive Skills

- **Reactive:** Triggered by user requests or contextual cues (e.g., `github-pr-workflow`).
- **Routine:** Triggered by the daemon scheduler (e.g., `daily-maintenance`). These must have a `cron:` field in the frontmatter. Routine skills should ideally log to the journal and only post to Slack if there's actual signal.

## Common Anti-patterns
- **The "Everything" Skill:** Bundling disparate workflows into one massive markdown file.
- **Overly-broad `when_to_use`:** "When writing code." (Will trigger too often and dilute the context window).
- **Silent Failures:** Not instructing Sam on how to verify success or what to do if a step fails. Always include verification loops.
