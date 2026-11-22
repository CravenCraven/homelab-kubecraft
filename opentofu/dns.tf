locals {
  # Hostnames served through the homelab-kubecraft tunnel.
  # Must match the ingress list in apps/base/cloudflared/configmap.yaml.
  tunnel_hosts = toset([
    "abs",
    "auth",
    "feeds",
    "home",
    "links",
    "me",
    "mealie",
    "n8n",
    "ntfy",
    "paperless",
    "pgadmin",
    "up",
    "uptime",
    "vault",
    "zentao",
  ])

  # Local-only hostnames: grey cloud, point straight at the node.
  local_hosts = toset([
    "grafana",
    "lds",
  ])
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = local.tunnel_hosts

  zone_id = var.zone_id
  name    = "${each.key}.projectpattie.com"
  type    = "CNAME"
  content = "${var.tunnel_id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "local" {
  for_each = local.local_hosts

  zone_id = var.zone_id
  name    = "${each.key}.projectpattie.com"
  type    = "A"
  content = var.node_ip
  proxied = false
  ttl     = 1
}
