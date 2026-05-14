---
name: slack-blockers-canvas
description: Maintain the channel canvas "Sam's Blockers" as the live queue of anything Sam is blocked on, with enough links and context that any teammate can pick an item up quickly.
when_to_use: When Sam gets blocked on external input, review, access, deploy/restart, or a policy decision; and during daily reflection to keep blocker status fresh.
---

# Skill: slack-blockers-canvas

Use the Slack canvas **"Sam's Blockers"** attached to the channel as the source of truth for active blockers.
Current canvas URL: <https://dembraneworkspace.slack.com/docs/T05KDCZHH1T/F0B3G5T4HF0|Sam's Blockers>.

Purpose: if a teammate has spare capacity, they should be able to open the canvas, pick one item, and unblock it without back-and-forth.

## Required structure

Group by priority (not by repo, not by owner):

- `## P0 - urgent / blocking active delivery`
- `## P1 - high priority`
- `## P2 - normal priority`
- `## Recently unblocked (last 7 days)` (optional, short)

Do **not** include a "standing constraints" section.

## Blocker entry template

Each blocker is one bullet with nested metadata:

- `<short title>`
  - `needs:` what action/input is required from humans
  - `impact:` what is blocked right now
  - `links:` Slack thread/message + PR/issue/commit links relevant to acting on it
  - `last_seen_ts:` ISO8601 timestamp in local timezone

`last_seen_ts` is required on every active item.

## Content rules

- Each field is one line — `needs:` and `impact:` already carry the consequence framing (per `src/identity.md` "Lead with the consequence"). Don't pile a third narrative line below them; if a blocker needs more context, link a thread under `links:`.
- Prefer forward-looking asks ("need X approved before search v2 ships", "need Y secret added before the cron skill can run") over status that only names the event ("X is pending", "Y is in progress"). The next reader should know what to *do* with this entry without opening links.
- If an item no longer blocks work, move it to "Recently unblocked" or remove it.
- If priority changes, move the item between sections and update `last_seen_ts`.

## Update moments

Update the canvas whenever any of these happens:

- Sam becomes blocked.
- A blocker gets new context or links.
- Priority changes.
- Blocker is resolved.
- Daily reflection runs (end-of-day freshness pass).

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
