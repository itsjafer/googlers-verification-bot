variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "project_number" {
  description = "GCP project number (used for billing budget filter)"
  type        = string
}

variable "billing_account_id" {
  description = "GCP billing account ID for the monthly budget alert"
  type        = string
}

variable "region" {
  description = "Primary GCP region for Cloud Run, Cloud Tasks, and Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "vertex_location" {
  description = "Vertex AI endpoint location"
  type        = string
  default     = "global"
}

variable "gemini_model" {
  description = "Vertex AI Gemini model ID"
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "googlers-verify-bot"
}

variable "container_image" {
  description = "Container image URI for Cloud Run"
  type        = string
}

variable "internal_form_url_template" {
  description = "Pre-filled internal @google.com Google Form URL containing {user_id}"
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "GitHub repository in owner/repo format allowed to deploy via Workload Identity Federation"
  type        = string
  default     = "itsjafer/googlers-verification-bot"
}

variable "monthly_budget_usd" {
  description = "Monthly GCP billing budget alert threshold in USD"
  type        = string
  default     = "1"
}
