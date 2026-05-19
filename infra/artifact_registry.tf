resource "google_artifact_registry_repository" "sam" {
  location      = local.config.gcp_region
  repository_id = "sam"
  format        = "DOCKER"
  description   = "Container images for Sam (built by GitHub Actions, deployed to Cloud Run)."

  # Immutable tags prevent silent overwrites of moving tags like :latest
  # — every image is content-addressable by SHA, and an attacker (or a
  # mistake) can't repoint a tag to malicious bits.
  docker_config {
    immutable_tags = true
  }

  # Inherit project-level Container Scanning (enabled via the
  # containerscanning.googleapis.com API). Catches OS/package CVEs in
  # pushed images automatically.
  vulnerability_scanning_config {
    enablement_config = "INHERITED"
  }

  # Cleanup policies — every CI build pushes a new image; without these,
  # storage cost grows forever. Keep the 10 most recent versions of any
  # image; delete untagged blobs older than 7 days.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-untagged-7d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.required]

  # Destroying the repo destroys all images — block accidental tear-down.
  lifecycle {
    prevent_destroy = true
  }
}
