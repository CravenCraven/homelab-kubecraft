terraform {
  backend "kubernetes" {
    secret_suffix  = "state"
    namespace      = "opentofu"
    config_path    = "~/.kube/config"
    config_context = "homelab"
  }
}
