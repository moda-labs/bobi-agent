variable "cloudflare_account_id" {
  description = "Cloudflare account that owns the scratch Worker and KV namespace."
  type        = string
}

variable "worker_name" {
  description = "Unique Worker name. Never use the shipped production default."
  type        = string

  validation {
    condition = (
      length(trimspace(var.worker_name)) > 0 &&
      length(var.worker_name) <= 63 &&
      trimspace(var.worker_name) == var.worker_name &&
      can(regex("^[a-z0-9]$|^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.worker_name)) &&
      var.worker_name != "bobi-events"
    )
    error_message = "worker_name must be a valid lowercase Workers name (max 63 characters) and must not be bobi-events."
  }
}

variable "worker_bundle_path" {
  description = "Absolute path to the index.js produced by wrangler deploy --dry-run."
  type        = string

  validation {
    condition     = fileexists(var.worker_bundle_path)
    error_message = "worker_bundle_path must name an existing Worker bundle."
  }
}

variable "release_version" {
  description = "Version stamp exposed by the Worker's /health route."
  type        = string
}

variable "release_sha" {
  description = "Source SHA exposed by the Worker's /health route."
  type        = string
}

variable "internal_do_secret" {
  description = "Per-deployment secret for Worker-to-Durable-Object authentication."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.internal_do_secret) >= 32
    error_message = "internal_do_secret must contain at least 32 characters."
  }
}

variable "fleet_operator_token" {
  description = "Bearer token for the Worker's fleet read/write API."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.fleet_operator_token) >= 32
    error_message = "fleet_operator_token must contain at least 32 characters."
  }
}
