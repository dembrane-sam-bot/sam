---
name: slack-blockers-canvas
description: Maintain the channel canvas "Sam's Blockers" as the live queue of anything Sam is blocked on, with enough links and context that any teammate can pick an item up quickly.
when_to_use: At the moment Sam gets blocked on external input/review/access/deploy/decision (reactive update), at the moment a blocker resolves (reactive update), and during the daily-maintenance 22:00 routine which does the freshness pass (see `src/skills/daily-maintenance.md` §4).
---

# Skill: slack-blockers-canvas

Use the Slack canvas **"Sam's Blockers"** attached to the channel as the source of truth for active blockers.
Current canvas URL: <https://dembraneworkspace.slack.com/docs/T05KDCZHH1T/F0B3G5T4HF0|Sam's Blockers>.

Purpose: if a teammate has spare capacity, they should be able to open the canvas, pick one item, and unblock it without back-and-forth. The format is a kanban-by-bottleneck — readers scan by "what's needed to unblock this," not by priority — because the columns sort the items by who or what can actually move them.

## Required structure

Top of the canvas: a single freshness line, italicised:

```
*as of <YYYY-MM-DD HH:MM TZ>*
```

Then three section headings, each one a kanban column. Use these exact strings:

- `## 🟥 Waiting on humans`
- `## 🟨 Waiting on info / access`
- `## 🟩 Recently unblocked (last 7 days)`

If a column has no items, write `_(none)_` underneath the heading. Don't delete the column.

Do **not** include a "P0 / P1 / P2" top-level grouping. Priority is folded into an inline `[P0]` / `[P1]` / `[P2]` tag on each item. Do **not** include a "standing constraints" section.

## Item template

One checkbox per item, single line:

```
- [ ] **<consequence-first title>** [<priority tag>] — <what's needed> _<short rationale or impact>_ ([link1](url), [link2](url))
```

Each piece:

- `[ ]` — unchecked while blocked. Use `[x]` only in the "Recently unblocked" section.
- `**<consequence-first title>**` — the forward-looking state, not the event. "Prod alerting overhaul live" beats "Review monitoring PRs." (See `src/identity.md` "Lead with the consequence.")
- `[Pn]` — priority tag: `[P0]` urgent / blocking active delivery, `[P1]` high, `[P2]` normal. One tag per item.
- The tail clause names what's needed to move the item, with a short rationale or impact in italics if it adds context. Links inline at the end.

Examples:

```
- [ ] **Prod alerting overhaul live** [P1] — Sameer review+merge #22 → #23 → #24. _Until then #prod-alerts stays noisy._ ([thread](url))
- [ ] **#alerts-non-prod webhook sealed** [P1] — secret added to monitoring-secrets SealedSecret. _ECHO-814 stuck in Triage._ ([ECHO-814](url))
```

For "Recently unblocked":

```
- [x] canvases:write granted (2026-05-14)
- [x] Daemon on commit d6cb5c1 (2026-05-14)
```

## Content rules

- One line per item. If a blocker needs more context than fits, link a Slack thread or issue — don't pile narrative lines.
- Lead with the consequence in the bold title. The tail names the action; the italic rationale (optional) names the impact.
- Choose the right column by asking *what is the bottleneck*: a person who needs to act (🟥), or a piece of information / access / data Sam doesn't have (🟨). If it's both, put it in 🟥 — humans unblock faster than chasing missing info usually does.
- If priority changes, just edit the `[Pn]` tag and refresh the canvas-level `*as of …*` timestamp.
- If an item is resolved, move it from 🟥 or 🟨 to 🟩 and flip the checkbox to `[x]`. Items in 🟩 older than 7 days get removed.

## When to update

Two update modes — reactive (in-the-moment) and the daily freshness pass.

**Reactive — at the moment of change.** Sam writes to the canvas as soon as the state shifts:

- Sam becomes blocked on something new → add the item to 🟥 or 🟨.
- A blocker gets new context, links, or a priority shift → edit the existing item.
- A blocker is resolved → move to 🟩 and flip the checkbox to `[x]`.

These happen inline during the session that surfaced the change. Don't queue them for later.

**Freshness pass — daily-maintenance.** The 22:00 daily-maintenance routine is where Sam reconciles teammate edits, prunes stale 🟩 entries (older than 7 days), refreshes the top-of-canvas `*as of …*` timestamp, and verifies every active item still describes a real blocker. See `src/skills/daily-maintenance.md` §4 — that skill is responsible for triggering this pass; this skill defines *how* the pass writes to the canvas.

Sam does not subscribe to canvas-edit events or poll the canvas between routines. Teammate edits are caught by the 22:00 pass (and by any session Sam happens to run that touches blockers). If a teammate's edit needs an immediate response, they'll @mention Sam — the normal Slack flow already covers it.

Always refresh the top-of-canvas `*as of …*` timestamp when Sam edits the canvas. One canvas-level timestamp replaces any per-item `last_seen_ts`.

## Handling teammate edits during the freshness pass

When daily-maintenance reads the canvas and finds teammate edits Sam hasn't seen yet:

1. **If a teammate added a blocker:** leave their wording unless it's actively misleading. Acknowledge in `SAM_CHANNEL` only if Sam has useful context to add (a related thread, a known unblocker).
2. **If a teammate marked something resolved** but didn't move it: move it to 🟩 with the resolution date.
3. **If a teammate edited an item Sam wrote** and the edit is correct or stylistic: leave it. If the edit is wrong (e.g. misstates what's needed), post in `SAM_CHANNEL` naming the disagreement *before* reverting — never silently overwrite a teammate's edit.

Silence is the default. Only post when a teammate's edit needs acknowledgement, correction, or coordination.

## Links in blocker entries

The `links:` field on every active blocker entry MUST include the direct URL to the actionable resource — the PR, issue, or Slack thread a teammate would open to act on it. A bare PR number (`echo #572`) is not enough; include the full GitHub URL.

Example: `links: https://github.com/Dembrane/echo/pull/572`

## Writing to the canvas (API patterns)

**Single-section write — use `insert_at_start`** when the canvas is empty or you want to replace everything:
```
POST canvases.edit
{"canvas_id":"<id>","changes":[{"operation":"insert_at_start","document_content":{"type":"markdown","markdown":"<full content>"}}]}
```

**canvases.edit only allows 1 change per call.** Batch deletes require looping.

**Canvas sections accumulate.** Each `replace` on a section auto-splits the new markdown into multiple sections; old sections are NOT automatically removed. To avoid duplicate headings and stale content, follow this pattern on every update:
1. Look up all current sections using `canvases.sections.lookup` with several search terms.
2. Delete every found section one by one (loop, 1 change per call). The last section will get `cant_delete_last_section` — keep it.
3. Replace that last section, or if the canvas is empty, use `insert_at_start`.

**Verify after write** by searching for a known string (e.g., a linear ID or GitHub URL) — `canvases.sections.lookup` returning that string confirms the content is there.

## If canvas write fails

If Slack API returns missing scope (for example `canvases:write`):

1. Post one concise Slack thread update naming the missing scope(s).
2. Include what Sam needs from the operator (app scope update + reinstall).
3. Keep a temporary blocker note in the journal's daily synthesis until canvas access is restored.

Retry canvas sync once access is restored.
