# Secret Manager — secret resources only. Values are populated out-of-band
# by `infra/scripts/upload-secrets.sh`. Keeping values out of Terraform state
# means contributors can apply this file without ever seeing the secret material.
resource "google_secret_manager_secret" "sam" {
  for_each  = local.secret_ids
  secret_id = each.value

  # user_managed replication pinned to EU regions — `auto {}` would let
  # Google replicate globally, which violates Sam's EU-only data-residency
  # policy. Two replicas for HA; same cost as a single replica.
  replication {
    user_managed {
      replicas {
        location = "europe-west1"
      }
      replicas {
        location = "europe-west4"
      }
    }
  }

  # Block accidental destroy of secrets via TF — secrets are sensitive
  # and Secret Manager has its own soft-delete (30d) so the lifecycle
  # belt-and-suspenders here is intentional.
  deletion_protection = true

  depends_on = [google_project_service.required]
}

# Runtime SA needs read access to every secret it'll mount.
resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each  = google_secret_manager_secret.sam
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
