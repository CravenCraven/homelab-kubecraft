terraform {
  required_version = "~> 1.11"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.29"
    }
  }
}

# Auth comes from the CLOUDFLARE_API_TOKEN environment variable.
# No credentials live in this folder.
provider "cloudflare" {}
