variable "zone_id" {
  description = "Cloudflare zone ID for projectpattie.com"
  type        = string
}

variable "tunnel_id" {
  description = "ID of the homelab-kubecraft Cloudflare tunnel"
  type        = string
}

variable "node_ip" {
  description = "LAN IP Traefik answers on, used by local-only records"
  type        = string
  default     = "10.0.0.106"
}
