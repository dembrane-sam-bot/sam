# Most former outputs are now in config.generated.yaml, which TF writes
# automatically and you commit to git. These remaining outputs are just for
# operator convenience after apply.

output "next_steps" {
  description = "What to do after `terraform apply`."
  value = <<-EOT

    Done. Now:

      1. Commit the generated config:
         git add infra/config.generated.yaml
         git commit -m "infra: capture WIF provider + SA emails from terraform apply"
         git push

      2. Populate the 5 secret values from your local .env:
         bash infra/scripts/upload-secrets.sh

      3. Push or merge to main — the workflow will deploy.

    No GitHub Actions secrets or variables to set. config.yaml + config.generated.yaml
    are the only places the workflow reads from.
  EOT
}
