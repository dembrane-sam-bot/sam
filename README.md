# Sam

An engineering coworker.

This repo is Sam — identity, scope, capabilities, skills, and runtime. Read `src/identity.md` to understand who Sam is. Read files in `src/capabilities/` to understand the shape of Sam's work. Skills under `src/skills/` are the specific patterns Sam reaches for when triggered.

## Principles 
![Sam](https://github.com/user-attachments/assets/755678fd-4a6f-4b2d-be7b-0992ee4421a4)

- multiplayer, self maintaining claude code
- communication channel
- project management
- sandbox as a place of work
- loop triggered by events

## Open questions
- use calendar over cron? set reminders, listen to webhooks on cal events? more visibility and also we can log times in the cal when sam works and on what. important events are tracked, sam can kick off a smoke test 1d before, see if all reqs are met.
- 

## Layout

    src/
      identity.md         — who Sam is
      scope.md            — what Sam works on
      capabilities/*.md   — what Sam can always rely on knowing (hot-loaded into every session)
      skills/*.md         — specific patterns, loaded on demand (catalog in system prompt, body via `Read`)
      runtime/            — the daemon and supporting code (Tier 3 — not Sam-editable)

    data/                 — Sam's working state (gitignored)
    .claude/              — Sam's Claude Code credentials (gitignored, created by `claude login` inside the container)

## Capabilities vs skills

Both live under `src/` as markdown files, but they're loaded differently and serve different purposes:

- **Capability** = identity-shaped, always-relevant. Voice, operational defaults, what Sam can/cannot do, principles. The daemon **hot-loads the full content** of every capability into the system prompt of every session.
- **Skill** = a specific pattern Sam applies when a trigger condition fires. The daemon **emits only the frontmatter** (name + description + `when_to_use` + path) into the system prompt as a catalog; Sam decides to `Read src/skills/<name>.md` when the trigger matches.

Rule of thumb: if Sam should know it on every message, it's a capability. If Sam only needs it sometimes, it's a skill. Don't promote a skill to a capability to save Sam a `Read` — the lazy-load design is deliberate.

See `src/capabilities/self-maintenance.md` for the skill-frontmatter convention and the flow for proposing changes.

## Running Sam

Prereqs on the host:

- Docker and Docker Compose
- `.env` filled in (see `.env.example`)

First-run auth (inside the container):

    docker compose up -d --build
    docker exec -it sam claude login

After login, Sam's credentials persist in `./.claude/` across restarts. Re-login isn't required unless tokens are revoked.

Start:

    docker compose up -d

Stop:

    docker compose down

Logs:

    docker compose logs -f sam

## State

Sam's working state lives in `./data/`:

- `data/journal.md` — Sam's append-only journal
- `data/sam.lock` — single-instance lockfile
- `data/cursor.json` — last-seen Slack ts (for catch-up after downtime)
- `data/repos/` — repos Sam has cloned to work on

This directory is gitignored. Don't commit it.

## Updating Sam

Sam can propose changes to anything in `src/` *except* `src/runtime/` via PR. See `src/capabilities/self-maintenance.md`. The runtime — the body Sam runs in — is Tier 3 and is written by Sameer.

Deploys happen manually:

    git pull
    docker compose up -d --build

