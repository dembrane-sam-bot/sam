# All non-secret config is read from config.yaml — one editable file, not split
# across TF variables, workflow YAML, and GitHub Actions vars/secrets.
locals {
  config     = yamldecode(file("${path.module}/config.yaml"))
  secret_ids = toset(values(local.config.secrets))
}

# Auth picked up from the operator's gcloud ADC
# (run `gcloud auth application-default login` before `terraform apply`).
provider "google" {
  project = local.config.gcp_project_id
  region  = local.config.gcp_region
}
