terraform {
  required_version = ">= 1.5"

  # Explicit local backend — single-operator setup. If we ever need multi-user
  # apply, migrate to `backend "gcs"` with state in a versioned bucket.
  backend "local" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }
}
