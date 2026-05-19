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

## Capabilities vs skills

Both live under `src/` as markdown files, but they're loaded differently and serve different purposes:

- **Capability** = identity-shaped, always-relevant. Voice, operational defaults, what Sam can/cannot do, principles. The daemon **hot-loads the full content** of every capability into the system prompt of every session.
- **Skill** = a specific pattern Sam applies when a trigger condition fires. The daemon **emits only the frontmatter** (name + description + `when_to_use` + path) into the system prompt as a catalog; Sam decides to `Read src/skills/<name>.md` when the trigger matches.

Rule of thumb: if Sam should know it on every message, it's a capability. If Sam only needs it sometimes, it's a skill. Don't promote a skill to a capability to save Sam a `Read` — the lazy-load design is deliberate.

See `src/capabilities/self-maintenance.md` for the skill-frontmatter convention and the flow for proposing changes.

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

