# Two service accounts on purpose:
#   - deploy  : assumed by GitHub Actions via WIF; pushes images + deploys.
#   - runtime : attached to the Cloud Run service; what Sam actually runs as.
# Separation keeps the deploy-time blast radius distinct from runtime
# permissions (Sam doesn't need to deploy itself; the deployer doesn't
# need Vertex AI access).

resource "google_service_account" "deploy" {
  account_id   = "sam-deploy"
  display_name = "Sam — GitHub Actions deployer (WIF)"
  description  = "Assumed by GitHub Actions to build images and deploy Cloud Run."
  disabled     = false

  depends_on = [google_project_service.required]

  # SA account_ids stay reserved after deletion; re-creating with the same
  # ID fails for ~30 days. Prevent accidental destroy.
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "runtime" {
  account_id   = "sam-runtime"
  display_name = "Sam — Cloud Run runtime"
  description  = "Identity Sam runs as. Reads secrets, calls Vertex AI."
  disabled     = false

  depends_on = [google_project_service.required]

  lifecycle {
    prevent_destroy = true
  }
}

# ── Deploy SA roles ──────────────────────────────────────────────────────
# Push container images. Scoped to the single AR repo Sam deploys to
# (tighter than project-wide artifactregistry.writer).
resource "google_artifact_registry_repository_iam_member" "deploy_artifact_writer" {
  project    = local.config.gcp_project_id
  location   = google_artifact_registry_repository.sam.location
  repository = google_artifact_registry_repository.sam.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

# Deploy / update Cloud Run services. Project-level binding is acceptable
# here because this is a dedicated project for Sam; tightening to a
# per-service binding would require the service to exist first (chicken/egg
# on bootstrap).
resource "google_project_iam_member" "deploy_run_admin" {
  project = local.config.gcp_project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Allow the deployer to "act as" the runtime SA when deploying.
# Without this, `gcloud run deploy --service-account=sam-runtime@...` fails.
resource "google_service_account_iam_member" "deploy_act_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

# ── Runtime SA roles ─────────────────────────────────────────────────────
# Call Vertex AI (Gemini + Anthropic Claude via partner endpoint).
resource "google_project_iam_member" "runtime_vertex_user" {
  project = local.config.gcp_project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# (Per-secret accessor binding lives in secrets.tf to keep it co-located
#  with the secret resources.)
