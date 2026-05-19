# Workload Identity Federation — lets GitHub Actions authenticate as the
# deploy SA without a long-lived JSON key. GitHub presents its OIDC token,
# WIF verifies it (issuer + repo claim), and grants a short-lived GCP token.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "Identity pool for GitHub Actions workflows."

  depends_on = [google_project_service.required]

  # Soft-delete on WIF pools is 30 days; re-creating with the same ID within
  # that window fails. Prevent accidental destroy.
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_iam_workload_identity_pool_provider" "github_actions" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # Pin to *this specific org account* (numeric ID) AND the repo name.
  # The repo-name check alone is spoof-vulnerable: if the Dembrane org were
  # deleted, an attacker could re-register the name and mint tokens. The
  # numeric owner ID can never be reissued, so combining the two closes that.
  # Reference: cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
  attribute_condition = "assertion.repository_owner_id == \"${local.config.github_owner_id}\" && attribute.repository == \"${local.config.github_repo}\""
}

# Bind the GitHub repo principal to the deploy SA.
# This is what makes `permissions: id-token: write` in the workflow → a valid
# GCP token for sam-deploy@.
resource "google_service_account_iam_member" "github_wif_binding" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${local.config.github_repo}"
}
