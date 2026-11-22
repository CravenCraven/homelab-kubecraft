variable "state_passphrase" {
  description = "Passphrase that encrypts the OpenTofu state"
  type        = string
  sensitive   = true
}


terraform {
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

