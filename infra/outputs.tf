output "cloud_run_url" {
  description = "Live Cloud Run service URL"
  value       = google_cloud_run_v2_service.bot.uri
}

output "interactions_endpoint_url" {
  description = "Discord Interactions Endpoint URL"
  value       = "${google_cloud_run_v2_service.bot.uri}/interactions"
}

output "internal_verify_endpoint_url" {
  description = "Internal @google.com Google Form webhook endpoint"
  value       = "${google_cloud_run_v2_service.bot.uri}/internal-verify"
}

output "artifact_registry_repo" {
  description = "Docker image repository path"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.bot.repository_id}"
}

output "wif_provider_name" {
  description = "Workload Identity Provider resource name for GitHub Actions"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "github_deployer_service_account" {
  description = "Service account email used by GitHub Actions CD"
  value       = google_service_account.github_deployer.email
}
