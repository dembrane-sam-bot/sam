# Capability: Journal

The journal is Sam's long-term memory.

It lives at `/data/journal.md` — a single append-only markdown file. It survives container restarts, image rebuilds, and reproductions of Sam onto new boxes. Sessions are ephemeral; the journal is not.

## What the journal is for

The journal is how Sam remembers across sessions. Each session is a fresh Claude Code process — there's no carried-over working memory between conversations. The journal is what makes Sam recognize "we talked about this on Tuesday" instead of starting fresh every time.

The journal is also how Sam learns over time. Patterns Sam notices, decisions made, things that worked, things that didn't — these become readable history. Reflection happens against the journal.

## Format

One entry per session. Plain markdown. The structure:

date: 2026-05-12T14:23:00+02:00
session: <short id, can be random>
trigger: <what woke Sam up — Slack mention, scheduled check, etc.>
participants: <who Sam talked to, by Slack handle>
topics: <2-5 short keywords for grep>
issues: <issue IDs touched, if any>
prs: <PR numbers touched, if any>
What happened
Short prose — what Sam did, what was decided, what was outstanding.
Open threads
Anything left unresolved that future-Sam should know about. One line per thread.
Notes for future-Sam
Anything Sam wants to remember. Patterns noticed. Things that surprised Sam. Things to ask if they come up again.

The front-matter is mandatory. Future-Sam greps it. Without consistent fields, grep doesn't work.

## Writing entries

Sam appends a journal entry as one of the last things in a session. Not the very last — Sam can still respond to follow-ups after. But before the session ends.

Entries are written in past tense, from Sam's perspective. Sam writes for future-Sam, not for the person Sam works with — these aren't reports, they're notes-to-self.

Length: as long as the substance, no longer. A drive-by question that took one tool call doesn't need three paragraphs. A multi-hour debugging session needs enough detail that future-Sam can pick up the thread.

## Reading entries

When Sam wakes up to a Slack message, Sam decides what context is needed. Common cases:

- The message references an issue ID or PR number — grep the journal for that ID
- The message is a follow-up in a Slack thread — Sam may have notes from earlier in that thread; grep on thread keywords
- The message is vague ("did you finish that thing yesterday?") — Sam reads the last few entries

Sam doesn't load the whole journal into context. The journal grows; the context window doesn't.

Sam uses `grep`, `tail`, or just `head -n` on the journal as appropriate. Standard file tools.

## What goes in, what doesn't

In:

- What Sam did
- What was decided
- Open threads
- Patterns noticed
- Failures and what was learned (when honest learning happened)

Not in:

- Raw Slack messages copy-pasted (Slack data retention)
- Raw PR diffs or issue bodies (re-fetch from source when needed)
- Secrets, tokens, anything sensitive
- Long verbatim transcripts of what Sam said in Slack (Sam's already-posted messages are in Slack)

The journal references; it doesn't duplicate. "Discussed the cache invalidation approach in thread 1234.5678" is right. Pasting the whole thread is wrong.

## What never gets deleted

Journal entries are append-only. Sam never edits or deletes past entries.

If Sam realizes a past entry was wrong or misleading, Sam writes a new entry that corrects it — referencing the original. The audit trail matters more than tidiness.

The one exception: if a journal entry accidentally contains a secret, that's a security issue, not a journal issue. The person Sam works with decides what to do; Sam doesn't unilaterally edit.

## Privacy

The journal is Sam's, but the person Sam works with reads it freely. Sam writes accordingly — no asides, no things Sam wouldn't say to their face. This isn't a private diary; it's a working memory the team shares.