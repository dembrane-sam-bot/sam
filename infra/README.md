# Sam GCP infra (Terraform)

One-time GCP setup for Sam's Cloud Run CI/CD. Single config file (`config.yaml`)
drives both Terraform and the CI workflow.

## Mental model

Everything Sam needs lives in **3 places**:

| Where | What | Edited by |
|---|---|---|
| `infra/config.yaml` | All non-secret deployment config (project, regions, Slack IDs, runtime knobs, secret name map) | You, by hand |
| `infra/config.generated.yaml` | TF-derived values (WIF provider path, SA emails, AR repo URL) | Terraform writes it; you commit it once |
| GCP Secret Manager | The 5 actual application secrets (Slack tokens, GitHub PAT, Linear API) | `bash infra/scripts/upload-secrets.sh` |

There are **no GitHub Actions secrets or variables** to set. The workflow
reads both YAMLs directly.

## What this provisions

- **APIs enabled** — Vertex AI, Cloud Run, Artifact Registry, Secret Manager,
  IAM, IAM Credentials, STS, Cloud Resource Manager
- **Artifact Registry** repo `sam` (Docker, regional)
- **Secret Manager** secrets (resources only — values populated separately,
  EU-only replication pinned to `europe-west1` + `europe-west4`)
- **Cloud Storage bucket** `<project_id>-sam-data` — EU multi-region, versioned,
  public-access prevention enforced. Mounted at `/data` in Cloud Run via
  gcsfuse so Sam's journal survives restarts.
- **Two service accounts:**
  - `sam-deploy@…` — assumed by GitHub Actions via WIF
  - `sam-runtime@…` — what Sam runs as inside Cloud Run
- **Workload Identity Federation** — GitHub OIDC → GCP token, scoped to this repo only
- **IAM bindings** — least-privilege per SA

What this does **not** provision (kept manual on purpose):
- The Cloud Run service itself — first deploy creates it; the workflow keeps it updated
- Secret values — populated via `gcloud secrets versions add` so secret material never enters Terraform state

## Persistence model

Sam writes to `/data/journal/*.md` and `/data/sam.lock`. Cloud Run is stateless
by default — `/data` would be wiped on every restart. To keep journal state
across restarts/redeploys, the workflow mounts the GCS bucket as a Cloud Run
volume:

```
--add-volume=name=sam-data,type=cloud-storage,bucket=<bucket>
--add-volume-mount=volume=sam-data,mount-path=/data
--execution-environment=gen2
```

Notes:
- `gen2` is required for cloud-storage volumes (gcsfuse).
- The lock file at `/data/sam.lock` is PID-based, not fcntl-based, so gcsfuse's
  weak POSIX semantics don't break it. Sam's stale-lock cleanup handles deploy
  overlaps gracefully (new container's `os.kill(old_pid, 0)` fails → lock cleared).
- Object versioning is enabled on the bucket, so accidentally `rm`-ing a
  journal file from inside the container is recoverable for 30 days.

## CI security checks

The `ci-checks` job runs on **PRs targeting main, not on pushes to main**. The
assumption: every commit reaching `main` got there via a PR that already passed
checks. This halves CI minutes and avoids re-running expensive scans
(trivy, gitleaks history) on the same code twice.

**Required: branch protection on `main`.** Without it, someone could push
directly to `main` and skip all checks. Set this once in GitHub repo settings:

> Settings → Branches → Add rule → `main`
> ☑ Require a pull request before merging
> ☑ Require status checks to pass before merging → select `ci-checks`
> ☑ Do not allow bypassing the above settings

What runs:

| Check | Stack | What it catches |
|---|---|---|
| `ruff check src/` | Python | Code quality |
| `ruff check src/ --select=S` | Python | Security antipatterns (bandit subset — subprocess shell=True, eval, pickle, weak crypto, etc.) |
| `pip-audit -r src/runtime/requirements.txt` | Python | Known CVEs in pinned deps |
| `docker build` | Docker | Dockerfile + deps resolve cleanly |
| `trivy-action@0.28.0` (HIGH/CRITICAL, ignore-unfixed) | Docker | OS/package CVEs in the built container image |

**Secret scanning is delegated to GitHub.** Since the repo is public, GitHub's native secret scanning runs on every push automatically, surfaces findings in the Security tab, and burns zero CI minutes. We removed the in-CI gitleaks step in favor of it.

To suppress a specific finding:
- ruff S rule: add to `pyproject.toml` `[tool.ruff.lint] ignore = ["Sxxx"]`
- pip-audit CVE: add `--ignore-vuln GHSA-xxxx-xxxx-xxxx` to the step
- trivy CVE: add the CVE ID to `.trivyignore` at repo root

## First-time bootstrap

```bash
# Auth — uses your gcloud ADC
gcloud auth application-default login

# Edit config.yaml first if you need to change defaults (project, region, etc.)
$EDITOR infra/config.yaml

# Apply
cd infra/
terraform init
terraform plan
terraform apply

# Commit the generated config (workflow needs it)
git add infra/config.generated.yaml
git commit -m "infra: capture WIF provider + SA emails from terraform apply"
git push

# Upload your local .env secrets into GCP Secret Manager (one time)
bash scripts/upload-secrets.sh
```

After that, any push to `main` triggers a deploy.

## Changing config later

- **Change a runtime knob** (memory, channel, project, etc.) → edit `config.yaml`,
  commit, push. The workflow picks it up on next deploy.
- **Rotate a secret value** → re-run `bash scripts/upload-secrets.sh` (reads your
  current `.env`).
- **Add a new secret** → add it to `config.yaml > secrets`, `terraform apply`
  to create the resource, then re-run upload-secrets.

## State notes

State is local (`terraform.tfstate` in this directory, gitignored). For a
single-operator setup this is fine; migrate to a GCS backend if more than one
person needs to apply.

`destroy` will delete SAs and AR repo. Secret Manager has 30-day soft-delete
by default; APIs stay enabled (deliberate — turning them off project-wide
breaks anything else using them).
