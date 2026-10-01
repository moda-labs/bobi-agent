output "worker_url" {
  description = "Public workers.dev URL for the deployed Bobi event server."
  value       = cloudflare_worker.events.subdomain.url
}

output "worker_name" {
  description = "Name of the deployed Worker."
  value       = cloudflare_worker.events.name
}

output "kv_namespace_id" {
  description = "ID of the isolated EVENTS namespace."
  value       = cloudflare_workers_kv_namespace.events.id
}
