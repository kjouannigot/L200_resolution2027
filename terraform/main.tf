provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = [
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "discoveryengine.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
}

resource "google_project_service" "enabled_apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Least-privilege dedicated runtime Service Account
resource "google_service_account" "agent_runtime_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "2027 Resolutions Atelier ADK Runtime Service Account"
  depends_on   = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "agent_sa_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/datastore.user",
    "roles/dlp.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

# Secure Secret Manager containers (secret versions populated out-of-band, never in IaC state)
resource "google_secret_manager_secret" "hitl_signing_secret" {
  secret_id = "resolutions-2027-hitl-signing-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "hitl_secret_accessor" {
  secret_id = google_secret_manager_secret.hitl_signing_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

resource "google_secret_manager_secret" "db_connection_secret" {
  secret_id = "resolutions-2027-session-db-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "db_secret_accessor" {
  secret_id = google_secret_manager_secret.db_connection_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

# Persistent Firestore Native Database & Vector Index for Guest Memory Retrieval
resource "google_firestore_database" "guest_memory_db" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.enabled_apis]
}

resource "google_firestore_index" "memory_vector_index" {
  project    = var.project_id
  database   = google_firestore_database.guest_memory_db.name
  collection = "resolutions_2027_memories"

  fields {
    field_path = "guest_key"
    order      = "ASCENDING"
  }

  fields {
    field_path = "embedding"
    vector_config {
      dimension = 768
      flat {}
    }
  }
}

# Cloud DLP De-identification Template for PII Scrubbing
resource "google_data_loss_prevention_deidentify_template" "pii_redaction_template" {
  parent       = "projects/${var.project_id}/locations/global"
  display_name = "Resolutions 2027 Agent PII Redaction"
  description  = "Redacts emails, phone numbers, SSNs, and credit cards from logs & memory."

  deidentify_config {
    info_type_transformations {
      transformations {
        info_types { name = "EMAIL_ADDRESS" }
        info_types { name = "PHONE_NUMBER" }
        info_types { name = "CREDIT_CARD_NUMBER" }
        info_types { name = "US_SOCIAL_SECURITY_NUMBER" }
        primitive_transformation {
          replace_with_info_type_config {}
        }
      }
    }
  }
  depends_on = [google_project_service.enabled_apis]
}

# Artifact Registry Repository for Agent Containers
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "agents"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# Cloud Run v2 Service hosting ADK Agent + Interactive Web UI
resource "google_cloud_run_v2_service" "resolutions_agent_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.agent_runtime_sa.email

    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }
      env {
        name  = "ENABLE_FIRESTORE_SYNC"
        value = "true"
      }
      env {
        name = "HITL_EPHEMERAL_SIGNING_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.hitl_signing_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.hitl_secret_accessor,
    google_firestore_database.guest_memory_db,
  ]
}
