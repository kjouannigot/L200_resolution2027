variable "project_id" {
  description = "Google Cloud Project ID hosting the 2027 Resolutions Atelier Agent."
  type        = string
}

variable "region" {
  description = "Primary GCP region for Cloud Run and Vertex AI."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "resolutions-2027-agent"
}

variable "container_image" {
  description = "Container image URI in Artifact Registry."
  type        = string
  default     = "us-central1-docker.pkg.dev/example-project/agents/resolutions-2027-agent:latest"
}
