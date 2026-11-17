output "cloud_run_service_url" {
  description = "Live HTTPS URL for the 2027 Resolutions Atelier Agent & Web UI."
  value       = google_cloud_run_v2_service.resolutions_agent_service.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege service account identity used by the ADK runtime."
  value       = google_service_account.agent_runtime_sa.email
}

output "dlp_deidentify_template_id" {
  description = "Provisioned Cloud DLP template resource name."
  value       = google_data_loss_prevention_deidentify_template.pii_redaction_template.id
}
