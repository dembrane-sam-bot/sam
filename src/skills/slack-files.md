---
name: slack-files
description: How Sam downloads and reads files attached to Slack messages (images, PDFs, snippets) using the bot token.
when_to_use: When the inbound Slack message includes an "Attached files" section and the file contents matter for the response.
---

# Skill: slack-files

When a Slack message has attachments, the daemon surfaces them in an `## Attached files` section of the initial user message: name, mimetype, size, and `url_private`. The file itself is not downloaded. Sam decides whether to fetch.

## When to fetch

Fetch only if the content is relevant to the task. The first move is to read the message text and decide:

- The user is asking Sam to look at the screenshot → fetch
- The user is sharing a PDF and asking a question about it → fetch
- The user uploaded a file but the message is about something else → don't fetch
- The file is a 50 MB log archive and the question is "did this finish?" → don't fetch the file, look at the message text or the user's history

Sam doesn't need to fetch to know the file is there. Mentioning what was attached without opening it is sometimes the right move.

## Fetching

The download URL is auth-gated. Send the bot token in the Authorization header:

```bash
mkdir -p /tmp/sam/$SLACK_FILE_ID && \
curl -sS -L \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "$SLACK_FILE_URL" \
  -o /tmp/sam/$SLACK_FILE_ID/$SLACK_FILE_NAME
```

Use `/tmp/sam/<file_id>/<name>` so multiple files don't collide. `/tmp` is ephemeral inside the container, so cleanup is automatic on session end. Don't write to `/data/` — that's persistent state and files don't belong there.

If the response is HTML or a login page, the token didn't authenticate. Don't retry blindly; say in Slack that the download failed and stop.

## Reading by mimetype

- `image/*` (PNG, JPG, GIF, WebP) — `Read /tmp/sam/<id>/<name>`. Claude reads images natively.
- `application/pdf` — `Read` works. For large PDFs (more than 10 pages), pass the `pages` argument (e.g., `pages: "1-5"`); otherwise the read fails.
- `text/*`, `application/json`, source code (`.py`, `.ts`, etc.) — `Read` for the full file. For very large logs, `head`/`tail`/`grep` via `Bash` is more efficient.
- `application/zip`, `application/x-tar`, `.gz` — extract with `Bash` first, then `Read` the extracted content. Never assume the archive is small.
- Anything else — try `file /tmp/sam/<id>/<name>` to identify, then decide. If it's a format Sam doesn't handle, say so in Slack rather than guess.

## Size guardrail

Each attached file has a `size` field in bytes. Before fetching, check it. Under 5 MB: fetch freely. 5–50 MB: fetch only if the message clearly needs it. Over 50 MB: don't fetch unless the user explicitly asks; respond in Slack and ask whether to proceed.

## What Sam does not do

- Fetch every attachment because they're there. Lazy is the default.
- Share `url_private` outside Slack. The URL contains an auth-bearer hint and isn't meant to be passed around.
- Re-upload the file content into Slack. The file is already there; refer to it by name.
- Save fetched files into `/data/`. That dir is persistent and meant for journal/repos/cursor only.
- Retry on auth failure. If the bot token doesn't authenticate, something is wrong above Sam's pay grade.

## Multiple files

The `## Attached files` section may list more than one. Treat them independently — fetch only the ones that matter for the response. Sam doesn't need to acknowledge every attachment.
