# Enable the GCP APIs Sam depends on. Declaring these in TF (instead of
# expecting the operator to enable them manually) makes `terraform apply`
# self-sufficient on a fresh project.
#
# disable_on_destroy = false is deliberate: `terraform destroy` should NOT
# turn these APIs off project-wide. That would break anything else on the
# project that happens to use them, and re-enabling can take minutes.
locals {
  required_apis = [
    "aiplatform.googleapis.com",           # Vertex AI (Gemini + Claude partner endpoint)
    "run.googleapis.com",                  # Cloud Run
    "artifactregistry.googleapis.com",     # Container image registry
    "containerscanning.googleapis.com",    # AR vulnerability scanning (disabled by default)
    "secretmanager.googleapis.com",        # Slack/GitHub/Linear secrets
    "iam.googleapis.com",                  # Service accounts
    "iamcredentials.googleapis.com",       # WIF token exchange
    "sts.googleapis.com",                  # WIF token exchange
    "cloudresourcemanager.googleapis.com", # TF reads project metadata via this
    "logging.googleapis.com",              # Cloud Run writes logs here (auto-enabled, explicit for reproducibility)
    "monitoring.googleapis.com",           # Cloud Run metrics (auto-enabled, explicit for reproducibility)
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)
  project  = local.config.gcp_project_id
  service  = each.value

  disable_on_destroy         = false
  disable_dependent_services = false
}
