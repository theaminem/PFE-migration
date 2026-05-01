# ─── Credentials OpenStack ────────────────────────────────────────────────────

variable "os_auth_url" {
  description = "URL d'authentification Keystone"
  type        = string
}

variable "os_username" {
  description = "Utilisateur OpenStack"
  type        = string
}

variable "os_password" {
  description = "Mot de passe OpenStack"
  type        = string
  sensitive   = true
}

variable "os_project_name" {
  description = "Projet OpenStack"
  type        = string
}

variable "os_region" {
  description = "Région OpenStack"
  type        = string
  default     = "RegionOne"
}

# ─── Réseau ───────────────────────────────────────────────────────────────────

variable "provider_network" {
  description = "Nom du réseau provider"
  type        = string
  default     = "provider"
}

variable "migration_network_cidr" {
  description = "CIDR du réseau interne"
  type        = string
  default     = "10.10.10.0/24"
}

variable "migration_gateway" {
  description = "Gateway du réseau interne"
  type        = string
  default     = "10.10.10.1"
}

# ─── Image et clé SSH ─────────────────────────────────────────────────────────

variable "image_name" {
  description = "Image de base pour les instances"
  type        = string
  default     = "ubuntu-22.04"
}

variable "ssh_public_key" {
  description = "Clé publique SSH pour les instances"
  type        = string
}

# ─── Instances ────────────────────────────────────────────────────────────────

variable "instances" {
  description = "Configuration des instances à créer"
  type = map(object({
    internal_ip  = string
    flavor       = string
  }))
  default = {
    mariadb = { internal_ip = "10.10.10.10", flavor = "m1.mariadb" }
    apache  = { internal_ip = "10.10.10.20", flavor = "m1.apache"  }
    backup  = { internal_ip = "10.10.10.30", flavor = "m1.backup"  }
    ftp     = { internal_ip = "10.10.10.40", flavor = "m1.ftp"     }
    nfs     = { internal_ip = "10.10.10.50", flavor = "m1.nfs"     }
  }
}
