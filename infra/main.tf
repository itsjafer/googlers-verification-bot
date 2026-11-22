locals {
  enabled_apis = toset([
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "billingbudgets.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each           = local.enabled_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Least-privilege runtime service account for Cloud Run + Cloud Tasks OIDC
resource "google_service_account" "bot" {
  account_id   = "verify-bot-sa"
  display_name = "Googlers Discord Verification Bot"
  project      = var.project_id
}

resource "google_project_iam_member" "bot_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.bot.email}"
}

resource "google_project_iam_member" "bot_tasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.bot.email}"
}

resource "google_service_account_iam_member" "bot_self_act_as" {
  service_account_id = google_service_account.bot.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.bot.email}"
}

# Artifact Registry repository with aggressive cleanup policy to stay inside 0.5 GB Always Free tier
resource "google_artifact_registry_repository" "bot" {
  location      = var.region
  repository_id = "verify-bot"
  description   = "Googlers Discord verification bot container images"
  format        = "DOCKER"
  project       = var.project_id

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }

  cleanup_policies {
    id     = "keep-2-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  depends_on = [google_project_service.apis]
}

# Cloud Tasks queue (Always Free tier: 1M operations/month)
resource "google_cloud_tasks_queue" "verification" {
  name     = "verification-queue"
  location = var.region
  project  = var.project_id

  rate_limits {
    max_dispatches_per_second = 5
    max_concurrent_dispatches = 3
  }

  retry_config {
    max_attempts = 3
    min_backoff  = "5s"
    max_backoff  = "30s"
  }

  depends_on = [google_project_service.apis]
}

# Single packed JSON secret container (values are managed out-of-band so secrets never enter Terraform state)
resource "google_secret_manager_secret" "bot_secrets" {
  secret_id = "bot-secrets"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "bot_secret_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.bot_secrets.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bot.email}"
}

# Cloud Run v2 service (scale-to-zero, Always Free tier: 2M requests/month)
resource "google_cloud_run_v2_service" "bot" {
  name                = var.service_name
  location            = var.region
  project             = var.project_id
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.bot.email
    timeout                          = "60s"
    max_instance_request_concurrency = 20

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "VERTEX_LOCATION"
        value = var.vertex_location
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "CLOUD_TASKS_LOCATION"
        value = var.region
      }
      env {
        name  = "CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.verification.name
      }
      env {
        name  = "TASK_SERVICE_ACCOUNT"
        value = google_service_account.bot.email
      }
      env {
        name  = "SERVICE_URL"
        value = "https://${var.service_name}-${var.project_number}.${var.region}.run.app"
      }
      env {
        name  = "INTERNAL_FORM_URL_TEMPLATE"
        value = var.internal_form_url_template
      }
      env {
        name = "APP_SECRETS_JSON"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bot_secrets.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    # Image tag/digest is updated continuously by GitHub Actions CD on push to main
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      scaling,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.bot_secret_accessor,
  ]
}

# Public HTTP access is required for Discord Interactions Endpoint (Ed25519 signature-verified in app)
# /tasks/* verifies Google-signed Cloud Tasks OIDC JWTs; /internal-verify verifies INTERNAL_FORM_SECRET
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.bot.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Monthly $1 billing budget alert scoped strictly to this project
resource "google_billing_budget" "monthly_alert" {
  billing_account = var.billing_account_id
  display_name    = "Googlers Discord Verify - USD ${var.monthly_budget_usd}/mo Alert"

  budget_filter {
    projects        = ["projects/${var.project_number}"]
    calendar_period = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = var.monthly_budget_usd
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.apis]
}
