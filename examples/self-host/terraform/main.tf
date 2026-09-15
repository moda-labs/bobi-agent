resource "cloudflare_workers_kv_namespace" "events" {
  account_id = var.cloudflare_account_id
  title      = "${var.worker_name}-events"
}

resource "cloudflare_worker" "events" {
  account_id = var.cloudflare_account_id
  name       = var.worker_name

  observability = {
    enabled = true
  }

  subdomain = {
    enabled          = true
    previews_enabled = false
  }
}

// Cloudflare applies a Durable Object migration only when its version is
// deployed, and rejects a version that introduces and binds the class at once.
// Bootstrap with the migration-only version, deploy it, then publish the
// serving version with the binding.
resource "cloudflare_worker_version" "migration" {
  account_id = var.cloudflare_account_id
  worker_id  = cloudflare_worker.events.id

  main_module        = "index.js"
  compatibility_date = "2025-05-26"
  compatibility_flags = [
    "nodejs_compat",
  ]

  modules = [{
    name         = "index.js"
    content_file = var.worker_bundle_path
    content_type = "application/javascript+module"
  }]

  bindings = [
    {
      name         = "EVENTS"
      type         = "kv_namespace"
      namespace_id = cloudflare_workers_kv_namespace.events.id
    },
    {
      name = "CF_VERSION_METADATA"
      type = "version_metadata"
    },
    {
      name = "BOBI_RELEASE_VERSION"
      type = "plain_text"
      text = var.release_version
    },
    {
      name = "BOBI_RELEASE_SHA"
      type = "plain_text"
      text = var.release_sha
    },
    {
      name = "INTERNAL_DO_SECRET"
      type = "secret_text"
      text = var.internal_do_secret
    },
    {
      name = "FLEET_OPERATOR_TOKEN"
      type = "secret_text"
      text = var.fleet_operator_token
    },
  ]

  migrations = {
    new_tag            = "v1"
    new_sqlite_classes = ["DeploymentSession"]
  }
}

resource "cloudflare_workers_deployment" "migration" {
  account_id  = var.cloudflare_account_id
  script_name = cloudflare_worker.events.name
  strategy    = "percentage"

  versions = [{
    version_id = cloudflare_worker_version.migration.id
    percentage = 100
  }]
}

resource "cloudflare_worker_version" "events" {
  account_id = var.cloudflare_account_id
  worker_id  = cloudflare_worker.events.id

  main_module        = "index.js"
  compatibility_date = "2025-05-26"
  compatibility_flags = [
    "nodejs_compat",
  ]

  modules = [{
    name         = "index.js"
    content_file = var.worker_bundle_path
    content_type = "application/javascript+module"
  }]

  bindings = [
    {
      name         = "EVENTS"
      type         = "kv_namespace"
      namespace_id = cloudflare_workers_kv_namespace.events.id
    },
    {
      name       = "DEPLOYMENT_SESSION"
      type       = "durable_object_namespace"
      class_name = "DeploymentSession"
    },
    {
      name = "CF_VERSION_METADATA"
      type = "version_metadata"
    },
    {
      name = "BOBI_RELEASE_VERSION"
      type = "plain_text"
      text = var.release_version
    },
    {
      name = "BOBI_RELEASE_SHA"
      type = "plain_text"
      text = var.release_sha
    },
    {
      name = "INTERNAL_DO_SECRET"
      type = "secret_text"
      text = var.internal_do_secret
    },
    {
      name = "FLEET_OPERATOR_TOKEN"
      type = "secret_text"
      text = var.fleet_operator_token
    },
  ]

  depends_on = [cloudflare_workers_deployment.migration]
}

resource "cloudflare_workers_deployment" "events" {
  account_id  = var.cloudflare_account_id
  script_name = cloudflare_worker.events.name
  strategy    = "percentage"

  versions = [{
    version_id = cloudflare_worker_version.events.id
    percentage = 100
  }]
}
