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
      runtime/            — the daemon and supporting code (Tier 3 — proposed with higher discipline, see Updating Sam)

    data/                 — Sam's working state (gitignored)
    .claude/              — Sam's Claude Code credentials (gitignored, created by `claude login` inside the container)

## Design Decisions

Why things are built the way they are:

- **Inverted Architecture (Flash workers vs Pro main loop):** Sam's core orchestrator runs on a powerful reasoning model (Gemini Pro) to handle multi-hop planning, Slack context, and complex decisions. However, the actual execution (file reading, git commands, grepping) is farmed out to a fleet of cheap, fast workers running Gemini Flash via `worker` and `parallel_workers` tools. This solves the "stalling problem" of smaller models getting lost on multi-step tasks while avoiding the cost and latency of running every tool call through a huge model. Crucially, these workers have full write access (unlike typical read-only subagents); they are doers, not just readers.
- **Filesystem-as-database:** Sam stores state (like the daily journal and cloned repos) directly as markdown files on disk rather than in a database. This ensures state is naturally version-controllable, deeply human-readable, easily grep-able by parallel workers, and fits cleanly into the LLM context window.
- **Review Gates:** Sam has the ability to self-author PRs modifying its own logic and substrate (Tier 1-3). The architectural boundary for safety relies heavily on GitHub branch protection (a human must approve PRs on `main`). This pushes the burden of safety to the platform rather than relying entirely on prompt engineering.

## Capabilities vs skills

Both live under `src/` as markdown files, but they're loaded differently and serve different purposes:

- **Capability** = identity-shaped, always-relevant. Voice, operational defaults, what Sam can/cannot do, principles. The daemon **hot-loads the full content** of every capability into the system prompt of every session.
- **Skill** = a specific pattern Sam applies when a trigger condition fires. The daemon **emits only the frontmatter** (name + description + `when_to_use` + path) into the system prompt as a catalog; Sam decides to `Read src/skills/<name>.md` when the trigger matches.

Rule of thumb: if Sam should know it on every message, it's a capability. If Sam only needs it sometimes, it's a skill. Don't promote a skill to a capability to save Sam a `Read` — the lazy-load design is deliberate.

### How Sam's Skills differ from Anthropic / MCP
While Anthropic's MCP (Model Context Protocol) and standard agent tools are active, executable programs or servers, Sam's skills are fundamentally just **lazy-loaded Markdown files**. They act as instruction manuals rather than execution environments. 

Compared to Anthropic's flat-file skill structure:
- **Folder-per-skill:** Sam structures skills in directories (`src/skills/<name>/skill.md`), allowing bundled helper scripts, templates, and references alongside the skill.
- **Two-field split vs Pushy descriptions:** Anthropic merges "what this does" and "when to trigger it" into an aggressive `description` field. Sam splits them cleanly into `description` (catalog scanning) and `when_to_use` (boolean trigger conditions), keeping the catalog human-readable.
- **Lightweight parser:** Sam deliberately stripped away the heavy subagent/eval-viewer machinery present in Anthropic's skill creator.

## Routines

Routines are proactive, scheduled tasks (like a cron job) that Sam executes independently without a human prompting via Slack. 

- **Discovery and Scheduling:** A skill becomes a routine simply by adding a `cron: "expression"` field in its YAML frontmatter. On startup, the daemon scans `src/skills/` and spawns async background tasks for these schedules.
- **Execution Mechanism:** When a cron fires, the daemon doesn't simulate a user message; it injects a synthetic `SCHEDULED SKILL invocation` block into a fresh session context and points Sam to read the target skill.
- **Delta Checks & Silence:** Routines are instructed to grep the daily journal for past executions to perform "delta checks" (only processing what changed since last run). By default, if there is no new substance, Sam is instructed to remain silent and not post empty updates to Slack.
- **Primary Example:** The built-in `daily-maintenance` routine (runs at 07:00 local time). It reviews yesterday's journal, reconciles open blockers in Linear, performs skill hygiene, checks for merged PRs, and opens Tier 1 self-PRs to codify newly learned patterns.

## Setup

Sam runs in two places: locally for development on Sameer's laptop, and on GCP Cloud Run for the live service. Both share the same image (`./dockerfile`) and the same source (`src/`); they differ in the host platform and the path to credentials.

### Local development

Prereqs on the host:

- Docker and Docker Compose
- `.env` filled in — Slack tokens (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`), GCP creds for Vertex AI (`GOOGLE_APPLICATION_CREDENTIALS` or `gcloud auth application-default login`), `GITHUB_TOKEN`, `LINEAR_API_KEY`. The schema lives in `.env.example`.

Start, stop, logs:

    docker compose up -d --build
    docker compose down
    docker compose logs -f sam

Local Sam writes its state to `./data/` on the host (mounted into the container at `/data`). The `data/` directory is gitignored.

### Cloud production

Single-instance Cloud Run service in `europe-west1` (EU residency mandate). Sam is **outbound-only** over Slack Socket Mode, but the Cloud Run service contract requires an HTTP server on `$PORT` — `src/runtime/daemon.py` includes a minimal aiohttp `/healthz` server purely to satisfy that contract.

Provisioned by Terraform in `infra/` — see `infra/README.md` for the full deploy guide. Short version of what's in place:

| Layer | What | Source |
|---|---|---|
| GCP project | `dembrane-sameer-cli` in `europe-west1` | `infra/config.yaml` |
| Artifact Registry | `sam` repo (Docker, regional EU) | `infra/artifact_registry.tf` |
| Secret Manager | 5 secrets (Slack, GitHub PAT, Linear) — `user_managed` replication pinned to `europe-west1` + `europe-west4` for EU residency | `infra/secrets.tf` + `infra/scripts/upload-secrets.sh` |
| GCS bucket | `<project>-sam-data`, EU multi-region, versioned. Mounted at `/data` via gcsfuse so Sam's journal survives revisions | `infra/storage.tf` |
| Service accounts | `sam-deploy@` (assumed by GHA via WIF) and `sam-runtime@` (what Sam runs as) | `infra/iam.tf` |
| Workload Identity Federation | GitHub OIDC → GCP token, pinned to `Dembrane/sam` by numeric `repository_owner_id` (immune to org-rename hijack) | `infra/wif.tf` |
| Cloud Run service | gen2 execution environment (required for gcsfuse mount), `min_instances=1`, `max_instances=1`, liveness probe on `/healthz` | created by first deploy, configured by `.github/workflows/ci-deploy.yml` |
| CI/CD | GitHub Actions on `pull_request` + `merge_group` + `push:main`; builds via BuildKit GHA cache, pushes to AR, deploys via `gcloud run deploy` | `.github/workflows/ci-deploy.yml` |

Deploys are automatic on merge to `main`. There is no manual `docker compose` step in production.

### Systemic gates

Every change to `main` passes through a stack of automated and human checks. The full ruleset is enforced by GitHub's "Protect main" branch protection ruleset and the merge queue config. What each gate prevents:

| Gate | What it enforces | Why it exists |
|---|---|---|
| **Required approving review** (`required_approving_review_count: 1`) | A reviewer other than the PR author must approve | Stops Sam — or anyone — from merging their own work unreviewed. GitHub blocks self-approval, so the gate is structural |
| **Required status check: `ci-checks`** | `ruff` lint + bandit-rule security lint + `pip-audit` CVE scan + Docker build + `trivy` HIGH/CRITICAL image scan | Stops broken builds and known-vulnerable dependencies from landing |
| **Dismiss stale reviews on push** | New commits after approval invalidate the approval | Stops the "approve then sneak in a change" pattern |
| **Require last-push approval** | The reviewer must approve the *last* push, not an earlier one | Closes the same gap from a different angle |
| **Non-fast-forward** | History on `main` cannot be rewritten | No force-pushes, no rebase-overwrites |
| **Deletion blocked** | `main` cannot be deleted | Obvious |
| **Merge queue** (squash, all-green) | PRs merge through a queue that re-runs `ci-checks` on the rebased candidate ref before letting the merge complete | Stops two independently-passing PRs from merging into a broken state |
| **Secret scanning** (GitHub native, free on public repos) | Surfaces leaked tokens and known secret patterns in pushed commits | Defense-in-depth for the pre-push self-checks in `src/capabilities/self-maintenance.md` |

Sam's `gh pr merge --auto --squash` queues the merge but does not bypass any gate — the merge fires only when every gate above is satisfied.

## State

Sam's working state lives in `/data` (locally bind-mounted from `./data/`; in Cloud Run, the gcsfuse-mounted GCS bucket):

- `data/journal/*.md` — Sam's per-day append-only journal (legacy `data/journal.md` still present for continuity)
- `data/sam.lock` — single-instance lockfile (skipped on Cloud Run — single-instance is enforced by `min_instances=max_instances=1`)
- `data/cursor.json` — last-seen Slack ts (for catch-up after downtime)
- `data/repos/` — repos Sam has cloned to work on

This directory is gitignored locally and lives in a private GCS bucket in production. Don't commit it.

## Updating Sam

Sam authors PRs against everything in this repo — including `src/runtime/` (Tier 3). The "Updating Sam" loop is:

1. Sam opens a PR with `gh pr create`, then `gh pr merge --auto --squash <PR#>` to activate auto-merge.
2. Sam posts the PR link in Slack with a one-line summary.
3. Sameer (or a designated reviewer) approves. The required-review gate clears.
4. CI runs through the merge queue. If everything stays green, GitHub squash-merges the PR.
5. The merge-to-`main` push fires the deploy workflow, which builds and pushes a new image, then `gcloud run deploy`'s a new Cloud Run revision.
6. Cloud Run's startup probe gates the rollout; the liveness probe (`/healthz` every 60s, 10 failures = ~10 min) catches a hung daemon afterwards.

Tier 3 PRs follow the same flow but with higher discipline — small scope, one concept, explicit justification. See `src/capabilities/self-maintenance.md` for what counts as which tier and the pre-push self-checks Sam runs before pushing.
