# Persistent storage for Sam's state (journal, cursors, lock file).
#
# Cloud Run containers are stateless by default — /data is ephemeral scratch
# that's wiped on every restart/redeploy. Sam writes to /data/journal/*.md
# (daemon.py) so we mount a GCS bucket there via Cloud Run's native gcsfuse
# support (gen2 execution environment).
#
# Bucket name is derived from project_id to satisfy GCS's global-unique-names
# constraint without requiring the operator to think one up.
locals {
  data_bucket_name = "${local.config.gcp_project_id}-sam-data"
}

resource "google_storage_bucket" "sam_data" {
  name          = local.data_bucket_name
  location      = "EU" # multi-region, dual-region EU storage (HA, still EU-only)
  storage_class = "STANDARD"

  # Modern practice — disables legacy ACLs, all access via IAM.
  uniform_bucket_level_access = true

  # Bucket is mounted only inside Sam's container; never expose to the internet.
  public_access_prevention = "enforced"

  # Recover from accidental writes/deletes for 30 days (object versions).
  # The lifecycle_rules below bound storage growth so versions don't accrue
  # forever.
  versioning {
    enabled = true
  }

  # Delete noncurrent (overwritten) versions once 10 newer ones exist —
  # journal files get rewritten on every Sam action; without this, every
  # rewrite leaves a noncurrent version forever.
  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }

  # Abort interrupted multipart uploads after 1 day so they don't bill.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  # Default is false; making explicit so `terraform destroy` requires
  # emptying the bucket first (catches the "oh no my journal" foot-gun).
  force_destroy = false

  depends_on = [google_project_service.required]

  # Destroying the bucket destroys all journal history — block accidental destroy.
  lifecycle {
    prevent_destroy = true
  }
}

# Runtime SA needs full object access (read + write + delete) — Sam rotates
# journal files. objectUser is the modern, fine-grained role (objectAdmin
# additionally grants setIamPolicy, which a workload should not need).
resource "google_storage_bucket_iam_member" "runtime_data_access" {
  bucket = google_storage_bucket.sam_data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runtime.email}"
}
