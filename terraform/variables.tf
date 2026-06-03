# ─── Image et clé SSH ─────────────────────────────────────────────────────────

variable "image_name" {
  description = "Image de base pour les instances"
  type        = string
  default     = "ubuntu-noble-24.04-amd64"
}

variable "ssh_public_key" {
  description = "Clé publique SSH pour les instances"
  type        = string
  default     = ""
}

# ─── Instances ────────────────────────────────────────────────────────────────

variable "instances" {
  description = "Configuration des instances à créer"
  type = map(object({
    flavor = string
  }))
  default = {
    mariadb = { flavor = "m1.medium" }
    apache  = { flavor = "m1.medium" }
    backup  = { flavor = "m1.medium" }
    ftp     = { flavor = "m1.medium" }
    nfs     = { flavor = "m1.medium" }
  }
}

# ─── Réseau ───────────────────────────────────────────────────────────────────

variable "external_network_id" {
  description = "ID du réseau public externe (PublicNetwork)"
  type        = string
}

variable "apache_floating_ip" {
  description = "Floating IP pré-allouée pour l'instance Apache (porte d'entrée SSH)"
  type        = string
}

variable "private_subnet_cidr" {
  description = "CIDR du sous-réseau privé interne des instances"
  type        = string
  default     = "10.10.0.0/24"
}
